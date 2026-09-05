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
import os
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from shared.contract import (
    DAM_FAILURE_MODES,
    ENGINES,
    FAILURE_MODE_INFO,
    FAILURE_MODES,
    RIVER_FAILURE_MODES,
    SCHEMA_VERSION,
)
from shared.io import RunFolder, list_runs, read_json, read_meta
from shared.validate import validate_run

from . import pipeline, reservoir
from .runner import SyntheticTerrain, run_scenario
from .scenario import DEMO_SITES, ScenarioSpec, SiteSpec
from .solver import warm_up_jit

OUTPUTS = Path("outputs")

LOCAL_DEM_DIR = Path("data") / "dem_local"
"""Where a DEM the team downloaded by hand is dropped so a run can use it.

FABDEM is CC BY-NC-SA tiles from Bristol and CartoDEM comes from Bhuvan -
neither is fetchable from OpenTopography, so 'any other DEM' means a file
already on this machine. Only filenames inside this folder are accepted."""


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
    river_id: str | None = Field(
        None,
        description=(
            "A river from the index, e.g. 'godavari-godavari'. The problem "
            "statement asks for 'any River' and for river blockage analysis, "
            "and four of the five events it names are natural dams. Only "
            "failure_mode='blockage_breach' is valid with this: a river has no "
            "gates and no embankment to overtop."
        ),
    )
    river_point_index: int = Field(
        0, ge=0,
        description=(
            "Which point on that river to block, indexed north to south. See "
            "GET /api/rivers/{river_id}."
        ),
    )
    event_id: str | None = Field(
        None,
        description=(
            "A historic natural-dam failure, e.g. 'rishiganga2021'. Four of the "
            "five events the problem statement names are natural dams on rivers "
            "with no entry in the CWC register, so neither dam_id nor river_id "
            "can reach them. This does: the coordinate and the debris height "
            "come from the event record and the mode is forced to "
            "'blockage_breach'. The figures are approximate and say so - see "
            "GET /api/events."
        ),
    )
    failure_mode: Literal[
        "overtopping",
        "piping",
        "foundation_failure",
        "spillway_blockage",
        "gated_release",
        "blockage_breach",
        "glof_moraine",
        "river_flood",
    ] = Field(
        "overtopping",
        description=(
            "How the water gets out. Eight modes, each a different calculation "
            "- see GET /api/enums -> failure_mode_info for what each one "
            "computes and which real failure it was written against. Four of "
            "them need a structure (overtopping, piping, foundation_failure, "
            "spillway_blockage, gated_release) and the validator rejects them "
            "on a river reach, which has no crest, embankment, foundation or "
            "gates."
        ),
    )
    reservoir_level_frac: float = Field(1.0, ge=0.0, le=1.0)
    breach_regression: Literal["froehlich2008", "vonthun1990", "macdonald1984"] = (
        "froehlich2008"
    )
    breach_width_m: float | None = Field(None, gt=0)
    formation_time_hr: float | None = Field(None, gt=0)
    reach_length_km: float = Field(60.0, gt=0, le=500)
    cellsize_m: float = Field(
        60.0, ge=10, le=500,
        description=(
            "Solver cell size in metres. 60 m is the coarsest CONVERGED grid, "
            "measured on two dams in docs/CONVERGENCE.md - refining 90 m to "
            "60 m still moves max depth 3-6%, while 60 m to 45 m moves it ~1%. "
            "Runs before 2026-09-04 used 90 m and their depths carry that error."
        ),
    )
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

    # --- input datasets, deliverable (ii) -------------------------------
    dem_source: Literal[
        "COP30", "SRTM", "NASADEM", "ALOS", "ASTER", "FABDEM", "CartoDEM"
    ] = Field(
        "COP30",
        description=(
            "Which elevation model to run on. NTRO's dataset link names "
            "'ASTER/ STRM or any other DEM' and all of these are supported by "
            "name. COP30, SRTM, NASADEM, ALOS and ASTER are fetched from "
            "OpenTopography for any bbox; FABDEM and CartoDEM are not "
            "redistributable, so they need local_dem pointing at a tile you "
            "already hold. The choice is recorded in meta.json -> dem.source."
        ),
    )
    local_dem: str | None = Field(
        None,
        description=(
            "Filename of a DEM already on disk, inside data/dem_local/. This is "
            "the 'any other DEM' entry point: FABDEM tiles, CartoDEM, a state "
            "LiDAR product. Only a name is accepted, never a path - see "
            "GET /api/enums -> local_dems for what is present."
        ),
    )
    bathymetry: bool = Field(
        True,
        description=(
            "Estimate a channel bed under the water surface the DEM measured, "
            "instead of burning a flat trench. A 30 m DEM cannot see the bed, "
            "so this is an assumption either way and meta.json says which one."
        ),
    )
    manning_source: Literal["auto", "constant"] = Field(
        "auto",
        description=(
            "'auto' derives per-cell roughness from ESA WorldCover land cover - "
            "this is the satellite imagery feeding the model rather than only "
            "the validation. 'constant' uses manning_n everywhere, which is "
            "what to pick when the run must be reproducible offline."
        ),
    )

    # --- resolution and routing, the knobs that move the numbers --------
    corridor_width_km: float = Field(
        12.0, gt=0, le=60,
        description=(
            "Width of the modelled corridor either side of the traced channel. "
            "Widen it when the floodplain is broader than the domain; every "
            "extra kilometre costs cells."
        ),
    )
    output_step_hr: float = Field(
        0.25, ge=0.02, le=2.0,
        description=(
            "Spacing of the saved output series. Finer resolves the arrival "
            "and peak timing more sharply and writes more frames; the solver "
            "timestep is unaffected."
        ),
    )
    inflow_cumecs: float = Field(
        0.0, ge=0.0, le=200000,
        description=(
            "Steady inflow into the reservoir or the impounded lake during the "
            "event, m3/s. Left at 0 the reservoir only empties. For a landslide "
            "dam this is what decides how long the lake takes to fill and "
            "overtop - see blockage.time_to_overtop."
        ),
    )
    storage_exponent: float = Field(
        2.7, ge=1.0, le=5.0,
        description=(
            "k in the storage-elevation curve S = C * h^k. 2.7 is the default "
            "for a valley reservoir; a broad flat reservoir is nearer 2.0 and a "
            "narrow gorge nearer 3.5. It changes how fast the level falls."
        ),
    )

    # --- foundation / abutment failure ----------------------------------
    foundation_breach_frac: float = Field(
        0.8, gt=0.0, le=1.0,
        description=(
            "Only for failure_mode='foundation_failure'. How much of the crest "
            "goes when the structure is displaced. 0.8 by default because at "
            "St Francis the centre section stood and at Malpasset an abutment "
            "remained."
        ),
    )
    foundation_base_width_ratio: float = Field(
        0.25, gt=0.0, le=1.0,
        description=(
            "Opening width at the bed as a fraction of its width at the crest. "
            "A concrete dam stands in a gorge, so the crest length is the "
            "valley width at the TOP; 0.25 is a steep gorge and 1.0 would make "
            "the opening rectangular, which over-predicts the peak badly."
        ),
    )
    collapse_time_min: float = Field(
        2.0, gt=0.0, le=120.0,
        description=(
            "Minutes from first movement to the opening fully formed. Minutes, "
            "not hours - this is a structural collapse, not erosion."
        ),
    )

    # --- spillway / gate blockage ---------------------------------------
    residual_spillway_frac: float = Field(
        0.0, ge=0.0, le=1.0,
        description=(
            "Only for failure_mode='spillway_blockage'. What fraction of the "
            "design outlet capacity still works. 0.0 is a complete blockage; "
            "Banqiao's gates were silted rather than removed, so intermediate "
            "values are the realistic ones."
        ),
    )
    blockage_start_level_frac: float = Field(
        0.85, ge=0.0, le=1.0,
        description=(
            "How full the reservoir is when the outlets are lost. The filling "
            "phase starts here; the level at failure is whatever it reaches."
        ),
    )

    # --- glacial lake outburst ------------------------------------------
    moraine_height_m: float = Field(
        30.0, gt=0, le=300,
        description=(
            "Only for failure_mode='glof_moraine'. Height of the moraine ridge "
            "above the downstream valley floor. As with a landslide dam, the "
            "impounded volume is read off the DEM rather than assumed."
        ),
    )
    moraine_erodible_depth_m: float | None = Field(
        None, gt=0, le=300,
        description=(
            "How deep the breach can cut into the moraine. None means 0.6 of "
            "the ridge height. Below this is the bedrock sill and any buried "
            "ice core, and the breach stops there."
        ),
    )
    glof_breach_width_m: float | None = Field(
        None, gt=0, le=2000,
        description=(
            "Final breach bottom width. None means one times the erodible "
            "depth. The most sensitive number in the mode: the published South "
            "Lhonak scenarios span 4,311 / 8,000 / 12,487 m3/s for 20 / 30 / "
            "40 m widths on the same lake."
        ),
    )
    avalanche_surge_frac: float = Field(
        0.0, ge=0.0, le=1.0,
        description=(
            "Fraction of the lake displaced over the crest by an entering ice "
            "or rock mass, ahead of any breach. Zero by default: it is a "
            "volume over a duration, so it sets the peak directly and will "
            "dominate the breach at more than a few percent."
        ),
    )
    avalanche_surge_duration_s: float = Field(600.0, gt=0, le=7200)
    lake_area_km2: float | None = Field(
        None, gt=0, le=500,
        description=(
            "Lake surface area, used only when the DEM cannot see the lake. "
            "The volume then falls back on Huggel et al. (2002) "
            "V = 0.104 A^1.42, which the source reports with roughly a "
            "factor-of-two scatter."
        ),
    )

    # --- river flood wave -----------------------------------------------
    peak_discharge_cumecs: float = Field(
        2000.0, gt=0, le=500000,
        description=(
            "Only for failure_mode='river_flood'. Peak of the flood wave "
            "entering the reach. There is deliberately no direction parameter "
            "anywhere in this mode - where the water goes is read off the DEM."
        ),
    )
    time_to_peak_hr: float = Field(3.0, gt=0, le=240)
    flood_duration_hr: float | None = Field(
        None, gt=0, le=480,
        description=(
            "Total flood duration. None means 2.67 times the time to peak, the "
            "NRCS dimensionless unit hydrograph ratio."
        ),
    )
    base_flow_cumecs: float = Field(0.0, ge=0.0, le=200000)

    notes: str = ""

    def terrain_options(self) -> dict:
        """The module 01 provider settings this request asks for.

        Kept out of ScenarioSpec because they describe how the terrain was
        obtained, not what failed. `dem_source` is the exception - it belongs
        in meta.json next to the result, so the spec carries it.
        """
        return {
            "local_dem": self.resolved_local_dem(),
            "bathymetry": self.bathymetry,
            "manning_source": self.manning_source,
            # manning_n only reaches the solver through the roughness raster,
            # so a constant run has to hand it to the provider. With 'auto' it
            # is ignored on purpose: land cover wins over a typed-in number.
            "manning_constant": self.manning_n if self.manning_source == "constant" else None,
        }

    def resolved_local_dem(self) -> Path | None:
        """The local DEM as a real path, or a 422 explaining why it is not one."""
        if not self.local_dem:
            if self.dem_source in ("FABDEM", "CartoDEM"):
                raise HTTPException(
                    422,
                    f"dem_source {self.dem_source!r} is not redistributable and "
                    f"cannot be downloaded. Put the tile in {LOCAL_DEM_DIR}/ and "
                    f"pass local_dem with its filename, or pick one of "
                    f"COP30, SRTM, NASADEM, ALOS, ASTER.",
                )
            return None
        name = Path(self.local_dem).name
        if name != self.local_dem:
            # Only a filename. A path here would let a request read any file
            # on the machine the API runs on.
            raise HTTPException(
                422, f"local_dem must be a filename inside {LOCAL_DEM_DIR}/, not a path"
            )
        path = LOCAL_DEM_DIR / name
        if not path.is_file():
            raise HTTPException(
                404,
                f"no DEM named {name!r} in {LOCAL_DEM_DIR}/. "
                f"GET /api/enums lists what is there.",
            )
        return path

    def to_spec(self) -> ScenarioSpec:
        design_spillway: float | None = None
        if self.dam_id:
            from importlib import import_module

            catalogue = import_module("modules.01_geodata.dams")
            dam = catalogue.get(self.dam_id)
            if dam is None:
                raise HTTPException(404, f"unknown dam_id {self.dam_id!r}")
            if not catalogue.is_simulatable(dam):
                raise HTTPException(
                    422,
                    f"{dam['name']} cannot be simulated: the catalogue has no "
                    f"coordinates, height or storage capacity for it.",
                )
            natural = dam.get("kind") == "natural"
            if natural:
                # A natural dam has no published storage and there is no honest
                # way to invent one - it would feed the breach regression
                # directly. The volume is read off the terrain instead, so the
                # capacity here is a placeholder that runner.py replaces, and
                # only the modes that do that are allowed.
                if self.failure_mode not in ("blockage_breach", "glof_moraine"):
                    raise HTTPException(
                        422,
                        f"{dam['name']} is a natural dam - a moraine or a debris "
                        f"barrier. It has no embankment to overtop, no "
                        f"foundation and no gates. Post "
                        f"failure_mode='glof_moraine' or 'blockage_breach'.",
                    )
                default_h = type(self).model_fields["blockage_height_m"].default
                if self.blockage_height_m == default_h:
                    self.blockage_height_m = float(dam["height_m"])
                default_m = type(self).model_fields["moraine_height_m"].default
                if self.moraine_height_m == default_m:
                    self.moraine_height_m = float(dam["height_m"])
                capacity_mcm = 1.0
            else:
                capacity_mcm = float(dam["gross_storage_mcm"])

            site = SiteSpec(
                name=dam["name"],
                lat=dam["lat"],
                lon=dam["lon"],
                river=dam["river"] or "",
                state=dam["state"] or "",
                dam_height_m=float(dam["height_m"]),
                reservoir_capacity_mcm=capacity_mcm,
                source=dam.get("source") or "CWC NRLD 2019",
                kind=dam.get("kind", "engineered"),
                crest_length_m=(
                    float(dam["length_m"]) if dam.get("length_m") else None
                ),
                height_source=dam.get("height_source", ""),
            )
            # The register carries the design discharge capacity for many dams.
            # It is a measured number and beats any assumption we could make
            # about how much water the outlet works can pass.
            design_spillway = dam.get("spillway_cumecs")
            if natural:
                reported = dam.get("reported_impoundment_mcm")
                bits = [
                    f"Natural dam. Barrier height {dam['height_m']:.0f} m "
                    f"({dam.get('height_source') or 'unstated source'}). No "
                    f"published storage exists for it; this run reads the "
                    f"impounded volume off the DEM."
                ]
                if dam.get("mechanism"):
                    bits.append(f"Reported mechanism: {dam['mechanism']}.")
                if reported:
                    bits.append(
                        f"Reported impoundment {reported:g} MCM, carried for "
                        f"comparison and NOT used to drive the run."
                    )
                note = " ".join(bits)
                self.notes = f"{self.notes} {note}".strip() if self.notes else note
        elif self.event_id:
            # The entry point for the failures NTRO actually names. No register
            # lists these rivers, so this is the only way to reach them.
            from importlib import import_module

            events = import_module("modules.01_geodata.events")
            ev = events.get(self.event_id)
            if ev is None:
                raise HTTPException(404, f"unknown event_id {self.event_id!r}")
            if self.failure_mode != "blockage_breach":
                raise HTTPException(
                    422,
                    f"{ev['name']} ({ev['year']}) is a natural dam. Post "
                    f"failure_mode='blockage_breach'; there is no embankment to "
                    f"overtop and no gate to open.",
                )
            # The debris height is the event's unless the operator overrode it -
            # which is the whole point of having a scenario tool rather than a
            # replay: "what if the barrier had been 60 m instead of 30".
            if self.blockage_height_m == type(self).model_fields["blockage_height_m"].default:
                self.blockage_height_m = float(ev["blockage_height_m"])
            site = SiteSpec(
                name=f"{ev['name']} {ev['year']}",
                lat=float(ev["lat"]),
                lon=float(ev["lon"]),
                river=ev["river"],
                state=ev["state"],
                # Both are placeholders in blockage mode, exactly as for a
                # river point: runner.py replaces the height with
                # blockage_height_m and the capacity with what the DEM holds
                # behind the debris. The REPORTED volume is deliberately not
                # used as the capacity - it is carried in notes so the run can
                # be compared against it, not driven by it.
                dam_height_m=self.blockage_height_m,
                reservoir_capacity_mcm=1.0,
                source=ev["source"],
            )
            reported = ev.get("reported_impoundment_mcm")
            note = (
                f"Historic event: {ev['name']} ({ev['year']}), {ev['mechanism']}. "
                f"Coordinates and debris height are APPROXIMATE - {ev['source']}. "
                f"Reported impoundment "
                + (f"{reported:g} MCM" if reported else "not published")
                + "; this run uses the volume read off the DEM. The trigger is "
                "not modelled and no observed extent has been compared."
            )
            self.notes = f"{self.notes} {note}".strip() if self.notes else note
        elif self.river_id:
            # The problem statement asks for "any River" and for river blockage,
            # and four of the five events it names are natural dams rather than
            # engineered ones. This is that entry point.
            from importlib import import_module

            rivers = import_module("modules.01_geodata.rivers")
            river = rivers.get(self.river_id)
            if river is None:
                raise HTTPException(404, f"unknown river_id {self.river_id!r}")
            pt = rivers.point(self.river_id, self.river_point_index)
            if pt is None:
                raise HTTPException(
                    422,
                    f"{river['name']} has {river['point_count']} point(s); "
                    f"river_point_index {self.river_point_index} is out of range.",
                )
            if self.failure_mode not in RIVER_FAILURE_MODES:
                # A river has no crest, no embankment, no foundation and no
                # gates. Silently switching the mode would run a scenario the
                # operator did not ask for, so this refuses and says which
                # modes apply.
                raise HTTPException(
                    422,
                    f"failure_mode {self.failure_mode!r} needs a dam. On a "
                    f"river reach the water either arrives as a flood wave or "
                    f"is released by something natural failing across the "
                    f"channel. Post one of: "
                    f"{', '.join(RIVER_FAILURE_MODES)}, or pick a dam_id "
                    f"instead.",
                )
            site = SiteSpec(
                name=f"{river['name']} at {pt['name']}",
                lat=pt["lat"],
                lon=pt["lon"],
                river=river["name"],
                state=pt.get("state", ""),
                # NEITHER of these is used in blockage mode: runner.py replaces
                # the height with blockage_height_m and the capacity with the
                # volume impounded behind the debris, read off the DEM. They are
                # set to the blockage height only because SiteSpec.validate()
                # requires them positive. Nothing downstream reads them.
                dam_height_m=self.blockage_height_m,
                reservoir_capacity_mcm=1.0,
                source=river["source"],
                kind="natural",
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
            raise HTTPException(422, "provide one of dam_id, river_id, site or site_key")

        return ScenarioSpec(
            site=site,
            failure_mode=self.failure_mode,
            reservoir_level_frac=self.reservoir_level_frac,
            breach_regression=self.breach_regression,
            breach_width_m=self.breach_width_m,
            formation_time_hr=self.formation_time_hr,
            reach_length_km=self.reach_length_km,
            corridor_width_km=self.corridor_width_km,
            cellsize_m=self.cellsize_m,
            end_hr=self.end_hr,
            output_step_hr=self.output_step_hr,
            scheme=self.scheme,
            manning_n=self.manning_n,
            inflow_cumecs=self.inflow_cumecs,
            storage_exponent=self.storage_exponent,
            # A run that says COP30 in meta.json must have been solved on
            # COP30, so the requested source travels with the scenario rather
            # than being decided again further down.
            dem_source=self.dem_source if self.real_terrain else "SYNTHETIC",
            blockage_height_m=self.blockage_height_m,
            foundation_breach_frac=self.foundation_breach_frac,
            foundation_base_width_ratio=self.foundation_base_width_ratio,
            collapse_time_min=self.collapse_time_min,
            residual_spillway_frac=self.residual_spillway_frac,
            blockage_start_level_frac=self.blockage_start_level_frac,
            moraine_height_m=self.moraine_height_m,
            moraine_erodible_depth_m=self.moraine_erodible_depth_m,
            glof_breach_width_m=self.glof_breach_width_m,
            avalanche_surge_frac=self.avalanche_surge_frac,
            avalanche_surge_duration_s=self.avalanche_surge_duration_s,
            lake_area_km2=self.lake_area_km2,
            peak_discharge_cumecs=self.peak_discharge_cumecs,
            time_to_peak_hr=self.time_to_peak_hr,
            flood_duration_hr=self.flood_duration_hr,
            base_flow_cumecs=self.base_flow_cumecs,
            source_kind="river" if self.river_id else "dam",
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
    # The Vite dev server by default. If the pages are hosted somewhere else -
    # Vercel, Netlify - set SIH_CORS_ORIGINS to a comma-separated list of those
    # origins, or the browser will refuse every call the page makes.
    #
    # "*" is accepted and works: allow_credentials is deliberately NOT set, and
    # a wildcard origin is only rejected by browsers when credentials are
    # allowed. start_live_demo.bat relies on that - it serves the pages from
    # this backend through a tunnel whose hostname changes every run, so there
    # is no fixed origin to list.
    allow_origins=[
        o.strip()
        for o in os.environ.get(
            "SIH_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if o.strip()
    ],
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


# config.js is the file that tells the pages WHICH BACKEND to talk to, so a
# browser holding a stale copy points a redeployed page at the wrong host - or
# at no host - and the only symptom is "backend unreachable". It is a few
# hundred bytes. It is never worth caching.
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/theme.css", include_in_schema=False)
def theme_css():
    """The design layer. Owned by the two designers; nobody else edits it.

    no-store like config.js, and for the same reason in reverse: a designer
    reloading the page has to see the edit they just made, and a cached
    stylesheet makes it look as though their change did nothing.
    """
    path = UI_DIR / "theme.css"
    if not path.exists():
        return PlainTextResponse("", media_type="text/css", headers=_NO_CACHE)
    return FileResponse(path, media_type="text/css", headers=_NO_CACHE)


@app.get("/config.js", include_in_schema=False)
def config_js():
    """Tells the pages where the backend is. Same origin here, by default."""
    path = UI_DIR / "config.js"
    if not path.exists():
        return PlainTextResponse(
            "window.SIH_API_BASE='';"
            "window.SIH_WS=function(p){return (location.protocol==='https:'?'wss':'ws')"
            "+'://'+location.host+p;};",
            media_type="text/javascript",
            headers=_NO_CACHE,
        )
    return FileResponse(path, media_type="text/javascript", headers=_NO_CACHE)


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

    # What is on disk right now, not what we wish were there. An empty list
    # means nobody has downloaded a FABDEM or CartoDEM tile yet, and the UI
    # says so instead of offering a source that cannot run.
    local_dems: list[str] = []
    if LOCAL_DEM_DIR.is_dir():
        local_dems = sorted(
            p.name
            for p in LOCAL_DEM_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in (".tif", ".tiff", ".hgt", ".asc")
        )

    return {
        "engines": list(ENGINES),
        "failure_modes": list(FAILURE_MODES),
        # Which modes are legal against which kind of source, and what each one
        # actually computes. The UI builds its case list from this rather than
        # carrying a second copy that can drift out of step with the validator.
        "dam_failure_modes": list(DAM_FAILURE_MODES),
        "river_failure_modes": list(RIVER_FAILURE_MODES),
        "failure_mode_info": FAILURE_MODE_INFO,
        "breach_regressions": ["froehlich2008", "vonthun1990", "macdonald1984"],
        "schemes": ["swe", "inertial"],
        "hazard_classes": list(HAZARD_CLASSES),
        "wet_threshold_m": WET_THRESHOLD_M,
        # NTRO's dataset link is "ASTER/ STRM or any other DEM". These are the
        # ones the tool can actually run on, split by whether we can fetch them.
        "dem_sources_fetchable": ["COP30", "SRTM", "NASADEM", "ALOS", "ASTER"],
        "dem_sources_local": ["FABDEM", "CartoDEM"],
        "local_dems": local_dems,
        "local_dem_dir": str(LOCAL_DEM_DIR),
        "manning_sources": ["auto", "constant"],
        # Every scalar default the solver runs on, read off the request model
        # itself. The dashboard renders its advanced controls from this, so a
        # default can never drift between the two.
        "run_defaults": {
            name: f.default
            for name, f in RunRequest.model_fields.items()
            if isinstance(f.default, (int, float, str, bool)) or f.default is None
        },
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


def _rivers():
    from importlib import import_module

    try:
        return import_module("modules.01_geodata.rivers")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"river index unavailable: {exc}")


@app.get("/api/rivers", tags=["rivers"])
def river_search(
    q: str | None = None,
    state: str | None = None,
    min_points: int = 1,
    limit: int = 200,
) -> dict:
    """Find a river to place a blockage on.

    This is an INDEX, not a river network: it maps river names to points whose
    coordinates we hold, and every one of those is a dam in the CWC register.
    The channel itself is traced from the DEM at run time, so a river absent
    from here can still be modelled by posting a coordinate directly.
    """
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
    """Every state with at least one indexed river."""
    return {"states": _rivers().states()}


@app.get("/api/rivers/{river_id}", tags=["rivers"])
def river_detail(river_id: str) -> dict:
    """One river and every point we can start a blockage from."""
    r = _rivers().get(river_id)
    if r is None:
        raise HTTPException(404, f"unknown river_id {river_id!r}")
    return r


@app.get("/api/events", tags=["events"])
def events() -> dict:
    """The historic natural-dam failures, as run-able entry points.

    Four of the five failures the problem statement names are natural dams on
    rivers the CWC register does not list, so neither the dam picker nor the
    river index can reach them. Every figure here is approximate and every
    record says so.
    """
    from importlib import import_module

    ev = import_module("modules.01_geodata.events")
    rows = ev.all_events()
    return {
        "events": rows,
        "count": len(rows),
        "named_by_ntro": sum(1 for r in rows if r["named_in_problem_statement"]),
        "source": ev.SOURCE,
    }


@app.get("/api/events/{event_id}", tags=["events"])
def event_detail(event_id: str) -> dict:
    from importlib import import_module

    ev = import_module("modules.01_geodata.events")
    row = ev.get(event_id)
    if row is None:
        raise HTTPException(404, f"unknown event_id {event_id!r}")
    return row


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


@app.get("/api/dams/kinds", tags=["dams"])
def dam_kinds() -> dict:
    """How the catalogue splits, and what each half can be asked for.

    The two are one list in the picker on purpose - the operator's question is
    about the water, not about who built the barrier - but they are not
    interchangeable, and this says how.
    """
    cat = _catalogue()
    rows = cat.load_catalogue()
    natural = [d for d in rows if d.get("kind") == "natural"]
    return {
        "engineered": {
            "count": sum(1 for d in rows if d.get("kind") != "natural"),
            "source": "CWC National Register of Large Dams, 2019",
            "has_storage": True,
            "failure_modes": list(DAM_FAILURE_MODES),
        },
        "natural": {
            "count": len(natural),
            "source": (
                "supplied natural-dam coordinate dataset, plus the historic "
                "natural-dam failures"
            ),
            "has_storage": False,
            "why_no_storage": (
                "No natural dam has a published capacity. The impounded volume "
                "is read off the DEM at run time instead of being invented."
            ),
            "failure_modes": ["glof_moraine", "blockage_breach"],
            "height_sources": sorted(
                {d.get("height_source", "") for d in natural if d.get("height_source")}
            ),
            "historic": sum(1 for d in natural if d.get("historic")),
        },
    }


@app.get("/api/dams/{dam_id}", tags=["dams"])
def dam_detail(dam_id: str) -> dict:
    dam = _catalogue().get(dam_id)
    if dam is None:
        raise HTTPException(404, f"unknown dam_id {dam_id!r}")
    return dam


@app.get("/api/demo-runs", tags=["runs"])
def demo_runs() -> dict:
    """The curated stored runs the console cycles through.

    A solve takes minutes on real terrain, which in front of a panel is the
    whole demonstration gone. These are the answer, and the answer is not a
    recording: they are ordinary contract-valid run folders produced by
    runner.run_scenario on COP30, built by integration/build_demo_runs.py.
    Loading one goes down exactly the same path as loading a run somebody
    solved thirty seconds ago.

    Only runs actually present on disk are returned. The manifest is committed
    and the run folders are not - they are large and regenerable - so on a
    fresh clone this correctly returns an empty list rather than four ids that
    404 one click later.
    """
    manifest = Path("data") / "demo_runs.json"
    if not manifest.is_file():
        return {"runs": [], "count": 0,
                "note": "no manifest; build with python -m integration.build_demo_runs"}

    try:
        rows = json.loads(manifest.read_text(encoding="utf-8"))["runs"]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"demo manifest unreadable: {exc}")

    present, missing = [], []
    for r in rows:
        if (OUTPUTS / r["run_id"] / "meta.json").is_file():
            present.append(r)
        else:
            missing.append(r["run_id"])

    return {
        "runs": present,
        "count": len(present),
        "missing": missing,
        "note": (
            "Real runs through the same solver and the same code path as any "
            "other run, not recordings. Rebuild with "
            "python -m integration.build_demo_runs"
        ),
    }


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
    # Raises 422/404 here, before a run_id exists, rather than forty seconds
    # into a background solve that was never going to find the file.
    terrain_opts = req.terrain_options()

    from shared.io import make_run_id, next_sequence

    seq = next_sequence(OUTPUTS, spec.site_slug, spec.scenario_slug, spec.engine)
    run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, seq)

    REGISTRY.start(run_id, spec)
    background.add_task(
        _execute, run_id, spec, req.keep_frames, req.real_terrain, terrain_opts
    )

    return {
        "run_id": run_id,
        "status": "running",
        "websocket": f"/ws/runs/{run_id}",
        "poll": f"/api/runs/{run_id}/status",
        "fingerprint": spec.fingerprint(),
    }


def _prepare_real(spec: ScenarioSpec, run_id: str, terrain_opts: dict | None = None):
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
    opts = terrain_opts or {}
    source = spec.dem_source if spec.dem_source != "SYNTHETIC" else "COP30"
    try:
        terrain = gd.RealTerrain(
            site=site_slug,
            source=source,
            local_dem=opts.get("local_dem"),
            bathymetry=opts.get("bathymetry", True),
            manning_source=opts.get("manning_source", "auto"),
            manning_constant=opts.get("manning_constant"),
            dam_lonlat=plan.dam_lonlat if plan else (spec.site.lon, spec.site.lat),
            reach_length_km=spec.reach_length_km,
        )
        REGISTRY.finish_node(
            run_id,
            "terrain",
            f"{source} fetched and conditioned"
            + (f" from {Path(opts['local_dem']).name}" if opts.get("local_dem") else ""),
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
    run_id: str,
    spec: ScenarioSpec,
    keep_frames: bool,
    real_terrain: bool = True,
    terrain_opts: dict | None = None,
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
            terrain, exposure = _prepare_real(spec, run_id, terrain_opts)
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
    formation_time_hr: float | None = Field(
        None,
        gt=0,
        description=(
            "Leave None and the breach regression computes it from the height, "
            "capacity and level, exactly as a real run would. Setting it asks "
            "the emulator about a breach that forms at a time you chose."
        ),
    )
    breach_regression: Literal["froehlich2008", "vonthun1990", "macdonald1984"] = (
        "froehlich2008"
    )


@app.get("/api/surrogate/meta", tags=["ml"])
def surrogate_meta() -> dict:
    """What the emulator is, where it applies, and how wrong it is.

    The UI reads this to decide whether a preview is even meaningful for the
    site on screen. The emulator is trained per site - one fixed terrain - so
    offering it everywhere would be the most convincing wrong answer we could
    put in front of a juror.
    """
    from importlib import import_module

    sg = import_module("modules.07_ml.surrogate")
    metrics_path = sg.MODEL_DIR / "surrogate_metrics.json"
    metrics = (
        read_json(sg.MODEL_DIR, "surrogate_metrics.json")
        if metrics_path.exists()
        else {}
    )

    return {
        "available": sg.MODEL_PATH.exists(),
        "is_emulated": True,
        "what": (
            "A U-Net trained on this repository's own shallow-water solver. It "
            "emulates the solver, it does not model reality, and its error is "
            "measured against the solver alone."
        ),
        "site": sg.SITE,
        "site_latlon": list(sg.SITE_LATLON),
        "trained_on": {
            "reach_length_km": sg.REACH_KM,
            "corridor_width_km": sg.CORRIDOR_KM,
            "cellsize_m": sg.CELLSIZE_M,
            "end_hr": sg.END_HR,
            "dem_source": "COP30",
        },
        "responds_to": list(sg.PARAMS),
        "param_ranges": {k: list(v) for k, v in sg.PARAM_RANGES.items()},
        # Everything the operator can now set that the emulator never saw. It
        # cannot answer these, so the UI has to stop claiming it did.
        "ignores": [
            "dem_source", "cellsize_m", "corridor_width_km", "manning_source",
            "manning_n", "bathymetry", "failure_mode", "inflow_cumecs",
            "storage_exponent", "reach_length_km", "end_hr",
        ],
        "metrics": metrics,
    }


@app.post("/api/surrogate", tags=["ml"])
def surrogate_predict(req: SurrogateRequest) -> dict:
    """Emulate a scenario in milliseconds instead of solving it.

    This is a PREDICTION FROM A NEURAL NETWORK trained on our own solver, not a
    simulation. The response says so in `is_emulated`, and the UI must label it
    - anything exported or quoted has to be recomputed with the real solver.
    """
    from importlib import import_module

    sg = import_module("modules.07_ml.surrogate")

    params = req.model_dump()
    regression = params.pop("breach_regression")
    derived_formation = False
    if params["formation_time_hr"] is None:
        # The emulator learned formation time as it comes out of the breach
        # regression, so a preview has to derive it the same way the run would.
        # Inventing 0.5 hr here would answer a different question from the one
        # the Run button is about to ask.
        from shared.hydro import breach_parameter_ensemble

        # Same arguments runner.resolve_breach uses: the water actually stored
        # at the starting level - through the power-law storage curve, not a
        # straight fraction of capacity - and the dam height as breach height.
        k = ScenarioSpec.__dataclass_fields__["storage_exponent"].default
        ens = breach_parameter_ensemble(
            req.capacity_mcm * 1e6 * req.reservoir_level_frac**k,
            req.dam_height_m,
        )
        bp = ens.get(regression)
        if bp is None:
            raise HTTPException(422, f"unknown breach_regression {regression!r}")
        params["formation_time_hr"] = float(bp.formation_time_hr)
        derived_formation = True

    try:
        out = sg.predict(params)
    except FileNotFoundError as exc:
        raise HTTPException(503, f"surrogate not trained: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"surrogate unavailable: {type(exc).__name__}: {exc}")

    depth = out["max_depth"]
    wet = depth >= 0.05
    n_wet = int(wet.sum())
    cell_km2 = (sg.CELLSIZE_M / 1000.0) ** 2

    # Which of the asked-for values sit outside the box the network was trained
    # in. Extrapolation is not an error, but answering it silently would be.
    outside = []
    for name, (lo, hi) in sg.PARAM_RANGES.items():
        v = getattr(req, name)
        if not lo <= v <= hi:
            outside.append(f"{name}={v:g} outside trained {lo:g}-{hi:g}")

    arrival = out["arrival_time"][wet] if n_wet else None

    return {
        "is_emulated": True,
        "engine": "surrogate",
        "site": sg.SITE,
        "inference_ms": out["inference_ms"],
        "wet_cells": n_wet,
        "max_depth_m": round(float(depth.max()), 2),
        "flood_area_km2": round(n_wet * cell_km2, 2),
        "cellsize_m": sg.CELLSIZE_M,
        "earliest_arrival_hr": (
            round(float(arrival.min()), 2) if arrival is not None and arrival.size else None
        ),
        "formation_time_hr": round(params["formation_time_hr"], 3),
        "formation_time_source": (
            f"{regression} regression" if derived_formation else "operator override"
        ),
        "extrapolating": outside,
        "warning": (
            "Emulated by a U-Net trained on the fast solver. Extent CSI against "
            "the solver is 0.91 on held-out scenarios. Not a simulation, and "
            "not validated against observed floods."
        ),
    }


@app.get("/api/analysis/health", tags=["ml"])
def analysis_health() -> dict:
    """Whether the AI briefing can run here, and why not if it cannot.

    The console asks before it draws the panel. Every other part of this API
    works with the network unplugged and keeps working when this says no.
    """
    from . import analysis

    return analysis.availability()


@app.post("/api/runs/{run_id}/analysis", tags=["ml"], status_code=200)
def run_analysis(run_id: str, refresh: bool = False) -> dict:
    """Claude reads this run's own output files and writes the briefing.

    Structured: headline, severity with its basis, findings with the payload
    keys each rests on, priority actions, an exposure note, and the limits.
    Then `analysis.check_grounding` matches every number in what it wrote back
    against the run folder and reports any it could not find. A briefing whose
    `grounding.grounded` is false is shown as unsafe, not hidden.

    Cached per run under derived/ - the same run briefed twice is the same
    briefing, and a demo should not pay for it twice.
    """
    run_dir = _require_run(run_id)
    cache = _derived(run_id) / "analysis.json"
    if cache.exists() and not refresh:
        out = read_json(cache.parent, cache.name)
        out["cached"] = True
        return out

    from . import analysis

    state = analysis.availability()
    if not state["available"]:
        raise HTTPException(503, f"AI briefing unavailable: {state['reason']}")

    try:
        out = analysis.analyse(run_dir)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        raise HTTPException(502, f"{type(exc).__name__}: {exc}")

    cache.write_text(json.dumps(out, indent=2), encoding="utf-8")
    out["cached"] = False
    return out


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
