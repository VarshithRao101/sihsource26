"""
modules/04_backend/api.py - the API the dashboard talks to.

    uvicorn modules.04_backend.api:app --reload --port 8000
    open http://localhost:8000/docs

Person 5 builds the entire frontend against this. It answers with real data
from day one because `python -m shared.fake` fills outputs/ with contract-valid
runs before any solver has ever been pointed at real terrain.

Design rules for this file:
  * Every response the frontend renders carries `is_fake` and `engine`. The UI
    is required to show a SYNTHETIC banner when is_fake is true, and to label
    surrogate predictions as predictions. The API makes that impossible to
    forget by never omitting the flags.
  * Long solves run in the background and stream progress over a WebSocket.
    A jury demo that shows a spinner for 40 seconds has already lost the room.
  * Nothing here computes physics. It calls runner.run_scenario, which is the
    single path from a scenario to a run folder.

Owner: person 4 / captain.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from shared.contract import ENGINES, FAILURE_MODES, SCHEMA_VERSION
from shared.io import RunFolder, list_runs, read_json, read_meta
from shared.validate import validate_run

from . import reservoir
from .runner import SyntheticTerrain, run_scenario
from .scenario import DEMO_SITES, ScenarioSpec, SiteSpec
from .solver import warm_up_jit

OUTPUTS = Path("outputs")


# ==========================================================================
# In-flight run registry
# ==========================================================================


class RunRegistry:
    """Tracks solves that are currently running, so the WebSocket can follow one.

    Deliberately in-process and in-memory. This is a hackathon demo served to
    one browser on localhost, not a cluster scheduler - a Redis queue here would
    be three days spent on a problem we do not have.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self, run_id: str, spec: ScenarioSpec) -> None:
        self._state[run_id] = {
            "run_id": run_id,
            "status": "running",
            "pct": 0.0,
            "t_hr": 0.0,
            "wet_cells": 0,
            "max_depth_m": 0.0,
            "site": spec.site.name,
            "engine": spec.engine,
            "error": None,
        }
        self._queues.setdefault(run_id, [])

    def publish(self, run_id: str, update: dict) -> None:
        """Called from the solver thread. Hops back onto the event loop."""
        state = self._state.get(run_id)
        if state is None:
            return
        state.update(update)
        payload = dict(state)
        for q in self._queues.get(run_id, []):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(q.put_nowait, payload)

    def finish(self, run_id: str, error: str | None = None) -> None:
        self.publish(
            run_id,
            {"status": "failed" if error else "done", "pct": 100.0, "error": error},
        )

    def get(self, run_id: str) -> dict | None:
        return self._state.get(run_id)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        if run_id in self._queues and q in self._queues[run_id]:
            self._queues[run_id].remove(q)

    def active(self) -> list[dict]:
        return [s for s in self._state.values() if s["status"] == "running"]


REGISTRY = RunRegistry()


# ==========================================================================
# Request / response models
# ==========================================================================


class SiteIn(BaseModel):
    name: str
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    river: str = ""
    state: str = ""
    dam_height_m: float = Field(60.0, gt=0, le=350)
    reservoir_capacity_mcm: float = Field(5.0, gt=0)
    source: str = "user"


class RunRequest(BaseModel):
    """What the dashboard posts to start a simulation."""

    site: SiteIn | None = None
    site_key: str | None = Field(
        None, description="Shortcut for a built-in demo site, e.g. 'chungthang'."
    )
    dam_id: str | None = Field(
        None,
        description=(
            "A dam from the CWC National Register of Large Dams, e.g. 'OD01MH0001'. "
            "The site is filled in from the register - name, river, coordinates, "
            "height and gross storage - so the operator picks a dam instead of "
            "typing physics."
        ),
    )
    failure_mode: Literal[
        "overtopping", "piping", "gated_release", "blockage_breach"
    ] = "overtopping"
    reservoir_level_frac: float = Field(1.0, ge=0.0, le=1.0)
    breach_regression: Literal["froehlich2008", "vonthun1990", "macdonald1984"] = (
        "froehlich2008"
    )
    breach_width_m: float | None = Field(None, gt=0)
    formation_time_hr: float | None = Field(None, gt=0)
    reach_length_km: float = Field(60.0, gt=0, le=500)
    cellsize_m: float = Field(90.0, ge=10, le=500)
    end_hr: float = Field(12.0, gt=0, le=120)
    scheme: Literal["swe", "inertial"] = "swe"
    manning_n: float = Field(0.035, ge=0.01, le=0.2)
    keep_frames: bool = False
    real_terrain: bool = True
    """Use module 01's downloaded, conditioned DEM. False falls back to the
    synthetic valley, which is fast but marks the run is_fake and bars it from
    the demo."""
    notes: str = ""

    def to_spec(self) -> ScenarioSpec:
        if self.dam_id:
            from importlib import import_module

            catalogue = import_module("modules.01_geodata.dams")
            dam = catalogue.get(self.dam_id)
            if dam is None:
                raise HTTPException(404, f"unknown dam_id {self.dam_id!r}")
            if not (dam["has_coords"] and dam["height_m"] and dam["gross_storage_mcm"]):
                raise HTTPException(
                    422,
                    f"{dam['name']} cannot be simulated: the register has no "
                    f"coordinates, height or storage capacity for it.",
                )
            site = SiteSpec(
                name=dam["name"],
                lat=dam["lat"],
                lon=dam["lon"],
                river=dam["river"] or "",
                state=dam["state"] or "",
                dam_height_m=float(dam["height_m"]),
                reservoir_capacity_mcm=float(dam["gross_storage_mcm"]),
                source="CWC NRLD 2019",
            )
        elif self.site_key:
            site = DEMO_SITES.get(self.site_key)
            if site is None:
                raise HTTPException(
                    404, f"unknown site_key {self.site_key!r}; try {list(DEMO_SITES)}"
                )
        elif self.site:
            site = SiteSpec(**self.site.model_dump())
        else:
            raise HTTPException(422, "provide either site or site_key")

        return ScenarioSpec(
            site=site,
            failure_mode=self.failure_mode,
            reservoir_level_frac=self.reservoir_level_frac,
            breach_regression=self.breach_regression,
            breach_width_m=self.breach_width_m,
            formation_time_hr=self.formation_time_hr,
            reach_length_km=self.reach_length_km,
            cellsize_m=self.cellsize_m,
            end_hr=self.end_hr,
            scheme=self.scheme,
            manning_n=self.manning_n,
            notes=self.notes,
        )


# ==========================================================================
# App
# ==========================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    REGISTRY.bind_loop(asyncio.get_running_loop())
    # Pay the numba compile cost now, not while a juror is watching.
    seconds = await asyncio.get_running_loop().run_in_executor(None, warm_up_jit)
    app.state.jit_warmup_s = round(seconds, 2)

    # Same reasoning for the ML emulator: loading the checkpoint and
    # initialising CUDA costs 2-5 seconds on the FIRST prediction, which is
    # precisely the moment someone drags a slider for the first time. Pay it
    # now. Failure here is not fatal - the surrogate is an enhancement, and the
    # endpoint reports honestly when it is unavailable.
    def _warm_surrogate() -> str:
        try:
            from importlib import import_module

            sg = import_module("modules.07_ml.surrogate")
            out = sg.predict(
                {
                    "reservoir_level_frac": 1.0,
                    "capacity_mcm": 5.0,
                    "dam_height_m": 60.0,
                    "formation_time_hr": 0.5,
                }
            )
            return f"ready ({out['inference_ms']} ms cold)"
        except Exception as exc:  # noqa: BLE001
            return f"unavailable: {type(exc).__name__}"

    app.state.surrogate = await asyncio.get_running_loop().run_in_executor(
        None, _warm_surrogate
    )
    OUTPUTS.mkdir(exist_ok=True)
    yield


app = FastAPI(
    title="SIH26161 - Dam Break Inundation Modelling",
    description=(
        "Flood simulation console API. Every response that describes a run "
        "carries `is_fake` and `engine`; the dashboard must surface both."
    ),
    version=SCHEMA_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # The Vite dev server. Tightened before anything is deployed anywhere.
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


UI_DIR = Path(__file__).resolve().parents[1] / "05_frontend"


@app.get("/", include_in_schema=False)
def dashboard():
    """The operator console. Deliberately plain - module 05 restyles it."""
    index = UI_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "UI not built", "docs": "/docs"}, status_code=404)
    return FileResponse(index, media_type="text/html")


# --------------------------------------------------------------------------
# Reservoir level simulation - the interactive y = f(t, V) console
# --------------------------------------------------------------------------
#
# Served here rather than in 05_frontend because the physics lives in
# reservoir.py next door and module 05 belongs to the frontend pair. The page
# steps the model itself in the browser - a network round trip per frame would
# be absurd - and this endpoint keeps the constants in one place so the two
# implementations cannot drift apart silently.

RESERVOIR_UI_DIR = Path(__file__).resolve().parent / "static" / "reservoir"

if RESERVOIR_UI_DIR.exists():
    app.mount(
        "/reservoir",
        StaticFiles(directory=RESERVOIR_UI_DIR, html=True),
        name="reservoir-ui",
    )


class ReservoirRequest(BaseModel):
    """Batch run of the reservoir model. The live page does not use this -
    it exists so a result can be reproduced, scripted or plotted server side."""

    hours: float = Field(24.0, gt=0, le=8760)
    sample_every_s: float = Field(60.0, gt=0)
    config: dict = Field(default_factory=dict)


@app.get("/api/reservoir/config", tags=["reservoir"])
def reservoir_config() -> dict:
    """Defaults, constants and thresholds for the level simulation.

    The browser fetches this on load so the page and reservoir.py agree on
    every number. If the fetch fails the page falls back to its own copy and
    says so on screen.
    """
    cfg = reservoir.ReservoirConfig()
    return {
        "defaults": cfg.as_dict(),
        "constants": {
            "gravity": reservoir.GRAVITY,
            "orifice_cd": reservoir.ORIFICE_CD,
            "weir_c_si": reservoir.WEIR_C_SI,
        },
        "derived": {
            "capacity_m3": cfg.capacity_m3(),
            "spillway_crest_m": cfg.spillway_crest_m(),
            "outlet_invert_m": cfg.outlet_invert_m(),
            "outlet_area_m2": cfg.outlet_area_m2(),
        },
        "assumptions": reservoir.ASSUMPTIONS,
        "reference_implementation": "modules/04_backend/reservoir.py",
        "is_fake": False,
    }


@app.post("/api/reservoir/simulate", tags=["reservoir"])
def reservoir_simulate(req: ReservoirRequest) -> dict:
    """Integrate dV/dt = Q_in(t) - Q_out(y) and return the trajectory.

    Same code path the tests use, so a curve pulled from here is the curve
    pytest checked. The mass balance is returned with it, always.
    """
    known = set(reservoir.ReservoirConfig().as_dict())
    unknown = set(req.config) - known
    if unknown:
        raise HTTPException(422, f"unknown config keys: {sorted(unknown)}")
    cfg = reservoir.ReservoirConfig(**{**reservoir.ReservoirConfig().as_dict(), **req.config})
    return reservoir.simulate(cfg, req.hours, req.sample_every_s)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "surrogate": getattr(app.state, "surrogate", "not initialised"),
        "jit_warmup_s": getattr(app.state, "jit_warmup_s", None),
        "runs_on_disk": len(list_runs(OUTPUTS)),
        "active_runs": len(REGISTRY.active()),
    }


@app.get("/api/enums", tags=["meta"])
def enums() -> dict:
    """Everything the UI needs to build its dropdowns without hardcoding."""
    from shared.contract import HAZARD_CLASSES, WET_THRESHOLD_M

    return {
        "engines": list(ENGINES),
        "failure_modes": list(FAILURE_MODES),
        "breach_regressions": ["froehlich2008", "vonthun1990", "macdonald1984"],
        "schemes": ["swe", "inertial"],
        "hazard_classes": list(HAZARD_CLASSES),
        "wet_threshold_m": WET_THRESHOLD_M,
    }


@app.get("/api/sites", tags=["sites"])
def sites() -> dict:
    """Built-in demo sites.

    Module 01 replaces this with the full GRanD + CWC NRLD dam database, which
    is what makes 'point it at any dam' true. Until then these two are the
    documented events we can defend in Q&A.
    """
    return {
        "sites": [
            {"key": k, **v.__dict__} for k, v in DEMO_SITES.items()
        ],
        "note": (
            "Replaced by data/dams/dams.geojson (GRanD v1.3 + CWC NRLD) once "
            "module 01 lands. Figures here are from public post-event reports "
            "and are marked in each record's `source`."
        ),
    }


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def _catalogue():
    from importlib import import_module

    try:
        return import_module("modules.01_geodata.dams")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"dam catalogue unavailable: {exc}")


@app.get("/api/dams/states", tags=["dams"])
def dam_states() -> dict:
    """Every state with at least one simulatable dam."""
    cat = _catalogue()
    try:
        return {"states": cat.states()}
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/dams/cities", tags=["dams"])
def dam_cities(state: str) -> dict:
    """Nearest-city values inside a state - the second filter level.

    NRLD has no district column, so this is 'nearest city', not district. The
    label in the UI says so.
    """
    return {"state": state, "cities": _catalogue().cities(state)}


@app.get("/api/dams", tags=["dams"])
def dam_search(
    state: str | None = None,
    city: str | None = None,
    q: str | None = None,
    limit: int = 200,
    include_unsimulatable: bool = False,
) -> dict:
    """Search the National Register of Large Dams."""
    cat = _catalogue()
    try:
        rows = cat.search(
            state=state, city=city, q=q,
            simulatable_only=not include_unsimulatable, limit=limit,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc))
    return {"count": len(rows), "dams": rows, "source": "CWC NRLD 2019"}


@app.get("/api/dams/{dam_id}", tags=["dams"])
def dam_detail(dam_id: str) -> dict:
    dam = _catalogue().get(dam_id)
    if dam is None:
        raise HTTPException(404, f"unknown dam_id {dam_id!r}")
    return dam


@app.get("/api/runs", tags=["runs"])
def get_runs() -> dict:
    out = []
    for rf in list_runs(OUTPUTS):
        try:
            meta = rf.meta
        except Exception:
            continue
        out.append(
            {
                "run_id": rf.run_id,
                "engine": meta.get("engine"),
                "is_fake": meta.get("is_fake", True),
                "created_utc": meta.get("created_utc"),
                "site": (meta.get("site") or {}).get("name"),
                "failure_mode": (meta.get("scenario") or {}).get("failure_mode"),
                "results": meta.get("results", {}),
            }
        )
    return {"runs": out, "count": len(out), "active": REGISTRY.active()}


@app.post("/api/runs", tags=["runs"], status_code=202)
def create_run(req: RunRequest, background: BackgroundTasks) -> dict:
    """Start a simulation. Returns immediately with a run_id.

    Follow it on `ws://localhost:8000/ws/runs/{run_id}` for live progress, then
    fetch the results from `/api/runs/{run_id}` when status is `done`.
    """
    spec = req.to_spec()
    errs = spec.validate()
    if errs:
        raise HTTPException(422, {"invalid_scenario": errs})

    from shared.io import make_run_id, next_sequence

    seq = next_sequence(OUTPUTS, spec.site_slug, spec.scenario_slug, spec.engine)
    run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, seq)

    REGISTRY.start(run_id, spec)
    background.add_task(_execute, run_id, spec, req.keep_frames, req.real_terrain)

    return {
        "run_id": run_id,
        "status": "running",
        "websocket": f"/ws/runs/{run_id}",
        "poll": f"/api/runs/{run_id}/status",
        "fingerprint": spec.fingerprint(),
    }


def _prepare_real(spec: ScenarioSpec, run_id: str):
    """Trace the river, fetch terrain, download exposure. Returns (terrain, exposure).

    Every step here can fail on a network hiccup, and none of them should take
    the API down. On failure we raise, because the alternative - quietly
    dropping to synthetic terrain while the user asked for real - is exactly
    how a fabricated result reaches a juror.
    """
    from importlib import import_module

    gd = import_module("modules.01_geodata")
    site_slug = spec.site_slug

    REGISTRY.publish(run_id, {"stage": "tracing river", "pct": 0})
    plan = gd.plan_domain(
        lat=spec.site.lat,
        lon=spec.site.lon,
        site=site_slug,
        reach_length_km=spec.reach_length_km,
        corridor_width_km=spec.corridor_width_km,
    )
    # The dam moves onto the channel and the domain follows the traced river.
    spec.site.lat, spec.site.lon = plan.dam_lonlat[1], plan.dam_lonlat[0]
    spec.domain_bbox = plan.bbox

    REGISTRY.publish(run_id, {"stage": "fetching terrain", "pct": 0})
    terrain = gd.RealTerrain(
        site=site_slug,
        source=spec.dem_source if spec.dem_source != "SYNTHETIC" else "COP30",
        dam_lonlat=plan.dam_lonlat,
        reach_length_km=spec.reach_length_km,
    )

    REGISTRY.publish(run_id, {"stage": "downloading settlements", "pct": 0})
    try:
        exposure = gd.exposure.build_exposure(plan.bbox, site=site_slug)
    except Exception:
        exposure = None  # a flood map without names is still a valid run

    return terrain, exposure


def _execute(
    run_id: str, spec: ScenarioSpec, keep_frames: bool, real_terrain: bool = True
) -> None:
    """Runs on the background thread. Never raises into the request."""
    try:
        if real_terrain:
            terrain, exposure = _prepare_real(spec, run_id)
        else:
            terrain, exposure = SyntheticTerrain(), None

        run_scenario(
            spec,
            outputs_dir=OUTPUTS,
            terrain=terrain,
            run_id=run_id,
            keep_frames=keep_frames,
            exposure=exposure,
            progress=lambda u: REGISTRY.publish(run_id, u),
        )
        report = validate_run(OUTPUTS / run_id)
        if not report.ok:
            REGISTRY.finish(run_id, "; ".join(report.errors))
        else:
            REGISTRY.finish(run_id)
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        REGISTRY.finish(run_id, f"{type(exc).__name__}: {exc}")


@app.get("/api/runs/{run_id}/status", tags=["runs"])
def run_status(run_id: str) -> dict:
    state = REGISTRY.get(run_id)
    if state:
        return state
    if (OUTPUTS / run_id / "meta.json").exists():
        return {"run_id": run_id, "status": "done", "pct": 100.0}
    raise HTTPException(404, f"no run {run_id!r}")


@app.get("/api/runs/{run_id}", tags=["runs"])
def get_run(run_id: str) -> dict:
    run_dir = _require_run(run_id)
    meta = read_meta(run_dir)
    rf = RunFolder(run_dir)

    payload: dict[str, Any] = {
        "meta": meta,
        "is_fake": meta.get("is_fake", True),
        "engine": meta.get("engine"),
        "files": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
        "frames": len(rf.frames()),
    }
    for optional in ("impact.json", "uncertainty.json"):
        try:
            payload[optional.removesuffix(".json")] = read_json(run_dir, optional)
        except FileNotFoundError:
            payload[optional.removesuffix(".json")] = None
    return payload


@app.get("/api/runs/{run_id}/validate", tags=["runs"])
def validate(run_id: str) -> dict:
    """Run the contract validator and return what it found.

    Exposed on the API on purpose: the dashboard shows the validator verdict
    next to the results, so anyone looking at a number can see whether the run
    that produced it is contract-clean.
    """
    run_dir = _require_run(run_id)
    rep = validate_run(run_dir)
    return {
        "run_id": run_id,
        "ok": rep.ok,
        "errors": rep.errors,
        "warnings": rep.warnings,
        "facts": rep.facts,
    }


@app.get("/api/runs/{run_id}/evacuation", tags=["runs"])
def evacuation(run_id: str) -> dict:
    """Evacuation routes for a run, if road geometry was available."""
    run_dir = _require_run(run_id)
    path = run_dir / "evacuation.json"
    if not path.exists():
        raise HTTPException(
            404,
            "no evacuation plan for this run - it needs OSM road geometry, "
            "which means running with real terrain and exposure.",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/uncertainty", tags=["runs"])
def uncertainty(run_id: str) -> dict:
    """The honesty block: regression spread and, where computed, the Monte
    Carlo band."""
    run_dir = _require_run(run_id)
    path = run_dir / "uncertainty.json"
    if not path.exists():
        raise HTTPException(404, "no uncertainty block for this run")
    return json.loads(path.read_text(encoding="utf-8"))


class SurrogateRequest(BaseModel):
    """A what-if query for the trained emulator."""

    reservoir_level_frac: float = Field(1.0, ge=0.0, le=1.0)
    capacity_mcm: float = Field(5.0, gt=0)
    dam_height_m: float = Field(60.0, gt=0)
    formation_time_hr: float = Field(0.5, gt=0)


@app.post("/api/surrogate", tags=["ml"])
def surrogate_predict(req: SurrogateRequest) -> dict:
    """Emulate a scenario in milliseconds instead of solving it.

    This is a PREDICTION FROM A NEURAL NETWORK trained on our own solver, not a
    simulation. The response says so in `is_emulated`, and the UI must label it
    - anything exported or quoted has to be recomputed with the real solver.
    """
    try:
        from importlib import import_module

        sg = import_module("modules.07_ml.surrogate")
        out = sg.predict(req.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(503, f"surrogate not trained: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"surrogate unavailable: {type(exc).__name__}: {exc}")

    depth = out["max_depth"]
    wet = depth >= 0.05
    return {
        "is_emulated": True,
        "engine": "surrogate",
        "inference_ms": out["inference_ms"],
        "wet_cells": int(wet.sum()),
        "max_depth_m": round(float(depth.max()), 2),
        "flood_area_km2": None,
        "warning": (
            "Emulated by a U-Net trained on the fast solver. Extent CSI against "
            "the solver is 0.91 on held-out scenarios. Not a simulation, and "
            "not validated against observed floods."
        ),
    }


@app.get("/api/runs/{run_id}/hydrograph", tags=["runs"])
def hydrograph(run_id: str) -> dict:
    from shared.io import read_hydrograph

    run_dir = _require_run(run_id)
    t, q = read_hydrograph(run_dir)
    return {
        "run_id": run_id,
        "time_hr": [round(float(v), 4) for v in t],
        "discharge_cumecs": [round(float(v), 2) for v in q],
        "peak_cumecs": round(float(q.max()), 1),
    }


@app.get("/api/runs/{run_id}/extent", tags=["runs"])
def extent(run_id: str) -> JSONResponse:
    run_dir = _require_run(run_id)
    with open(run_dir / "extent.geojson", "r", encoding="utf-8") as fh:
        return JSONResponse(json.load(fh))


@app.get("/api/runs/{run_id}/file/{filename}", tags=["runs"])
def get_file(run_id: str, filename: str) -> FileResponse:
    """Serve a raw artefact - packed.png, a GeoTIFF, hydrograph.csv."""
    run_dir = _require_run(run_id)
    # Path traversal guard: resolve and confirm the result is still inside.
    target = (run_dir / filename).resolve()
    if not str(target).startswith(str(run_dir.resolve())) or not target.is_file():
        raise HTTPException(404, f"no file {filename!r} in {run_id}")
    return FileResponse(target)


# --------------------------------------------------------------------------
# Exports - .shp and .kml are named in the problem statement
# --------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/export", tags=["export"])
def export(run_id: str, format: Literal["kml", "shp", "geojson"] = "kml") -> FileResponse:
    """Export the flood extent as KML, shapefile or GeoJSON.

    NTRO asks for .shp or .kml explicitly. KML opens in Google Earth, which is
    what a district administrator actually has; shapefile is what a GIS cell
    will ask for.
    """
    run_dir = _require_run(run_id)
    meta = read_meta(run_dir)

    if format == "geojson":
        return FileResponse(
            run_dir / "extent.geojson",
            media_type="application/geo+json",
            filename=f"{run_id}_extent.geojson",
        )

    import geopandas as gpd

    gdf = gpd.read_file(run_dir / "extent.geojson")
    gdf["run_id"] = run_id
    gdf["engine"] = meta.get("engine")
    gdf["is_fake"] = meta.get("is_fake", True)
    gdf["site"] = (meta.get("site") or {}).get("name", "")
    gdf["failure_mode"] = (meta.get("scenario") or {}).get("failure_mode", "")

    tmp = Path(tempfile.mkdtemp(prefix=f"{run_id}_"))

    if format == "kml":
        out = tmp / f"{run_id}_extent.kml"
        gdf.to_file(out, driver="KML")
        return FileResponse(
            out, media_type="application/vnd.google-earth.kml+xml", filename=out.name
        )

    # Shapefile is a set of sidecar files, so it ships as a zip.
    shp_dir = tmp / run_id
    shp_dir.mkdir()
    gdf.to_file(shp_dir / f"{run_id}_extent.shp", driver="ESRI Shapefile")
    zip_path = tmp / f"{run_id}_extent_shp.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in shp_dir.iterdir():
            zf.write(part, part.name)
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


# --------------------------------------------------------------------------
# Engine comparison - a scoring line in the problem statement
# --------------------------------------------------------------------------


@app.get("/api/compare", tags=["compare"])
def compare(run_ids: str) -> dict:
    """Side-by-side table of several runs. `run_ids` is comma separated.

    The problem statement asks for SPH, Delft3D and our fast solver compared.
    This is that table, built from whatever run folders exist - it does not
    care which module produced them, only that they satisfy the contract.
    """
    rows = []
    for run_id in [r.strip() for r in run_ids.split(",") if r.strip()]:
        run_dir = OUTPUTS / run_id
        if not (run_dir / "meta.json").exists():
            rows.append({"run_id": run_id, "error": "not found"})
            continue
        meta = read_meta(run_dir)
        res = meta.get("results", {})
        rows.append(
            {
                "run_id": run_id,
                "engine": meta.get("engine"),
                "is_fake": meta.get("is_fake", True),
                "peak_discharge_cumecs": res.get("peak_discharge_cumecs"),
                "max_depth_m": res.get("max_depth_m"),
                "flood_area_km2": res.get("flood_area_km2"),
                "runtime_s": res.get("runtime_s"),
                "mass_balance_err_pct": res.get("mass_balance_err_pct"),
                "cellsize_m": (meta.get("domain") or {}).get("cellsize_m"),
            }
        )
    return {
        "rows": rows,
        "note": (
            "Runtimes are not comparable unless the runs share a domain and a "
            "cell size. Check cellsize_m before quoting a speed-up."
        ),
    }


# --------------------------------------------------------------------------
# WebSocket - live progress
# --------------------------------------------------------------------------


@app.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str) -> None:
    """Stream solver progress while it computes.

    The frontend opens this the moment POST /api/runs returns and drives a
    progress bar plus a live wet-cell count from it. That is the difference
    between a demo that looks alive and one that looks hung.
    """
    await websocket.accept()
    queue = REGISTRY.subscribe(run_id)
    try:
        state = REGISTRY.get(run_id)
        if state:
            await websocket.send_json(state)
        while True:
            update = await asyncio.wait_for(queue.get(), timeout=300.0)
            await websocket.send_json(update)
            if update.get("status") in ("done", "failed"):
                break
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        REGISTRY.unsubscribe(run_id, queue)


# --------------------------------------------------------------------------


def _require_run(run_id: str) -> Path:
    run_dir = OUTPUTS / run_id
    if not (run_dir / "meta.json").exists():
        raise HTTPException(404, f"no run {run_id!r}")
    return run_dir
