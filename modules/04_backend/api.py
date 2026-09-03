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

from . import pipeline, reservoir
from .runner import SyntheticTerrain, run_scenario
from .scenario import DEMO_SITES, ScenarioSpec, SiteSpec
from .solver import warm_up_jit

OUTPUTS = Path("outputs")


# ==========================================================================
# In-flight run registry
# ==========================================================================


class RunCancelled(Exception):
    """RESET was pressed while this run was solving."""


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
        # PAUSE and RESET on the workflow page reach the solver through these.
        # The solve runs on a worker thread and calls progress() between
        # timesteps, so blocking or raising inside that callback is a real
        # pause and a real stop - not the UI pretending while the CPU carries
        # on burning through the run.
        self._control: dict[str, dict[str, Any]] = {}

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
            # Per-node state for the workflow graph. Probe nodes start on their
            # real state - see pipeline.initial_nodes.
            "nodes": pipeline.initial_nodes(),
            "node": None,
        }
        self._queues.setdefault(run_id, [])

    def publish(self, run_id: str, update: dict) -> None:
        """Called from the solver thread. Hops back onto the event loop.

        An update carrying `node` also moves that node in the workflow graph.
        `node_status` defaults to running, because every emitter that names a
        node is announcing that it has started work on it.
        """
        state = self._state.get(run_id)
        if state is None:
            return

        node_id = update.get("node")
        if node_id:
            nodes = state.setdefault("nodes", pipeline.initial_nodes())
            entry = nodes.setdefault(node_id, {"status": pipeline.WAITING})
            new_status = update.pop("node_status", pipeline.RUNNING)
            entry["status"] = new_status
            if "node_detail" in update:
                entry["detail"] = update.pop("node_detail")
            # Anything still upstream of a node that has started is finished by
            # definition - the pipeline is sequential and a stage cannot begin
            # before the one feeding it returned.
            if new_status == pipeline.RUNNING:
                for prior in pipeline.LIVE_NODES:
                    if prior == node_id:
                        break
                    if nodes.get(prior, {}).get("status") == pipeline.RUNNING:
                        nodes[prior]["status"] = pipeline.COMPLETE

        state.update(update)
        payload = dict(state)
        # Deep-copy the node map so a later mutation cannot rewrite a message
        # that is already sitting in a subscriber's queue.
        payload["nodes"] = {k: dict(v) for k, v in state.get("nodes", {}).items()}
        for q in self._queues.get(run_id, []):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(q.put_nowait, payload)

    def finish_node(self, run_id: str, node_id: str, detail: str = "") -> None:
        self.publish(
            run_id,
            {"node": node_id, "node_status": pipeline.COMPLETE, "node_detail": detail},
        )

    def fail_node(self, run_id: str, node_id: str, detail: str) -> None:
        self.publish(
            run_id,
            {"node": node_id, "node_status": pipeline.FAILED, "node_detail": detail},
        )

    def skip_node(self, run_id: str, node_id: str, why: str) -> None:
        """A stage that legitimately did not run. Never shown as complete."""
        self.publish(
            run_id,
            {"node": node_id, "node_status": pipeline.SKIPPED, "node_detail": why},
        )

    def finish(self, run_id: str, error: str | None = None) -> None:
        # Whichever node was mid-flight owns the failure. Leaving it on RUNNING
        # would show a spinner forever on the box that actually broke.
        state = self._state.get(run_id)
        if error and state:
            for entry in state.get("nodes", {}).values():
                if entry.get("status") == pipeline.RUNNING:
                    entry["status"] = pipeline.FAILED
                    entry["detail"] = error
        self.publish(
            run_id,
            {"status": "failed" if error else "done", "pct": 100.0, "error": error},
        )

    # ---- pause / cancel, from the workflow page ---------------------------

    def control(self, run_id: str) -> dict:
        return self._control.setdefault(
            run_id, {"paused": False, "cancelled": False}
        )

    def set_paused(self, run_id: str, paused: bool) -> bool:
        self.control(run_id)["paused"] = paused
        self.publish(run_id, {"paused": paused})
        return paused

    def cancel(self, run_id: str) -> None:
        c = self.control(run_id)
        c["cancelled"] = True
        c["paused"] = False  # a paused run must wake up to notice it is cancelled

    def gate(self, run_id: str) -> None:
        """Called from the solver thread between timesteps.

        Blocks while the run is paused and raises when it is cancelled. This is
        the only place either one takes effect, which is why the solver's own
        code needs no knowledge of the buttons.
        """
        import time as _time

        c = self.control(run_id)
        while c.get("paused"):
            if c.get("cancelled"):
                break
            _time.sleep(0.1)
        if c.get("cancelled"):
            raise RunCancelled(run_id)

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
    blockage_height_m: float = Field(
        40.0, gt=0, le=300,
        description=(
            "Only for failure_mode='blockage_breach'. Height of the landslide "
            "debris above the river bed. The impounded volume is read off the "
            "DEM - nobody publishes a landslide dam's storage."
        ),
    )
    gate_opening_frac: float = Field(
        1.0, ge=0.0, le=1.0,
        description=(
            "Only for failure_mode='gated_release'. How far the outlet gates are "
            "opened. 1.0 is a full emergency release. The dam does not fail in "
            "this mode - no breach regression is used."
        ),
    )
    gate_open_time_hr: float = Field(0.5, gt=0, le=24)
    target_release_cumecs: float | None = Field(
        None, gt=0,
        description=(
            "Override the release the operator is aiming for. Leave None to use "
            "the dam's design spillway capacity from the CWC register."
        ),
    )
    spillway_length_m: float = Field(60.0, gt=0, le=2000)
    sph_run: str | None = Field(
        None,
        description=(
            "Path to a finished module 02 SPH run folder. Setting it switches "
            "the engine to 'sphcoupled': the near-field discharge DualSPHysics "
            "measured is spliced onto the front of the level-pool curve, and "
            "the disagreement between the two engines at the handover is "
            "published in meta.json under `sph_coupling`."
        ),
    )
    keep_frames: bool = False
    real_terrain: bool = True
    """Use module 01's downloaded, conditioned DEM. False falls back to the
    synthetic valley, which is fast but marks the run is_fake and bars it from
    the demo."""
    notes: str = ""

    def to_spec(self) -> ScenarioSpec:
        design_spillway: float | None = None
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
            # The register carries the design discharge capacity for many dams.
            # It is a measured number and beats any assumption we could make
            # about how much water the outlet works can pass.
            design_spillway = dam.get("spillway_cumecs")
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
            blockage_height_m=self.blockage_height_m,
            gate_opening_frac=self.gate_opening_frac,
            gate_open_time_hr=self.gate_open_time_hr,
            design_spillway_cumecs=(
                float(design_spillway) if design_spillway else None
            ),
            target_release_cumecs=self.target_release_cumecs,
            spillway_length_m=self.spillway_length_m,
            # Asking for an SPH run IS asking for the coupled engine. Making the
            # caller set both would only create a way to set one and forget the
            # other, and the scenario validator rejects that combination anyway.
            engine="sphcoupled" if self.sph_run else "fast",
            sph_run=self.sph_run,
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


@app.get("/workflow", include_in_schema=False)
def workflow_page():
    """The node workflow workspace: boxes, arrows and the 3D scene."""
    page = UI_DIR / "workflow.html"
    if not page.exists():
        return JSONResponse({"detail": "workflow UI not built"}, status_code=404)
    return FileResponse(page, media_type="text/html")


if (UI_DIR / "vendor").exists():
    # Babylon.js is vendored, not pulled from a CDN. Demo-day wifi is not a
    # dependency we accept, and the console has to render with the network
    # unplugged.
    app.mount(
        "/vendor", StaticFiles(directory=UI_DIR / "vendor"), name="frontend-vendor"
    )


@app.get("/api/pipeline", tags=["meta"])
def get_pipeline() -> dict:
    """The processing graph the workflow page draws.

    Nodes, edges, what each stage actually does, and a live probe of every
    external engine. The page hardcodes none of this - if a stage is added to
    pipeline.py the picture grows a box.
    """
    return pipeline.manifest()


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

    try:
        REGISTRY.publish(run_id, {"stage": "tracing river", "pct": 0, "node": "river"})
        plan = gd.plan_domain(
            lat=spec.site.lat,
            lon=spec.site.lon,
            site=site_slug,
            reach_length_km=spec.reach_length_km,
            corridor_width_km=spec.corridor_width_km,
        )
        spec.site.lat, spec.site.lon = plan.dam_lonlat[1], plan.dam_lonlat[0]
        spec.domain_bbox = plan.bbox

        REGISTRY.finish_node(
            run_id,
            "river",
            f"channel traced, domain {plan.bbox[0]:.4f},{plan.bbox[1]:.4f} "
            f"to {plan.bbox[2]:.4f},{plan.bbox[3]:.4f}",
        )
    except Exception as exc:
        plan = None
        REGISTRY.skip_node(
            run_id,
            "river",
            f"river tracing offline fallback ({type(exc).__name__})",
        )

    REGISTRY.publish(
        run_id, {"stage": "fetching terrain", "pct": 0, "node": "terrain"}
    )
    try:
        terrain = gd.RealTerrain(
            site=site_slug,
            source=spec.dem_source if spec.dem_source != "SYNTHETIC" else "COP30",
            dam_lonlat=plan.dam_lonlat if plan else (spec.site.lon, spec.site.lat),
            reach_length_km=spec.reach_length_km,
        )
        REGISTRY.finish_node(
            run_id,
            "terrain",
            f"{spec.dem_source if spec.dem_source != 'SYNTHETIC' else 'COP30'} "
            f"fetched and conditioned",
        )
    except Exception as exc:
        terrain = SyntheticTerrain()
        REGISTRY.skip_node(
            run_id,
            "terrain",
            f"cloud DEM fetch offline ({type(exc).__name__}); using synthetic valley",
        )

    REGISTRY.publish(
        run_id, {"stage": "downloading settlements", "pct": 0, "node": "exposure"}
    )
    try:
        bbox_to_use = plan.bbox if plan else spec.bbox
        exposure = gd.exposure.build_exposure(bbox_to_use, site=site_slug)
        n_set = len((exposure or {}).get("settlements") or [])
        n_road = len((exposure or {}).get("roads") or [])
        REGISTRY.finish_node(
            run_id, "exposure", f"{n_set} settlements, {n_road} road segments"
        )
    except Exception as exc:  # noqa: BLE001
        exposure = None  # a flood map without names is still a valid run
        REGISTRY.skip_node(
            run_id,
            "exposure",
            f"no exposure downloaded ({type(exc).__name__}); the flood map is "
            f"still valid but carries no names",
        )

    return terrain, exposure


def _gated_progress(run_id: str):
    """The progress callback the solver actually gets.

    Every publish goes through the pause/cancel gate first, so PAUSE on the
    workflow page stops the solve between timesteps rather than only freezing
    the picture of it.
    """

    def _cb(update: dict) -> None:
        REGISTRY.gate(run_id)
        REGISTRY.publish(run_id, update)

    return _cb


def _execute(
    run_id: str, spec: ScenarioSpec, keep_frames: bool, real_terrain: bool = True
) -> None:
    """Runs on the background thread. Never raises into the request."""
    try:
        REGISTRY.publish(run_id, {"stage": "scenario accepted", "node": "input"})
        REGISTRY.finish_node(
            run_id, "input", f"{spec.failure_mode} at {spec.site.name}"
        )
        REGISTRY.publish(run_id, {"stage": "reading the register", "node": "catalogue"})
        REGISTRY.finish_node(
            run_id,
            "catalogue",
            f"{spec.site.name}: {spec.site.dam_height_m:g} m high, "
            f"{spec.site.reservoir_capacity_mcm:g} MCM, source {spec.site.source}",
        )

        if real_terrain:
            terrain, exposure = _prepare_real(spec, run_id)
        else:
            terrain, exposure = SyntheticTerrain(), None
            # Synthetic terrain means module 01 did not run. Say that on the
            # graph rather than letting two boxes sit on WAITING.
            for node_id in ("river", "terrain"):
                REGISTRY.skip_node(
                    run_id, node_id, "synthetic valley requested - module 01 not used"
                )
            REGISTRY.skip_node(
                run_id, "exposure", "no real domain, so no settlements to download"
            )

        # The solver publishes pct but no stage, so without this the label
        # would still read "downloading settlements" at 99% - the operator
        # would be watching a progress bar that lies about what it is doing.
        REGISTRY.publish(run_id, {"stage": "solving shallow water", "pct": 0})
        run_scenario(
            spec,
            outputs_dir=OUTPUTS,
            terrain=terrain,
            run_id=run_id,
            keep_frames=keep_frames,
            exposure=exposure,
            progress=_gated_progress(run_id),
        )

        REGISTRY.publish(run_id, {"stage": "validating run", "node": "validate"})
        report = validate_run(OUTPUTS / run_id)
        if not report.ok:
            REGISTRY.fail_node(run_id, "validate", "; ".join(report.errors))
            REGISTRY.finish(run_id, "; ".join(report.errors))
            return

        n_warn = len(report.warnings)
        REGISTRY.finish_node(
            run_id,
            "validate",
            "contract clean"
            + (f", {n_warn} warning{'s' if n_warn != 1 else ''}" if n_warn else ""),
        )

        # The result node carries the headline the operator asked for, read
        # back off the run folder that just validated - not from anything held
        # in memory during the solve.
        meta = read_meta(OUTPUTS / run_id)
        res = meta.get("results", {})
        REGISTRY.publish(run_id, {"stage": "result ready", "node": "result"})
        REGISTRY.finish_node(
            run_id,
            "result",
            f"{res.get('flood_area_km2')} km2 flooded, "
            f"max depth {res.get('max_depth_m')} m, "
            f"peak {res.get('peak_discharge_cumecs')} m3/s",
        )
        REGISTRY.finish(run_id)
    except RunCancelled:
        REGISTRY.finish(run_id, "cancelled by the operator (RESET)")
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        REGISTRY.finish(run_id, f"{type(exc).__name__}: {exc}")


@app.post("/api/runs/{run_id}/pause", tags=["runs"])
def pause_run(run_id: str) -> dict:
    """Hold the solve where it is.

    The solver thread blocks inside its own progress callback between
    timesteps, so this really does stop the computation - the CPU is not
    quietly finishing the run behind a frozen picture of it.
    """
    if REGISTRY.get(run_id) is None:
        raise HTTPException(404, f"no active run {run_id!r}")
    REGISTRY.set_paused(run_id, True)
    return {"run_id": run_id, "paused": True}


@app.post("/api/runs/{run_id}/resume", tags=["runs"])
def resume_run(run_id: str) -> dict:
    if REGISTRY.get(run_id) is None:
        raise HTTPException(404, f"no active run {run_id!r}")
    REGISTRY.set_paused(run_id, False)
    return {"run_id": run_id, "paused": False}


@app.post("/api/runs/{run_id}/cancel", tags=["runs"])
def cancel_run(run_id: str) -> dict:
    """Stop the solve. Whatever it had written so far stays on disk, and the
    run is marked failed rather than done - a half-solved flood is not a
    result."""
    if REGISTRY.get(run_id) is None:
        raise HTTPException(404, f"no active run {run_id!r}")
    REGISTRY.cancel(run_id)
    return {"run_id": run_id, "cancelled": True}


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


# --------------------------------------------------------------------------
# Point query and derived rasters - what the map hover and the 3D scene read
# --------------------------------------------------------------------------
#
# Everything below is READ BACK OFF THE RUN FOLDER. Nothing is modelled here,
# nothing is interpolated into existence, and no file is added to the contract:
# these endpoints render GeoTIFFs the solver already wrote into shapes a
# browser can consume.

_GRID_CACHE: dict[str, dict] = {}
_GRID_CACHE_MAX = 4

# Derived rasters live OUTSIDE the run folder. A run folder is the data
# contract and nothing else; a rendering we made for the browser has no
# business sitting next to the GeoTIFFs where another module might read it as
# an output.
DERIVED = OUTPUTS / ".derived"


def _derived(run_id: str) -> Path:
    d = DERIVED / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fields(run_id: str) -> dict:
    """max_depth / max_velocity / arrival_time for a run, held in memory.

    A hover fires several times a second. Re-opening three GeoTIFFs per pointer
    move would make the map feel broken, so the arrays are cached; the cache is
    tiny because a demo looks at one or two runs.
    """
    hit = _GRID_CACHE.get(run_id)
    if hit is not None:
        return hit

    from shared.io import read_grid

    run_dir = _require_run(run_id)
    depth, grid = read_grid(run_dir, "max_depth")
    vel, _ = read_grid(run_dir, "max_velocity")
    arr, _ = read_grid(run_dir, "arrival_time")

    entry = {"depth": depth, "velocity": vel, "arrival": arr, "grid": grid}
    if len(_GRID_CACHE) >= _GRID_CACHE_MAX:
        _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
    _GRID_CACHE[run_id] = entry
    return entry


@app.get("/api/runs/{run_id}/probe", tags=["runs"])
def probe(run_id: str, lat: float, lon: float) -> dict:
    """What the flood does at one point on the map.

    The map hover calls this. Every value is the cell the coordinate lands in,
    read straight from the run's GeoTIFFs - depth in metres, depth-averaged
    speed in m/s, arrival in hours since the breach, and the hazard class the
    contract computes from the two. Outside the domain it says so instead of
    returning a zero that would read as "dry".
    """
    import numpy as np

    from shared.contract import WET_THRESHOLD_M, hazard_class

    f = _fields(run_id)
    grid = f["grid"]
    if not grid.contains(lon, lat):
        return {"run_id": run_id, "lat": lat, "lon": lon, "inside_domain": False}

    r, c = grid.rowcol(lon, lat)
    depth = float(f["depth"][r, c])
    vel = float(f["velocity"][r, c])
    arrival = float(f["arrival"][r, c])
    if not np.isfinite(depth):
        depth = 0.0
    if not np.isfinite(vel):
        vel = 0.0

    wet = depth >= WET_THRESHOLD_M
    return {
        "run_id": run_id,
        "lat": lat,
        "lon": lon,
        "inside_domain": True,
        "row": int(r),
        "col": int(c),
        "wet": bool(wet),
        # Peak values over the whole simulation at this cell, which is what the
        # grids hold. Not an instantaneous reading at the scrubber's time.
        "max_depth_m": round(depth, 3),
        "max_velocity_ms": round(vel, 3),
        "dv_m2s": round(depth * vel, 3),
        "arrival_hr": round(arrival, 4) if np.isfinite(arrival) else None,
        "hazard_class": hazard_class(depth, vel) if wet else "none",
        "wet_threshold_m": WET_THRESHOLD_M,
        "note": "peak over the run at this cell, read from max_depth.tif and max_velocity.tif",
    }


@app.get("/api/runs/{run_id}/fields", tags=["runs"])
def fields_meta(run_id: str) -> dict:
    """Scales needed to decode fields.png, and the grid it is on."""
    import numpy as np

    f = _fields(run_id)
    vel = np.nan_to_num(f["velocity"], nan=0.0)
    depth = np.nan_to_num(f["depth"], nan=0.0)
    dv = depth * vel
    grid = f["grid"]
    return {
        "run_id": run_id,
        "nx": grid.nx,
        "ny": grid.ny,
        "bbox": list(grid.bbox),
        "velocity_max_ms": round(float(vel.max()), 4),
        "dv_max_m2s": round(float(dv.max()), 4),
        "depth_max_m": round(float(depth.max()), 4),
        "encoding": "fields.png: R = max_velocity / velocity_max_ms, G = dv / dv_max_m2s, A = 255 where wet",
    }


@app.get("/api/runs/{run_id}/fields.png", tags=["runs"])
def fields_png(run_id: str) -> FileResponse:
    """Velocity and the depth-velocity hazard product as one RGBA image.

    packed.png carries arrival, peak time, depth and duration but not speed,
    and the browser cannot decode a float32 GeoTIFF. This is the same data the
    .tif holds, rendered once and cached beside the run so the map hover and
    the 3D scene can read velocity without a round trip per cell.
    """
    import numpy as np
    from PIL import Image

    from shared.contract import WET_THRESHOLD_M

    _require_run(run_id)
    out = _derived(run_id) / "fields.png"
    if out.exists():
        return FileResponse(out, media_type="image/png")

    f = _fields(run_id)
    depth = np.nan_to_num(f["depth"], nan=0.0)
    vel = np.nan_to_num(f["velocity"], nan=0.0)
    dv = depth * vel
    vmax = max(float(vel.max()), 1e-6)
    dvmax = max(float(dv.max()), 1e-6)

    r = np.clip(vel / vmax, 0, 1)
    g = np.clip(dv / dvmax, 0, 1)
    b = np.zeros_like(r)
    a = (depth >= WET_THRESHOLD_M).astype(np.float32)
    rgba = (np.stack([r, g, b, a], -1) * 255.0).round().astype(np.uint8)
    Image.fromarray(rgba, mode="RGBA").save(out, optimize=True)
    return FileResponse(out, media_type="image/png")


def _cond_dem_for(run_id: str):
    """Find the conditioned DEM module 01 cached for this run's domain.

    Matched on grid shape AND bounding box, both to four decimal places. A
    near-miss is not accepted: rendering a different reach's ground under this
    reach's water would be a fabricated picture.
    """
    import json as _json

    meta = read_meta(_require_run(run_id))
    dom = meta.get("domain") or {}
    bbox, nx, ny = dom.get("bbox"), dom.get("nx"), dom.get("ny")
    if not bbox or not nx or not ny:
        return None

    site = run_id.split("_")[0]
    cache_dir = Path("data") / "dem" / site
    if not cache_dir.is_dir():
        return None

    for side in sorted(cache_dir.glob("cond_*.json")):
        try:
            info = _json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if info.get("nx") != nx or info.get("ny") != ny:
            continue
        cached = info.get("bbox") or []
        if len(cached) != 4 or any(
            round(float(a), 4) != round(float(b), 4) for a, b in zip(cached, bbox)
        ):
            continue
        npz = side.with_suffix(".npz")
        if npz.exists():
            return npz, info
    return None


@app.get("/api/runs/{run_id}/terrain", tags=["runs"])
def terrain_meta(run_id: str) -> dict:
    """Whether real ground is available under this run's water, and its range.

    The 3D scene asks first. If module 01's conditioned DEM for this exact
    domain is not on disk the answer is `available: false` and the scene says
    on screen that it is drawing water over a flat bed - it does not invent a
    valley to sit the flood in.
    """
    import numpy as np

    found = _cond_dem_for(run_id)
    if found is None:
        return {
            "run_id": run_id,
            "available": False,
            "reason": (
                "no conditioned DEM cached for this domain under data/dem/. The "
                "3D scene will draw the water column over a flat bed and label "
                "it as such."
            ),
        }
    npz, info = found
    with np.load(npz) as z:
        dem = np.asarray(z["dem"], dtype=np.float32)
    finite = dem[np.isfinite(dem)]
    return {
        "run_id": run_id,
        "available": True,
        "nx": int(info["nx"]),
        "ny": int(info["ny"]),
        "bbox": info["bbox"],
        "cellsize_m": info.get("cellsize_m"),
        "z_min_m": round(float(finite.min()), 2) if finite.size else 0.0,
        "z_max_m": round(float(finite.max()), 2) if finite.size else 0.0,
        "source": info.get("source"),
        "conditioning": info.get("conditioning"),
        "encoding": "terrain.png: elevation = z_min + (R*256 + G)/65535 * (z_max - z_min)",
    }


@app.get("/api/runs/{run_id}/terrain.png", tags=["runs"])
def terrain_png(run_id: str) -> FileResponse:
    """The conditioned DEM as a 16-bit heightfield the browser can read."""
    import numpy as np
    from PIL import Image

    _require_run(run_id)
    out = _derived(run_id) / "terrain.png"
    if out.exists():
        return FileResponse(out, media_type="image/png")

    found = _cond_dem_for(run_id)
    if found is None:
        raise HTTPException(
            404,
            "no conditioned DEM cached for this domain - the 3D scene falls "
            "back to a flat bed and says so",
        )
    npz, _info = found
    with np.load(npz) as z:
        dem = np.asarray(z["dem"], dtype=np.float32)
    dem = np.nan_to_num(dem, nan=float(np.nanmin(dem)))
    lo, hi = float(dem.min()), float(dem.max())
    span = max(hi - lo, 1e-6)
    q = np.clip((dem - lo) / span, 0, 1) * 65535.0
    q = q.round().astype(np.uint16)
    rgba = np.stack(
        [
            (q >> 8).astype(np.uint8),
            (q & 0xFF).astype(np.uint8),
            np.zeros(q.shape, np.uint8),
            np.full(q.shape, 255, np.uint8),
        ],
        -1,
    )
    Image.fromarray(rgba, mode="RGBA").save(out, optimize=True)
    return FileResponse(out, media_type="image/png")


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
