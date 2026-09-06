"""
integration/build_demo_runs.py - the ten runs the console loads instantly.

    python -m integration.build_demo_runs                 # build any that are missing
    python -m integration.build_demo_runs --force         # rebuild all ten
    python -m integration.build_demo_runs --list          # what is on disk now
    python -m integration.build_demo_runs --category river

WHY THIS EXISTS. A solve takes minutes on real terrain. In front of a panel
that is the whole demonstration gone, and the honest fix is not to fake the
solve - it is to have already done it. These are REAL runs through
runner.run_scenario, the same path the API uses, on real COP30 terrain, and
they land in outputs/ as ordinary contract-valid run folders. The console's
"Load a stored run" button cycles them one click at a time.

TWO SETS, BECAUSE THEY ARE TWO QUESTIONS. The console has a dam tab and a
river tab, and a dam and a river are not the same problem: a river has no
crest, no embankment, no foundation and no gates. So the manifest carries a
`category` and each tab cycles only its own five. Picking the river tab and
being handed a dam is the bug this split exists to remove.

THE FIVE DAMS. Not the five prettiest floods. Each is the case that shows
something the other four cannot:

  1. Machchhu II, overtopping. A real Indian dam-break with a documented
     outcome, so the result can be argued with rather than admired.
  2. Lower Manair, spillway blockage. The only case that produces a time to
     overtop - the hours between the outlets failing and the first water over
     the crest. That number is the reason the mode exists.
  3. South Lhonak, moraine outburst. A natural dam with no published storage,
     so the volume is read off the DEM; and the Sikkim 2023 failure is the
     event the problem statement's background is about.
  4. Idukki, foundation failure. A 169 m arch dam, which is the one class of
     structure the breach regressions in shared/hydro.py do not describe.
  5. Annamayya (Cheyyeru), overtopping. The only run in the whole repository
     that has been laid against an observed satellite extent. The comparison
     did not flatter us - bias 7.3 - and it is in the set for that reason.

THE FIVE RIVERS ARE THE FIVE THE PROBLEM STATEMENT NAMES. NTRO's Background
paragraph names Rishi Ganga (Feb 2021), Wapriyang (Nov 2021), Phuktal near
Sumdo (Mar 2015), Kosi (2008) and the Kashmir valley (2014). Three are
natural-dam blockages and come from modules/01_geodata/events.py. Two are not
blockages at all - Kosi 2008 was an embankment breach on an alluvial fan and
Kashmir 2014 was a valley-filling river flood - so those two run in
`river_flood` mode from a published peak discharge, and their coordinates and
discharges are APPROXIMATE and say so in meta.json. Nothing here is a hindcast:
no trigger is modelled and no observed extent is compared. See the header of
events.py, which says the same thing at more length.

STAGE TIMINGS. Every build records how long each pipeline stage actually took,
straight off the progress callback, into `stage_timings` in the manifest. The
workflow page replays a stored run against those measured durations rather than
inventing a pace. They are wall-clock seconds on the machine that built the run
and nothing more is claimed for them.

Everything is committed except the run folders themselves, which are large and
regenerable - run this once on a machine with network access to OpenTopography
and the console finds them.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUTS = REPO_ROOT / "outputs"
MANIFEST = REPO_ROOT / "data" / "demo_runs.json"


# --------------------------------------------------------------------------
# The five dams
# --------------------------------------------------------------------------
#
# `dam_id` is a catalogue id, so the height, storage, crest length and design
# spillway all come from the register rather than from this file. The only
# numbers here are the ones that describe the SCENARIO - what failed, how full
# it was, how far and how long to look.

DAM_DEMOS = [
    {
        "key": "machhu_overtopping",
        "category": "dam",
        "label": "Machchhu II, 1979 - overtopping",
        "why": (
            "Morbi, Gujarat, 11 August 1979. 600 mm in 24 hours produced an "
            "inflow near three times the spillway's design capacity, the water "
            "passed the earthen flanks and 2,100 m of embankment went. This is "
            "the one case in the set with a documented outcome to argue with."
        ),
        "dam_id": "GJ04MH0498",
        "failure_mode": "overtopping",
        "spec": {
            "reservoir_level_frac": 1.0,
            "reach_length_km": 60.0,
            "end_hr": 12.0,
            "inflow_cumecs": 16300.0,
        },
    },
    {
        "key": "manair_spillway_blockage",
        "category": "dam",
        "label": "Lower Manair - spillway blocked",
        "why": (
            "The outlets are gone and the inflow keeps arriving. The number "
            "this case produces and no other case can is the time to overtop: "
            "the hours between the blockage and the first water over the "
            "crest, which is the warning the operator actually has."
        ),
        "dam_id": "TL47HH0065",
        "failure_mode": "spillway_blockage",
        "spec": {
            "reach_length_km": 40.0,
            "end_hr": 12.0,
            "inflow_cumecs": 4000.0,
            "residual_spillway_frac": 0.15,
            "blockage_start_level_frac": 0.85,
        },
    },
    {
        "key": "lhonak_glof",
        "category": "dam",
        "label": "South Lhonak, 2023 - moraine outburst",
        "why": (
            "North Sikkim, 3-4 October 2023. The lake grew from 1.12 to "
            "1.63 km2 between 2016 and 2023 and then drained into the Teesta. "
            "A natural dam: no published storage exists, so the impounded "
            "volume comes from the 2023 lake area through Huggel et al. "
            "(2002) - because a 30 m DEM cannot resolve this basin, finding "
            "0.34 MCM behind the moraine against the ~25.7 MCM that actually "
            "drained. Both numbers are in meta.json. The breach stops at the "
            "bedrock sill rather than cutting through the whole ridge."
        ),
        "dam_id": "HISTSOUTHLHONAK2023",
        "failure_mode": "glof_moraine",
        "spec": {
            "reach_length_km": 60.0,
            "end_hr": 12.0,
            "moraine_height_m": 40.0,
            "glof_breach_width_m": 25.0,
            # The 2023 lake area. Supplying it is a deliberate statement that
            # the DEM has not seen this lake: at the published coordinate a
            # 30 m DEM holds 0.34 MCM behind the moraine, against roughly
            # 25.7 MCM that the outburst actually released. Both figures are
            # published in meta.json under glof_moraine so the disagreement is
            # visible. Neither is a measurement of this lake.
            "lake_area_km2": 1.63,
        },
    },
    {
        "key": "idukki_foundation",
        "category": "dam",
        "label": "Idukki - foundation failure of an arch dam",
        "why": (
            "A 169 m double-curvature arch dam in Kerala. This is the one "
            "class of structure the breach regressions in shared/hydro.py do "
            "not describe - Froehlich, Von Thun and MacDonald are all fitted "
            "to earthfill embankments, and an arch dam that loses its "
            "foundation is displaced whole rather than eroded. The opening "
            "here is geometry under critical-flow control, and meta.json says "
            "no regression was applied. READ THE WARNING ON THIS ONE: the run "
            "validates, but its peak velocity of 30.2 m/s is above the ~20 m/s "
            "the validator considers plausible for a dam-break front. A 169 m "
            "head into a Kerala gorge is where you would expect to find such a "
            "number, and it is also where a wet/dry front treatment is most "
            "likely to be flattering itself. Quote the depths and the extent; "
            "treat the velocity as an upper bound."
        ),
        "dam_id": "KL29VH0027",
        "failure_mode": "foundation_failure",
        "spec": {
            "reservoir_level_frac": 1.0,
            "reach_length_km": 50.0,
            "end_hr": 12.0,
            "foundation_breach_frac": 0.8,
            "collapse_time_min": 2.0,
        },
    },
    {
        "key": "annamayya_overtopping",
        "category": "dam",
        "label": "Annamayya (Cheyyeru), 2021 - overtopping",
        "why": (
            "Rajampet, Andhra Pradesh, 19 November 2021. An earthfill dam on "
            "the Cheyyeru that overtopped and failed on a floodplain, and the "
            "only run in this repository that has been laid against an "
            "observed satellite extent. It is in the set because the "
            "comparison did not flatter us: against a Sentinel-1 pass we "
            "simulate 12,416 wet cells to 1,698 observed, a bias of 7.3, "
            "because a full-reservoir worst case at maximum extent over 24 "
            "hours is not the same object as one satellite pass days later. "
            "POD rose seventeenfold from the Teesta gorge, which is what told "
            "us the gorge was a resolution problem. Read validation.json "
            "before quoting anything from it."
        ),
        "dam_id": "AP01MH0129",
        "failure_mode": "overtopping",
        "spec": {
            "reservoir_level_frac": 1.0,
            "reach_length_km": 50.0,
            "end_hr": 24.0,
            "cellsize_m": 90.0,
        },
    },
]


# --------------------------------------------------------------------------
# The five rivers - the five NTRO names in the Background paragraph
# --------------------------------------------------------------------------
#
# Three carry an `event_id` and come out of modules/01_geodata/events.py, so
# their coordinate, debris height and reported impoundment are that file's
# records and not this one's. Two carry a `site` block because they are not
# natural dams at all and events.py is deliberately only natural dams.

RIVER_DEMOS = [
    {
        "key": "rishiganga_2021",
        "category": "river",
        "label": "Rishi Ganga, Feb 2021 - landslide dam",
        "why": (
            "Chamoli, Uttarakhand, 7 February 2021 - the first event NTRO "
            "names. A rock-ice avalanche off Ronti peak ran into the Rishiganga "
            "and left a barrier that impounded the river. THE TRIGGER IS NOT "
            "MODELLED: this run starts at a 30 m barrier that already exists "
            "and fails, which is the part the hydrodynamics can speak to. The "
            "impounded volume is read off the terrain, not taken from the "
            "reported 0.8 MCM, and meta.json carries both so the gap is "
            "visible. The published coordinate for this event is not on a "
            "river in COP30 - it has a flow accumulation of one cell - so the "
            "barrier is placed on the trunk channel the DEM finds within 3 km "
            "and meta.json states the distance and the drop. Say that out loud "
            "before anyone measures it."
        ),
        "event_id": "rishiganga2021",
        "snap_to_trunk_km": 3.0,
        "failure_mode": "blockage_breach",
        "spec": {"reach_length_km": 25.0, "end_hr": 6.0, "cellsize_m": 60.0},
    },
    {
        "key": "wapriyang_2021",
        "category": "river",
        "label": "Wapriyang, Nov 2021 - debris barrier",
        "why": (
            "A Kameng tributary in Arunachal Pradesh, November 2021, the "
            "second event NTRO names. It is the hardest case in the set and it "
            "is here for that reason: a 25 m barrier on a steep tributary that "
            "a 30 m DEM can barely see, at a published coordinate that is not "
            "on a river at all - two cells of flow accumulation, which is "
            "hillslope. The barrier is placed on the trunk channel the DEM "
            "finds within 3 km and meta.json publishes the distance and the "
            "drop. If the terrain still holds almost nothing behind it, the "
            "run says so rather than manufacturing a lake."
        ),
        "event_id": "wapriyang2021",
        "snap_to_trunk_km": 3.0,
        "failure_mode": "blockage_breach",
        "spec": {"reach_length_km": 25.0, "end_hr": 6.0, "cellsize_m": 60.0},
    },
    {
        "key": "phuktal_2015",
        "category": "river",
        "label": "Phuktal near Sumdo, Mar 2015 - blockage breach",
        "why": (
            "The Tsarap Chu in Ladakh, blocked at the end of 2014 and "
            "overtopped in May 2015 after 110 days behind a reported 15 km "
            "lake - the third event NTRO names. It is also the clearest "
            "example in the repository of a volume that is NOT IDENTIFIABLE: "
            "rounding the conditioned DEM by a quarter of a millimetre flips "
            "428 of 123,256 D8 directions and moves this lake from 46.1 to "
            "2.5 MCM, a 94.5% swing. The run measures that swing itself and "
            "prints it in red. Read every discharge from this case as an order "
            "of magnitude."
        ),
        "event_id": "phuktal2015",
        "failure_mode": "blockage_breach",
        "spec": {"reach_length_km": 20.0, "end_hr": 4.0, "cellsize_m": 60.0},
    },
    {
        "key": "kosi_2008",
        "category": "river",
        "label": "Kosi, Aug 2008 - embankment breach and avulsion",
        "why": (
            "18 August 2008, the eastern afflux embankment at Kusaha in "
            "Sunsari district, Nepal, about 12 km upstream of the Kosi "
            "barrage - the fourth event NTRO names, and the one that is NOT a "
            "dam and NOT a natural barrier. The river was carrying roughly "
            "1.44 lakh cusecs, far under the barrage's design capacity, and "
            "still took most of that flow out through a breach in the "
            "embankment and abandoned its course east across the Bihar plain. "
            "So it runs in river_flood mode from a published peak rather than "
            "from a breach regression: there is no reservoir to empty, only a "
            "wave to route. THE COORDINATE AND THE DISCHARGE ARE APPROXIMATE, "
            "from published accounts, and meta.json says so. A 30 m DEM on an "
            "alluvial fan with no surveyed embankment is the weakest terrain "
            "in the set and the run is shown as a routing demonstration, not "
            "as a reconstruction of the 2008 avulsion."
        ),
        "site": {
            "name": "Kosi at Kusaha (2008)",
            "lat": 26.6200,
            "lon": 86.9300,
            "river": "Kosi",
            "state": "Sunsari, Nepal / Bihar",
            "source": (
                "approximate - published accounts of the 18 August 2008 "
                "Kusaha breach on the eastern afflux embankment, roughly 12 km "
                "upstream of the Kosi barrage. Not from a dataset held in this "
                "repository."
            ),
        },
        "failure_mode": "river_flood",
        "spec": {
            # ~1.44 lakh cusecs in the river on the day (~4,080 m3/s), of which
            # published accounts put roughly 85% through the breach. Both
            # figures are approximate and are recorded as such in the notes.
            "peak_discharge_cumecs": 3400.0,
            "time_to_peak_hr": 6.0,
            "base_flow_cumecs": 600.0,
            "reach_length_km": 40.0,
            "end_hr": 24.0,
            "cellsize_m": 90.0,
        },
    },
    {
        "key": "kashmir_jhelum_2014",
        "category": "river",
        "label": "Jhelum, Sep 2014 - Kashmir valley flood",
        "why": (
            "The Kashmir valley, first week of September 2014 - the fifth "
            "event NTRO names, and the other one that is neither a dam nor a "
            "barrier. Four days of rain put the Jhelum at Sangam far above the "
            "channel's carrying capacity and the flood filled the valley "
            "floor through Srinagar. It runs in river_flood mode: an NRCS "
            "unit-hydrograph wave entering the reach at a published peak, "
            "routed by the same shallow-water solver over the same conditioned "
            "COP30 terrain as every other case. THE COORDINATE AND THE PEAK "
            "ARE APPROXIMATE. This is the case that answers 'can the tool do a "
            "river with no dam on it at all', which is the literal wording of "
            "the problem statement title."
        ),
        "site": {
            "name": "Jhelum at Sangam (2014)",
            "lat": 33.7300,
            "lon": 75.0800,
            "river": "Jhelum",
            "state": "Jammu & Kashmir",
            "source": (
                "approximate - published accounts of the September 2014 "
                "Kashmir floods and the Sangam gauge. Not from a dataset held "
                "in this repository."
            ),
        },
        "failure_mode": "river_flood",
        "spec": {
            # ~1.2 lakh cusecs reported at Sangam at the peak (~3,400 m3/s),
            # against a channel capacity an order below it. Approximate.
            "peak_discharge_cumecs": 3400.0,
            "time_to_peak_hr": 12.0,
            "base_flow_cumecs": 300.0,
            "reach_length_km": 45.0,
            "end_hr": 36.0,
            "cellsize_m": 90.0,
        },
    },
]

DEMOS = DAM_DEMOS + RIVER_DEMOS


# --------------------------------------------------------------------------
# Stage timing
# --------------------------------------------------------------------------


class StageClock:
    """Wall-clock seconds per pipeline node, straight off the progress stream.

    runner.py already announces every stage it enters as {"node": ...}. This
    listens to that and closes the previous stage when the next one opens, so
    the durations are measured rather than apportioned. Stages the builder
    performs itself - domain planning, terrain, exposure, validation - are
    added by hand with mark(). Nothing here is estimated: a stage that was
    never entered simply has no entry.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.seconds: dict[str, float] = {}
        self._open: str | None = None
        self._t0 = time.perf_counter()

    def _close(self) -> None:
        if self._open is not None:
            now = time.perf_counter()
            self.seconds[self._open] = round(
                self.seconds.get(self._open, 0.0) + (now - self._t0), 3
            )
            self._t0 = now

    def open(self, node_id: str) -> None:
        self._close()
        if node_id not in self.order:
            self.order.append(node_id)
        self._open = node_id
        self._t0 = time.perf_counter()

    def mark(self, node_id: str, seconds: float) -> None:
        if node_id not in self.order:
            self.order.append(node_id)
        self.seconds[node_id] = round(self.seconds.get(node_id, 0.0) + seconds, 3)

    def close(self) -> None:
        self._close()
        self._open = None

    def progress(self, update: dict) -> None:
        node_id = update.get("node")
        if node_id:
            self.open(node_id)

    def as_rows(self) -> list[dict]:
        return [
            {"node": n, "seconds": self.seconds.get(n, 0.0)}
            for n in self.order
            if self.seconds.get(n, 0.0) > 0
        ]


# --------------------------------------------------------------------------


def _resolve_dam(demo: dict) -> dict:
    from importlib import import_module

    cat = import_module("modules.01_geodata.dams")
    if demo.get("dam_id"):
        dam = cat.get(demo["dam_id"])
        if dam is None:
            raise SystemExit(
                f"{demo['key']}: no dam with id {demo['dam_id']!r} in the catalogue"
            )
        return dam
    hits = cat.search(q=demo["dam_search"], limit=5)
    if not hits:
        raise SystemExit(f"{demo['key']}: nothing matched {demo['dam_search']!r}")
    return hits[0]


def _spec_for(demo: dict):
    """A ScenarioSpec built the same way the API builds one.

    Three shapes reach this: a catalogue dam, a historic natural-dam event out
    of modules/01_geodata/events.py, and a river with a coordinate written into
    this file. They differ only in where the SiteSpec comes from; the scenario
    is assembled identically afterwards, because the API does the same.
    """
    # import_module, not `from modules.04_backend...`: the package name starts
    # with a digit, so it is not a legal Python identifier in an import
    # statement. Every module in this repository reaches it this way.
    from importlib import import_module

    _sc = import_module("modules.04_backend.scenario")
    ScenarioSpec, SiteSpec = _sc.ScenarioSpec, _sc.SiteSpec

    if demo.get("event_id"):
        ev = import_module("modules.01_geodata.events")
        event = ev.get(demo["event_id"])
        if event is None:
            raise SystemExit(f"{demo['key']}: no event {demo['event_id']!r}")

        lat, lon = float(event["lat"]), float(event["lon"])
        placement = ""
        if demo.get("snap_to_trunk_km"):
            # Two of the published coordinates in events.py are not on a river.
            # events.check_point measures that and says so - the Rishi Ganga
            # point sits at 3,749 m with a flow accumulation of ONE - and it
            # deliberately does not move anything, because where a real barrier
            # stood is a question about the event and not about flow
            # accumulation. A run from an off-channel point floods nothing, and
            # a stored demo that floods nothing teaches the panel nothing.
            #
            # So the placement is made HERE, once, explicitly, and written into
            # the run's own notes: the barrier goes on the strongest flow path
            # the DEM finds within the stated radius of the published
            # coordinate, and the distance and drop are published with it. This
            # is a modelling decision, not a measurement of where the debris
            # was, and it says so in meta.json.
            chk = ev.check_point(event, radius_km=float(demo["snap_to_trunk_km"]))
            if chk["verdict"] == "hillslope":
                t = chk["trunk_channel"]
                lat, lon = float(t["latlon"][0]), float(t["latlon"][1])
                placement = (
                    f"BARRIER PLACEMENT: the published coordinate "
                    f"({chk['given'][0]}, {chk['given'][1]}) is not on a river "
                    f"in COP30 - flow accumulation there is "
                    f"{chk['accumulation_cells']:.0f} cell(s), which is "
                    f"hillslope. The barrier was placed on the strongest flow "
                    f"path within {demo['snap_to_trunk_km']} km, at "
                    f"({t['latlon'][0]}, {t['latlon'][1]}): "
                    f"{t['accumulation_cells']:.0f} cells of accumulation, "
                    f"{t['km_away']} km away and {t['m_below_given']} m below "
                    f"the published point. That is a modelling decision made "
                    f"to have a channel to block, not a measurement of where "
                    f"the debris stood. "
                )
                print(f"    placed on trunk channel {t['km_away']} km away "
                      f"({t['accumulation_cells']:.0f} cells)")

        site = SiteSpec(
            name=f"{event['name']} {event['year']}",
            lat=lat,
            lon=lon,
            river=event["river"],
            state=event["state"],
            # Placeholders in blockage mode - runner replaces both. See the
            # comment in api.RunRequest.to_spec.
            dam_height_m=float(event["blockage_height_m"]),
            reservoir_capacity_mcm=1.0,
            source=event["source"],
            kind="natural",
            height_source="reported",
        )
        notes = (
            f"{event['name']} ({event['year']}), {event['mechanism']}. "
            f"Approximate coordinate and height - {event['source']}. Trigger "
            f"not modelled; no observed extent compared. "
        ) + placement + demo["why"]
        spec = ScenarioSpec(
            site=site,
            failure_mode=demo["failure_mode"],
            blockage_height_m=float(event["blockage_height_m"]),
            dem_source="COP30",
            source_kind="river",
            notes=notes,
            tags=["demo", "river", demo["key"]],
            **demo["spec"],
        )
        spec.require_valid()
        return spec, {"name": site.name, "kind": "natural",
                      "event": demo["event_id"]}

    if demo.get("site"):
        s = demo["site"]
        site = SiteSpec(
            name=s["name"],
            lat=float(s["lat"]),
            lon=float(s["lon"]),
            river=s.get("river", ""),
            state=s.get("state", ""),
            # river_flood has no barrier and empties no reservoir. Both fields
            # are placeholders the validator wants positive; nothing downstream
            # of a river_flood run reads either one.
            dam_height_m=1.0,
            reservoir_capacity_mcm=1.0,
            source=s["source"],
            kind="natural",
            height_source="not applicable - no barrier in this mode",
        )
        spec = ScenarioSpec(
            site=site,
            failure_mode=demo["failure_mode"],
            dem_source="COP30",
            source_kind="river",
            notes=demo["why"],
            tags=["demo", "river", demo["key"]],
            **demo["spec"],
        )
        spec.require_valid()
        return spec, {"name": site.name, "kind": "river"}

    dam = _resolve_dam(demo)
    natural = dam.get("kind") == "natural"

    site = SiteSpec(
        name=dam["name"],
        lat=float(dam["lat"]),
        lon=float(dam["lon"]),
        river=dam.get("river") or "",
        state=dam.get("state") or "",
        dam_height_m=float(dam["height_m"]),
        # A natural dam has no published storage. runner.py replaces this with
        # what the terrain holds; it is 1.0 only because validate() wants it
        # positive, and nothing downstream reads it.
        reservoir_capacity_mcm=1.0 if natural else float(dam["gross_storage_mcm"]),
        source=dam.get("source") or "CWC NRLD 2019",
        kind=dam.get("kind", "engineered"),
        crest_length_m=float(dam["length_m"]) if dam.get("length_m") else None,
        height_source=dam.get("height_source", ""),
    )

    spec = ScenarioSpec(
        site=site,
        failure_mode=demo["failure_mode"],
        design_spillway_cumecs=(
            float(dam["spillway_cumecs"]) if dam.get("spillway_cumecs") else None
        ),
        dem_source="COP30",
        notes=demo["why"],
        tags=["demo", "dam", demo["key"]],
        **demo["spec"],
    )
    spec.require_valid()
    return spec, dam


def build_one(demo: dict) -> dict:
    """Solve one demo on real terrain, through the API's own path."""
    from importlib import import_module

    from shared.io import make_run_id, next_sequence, read_meta
    from shared.validate import validate_run

    gd = import_module("modules.01_geodata")
    rn = import_module("modules.04_backend.runner")

    clock = StageClock()

    # Stage 2 is the register - or the event table, or this file's own
    # coordinate - and _spec_for is exactly that lookup. Stage 1 is the
    # validation of what came back, which is what the API does before any
    # expensive work starts. Both are genuinely near-instant and the measured
    # timings should say so rather than carrying a constant somebody typed.
    t = time.perf_counter()
    spec, dam = _spec_for(demo)
    clock.mark("catalogue", time.perf_counter() - t)

    t = time.perf_counter()
    spec.require_valid()
    clock.mark("input", time.perf_counter() - t)

    reach_km = spec.reach_length_km

    print(f"  {demo['label']}")
    print(f"    site {spec.site.name} ({spec.site.lat:.4f}, {spec.site.lon:.4f}) "
          f"h={spec.site.dam_height_m} m kind={spec.site.kind} "
          f"mode={spec.failure_mode}")

    t = time.perf_counter()
    plan = gd.plan_domain(
        lat=spec.site.lat, lon=spec.site.lon, site=spec.site_slug,
        reach_length_km=reach_km, corridor_width_km=spec.corridor_width_km,
    )
    clock.mark("river", time.perf_counter() - t)

    spec.site.lat, spec.site.lon = plan.dam_lonlat[1], plan.dam_lonlat[0]
    spec.domain_bbox = plan.bbox

    t = time.perf_counter()
    terrain = gd.RealTerrain(
        site=spec.site_slug, source="COP30",
        dam_lonlat=plan.dam_lonlat, reach_length_km=reach_km,
    )
    clock.mark("terrain", time.perf_counter() - t)

    t = time.perf_counter()
    try:
        exposure = gd.exposure.build_exposure(plan.bbox, site=spec.site_slug)
    except Exception as exc:  # noqa: BLE001 - a flood map with no names is valid
        print(f"    exposure unavailable ({type(exc).__name__}: {exc})")
        exposure = None
    clock.mark("exposure", time.perf_counter() - t)

    seq = next_sequence(OUTPUTS, spec.site_slug, spec.scenario_slug, spec.engine)
    run_id = make_run_id(spec.site_slug, spec.scenario_slug, spec.engine, seq)

    t0 = time.perf_counter()
    clock.open("terrain")
    rn.run_scenario(spec, outputs_dir=OUTPUTS, terrain=terrain, run_id=run_id,
                    exposure=exposure, progress=clock.progress)
    clock.close()
    wall = time.perf_counter() - t0

    t = time.perf_counter()
    report = validate_run(OUTPUTS / run_id)
    clock.mark("validate", time.perf_counter() - t)

    t = time.perf_counter()
    meta = read_meta(OUTPUTS / run_id)
    clock.mark("result", time.perf_counter() - t)
    res = meta.get("results", {})
    print(f"    -> {run_id}  {wall:.0f}s  "
          f"{res.get('flood_area_km2', 0):.1f} km2  "
          f"peak {(res.get('peak_discharge_cumecs') or res.get('peak_outflow_cumecs') or 0):,.0f} m3/s  "
          f"valid={report.ok}")
    if not report.ok:
        for e in report.errors:
            print(f"       ERROR {e}")

    rows = clock.as_rows()
    return {
        "key": demo["key"],
        "category": demo["category"],
        "run_id": run_id,
        "label": demo["label"],
        "why": demo["why"],
        "failure_mode": demo["failure_mode"],
        "site": spec.site.name,
        "river": spec.site.river,
        "state": spec.site.state,
        "validates": report.ok,
        "runtime_s": round(wall, 1),
        # Measured, not apportioned. The workflow page paces its replay on
        # these; see the module docstring.
        "stage_timings": rows,
        "stage_total_s": round(sum(r["seconds"] for r in rows), 2),
    }


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["runs"]


def write_manifest(rows: list[dict]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {
                "runs": rows,
                "built_by": "python -m integration.build_demo_runs",
                "note": (
                    "Real runs through runner.run_scenario on COP30 terrain, "
                    "not recordings. Five dams and the five rivers the problem "
                    "statement names; `category` says which tab a run belongs "
                    "to. `stage_timings` are the wall-clock seconds each "
                    "pipeline stage actually took on the machine that built "
                    "the run - the workflow page replays against them. The run "
                    "folders are regenerable and are not committed; rebuild "
                    "them with the command above."
                ),
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def revalidate(existing: dict[str, dict]) -> int:
    """Re-run the validator over the manifest, changing nothing else.

    `validates` is a claim about a run made at the moment it was built, and the
    validator is not frozen - it gains checks, and a check can be corrected.
    Both happened: the capacity comparison was tightened, then found to be the
    wrong sum for a reservoir with an inflow arriving and for a moraine lake
    measured off imagery, and in between the two flagship dam runs sat in the
    manifest claiming `validates: true` while failing on disk.

    Re-solving five dams to discover that costs an hour of compute and changes
    no output file, so this re-reads what is already there and republishes the
    verdict. It is the cheap half of --force and the half that is usually the
    one needed.
    """
    from shared.validate import validate_run

    rows, changed = [], 0
    for d in DEMOS:
        prev = existing.get(d["key"])
        if not prev:
            continue
        run_dir = OUTPUTS / prev["run_id"]
        if not (run_dir / "meta.json").is_file():
            print(f"  {d['key']:<24} MISSING  {prev['run_id']}")
            rows.append(prev)
            continue
        report = validate_run(run_dir)
        was = prev.get("validates")
        if was != report.ok:
            changed += 1
        rows.append({**prev, "validates": report.ok})
        flag = "PASS" if report.ok else "FAIL"
        moved = "" if was == report.ok else f"   (was {was})"
        print(f"  {d['category']:<6} {d['key']:<24} {flag}  {prev['run_id']}{moved}")
        for e in report.errors:
            print(f"       ERROR {e}")

    write_manifest(rows)
    ok = sum(1 for r in rows if r.get("validates"))
    print(f"\n  {ok}/{len(rows)} stored runs validate"
          f"{f', {changed} verdict(s) changed' if changed else ''}")
    return 0 if ok == len(rows) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m integration.build_demo_runs")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the manifest already points at a run")
    ap.add_argument("--list", action="store_true", help="show the manifest and stop")
    ap.add_argument("--only", default=None, help="build one demo by key")
    ap.add_argument("--category", default=None, choices=["dam", "river"],
                    help="build only the dam set or only the river set")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-run the validator over the stored runs and update "
                         "the manifest, without re-solving anything")
    args = ap.parse_args(argv)

    existing = {r["key"]: r for r in load_manifest()}

    if args.revalidate:
        return revalidate(existing)

    if args.list:
        if not existing:
            print("no manifest yet. Run without --list to build.")
            return 0
        for d in DEMOS:
            r = existing.get(d["key"])
            on_disk = r and (OUTPUTS / r["run_id"] / "meta.json").is_file()
            print(f"  {d['category']:<6} {d['key']:<24} "
                  f"{'OK  ' if on_disk else 'MISSING'} "
                  f"{(r or {}).get('run_id', '-')}")
        return 0

    rows: list[dict] = []
    for d in DEMOS:
        skip = (args.only and d["key"] != args.only) or (
            args.category and d["category"] != args.category
        )
        if skip:
            if d["key"] in existing:
                rows.append(existing[d["key"]])
            continue
        prev = existing.get(d["key"])
        if (prev and not args.force
                and (OUTPUTS / prev["run_id"] / "meta.json").is_file()
                and prev.get("stage_timings")):
            print(f"  {d['label']}\n    already built: {prev['run_id']}")
            rows.append(prev)
            continue
        try:
            rows.append(build_one(d))
        except Exception as exc:  # noqa: BLE001
            # One demo failing must not cost the other nine. The manifest keeps
            # what worked and the console shows those.
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            if prev:
                rows.append(prev)

    write_manifest(rows)
    by_key = {r["key"]: r for r in rows}
    for cat in ("dam", "river"):
        want = [d for d in DEMOS if d["category"] == cat]
        ok = sum(
            1 for d in want
            if (r := by_key.get(d["key"]))
            and (OUTPUTS / r["run_id"] / "meta.json").is_file()
        )
        print(f"  {cat:<6} {ok}/{len(want)} stored runs on disk")
    print(f"  -> {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
