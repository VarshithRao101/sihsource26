"""
api/index.py - the read-only build of the console, for Vercel.

WHY THIS FILE EXISTS, in one paragraph, because the difference matters and a
juror may well ask.

modules/04_backend/api.py is the real backend: it solves. It needs numba to JIT
the shallow-water kernels, rasterio and GDAL to read terrain, torch for the
emulator, a writable disk for the run folder, a long-lived process to hold the
run registry, and a WebSocket to stream progress. None of those exist on a
serverless platform: functions are frozen the moment they return a response,
the filesystem is read-only apart from an ephemeral /tmp that is not shared
between invocations, WebSockets are not supported at all, and the dependency
bundle is capped at 250 MB against the 5.6 GB this project installs locally.

So this is not a port of that backend. It is a SEPARATE, DELIBERATELY SMALLER
app that serves what can honestly be served from static files: the two pages,
the processing graph, the dam register, and the outputs of runs that were
solved on a real machine and committed to the repository. It imports nothing
outside the standard library and fastapi - no numpy, no rasterio, no torch.

Anything that would require actually computing returns 501 with a message
saying so and pointing at the real backend. It never fakes a result, and it
never pretends a solve happened. `/health` reports mode="readonly" so the
frontend can grey out PLAY and say why rather than letting somebody press it
and watch nothing happen.

Run the real thing locally for a live demo:

    .venv\\Scripts\\python.exe -m uvicorn modules.04_backend.api:app --port 8000
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "modules" / "05_frontend"
OUTPUTS = REPO / "outputs"
DAMS = REPO / "data" / "dams" / "dams.geojson"

LIVE_BACKEND_HINT = (
    "This deployment is the read-only build. Solving needs numba, rasterio and "
    "a writable disk, none of which exist on a serverless host. Run "
    "`uvicorn modules.04_backend.api:app --port 8000` for the live pipeline."
)

app = FastAPI(
    title="SIH26161 - Dam Break Inundation (read-only build)",
    description=(
        "Serves the console, the processing graph and the outputs of runs that "
        "were solved on a real machine. It does not solve. Every endpoint that "
        "would need to compute returns 501 rather than a fabricated answer."
    ),
)


def _not_here(what: str):
    raise HTTPException(501, f"{what} is not available on this host. {LIVE_BACKEND_HINT}")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def _page(name: str) -> FileResponse:
    p = UI / name
    if not p.exists():
        raise HTTPException(404, f"{name} not found")
    return FileResponse(p, media_type="text/html")


@app.get("/", include_in_schema=False)
def console():
    return _page("index.html")


@app.get("/workflow", include_in_schema=False)
def workflow():
    return _page("workflow.html")


# The file that says WHICH BACKEND to talk to. A cached copy sends a
# redeployed page at the wrong host, and the only symptom is "backend
# unreachable". A few hundred bytes; never worth caching.
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/config.js", include_in_schema=False)
def config_js():
    p = UI / "config.js"
    if not p.exists():
        return PlainTextResponse(
            "window.SIH_API_BASE='';", media_type="text/javascript",
            headers=_NO_CACHE,
        )
    return FileResponse(p, media_type="text/javascript", headers=_NO_CACHE)


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    runs = [d.name for d in _run_dirs()]
    return {
        "status": "ok",
        "mode": "readonly",
        "reason": LIVE_BACKEND_HINT,
        "schema_version": "2.0",
        "runs_on_disk": len(runs),
        "runs": runs,
        # The real backend reports these after warming them. Here they are
        # genuinely absent rather than zero.
        "surrogate": "not loaded - torch is not installed on this host",
        "jit_warmup_s": None,
    }


@app.get("/api/pipeline", tags=["meta"])
def pipeline() -> dict:
    """The processing graph. Pure data, so it serves identically here.

    The engine probes inside it look for DualSPHysics, SFINCS and the Delft3D
    kernel on the local filesystem. On this host none of them are present, and
    the graph will say so - which is true of this host, and is the same
    mechanism that reports the truth on a machine where they ARE installed.
    """
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from importlib import import_module

    return import_module("modules.04_backend.pipeline").manifest()


@app.get("/api/enums", tags=["meta"])
def enums() -> dict:
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from shared.contract import (  # stdlib-only module
        ENGINES, FAILURE_MODES, HAZARD_CLASSES, WET_THRESHOLD_M,
    )

    return {
        "engines": list(ENGINES),
        "failure_modes": list(FAILURE_MODES),
        "breach_regressions": ["froehlich2008", "vonthun1990", "macdonald1984"],
        "schemes": ["swe", "inertial"],
        "hazard_classes": list(HAZARD_CLASSES),
        "wet_threshold_m": WET_THRESHOLD_M,
    }


# --------------------------------------------------------------------------
# Dam register - straight out of the committed GeoJSON
# --------------------------------------------------------------------------

_DAM_CACHE: list[dict] | None = None


def _dams() -> list[dict]:
    """Read dams.geojson once per warm instance. json only - no geopandas."""
    global _DAM_CACHE
    if _DAM_CACHE is None:
        if not DAMS.exists():
            raise HTTPException(503, "dam register not deployed with this build")
        with open(DAMS, "r", encoding="utf-8") as fh:
            fc = json.load(fh)
        rows = []
        for feat in fc.get("features", []):
            p = dict(feat.get("properties") or {})
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None]
            p.setdefault("lon", coords[0])
            p.setdefault("lat", coords[1])
            rows.append(p)
        _DAM_CACHE = rows
    return _DAM_CACHE


def _simulatable(d: dict) -> bool:
    return bool(d.get("lat") and d.get("lon") and d.get("height_m")
                and d.get("gross_storage_mcm"))


@app.get("/api/dams/states", tags=["dams"])
def dam_states() -> dict:
    states = sorted({d.get("state") for d in _dams()
                     if d.get("state") and _simulatable(d)})
    return {"states": states}


# --------------------------------------------------------------------------
# River index
#
# The console's River tab called these and got 404 here, because the endpoints
# were added to the real backend and not to this one. That is the tab answering
# "any River" - the phrase in the problem statement's own title - so it broke on
# the deployed build specifically.
#
# modules/01_geodata/rivers.py imports argparse, json, re, sys and pathlib and
# nothing else, so it can be imported here as-is rather than reimplemented. A
# second copy of the search semantics is exactly the kind of drift that ends
# with the two builds disagreeing about what a river is.
# --------------------------------------------------------------------------


def _rivers():
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from importlib import import_module

    try:
        return import_module("modules.01_geodata.rivers")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"river index unavailable: {exc}")


@app.get("/api/rivers", tags=["rivers"])
def river_search(q: str | None = None, state: str | None = None,
                 min_points: int = 1, limit: int = 200) -> dict:
    rv = _rivers()
    rows = rv.search(q=q, state=state, min_points=min_points, limit=limit)
    return {
        "count": len(rows),
        "rivers": rows,
        "source": rv.SOURCE,
        "note": (
            "Grouped on river name AND basin. Indian river names repeat across "
            "the country, and name-only grouping merged unrelated rivers - one "
            "'Ghataprabha' spanned 13 basins from Kerala to Kashmir."
        ),
    }


@app.get("/api/rivers/states", tags=["rivers"])
def river_states() -> dict:
    return {"states": _rivers().states()}


@app.get("/api/rivers/{river_id}", tags=["rivers"])
def river_detail(river_id: str) -> dict:
    r = _rivers().get(river_id)
    if r is None:
        raise HTTPException(404, f"unknown river_id {river_id!r}")
    return r


@app.get("/api/dams/cities", tags=["dams"])
def dam_cities(state: str) -> dict:
    cities = sorted({d.get("nearest_city") for d in _dams()
                     if d.get("state") == state and d.get("nearest_city")
                     and _simulatable(d)})
    return {"state": state, "cities": cities}


@app.get("/api/dams", tags=["dams"])
def dam_search(
    state: str | None = None,
    city: str | None = None,
    q: str | None = None,
    limit: int = 200,
    include_unsimulatable: bool = False,
) -> dict:
    rows = _dams()
    if not include_unsimulatable:
        rows = [d for d in rows if _simulatable(d)]
    if state:
        rows = [d for d in rows if d.get("state") == state]
    if city:
        rows = [d for d in rows if d.get("nearest_city") == city]
    if q:
        ql = q.lower()
        rows = [d for d in rows
                if ql in str(d.get("name", "")).lower()
                or ql in str(d.get("river", "")).lower()]
    rows = sorted(rows, key=lambda d: -(d.get("gross_storage_mcm") or 0))[:limit]
    return {"count": len(rows), "dams": rows, "source": "CWC NRLD 2019"}


@app.get("/api/dams/{dam_id}", tags=["dams"])
def dam_detail(dam_id: str) -> dict:
    for d in _dams():
        if d.get("id") == dam_id:
            return d
    raise HTTPException(404, f"unknown dam_id {dam_id!r}")


# --------------------------------------------------------------------------
# Runs - read back off committed run folders
# --------------------------------------------------------------------------


def _run_dirs() -> list[Path]:
    if not OUTPUTS.is_dir():
        return []
    return sorted(
        (d for d in OUTPUTS.iterdir() if d.is_dir() and (d / "meta.json").is_file()),
        key=lambda d: _meta(d).get("created_utc", ""),
        reverse=True,
    )


def _meta(d: Path) -> dict:
    try:
        with open(d / "meta.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _require(run_id: str) -> Path:
    d = OUTPUTS / run_id
    if not (d / "meta.json").is_file():
        raise HTTPException(404, f"no run {run_id!r} in this build")
    return d


def _optional_json(d: Path, name: str):
    p = d / name
    if not p.is_file():
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/api/runs", tags=["runs"])
def get_runs() -> dict:
    out = []
    for d in _run_dirs():
        m = _meta(d)
        out.append({
            "run_id": d.name,
            "engine": m.get("engine"),
            "is_fake": m.get("is_fake", True),
            "created_utc": m.get("created_utc"),
            "site": (m.get("site") or {}).get("name"),
            "failure_mode": (m.get("scenario") or {}).get("failure_mode"),
            "results": m.get("results", {}),
        })
    return {"runs": out, "count": len(out), "active": []}


@app.post("/api/runs", tags=["runs"], status_code=501)
def create_run():
    """Solving. The one thing this build genuinely cannot do."""
    _not_here("Starting a simulation")


@app.post("/api/runs/{run_id}/pause", tags=["runs"], status_code=501)
def pause_run(run_id: str):
    _not_here("Pausing a solve")


@app.post("/api/runs/{run_id}/resume", tags=["runs"], status_code=501)
def resume_run(run_id: str):
    _not_here("Resuming a solve")


@app.post("/api/runs/{run_id}/cancel", tags=["runs"], status_code=501)
def cancel_run(run_id: str):
    _not_here("Cancelling a solve")


@app.get("/api/runs/{run_id}/status", tags=["runs"])
def run_status(run_id: str) -> dict:
    _require(run_id)
    return {"run_id": run_id, "status": "done", "pct": 100.0, "mode": "readonly"}


@app.get("/api/runs/{run_id}", tags=["runs"])
def get_run(run_id: str) -> dict:
    d = _require(run_id)
    m = _meta(d)
    return {
        "meta": m,
        "is_fake": m.get("is_fake", True),
        "engine": m.get("engine"),
        "files": sorted(p.name for p in d.iterdir() if p.is_file()),
        "frames": 0,
        "impact": _optional_json(d, "impact.json"),
        "uncertainty": _optional_json(d, "uncertainty.json"),
    }


@app.get("/api/runs/{run_id}/evacuation", tags=["runs"])
def evacuation(run_id: str):
    data = _optional_json(_require(run_id), "evacuation.json")
    if data is None:
        raise HTTPException(404, "no evacuation plan for this run")
    return data


@app.get("/api/runs/{run_id}/uncertainty", tags=["runs"])
def uncertainty(run_id: str):
    data = _optional_json(_require(run_id), "uncertainty.json")
    if data is None:
        raise HTTPException(404, "no uncertainty block for this run")
    return data


@app.get("/api/runs/{run_id}/hydrograph", tags=["runs"])
def hydrograph(run_id: str) -> dict:
    """Parsed with the csv module. shared.io would pull in numpy."""
    d = _require(run_id)
    p = d / "hydrograph.csv"
    if not p.is_file():
        raise HTTPException(404, "no hydrograph.csv for this run")
    t, q = [], []
    with open(p, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                t.append(round(float(row["time_hr"]), 4))
                q.append(round(float(row["discharge_cumecs"]), 2))
            except (KeyError, ValueError):
                continue
    return {
        "run_id": run_id,
        "time_hr": t,
        "discharge_cumecs": q,
        "peak_cumecs": round(max(q), 1) if q else 0.0,
    }


@app.get("/api/runs/{run_id}/extent", tags=["runs"])
def extent(run_id: str) -> JSONResponse:
    d = _require(run_id)
    p = d / "extent.geojson"
    if not p.is_file():
        raise HTTPException(404, "no extent.geojson for this run")
    with open(p, "r", encoding="utf-8") as fh:
        return JSONResponse(json.load(fh))


@app.get("/api/runs/{run_id}/validate", tags=["runs"])
def validate(run_id: str) -> dict:
    """The validator reads every raster, which needs rasterio and numpy.

    Reporting "ok" without running it would be exactly the kind of unearned
    claim the contract validator exists to prevent, so this says plainly that
    it did not run.
    """
    _require(run_id)
    return {
        "run_id": run_id,
        "ok": False,
        "errors": [],
        "warnings": [
            "The contract validator did not run on this host - it needs "
            "rasterio to read the GeoTIFFs. This is NOT a validation failure; "
            "it is an unchecked run. " + LIVE_BACKEND_HINT
        ],
        "facts": {"validator_ran": False},
    }


@app.get("/api/runs/{run_id}/probe", tags=["runs"], status_code=501)
def probe(run_id: str, lat: float, lon: float):
    _not_here("The point query (depth, speed and hazard at a cell)")


@app.get("/api/runs/{run_id}/fields", tags=["runs"], status_code=501)
def fields_meta(run_id: str):
    _not_here("The velocity field")


@app.get("/api/runs/{run_id}/fields.png", tags=["runs"], status_code=501)
def fields_png(run_id: str):
    _not_here("The velocity texture")


@app.get("/api/runs/{run_id}/terrain", tags=["runs"], status_code=501)
def terrain_meta(run_id: str):
    _not_here("The terrain heightfield")


@app.get("/api/runs/{run_id}/file/{filename}", tags=["runs"])
def get_file(run_id: str, filename: str) -> FileResponse:
    d = _require(run_id)
    target = (d / filename).resolve()
    if not str(target).startswith(str(d.resolve())) or not target.is_file():
        raise HTTPException(404, f"no file {filename!r} in {run_id}")
    return FileResponse(target)


@app.get("/api/runs/{run_id}/export", tags=["export"])
def export(run_id: str, format: str = "geojson"):
    """GeoJSON is a file copy. .shp and .kml need geopandas and fiona."""
    d = _require(run_id)
    if format == "geojson":
        p = d / "extent.geojson"
        if not p.is_file():
            raise HTTPException(404, "no extent.geojson for this run")
        return FileResponse(p, media_type="application/geo+json",
                            filename=f"{run_id}_extent.geojson")
    _not_here(f"Export to .{format} (geopandas and fiona are not installed)")
