"""
modules/04_backend/pipeline.py - the processing graph, named once.

The workflow page draws boxes and arrows. This file is where those boxes and
arrows are defined, so the picture and the code cannot drift apart: every node
below is a real stage that some module actually executes, and `emitted_by`
names the file that publishes its progress.

Two rules this file exists to enforce:

  * A node that did not run says so. `skipped` and `absent` are first-class
    states, not failures dressed up. Delft3D in particular is reported from
    modules/03_delft3d/engine.py's filesystem probe - if the kernel is not
    installed the node reads "absent" and the graph draws it dashed. It is
    never marked complete, and nothing downstream of it is ever filled in.
  * Nothing here is decorative. `does`, `inputs` and `outputs` are what the
    stage genuinely consumes and writes, and the frontend shows them verbatim
    when a box is expanded.

Owner: captain (module 04).
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# Node states, in the order they can legally progress
# --------------------------------------------------------------------------

WAITING = "waiting"
RUNNING = "running"
COMPLETE = "complete"
FAILED = "failed"
SKIPPED = "skipped"      # did not run in THIS run, and we say why
ABSENT = "absent"        # the engine is not installed at all

NODE_STATES = (WAITING, RUNNING, COMPLETE, FAILED, SKIPPED, ABSENT)


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------
#
# `x` / `y` are grid coordinates, not pixels - the page multiplies them by its
# own spacing so it can zoom without the manifest caring.

NODES: list[dict[str, Any]] = [
    {
        "id": "input",
        "title": "Operator request",
        "subtitle": "dam + scenario",
        "kind": "input",
        "x": 0, "y": 2,
        "module": "modules/04_backend/api.py :: RunRequest",
        "emitted_by": "api._execute",
        "does": (
            "Takes what the operator actually chose - which dam, which failure "
            "mode, how full the reservoir is, how far downstream to look and "
            "for how long - and validates it before any expensive work starts. "
            "A scenario that cannot be simulated is rejected here with the "
            "reason, not halfway through a forty-second solve."
        ),
        "inputs": [
            "dam_id, or a site entered by hand",
            "failure mode",
            "reservoir level, or gate opening",
            "reach length and duration",
        ],
        "outputs": ["a validated ScenarioSpec", "run_id"],
        "sources": [],
    },
    {
        "id": "inflow",
        "title": "Rainfall to inflow",
        "subtitle": "CHIRPS nowcast",
        "kind": "validation",
        "x": 1, "y": 0,
        "module": "modules/07_ml/inflow.py",
        "emitted_by": "engine probe",
        "engine": "inflow",
        "does": (
            "The near-real-time INPUT side of the problem statement, which asks "
            "for Earth Engine in both directions. CHIRPS satellite rainfall over "
            "the catchment goes through an SCS curve-number runoff model and a "
            "routing step to give the reservoir inflow arriving now. It matters "
            "most for a river blockage, where the inflow decides how long the "
            "lake takes to fill - and that fill time is the only warning a "
            "landslide dam gives. Deliberately not an LSTM: there is no observed "
            "inflow series to learn from, and training one on our own runoff "
            "model would teach it our model and let us call the result learned "
            "knowledge."
        ),
        "inputs": ["catchment above the dam", "CHIRPS rainfall"],
        "outputs": ["reservoir inflow (m3/s)", "fill time for a blockage lake"],
        "sources": [
            "CHIRPS v2.0 rainfall via Google Earth Engine",
            "SCS curve number runoff (USDA NRCS)",
        ],
        "optional": True,
        "optional_note": (
            "Run on demand with python -m modules.07_ml.inflow. A live web "
            "request uses the inflow already on the scenario rather than "
            "nowcasting one mid-solve."
        ),
    },
    {
        "id": "catalogue",
        "title": "Dam register lookup",
        "subtitle": "CWC NRLD 2019",
        "kind": "data",
        "x": 1, "y": 2,
        "module": "modules/01_geodata/dams.py",
        "emitted_by": "api._execute",
        "does": (
            "Reads the dam out of the Central Water Commission National "
            "Register of Large Dams: coordinates, height, gross storage, and "
            "where it exists the design spillway discharge. Nobody types a "
            "latitude - every physical number the model uses comes from the "
            "register, so a wrong value is traceable to the register rather "
            "than to us."
        ),
        "inputs": ["dam_id"],
        "outputs": [
            "lat / lon",
            "dam height (m)",
            "gross storage (MCM)",
            "design spillway capacity (m3/s)",
        ],
        "sources": ["CWC National Register of Large Dams, 2019"],
    },
    {
        "id": "river",
        "title": "Trace the river",
        "subtitle": "D8 flow routing",
        "kind": "geodata",
        "x": 2, "y": 1,
        "module": "modules/01_geodata/domain.py :: plan_domain",
        "emitted_by": "api._prepare_real",
        "does": (
            "Downloads a coarse scouting DEM around the dam, fills its pits, "
            "computes D8 flow direction and accumulation, and walks the channel "
            "downstream for the requested reach length. The result is the "
            "simulation domain - a corridor that follows the real river instead "
            "of a rectangle drawn by hand. The dam is snapped onto the traced "
            "channel, because a dam sitting one cell off the river releases its "
            "water onto a hillside."
        ),
        "inputs": ["dam lat / lon", "reach length (km)"],
        "outputs": [
            "domain bbox",
            "dam position snapped to the channel",
            "traced channel cells",
        ],
        "sources": [
            "O'Callaghan & Mark (1984), D8 flow direction",
            "Barnes et al. (2014), priority-flood depression filling",
        ],
    },
    {
        "id": "breach",
        "title": "Breach and outflow",
        "subtitle": "hydrograph at the dam",
        "kind": "physics",
        "x": 2, "y": 3,
        "module": "shared/hydro.py",
        "emitted_by": "runner.run_scenario",
        "does": (
            "Turns the scenario into water leaving the structure. For a dam "
            "break, three independent empirical regressions size the breach and "
            "a reservoir-depletion integration gives the outflow hydrograph; "
            "all three are computed and none is averaged, because they disagree "
            "by up to a factor of ten and hiding that would be dishonest. For a "
            "controlled release no breach regression is used at all - the water "
            "leaves through the gates and spillway the dam was built with."
        ),
        "inputs": [
            "dam height",
            "storage volume",
            "reservoir level, or gate opening",
            "failure mode",
        ],
        "outputs": [
            "breach width and formation time",
            "hydrograph.csv (time_hr, discharge_cumecs)",
            "the three-regression spread",
        ],
        "sources": [
            "Froehlich (2008), ASCE J. Hydraul. Eng. 134(12)",
            "Von Thun and Gillette (1990)",
            "MacDonald and Langridge-Monopolis (1984)",
            "Fread (1988) orifice flow; broad-crested weir for the spillway",
        ],
    },
    {
        "id": "terrain",
        "title": "DEM and conditioning",
        "subtitle": "COP30, pit-filled, carved",
        "kind": "geodata",
        "x": 3, "y": 0,
        "module": "modules/01_geodata/terrain.py",
        "emitted_by": "api._prepare_real",
        "does": (
            "Fetches the elevation model for the domain and conditions it for "
            "hydraulics: fills the pits so water cannot pond in a data artefact, "
            "carves a monotonically descending channel to the outlet so the "
            "river actually drains, and estimates a bed under the water surface. "
            "The 30 m source is resampled to the solver's cell size. We have no "
            "measured bathymetry, and the conditioning string in meta.json says "
            "exactly what was done to the ground."
        ),
        "inputs": ["domain bbox", "cell size (m)"],
        "outputs": [
            "conditioned DEM on the solver grid",
            "per-cell Manning n from land cover",
        ],
        "sources": [
            "Copernicus GLO-30 (COP30); SRTM, NASADEM, ALOS and FABDEM also supported",
            "Lindsay (2016), least-cost channel carving",
            "ESA WorldCover 2021 to Manning n",
        ],
    },
    {
        "id": "exposure",
        "title": "Who is downstream",
        "subtitle": "OSM + WorldPop",
        "kind": "geodata",
        "x": 3, "y": 1,
        "module": "modules/01_geodata/exposure.py",
        "emitted_by": "api._prepare_real",
        "does": (
            "Downloads settlements, buildings and the road network inside the "
            "domain from OpenStreetMap, then assigns WorldPop 2020 constrained "
            "100 m population to the nearest listed settlement within 2 km so "
            "nobody is counted twice. A settlement with a real OSM census tag "
            "keeps it. Where no buildings are mapped the class default is kept "
            "and the record says so - it is never quietly filled in."
        ),
        "inputs": ["domain bbox"],
        "outputs": [
            "named settlements with population",
            "road geometry for evacuation routing",
        ],
        "sources": ["OpenStreetMap contributors", "WorldPop 2020, constrained, 100 m"],
        "optional": True,
        "optional_note": "A flood map without names is still a valid run.",
    },
    {
        "id": "sph",
        "title": "SPH near-field",
        "subtitle": "DualSPHysics v5.4, GPU",
        "kind": "engine",
        "x": 3, "y": 3,
        "module": "modules/02_sph/breach.py",
        "emitted_by": "engine probe",
        "engine": "sph",
        "does": (
            "Smoothed Particle Hydrodynamics through the breach opening itself - "
            "the first sixty seconds or so, where the flow is violent, three "
            "dimensional and not shallow. Roughly 99,000 particles on the GPU. "
            "It is a near-field cross-check on the breach outflow, not a routing "
            "engine: it cannot carry water 40 km downstream, and we do not ask "
            "it to. When a run is launched with sph_run set, this measured "
            "discharge IS the upstream boundary for its first minute - spliced "
            "onto the front of the level-pool curve, never blended with it, and "
            "the step between the two engines is published rather than smoothed "
            "away."
        ),
        "inputs": ["breach geometry", "reservoir head"],
        "outputs": [
            "near-field hydrograph (hydrograph.csv)",
            "agreement against the weir equation",
            "meta.json -> sph_coupling, when a run is coupled to it",
        ],
        "sources": ["DualSPHysics v5.4 (Dominguez et al. 2022)"],
        "optional": True,
        "optional_note": (
            "Run the particles separately on the GPU, then couple the result in: "
            "POST /api/runs with sph_run pointing at the finished SPH folder "
            "switches the engine to sphcoupled, and this node turns green with "
            "the handover it measured."
        ),
    },
    {
        "id": "solve",
        "title": "2D hydrodynamic solve",
        "subtitle": "shallow water, HLL",
        "kind": "solver",
        "x": 4, "y": 2,
        "module": "modules/04_backend/solver.py",
        "emitted_by": "solver.run_solver",
        "does": (
            "The actual flood. Two-dimensional shallow-water equations, HLL "
            "approximate Riemann solver, Audusse well-balanced reconstruction so "
            "a lake at rest stays at rest, wetting and drying, Manning friction "
            "from the land-cover grid. The breach hydrograph enters at the dam "
            "cells and the water routes over the conditioned terrain under a "
            "CFL-limited timestep. This node streams live: simulated time, wet "
            "cell count, maximum depth and the running volume ledger."
        ),
        "inputs": [
            "conditioned DEM",
            "Manning grid",
            "breach hydrograph",
            "inflow cells",
        ],
        "outputs": [
            "max depth",
            "arrival time",
            "time of peak",
            "max velocity",
            "duration",
            "mass balance",
        ],
        "sources": [
            "Harten, Lax and van Leer (1983), HLL Riemann solver",
            "Audusse et al. (2004), well-balanced hydrostatic reconstruction",
            "Verified: Ritter dam-break RMSE 0.218 m, lake-at-rest 2.9e-06 m",
        ],
        "live": True,
    },
    {
        "id": "surrogate",
        "title": "ML emulator",
        "subtitle": "U-Net what-if",
        "kind": "ml",
        "x": 4, "y": 4,
        "module": "modules/07_ml/surrogate.py",
        "emitted_by": "engine probe",
        "engine": "surrogate",
        "does": (
            "A U-Net trained on our own solver that answers a what-if in about "
            "twenty milliseconds instead of forty seconds, so an operator can "
            "drag a reservoir level and watch the extent move. It is a "
            "PREDICTION, not a simulation: extent CSI against our solver is "
            "0.909 on held-out scenarios and it has never been validated against "
            "a real flood. The API returns is_emulated true and a warning string "
            "with every answer, and anything quoted or exported is recomputed "
            "with the real solver. The problem statement asks for no machine "
            "learning at all - this is a bonus and is presented as one."
        ),
        "inputs": ["reservoir level, capacity, dam height, formation time"],
        "outputs": ["emulated depth field", "wet cells, max depth, inference time"],
        "sources": [
            "Trained on this repository own solver output; Dice+BCE loss with a "
            "separate wet/dry head",
        ],
        "optional": True,
        "optional_note": (
            "An emulator beside the solver, never in place of it. It does not "
            "run as part of a PLAY."
        ),
    },
    {
        "id": "grids",
        "title": "Write the contract",
        "subtitle": "GeoTIFFs, extent, texture",
        "kind": "output",
        "x": 5, "y": 2,
        "module": "shared/io.py",
        "emitted_by": "runner.run_scenario",
        "does": (
            "Writes the run folder every other module reads: five float32 "
            "GeoTIFFs on one shared grid, the flood outline as GeoJSON, the "
            "hydrograph as CSV, and packed.png - arrival time, time of peak, "
            "depth and duration packed into one RGBA image so the browser can "
            "replay the whole flood without downloading a raster per frame."
        ),
        "inputs": ["solver output grids"],
        "outputs": [
            "max_depth, arrival_time, time_of_peak, max_velocity, duration (GeoTIFF)",
            "extent.geojson",
            "packed.png",
        ],
        "sources": [
            "shared/contract.py - EPSG:4326, float32, NaN nodata, wet threshold 0.05 m",
        ],
    },
    {
        "id": "gee",
        "title": "Satellite check",
        "subtitle": "Sentinel-1 SAR",
        "kind": "validation",
        "x": 6, "y": 0,
        "module": "modules/06_gee_validation/sar.py",
        "emitted_by": "engine probe",
        "engine": "gee",
        "does": (
            "Google Earth Engine pulls Sentinel-1 GRD VV backscatter over the "
            "reach and change-detects water against the pre-event median, then "
            "scores our simulated extent against it - CSI, POD, FAR. This is the "
            "only comparison in the project against something nobody modelled. "
            "The honest answer so far is that it is weak on both reaches we "
            "tried, and the caveats travel with the number."
        ),
        "inputs": ["simulated extent", "event date"],
        "outputs": ["observed water mask", "CSI / POD / FAR, with caveats"],
        "sources": ["Sentinel-1 GRD via Google Earth Engine"],
        "optional": True,
        "optional_note": (
            "Needs a real observed event and Earth Engine credentials; it is run "
            "against a stored mask, not inside the live request."
        ),
    },
    {
        "id": "impact",
        "title": "Loss and damage",
        "subtitle": "who, how deep, how much",
        "kind": "ml",
        "x": 6, "y": 1,
        "module": "modules/07_ml/damage.py",
        "emitted_by": "runner.run_scenario",
        "does": (
            "Samples depth and velocity at every settlement, classifies the "
            "hazard from the depth-velocity product, and applies JRC depth-damage "
            "curves with a velocity aggravation factor to get buildings, roads "
            "and cropland damage in rupees. Population affected, houses affected "
            "and roads cut come out of the same pass. The replacement values are "
            "stated assumptions and the output says so in damage_curve_source."
        ),
        "inputs": ["max depth and max velocity grids", "settlements, roads"],
        "outputs": [
            "impact.json - settlements, population, roads cut, damage in Rs crore",
        ],
        "sources": [
            "Huizinga et al. (2017), JRC EUR 28552 EN, Asia depth-damage functions",
            "Clausen and Clark (1990), velocity aggravation",
        ],
    },
    {
        "id": "evacuation",
        "title": "Evacuation routing",
        "subtitle": "time-dependent Dijkstra",
        "kind": "ml",
        "x": 6, "y": 2,
        "module": "modules/07_ml/evacuation.py",
        "emitted_by": "runner.run_scenario",
        "does": (
            "Builds a graph from the OSM road network, marks each edge as cut at "
            "the hour the water arrives on it, and runs an exact time-dependent "
            "Dijkstra from every settlement to the nearest point that never "
            "floods. It returns the route, the walking time and the margin "
            "against the arrival time - a negative margin means the road is under "
            "water before anyone reaches the end of it. Deliberately not a neural "
            "network: labelled evacuation outcomes for Indian dam breaks do not "
            "exist, and a shortest safe path can be computed exactly rather than "
            "learned."
        ),
        "inputs": ["arrival time grid", "road graph", "settlements"],
        "outputs": ["evacuation.json - route, walk time and margin per settlement"],
        "sources": ["Dijkstra (1959), on a time-dependent edge cost"],
        "optional": True,
        "optional_note": "Needs OSM road geometry, so it needs real terrain and exposure.",
    },
    {
        "id": "uncertainty",
        "title": "What we do not know",
        "subtitle": "regression spread",
        "kind": "ml",
        "x": 6, "y": 3,
        "module": "modules/07_ml/montecarlo.py",
        "emitted_by": "runner.run_scenario",
        "does": (
            "Publishes the disagreement instead of hiding it. All three breach "
            "regressions are carried through to a peak discharge and the spread "
            "between them is reported as a ratio; where the Monte Carlo has been "
            "run it adds a p5-p95 band by sampling which regression is right "
            "rather than averaging them into a number no author would defend."
        ),
        "inputs": ["breach ensemble", "scenario"],
        "outputs": [
            "uncertainty.json - per-regression peaks, spread ratio, band where computed",
        ],
        "sources": [
            "The three breach regressions above; Gaussian-process surrogate for the band",
        ],
    },
    {
        "id": "sfincs",
        "title": "Second engine",
        "subtitle": "SFINCS cross-check",
        "kind": "engine",
        "x": 6, "y": 4,
        "module": "modules/09_sfincs/engine.py",
        "emitted_by": "engine probe",
        "engine": "sfincs",
        "does": (
            "Routes the same flood through an independent Deltares solver and "
            "compares the extent cell by cell - CSI 0.9653 against our solver on "
            "the Chungthang reach at the 60 m default, and 0.9607 at the old "
            "90 m grid. The two engines agree slightly BETTER on the finer grid "
            "and their maximum depths converge from 2.6 m apart to 1.2 m, which "
            "is what should happen if both are approaching the same solution. "
            "SFINCS is reduced-physics and it is NOT Delft3D; it is never "
            "presented as Delft3D. It is a cross-check between two "
            "implementations, not a validation against reality."
        ),
        "inputs": ["the same DEM and the same breach hydrograph"],
        "outputs": ["an independent extent; CSI against our solver"],
        "sources": ["SFINCS v2.4.0 (Deltares); integration/compare_routing.py"],
        "optional": True,
        "optional_note": (
            "Run offline through integration/compare_routing.py, not inside the "
            "live request."
        ),
    },
    {
        "id": "delft3d",
        "title": "Delft3D",
        "subtitle": "named in the problem statement",
        "kind": "engine",
        "x": 6, "y": 5,
        "module": "modules/03_delft3d/engine.py",
        "emitted_by": "engine probe",
        "engine": "delft3d",
        "does": (
            "The problem statement names Delft3D explicitly, and Delft3D-FLOW "
            "now solves our scenarios: modules/03_delft3d/case.py writes the "
            "case from the same conditioned DEM, grid, breach hydrograph, "
            "Manning value and wet threshold our own solver used, and "
            "integration/compare_delft3d.py runs the kernel and compares the "
            "two extents cell by cell. Godavari at Gangapur, 223 x 161 at 90 m: "
            "extent CSI 0.7379. Annamayya, 93 x 125: 0.7768. It runs OFFLINE, "
            "after a run exists, not inside a live request - which is why this "
            "node is a probe during a PLAY and reports what it found on disk. "
            "It never estimates a Delft3D result and never borrows another "
            "engine's numbers; SFINCS above is a different Deltares model and "
            "is labelled as one. The kernel was a BUILD, not a licence: "
            "Delft3D 4 is GPLv3 with public source but ships as source only, so "
            "d_hydro and flow2d3d were compiled here. Delft3D FM, the newer "
            "unstructured suite, is the one needing the licence we were not "
            "granted."
        ),
        "inputs": ["a finished run folder: conditioned DEM, hydrograph.csv, max_depth.tif"],
        "outputs": [
            "which kernel is installed and where",
            "Delft3D max depth and wet extent on our grid, and the CSI between the two engines",
        ],
        "sources": [
            "Delft3D 4 (GPLv3) - github.com/Deltares/Delft3D, build config d3d4-suite",
            "Delft3D FM / DIMR (licensed) - download.deltares.nl",
        ],
        "optional": True,
    },
    {
        "id": "compare",
        "title": "Compare the engines",
        "subtitle": "a deliverable in itself",
        "kind": "validation",
        "x": 7, "y": 4,
        "module": "integration/compare_engines.py + integration/compare_routing.py",
        "emitted_by": "engine probe",
        "engine": "compare",
        "does": (
            "The problem statement does not only ask for the engines - it asks "
            "for the scenarios to be COMPARED, which makes the comparison itself "
            "an asked-for output. This puts our solver, the weir equation, "
            "DualSPHysics and four empirical regressions in one table, and "
            "compares extents cell by cell against SFINCS (CSI 0.9653 on the "
            "Chungthang reach at the 60 m default). The Delft3D row is empty, "
            "and it stays empty rather than being filled in from another engine."
        ),
        "inputs": ["run folders from each engine, on the same domain"],
        "outputs": [
            "peak, depth, area, runtime and mass balance side by side",
            "CSI between two independent extents",
        ],
        "sources": ["integration/compare_engines.py; integration/compare_routing.py"],
        "optional": True,
        "optional_note": (
            "Run offline across finished runs, not inside a single request - the "
            "runs being compared have to exist first."
        ),
    },
    {
        "id": "validate",
        "title": "Contract validator",
        "subtitle": "a run that fails does not exist",
        "kind": "gate",
        "x": 7, "y": 2,
        "module": "shared/validate.py",
        "emitted_by": "api._execute",
        "does": (
            "The gate. Checks every required file is present, that all five "
            "rasters share one shape, transform and CRS, that units and nodata "
            "are what the contract says, that the hydrograph integrates to the "
            "released volume, and that meta.json carries its provenance. If it "
            "fails the run is marked failed and nothing downstream is shown - a "
            "run that does not validate is not handed to anybody."
        ),
        "inputs": ["the whole run folder"],
        "outputs": ["ok / errors / warnings / facts"],
        "sources": ["shared/contract.py, schema 2.0"],
    },
    {
        "id": "result",
        "title": "Flood, damage, evacuation",
        "subtitle": "the answer",
        "kind": "result",
        "x": 8, "y": 2,
        "module": "modules/05_frontend",
        "emitted_by": "api._execute",
        "does": (
            "What the operator asked for: the inundated area with depth and "
            "arrival time, the named settlements and how long each one has, the "
            "damage in rupees, the evacuation routes with their margins, and the "
            "export as .shp or .kml. Every number on this node was computed by "
            "the stages to its left, in this same run."
        ),
        "inputs": ["a validated run folder"],
        "outputs": [
            "flood extent and depth",
            "impact table",
            "evacuation plan",
            ".shp / .kml export",
        ],
        "sources": [],
    },
]

EDGES: list[dict[str, str]] = [
    {"from": "input", "to": "catalogue"},
    {"from": "catalogue", "to": "river"},
    {"from": "catalogue", "to": "breach"},
    {"from": "catalogue", "to": "inflow", "style": "dashed"},
    {"from": "inflow", "to": "breach", "label": "inflow", "style": "dashed"},
    {"from": "river", "to": "terrain"},
    {"from": "river", "to": "exposure"},
    {"from": "breach", "to": "sph", "label": "near field", "style": "dashed"},
    {"from": "terrain", "to": "solve", "label": "ground"},
    {"from": "breach", "to": "solve", "label": "inflow"},
    {"from": "solve", "to": "grids"},
    {"from": "grids", "to": "gee", "style": "dashed"},
    {"from": "grids", "to": "impact"},
    {"from": "grids", "to": "evacuation"},
    {"from": "exposure", "to": "impact", "label": "who"},
    {"from": "exposure", "to": "evacuation", "label": "roads"},
    {"from": "breach", "to": "uncertainty", "label": "ensemble"},
    {"from": "breach", "to": "surrogate", "label": "what-if", "style": "dashed"},
    {"from": "grids", "to": "sfincs", "style": "dashed"},
    {"from": "grids", "to": "delft3d", "style": "dashed"},
    {"from": "grids", "to": "compare", "style": "dashed"},
    {"from": "sfincs", "to": "compare", "style": "dashed"},
    {"from": "delft3d", "to": "compare", "style": "dashed"},
    {"from": "compare", "to": "result", "style": "dashed"},
    {"from": "impact", "to": "validate"},
    {"from": "evacuation", "to": "validate"},
    {"from": "uncertainty", "to": "validate"},
    {"from": "validate", "to": "result"},
]


# --------------------------------------------------------------------------
# What each stage actually depends on
# --------------------------------------------------------------------------
#
# The hidden half of the system. A juror looking at the graph sees seventeen
# boxes; what they cannot see is that "trace the river" means numba-compiled D8
# over a Copernicus tile pulled from OpenTopography with a key in .env, or that
# "loss and damage" is XGBoost over JRC curves. This block is that half, written
# down, so the picture is not quietly hiding its own supply chain.
#
# Every entry below was read off the imports and the endpoints in the code it
# names. Nothing here is aspirational - if a library is listed, that module
# imports it; if a service is listed, that module calls it.
#
#   code      python packages the stage imports (pinned in requirements.txt)
#   data      datasets and files it reads, with where they sit on disk
#   services  network services and the credential each one needs
#   engines   external binaries that are not python at all

DEPENDENCIES: dict[str, dict[str, list[str]]] = {
    "input": {
        "code": ["fastapi", "pydantic", "uvicorn", "starlette (WebSocket)"],
        "data": [],
        "services": [],
    },
    "inflow": {
        "code": ["earthengine-api", "numpy"],
        "data": ["UCSB-CHG/CHIRPS/DAILY (rainfall, 5 km, ~3 day lag)"],
        "services": [
            "Google Earth Engine - EE_SERVICE_ACCOUNT_EMAIL, EE_SERVICE_ACCOUNT_KEY, EE_PROJECT_ID",
        ],
    },
    "catalogue": {
        "code": ["pdfplumber + pdfminer.six (to build the register once)", "json"],
        "data": [
            "data/dams/dams.geojson - CWC NRLD 2019, 5,686 dams, 29 states",
            "GRanD v1.3 reservoir outlines",
        ],
        "services": ["none at run time - the register is on disk and works offline"],
    },
    "river": {
        "code": ["numpy", "numba (D8 and priority-flood are JIT compiled)", "rasterio", "requests"],
        "data": ["scouting DEM cached at data/dem/{site}_scout/"],
        "services": [
            "OpenTopography global DEM API - OPENTOPOGRAPHY_API_KEY",
            "cache hit means no network at all",
        ],
    },
    "terrain": {
        "code": [
            "rasterio + rasterio.warp (reproject, Resampling)",
            "numba (pit fill, carve, flow direction)",
            "scipy.ndimage",
            "pyproj",
            "numpy, requests",
        ],
        "data": [
            "Copernicus GLO-30 tiles at data/dem/{site}/COP30_*.tif",
            "conditioned grid cached as cond_*.npz beside it",
            "ESA/WorldCover/v200 land cover to Manning n",
        ],
        "services": [
            "OpenTopography - OPENTOPOGRAPHY_API_KEY",
            "Google Earth Engine for WorldCover - EE_PROJECT_ID",
        ],
    },
    "exposure": {
        "code": ["requests", "rasterio (+ warp transform)", "numpy"],
        "data": [
            "OpenStreetMap settlements, buildings and roads via Overpass",
            "data/worldpop/ind_ppp_2020_constrained.tif - WorldPop 2020, 100 m",
            "cached per site at data/exposure/{site}/",
        ],
        "services": [
            "Overpass API - overpass-api.de, falling back to overpass.kumi.systems",
            "no key required, and it is rate limited",
        ],
    },
    "breach": {
        "code": ["numpy only - shared/hydro.py is closed-form physics"],
        "data": ["nothing on disk; the dam height and storage come from the register"],
        "services": [],
    },
    "sph": {
        "code": ["numpy", "the DualSPHysics case is written as XML"],
        "data": ["modules/02_sph/cases/ - case definition, particle output as .bi4"],
        "services": [],
        "engines": ["DualSPHysics v5.4 binaries", "an NVIDIA GPU with CUDA"],
    },
    "solve": {
        "code": [
            "numba njit + prange - the solver kernels are compiled, not interpreted",
            "llvmlite (numba backend)",
            "numpy",
        ],
        "data": ["the conditioned DEM and Manning grid, in memory from the stages above"],
        "services": [],
    },
    "surrogate": {
        "code": ["torch 2.6 (+cu124)", "numpy"],
        "data": ["trained U-Net checkpoint under modules/07_ml/models/"],
        "services": [],
        "engines": ["CUDA if present; it falls back to CPU and says so"],
    },
    "grids": {
        "code": ["rasterio (GeoTIFF + features)", "shapely (polygonise)", "pillow (packed.png)", "numpy"],
        "data": ["writes outputs/{run_id}/ - the data contract every other module reads"],
        "services": [],
    },
    "gee": {
        "code": ["earthengine-api", "numpy", "requests"],
        "data": ["COPERNICUS/S1_GRD - Sentinel-1 GRD, VV, descending"],
        "services": [
            "Google Earth Engine - EE_SERVICE_ACCOUNT_EMAIL, EE_SERVICE_ACCOUNT_KEY, EE_PROJECT_ID",
        ],
    },
    "impact": {
        "code": ["xgboost", "scikit-learn", "numpy"],
        "data": [
            "JRC Huizinga (2017) Asia depth-damage curves, in code with the citation",
            "settlements and roads from the exposure stage",
        ],
        "services": [],
    },
    "evacuation": {
        "code": ["networkx (the road graph)", "numpy"],
        "data": ["OSM road geometry from the exposure stage", "the arrival-time grid"],
        "services": [],
    },
    "uncertainty": {
        "code": ["scikit-learn GaussianProcessRegressor (RBF + White kernels)", "numpy"],
        "data": ["the breach ensemble carried from the breach stage"],
        "services": [],
    },
    "sfincs": {
        "code": ["numpy", "the SFINCS case is written as plain text input files"],
        "data": ["the same DEM and hydrograph our own solver used"],
        "services": [],
        "engines": ["SFINCS v2.4.0 Galibier executable"],
    },
    "delft3d": {
        "code": ["nothing - there is no python side to a solver we have not built"],
        "data": [],
        "services": [],
        "engines": [
            "Delft3D 4 kernel (d_hydro + flow2d3d) - NOT BUILT. GPLv3, source "
            "public, ships as source only and has to be compiled",
            "Delft3D FM kernel or DIMR - NOT INSTALLED, and its suite needs a "
            "Deltares licence we were not granted",
        ],
    },
    "compare": {
        "code": ["numpy"],
        "data": ["finished run folders under outputs/, one per engine, on one domain"],
        "services": [],
    },
    "validate": {
        "code": ["rasterio (shape, transform, CRS, nodata)", "numpy"],
        "data": ["the whole run folder, read back off disk"],
        "services": [],
    },
    "result": {
        "code": ["geopandas + fiona + pyogrio (the .shp and .kml writers)", "shapely"],
        "data": ["outputs/{run_id}/ - extent, impact, evacuation, uncertainty"],
        "services": [
            "OpenStreetMap raster tiles for the map basemap - optional, the flood "
            "still draws without them",
        ],
    },
}

# Nodes that are never driven by a live web request. They carry an engine probe
# instead, so the graph shows the truth about each engine rather than a box that
# sits on WAITING forever.
PROBE_NODES = {n["id"] for n in NODES if n.get("engine")}

# Nodes the live pipeline does drive, in the order they are reached.
LIVE_NODES = [n["id"] for n in NODES if not n.get("engine")]


def _unavailable(what: str, exc: Exception) -> str:
    """Why a probe could not answer, in words a juror can read.

    A stripped host - the read-only Vercel build installs fastapi and nothing
    else - cannot import numpy or torch, and that is a fact about the HOST, not
    a fault in the module. Printing the bare exception put
    "probe failed: ModuleNotFoundError: No module named 'numpy'" on the graph,
    which reads as broken code on the one page we put in front of people. A
    missing dependency is named as a missing dependency; anything else still
    reports its exception, because an unexpected failure must not be dressed up
    as a routine absence.
    """
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        return (
            f"{what} is not available on this host: it needs {exc.name}, which "
            "this build does not install. The full backend has it."
        )
    return f"{what} unavailable: {type(exc).__name__}: {exc}"


def _probe_engines() -> dict[str, dict]:
    """Probe each external engine. Never raises - a probe that fails is a
    reported unknown, not a 500."""
    from importlib import import_module

    out: dict[str, dict] = {}

    def status_of(key: str, dotted: str, what: str) -> None:
        try:
            st = import_module(dotted).status()
            out[key] = {
                "installed": bool(st.get("installed")),
                "summary": st.get("summary") or "",
                "detail": st,
            }
        except Exception as exc:  # noqa: BLE001
            out[key] = {
                "installed": False,
                "summary": _unavailable(what, exc),
                "detail": {},
            }

    status_of("delft3d", "modules.03_delft3d.engine", "The Delft3D engine check")
    status_of("sfincs", "modules.09_sfincs.engine", "The SFINCS engine check")

    # SPH and GEE expose no status() of their own. Report whether the code and
    # its dependencies import, and say plainly that importability is all that
    # was checked - it is not a claim that a GPU run succeeded.
    for key, dotted, what in (
        ("sph", "modules.02_sph.breach", "DualSPHysics case builder"),
        ("gee", "modules.06_gee_validation.sar", "Earth Engine SAR classifier"),
        ("inflow", "modules.07_ml.inflow", "CHIRPS rainfall-runoff nowcast"),
        ("surrogate", "modules.07_ml.surrogate", "U-Net emulator"),
    ):
        try:
            import_module(dotted)
            out[key] = {
                "installed": True,
                "summary": f"{what} imports; not executed in this request",
                "detail": {},
            }
        except Exception as exc:  # noqa: BLE001
            out[key] = {
                "installed": False,
                "summary": _unavailable(what, exc),
                "detail": {},
            }

    # The engine comparison is a script run across finished run folders, so what
    # there is to probe is whether the script is there.
    from pathlib import Path as _Path

    scripts = [
        _Path("integration/compare_engines.py"),
        _Path("integration/compare_routing.py"),
    ]
    found = [str(x) for x in scripts if x.exists()]
    out["compare"] = {
        "installed": bool(found),
        "summary": (
            "comparison scripts present: " + ", ".join(found)
            if found
            else "comparison scripts not found under integration/"
        ),
        "detail": {"scripts": found},
    }
    return out


_ENGINE_CACHE: dict[str, dict] | None = None


def engine_status(refresh: bool = False) -> dict[str, dict]:
    """Cached engine probe. The filesystem scan is slow and the answer does not
    change while the server is up."""
    global _ENGINE_CACHE
    if _ENGINE_CACHE is None or refresh:
        _ENGINE_CACHE = _probe_engines()
    return _ENGINE_CACHE


def initial_nodes() -> dict[str, dict]:
    """The node state map a fresh run starts from.

    Probe nodes never enter WAITING: they are not going to run in this request,
    and a box that sits on WAITING for a whole demo reads as broken. They start
    on their true state - absent, or skipped with the reason attached.
    """
    states: dict[str, dict] = {}
    engines = engine_status()
    for node in NODES:
        eng = node.get("engine")
        if eng:
            st = engines.get(eng, {})
            states[node["id"]] = {
                "status": SKIPPED if st.get("installed") else ABSENT,
                "detail": st.get("summary") or "",
                "note": node.get("optional_note", ""),
            }
        else:
            states[node["id"]] = {"status": WAITING, "detail": "", "note": ""}
    return states


def manifest() -> dict:
    """Everything the workflow page needs to draw itself."""
    nodes = [dict(n, deps=DEPENDENCIES.get(n["id"], {})) for n in NODES]
    return {
        "nodes": nodes,
        "edges": EDGES,
        "states": list(NODE_STATES),
        "live_nodes": LIVE_NODES,
        "engines": engine_status(),
        "note": (
            "Every node is a real stage in this repository. A node marked absent "
            "is reported by a filesystem probe and is never filled in from "
            "another engine's numbers."
        ),
    }
