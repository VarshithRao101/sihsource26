"""
integration/historical_validation.py - the system against seven real failures.

    python -m integration.historical_validation            # table to stdout
    python -m integration.historical_validation --md       # write the report
    python -m integration.historical_validation --json     # machine-readable

WHAT THIS MEASURES, and what it deliberately does not.

The reference is data/observed/historical_dam_failures.pdf: seven documented
dam failures with dam type, height, crest length, storage, reported peak breach
discharge, travel distance, wave depth and casualties. Every number attributed
to "reported" in the output below is read off that document and nothing else.

MEASURED HERE: peak breach outflow. For each event we take the published dam
geometry - height, crest length, storage - choose the failure mode that matches
the documented mechanism, and run the SAME hydrograph code the solver uses as
its upstream boundary. The result is compared against the reported peak.

That comparison is worth making because it is not circular. Nothing in
shared/hydro.py was fitted to these seven events: Froehlich (2008) is fitted to
74 embankment failures, the critical-flow coefficient is Ritter's analytical
solution, and the moraine treatment comes from Huggel et al. (2002). Three of
the seven (Teton, St Francis, South Fork) are in the datasets those regressions
were built from, which is stated per row below - those three are a consistency
check rather than an out-of-sample test, and only four are genuinely blind.

NOT MEASURED HERE: inundation extent, arrival times, depths, casualties. Those
need the flood routed over the real terrain, and for five of the seven that is
not a test we can honestly run:

  * Teton and St Francis no longer have reservoirs. The DEM shows the valley as
    it is now, with the impoundment gone.
  * Banqiao was rebuilt in 1993 on the same site with different geometry.
  * The Ru River basin below Banqiao has been re-engineered since 1975.
  * South Fork's lake was never refilled.

Routing those on a modern DEM and calling the result a hindcast would be
measuring the wrong terrain and reporting the number as if it meant something.
Where a routed comparison IS defensible - Machchhu II, whose valley is
substantially unchanged and which sits in the CWC register - it is run by
integration/machhu1979.py and reported there.

SO: this file validates the breach and release physics. It does not validate
the hydrodynamics, and the report says so in its own header rather than letting
a reader assume a bigger claim than the evidence supports.

Owner: captain.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REPORT_MD = REPO_ROOT / "docs" / "HISTORICAL_VALIDATION.md"
REPORT_JSON = REPO_ROOT / "docs" / "historical_validation.json"

SOURCE = (
    "data/observed/historical_dam_failures.pdf - "
    "'HISTORICAL DAM FAILURES: FIELD PARAMETERS & IMPACT DATA', "
    "supplied reference document"
)


# --------------------------------------------------------------------------
# The seven, exactly as the reference document states them
# --------------------------------------------------------------------------
#
# `reported_*` fields are transcribed, not adjusted. Where the document gives a
# range ("2,000 to 10,000+ fatalities") both ends are kept. Where it gives a
# figure with a tilde the tilde is recorded in `reported_peak_qualifier`,
# because "~48,000" and "48,000" are different claims.

EVENTS = [
    {
        "no": 1,
        "name": "Machchhu Dam II",
        "place": "Morbi, Gujarat, India",
        "date": "11 August 1979",
        "dam_type": "Earthen bund with central masonry spillway",
        "height_m": 26.3,
        "crest_length_m": 4015.0,
        "storage_mcm": 110.0,
        "reported_peak_cumecs": 16307.0,
        "reported_peak_qualifier": "stated exactly",
        "mechanism": (
            "Massive overtopping. 600 mm in 24 h produced an inflow of "
            "16,300 m3/s against a design spillway capacity of 5,660 m3/s. "
            "Water overtopped the earthen flanks by 0.6 m and scoured away "
            "2,100 m of embankment."
        ),
        "our_mode": "overtopping",
        "mode_because": (
            "The document names overtopping and gives the depth over the "
            "flanks. This is the textbook case for the mode."
        ),
        "params": {"inflow_cumecs": 16300.0},
        "in_regression_dataset": False,
        "distance_km": 60.0,
        "reported_depth_m": "6 to 9 m wall, 6-10 m in Morbi city",
        "deaths": "2,000 to 10,000+",
    },
    {
        "no": 2,
        "name": "Banqiao Dam",
        "place": "Henan Province, China",
        "date": "8 August 1975",
        "dam_type": "Clay-core earthen embankment",
        "height_m": 118.0,
        "crest_length_m": 2028.0,
        "storage_mcm": 492.0,
        "reported_peak_cumecs": 78800.0,
        "reported_peak_qualifier": "stated exactly",
        "mechanism": (
            "Catastrophic overtopping driven by Typhoon Nina, 1,060 mm in "
            "24 h. Sluice gates could not open fully due to siltation "
            "blockages, triggering a chain-reaction collapse of 62 dams."
        ),
        "our_mode": "spillway_blockage",
        "mode_because": (
            "The document is explicit that the gates could not open. That is "
            "not ordinary overtopping - the reservoir had no relief capacity "
            "while the inflow arrived, which is the mode's definition."
        ),
        "params": {
            "inflow_cumecs": 13000.0,
            "residual_spillway_frac": 0.25,
            "design_spillway_cumecs": 1742.0,
            "blockage_start_level_frac": 0.80,
        },
        "params_note": (
            "Inflow 13,000 m3/s and a design capacity of 1,742 m3/s are the "
            "commonly cited figures for Banqiao and are NOT in the reference "
            "document. They are inputs we supplied, and the peak below "
            "therefore depends on them."
        ),
        "in_regression_dataset": False,
        "distance_km": 110.0,
        "reported_depth_m": "6 to 10 m wall at ~50 km/h",
        "deaths": "171,000 to 230,000",
    },
    {
        "no": 3,
        "name": "Teton Dam",
        "place": "Idaho, United States",
        "date": "5 June 1976",
        "dam_type": "Zoned earthfill",
        "height_m": 93.0,
        "crest_length_m": 945.0,
        "storage_mcm": 356.0,
        "reported_peak_cumecs": 65120.0,
        "reported_peak_qualifier": "stated exactly",
        "mechanism": (
            "Internal erosion and piping. Fractured volcanic rhyolite in the "
            "right abutment let reservoir water bypass the grout curtain and "
            "wash out the silt core during first filling."
        ),
        "our_mode": "piping",
        "mode_because": "The document names piping and describes the conduit.",
        "params": {},
        "in_regression_dataset": True,
        "distance_km": 130.0,
        "reported_depth_m": "2.5-3 m in residential zones; 45 m wide breach",
        "deaths": "11 to 14",
    },
    {
        "no": 4,
        "name": "St. Francis Dam",
        "place": "California, United States",
        "date": "12 March 1928",
        "dam_type": "Curved concrete gravity",
        "height_m": 62.5,
        "crest_length_m": 213.0,
        "storage_mcm": 47.0,
        "reported_peak_cumecs": 48000.0,
        "reported_peak_qualifier": "~ approximate in the source",
        "mechanism": (
            "Geological abutment failure. Gypsum veins in the western "
            "abutment dissolved when saturated; the eastern abutment slipped "
            "along lubricated Pelona schist fault planes."
        ),
        "our_mode": "foundation_failure",
        "mode_because": (
            "A concrete gravity dam displaced off its foundation. No "
            "embankment eroded, so no embankment regression applies - this is "
            "the case the mode was written for."
        ),
        "params": {"foundation_breach_frac": 0.8, "collapse_time_min": 2.0},
        "in_regression_dataset": True,
        "distance_km": 87.0,
        "reported_depth_m": "43 m initial wave in the canyon",
        "deaths": "431 to 450+",
    },
    {
        "no": 5,
        "name": "South Fork Dam (Johnstown)",
        "place": "Pennsylvania, United States",
        "date": "31 May 1889",
        "dam_type": "Earthen embankment",
        "height_m": 22.0,
        "crest_length_m": 280.0,
        "storage_mcm": 18.0,
        "reported_peak_cumecs": 8500.0,
        "reported_peak_qualifier": "~ approximate in the source",
        "mechanism": (
            "Crest lowering and debris clogs. The crest had been lowered for "
            "a carriage road and iron fish screens across the spillway "
            "blocked floating debris, leaving zero capacity in a record storm."
        ),
        "our_mode": "spillway_blockage",
        "mode_because": (
            "Screens across the spillway and a lowered crest. The relief "
            "capacity was gone before the storm arrived, which is the mode."
        ),
        "params": {
            "inflow_cumecs": 300.0,
            "residual_spillway_frac": 0.05,
            "design_spillway_cumecs": 280.0,
            "blockage_start_level_frac": 0.90,
        },
        "params_note": (
            "The inflow and the spillway capacity are not in the reference "
            "document; both are supplied here and the peak depends on them."
        ),
        "in_regression_dataset": True,
        "distance_km": 23.0,
        "reported_depth_m": "12 m wall at 65 km/h",
        "deaths": "2,209",
    },
    {
        "no": 6,
        "name": "Malpasset Dam",
        "place": "Frejus, France",
        "date": "2 December 1959",
        "dam_type": "Ultra-thin concrete arch",
        "height_m": 66.5,
        "crest_length_m": 222.0,
        "storage_mcm": 50.0,
        "reported_peak_cumecs": 8000.0,
        "reported_peak_qualifier": "~ approximate in the source",
        "mechanism": (
            "Foundation shearing under hydrostatic uplift. First filling "
            "exposed unmapped tectonic fault planes in the gneiss beneath the "
            "left arch thrust block."
        ),
        "our_mode": "foundation_failure",
        "mode_because": (
            "An arch dam that lost its thrust block. Nothing eroded; the arch "
            "was found displaced substantially intact."
        ),
        "params": {"foundation_breach_frac": 0.8, "collapse_time_min": 2.0},
        "in_regression_dataset": False,
        "distance_km": 12.0,
        "reported_depth_m": "40 m wall at 70 km/h",
        "deaths": "421",
    },
    {
        "no": 7,
        "name": "Tiware Dam",
        "place": "Ratnagiri, Maharashtra, India",
        "date": "2 July 2019",
        "dam_type": "Small earthen bund",
        "height_m": 13.5,
        "crest_length_m": 308.0,
        "storage_mcm": 2.4,
        "reported_peak_cumecs": 1200.0,
        "reported_peak_qualifier": "~ approximate in the source",
        "mechanism": (
            "Neglected piping and wall fissures. Over 400 mm in a day "
            "exploited unaddressed seepages reported by villagers; the "
            "central bund collapsed at 21:30."
        ),
        "our_mode": "piping",
        "mode_because": (
            "The document names piping and pre-existing seepage paths."
        ),
        "params": {},
        "in_regression_dataset": False,
        "distance_km": 18.0,
        "reported_depth_m": "fast mountain torrent, depth not stated",
        "deaths": "23",
    },
]


# --------------------------------------------------------------------------


def simulate(ev: dict) -> dict:
    """Peak outflow for one event, through the code the solver actually uses."""
    import numpy as np

    import shared.hydro as H

    h = ev["height_m"]
    cap = ev["storage_mcm"] * 1e6
    mode = ev["our_mode"]
    prm = dict(ev["params"])
    extra: dict = {}

    if mode == "foundation_failure":
        t, q, params = H.foundation_collapse_hydrograph(
            dam_height_m=h,
            capacity_m3=cap,
            crest_length_m=ev["crest_length_m"],
            reservoir_level_frac=1.0,
            breach_fraction_of_crest=prm.get("foundation_breach_frac", 0.8),
            collapse_time_s=prm.get("collapse_time_min", 2.0) * 60.0,
            duration_hr=12.0,
        )
        extra = {
            "opening_top_width_m": params.opening_top_width_m,
            "opening_bottom_width_m": params.opening_bottom_width_m,
            "regression_used": "none - see FoundationCollapse.basis",
        }

    elif mode == "spillway_blockage":
        fill = H.fill_to_overtopping(
            dam_height_m=h,
            capacity_m3=cap,
            inflow_cumecs=prm["inflow_cumecs"],
            design_spillway_cumecs=prm.get("design_spillway_cumecs"),
            residual_capacity_frac=prm.get("residual_spillway_frac", 0.0),
            starting_level_frac=prm.get("blockage_start_level_frac", 0.85),
        )
        breach = H.breach_parameter_ensemble(cap, h, "overtopping")["froehlich2008"]
        if fill.overtopped:
            t, q = H.breach_hydrograph(
                breach, dam_height_m=h, capacity_m3=cap,
                reservoir_level_frac=1.0, failure_mode="overtopping",
                inflow_cumecs=prm["inflow_cumecs"], duration_hr=12.0,
            )
        else:
            t = np.array([0.0, 12.0])
            q = np.array([0.0, 0.0])
        extra = {
            "time_to_overtop_hr": fill.time_to_overtop_hr,
            "overtopped": fill.overtopped,
            "breach_width_m": round(breach.average_width_m, 1),
            "regression_used": "froehlich2008 (post-overtopping breach)",
        }

    else:  # overtopping, piping
        breach = H.breach_parameter_ensemble(cap, h, mode)["froehlich2008"]
        t, q = H.breach_hydrograph(
            breach, dam_height_m=h, capacity_m3=cap,
            reservoir_level_frac=1.0, failure_mode=mode,
            inflow_cumecs=prm.get("inflow_cumecs", 0.0), duration_hr=12.0,
        )
        extra = {
            "breach_width_m": round(breach.average_width_m, 1),
            "formation_time_hr": round(breach.formation_time_hr, 3),
            "regression_used": "froehlich2008",
        }

    peak = float(q.max())
    reported = ev["reported_peak_cumecs"]
    # Released volume is NOT the same as stored volume whenever an inflow is
    # set, and on three of these events the inflow dwarfs the reservoir:
    # Machchhu II passed 16,300 m3/s for hours against a 110 MCM reservoir. The
    # two are reported separately, because "released 779 MCM of the 110 MCM
    # stored" reads as a broken model rather than as a flood.
    inflow_mcm = prm.get("inflow_cumecs", 0.0) * float(t[-1]) * 3600.0 / 1e6
    envelope = H.peak_outflow_envelope(cap, h)
    regs = H.peak_outflow_regressions(cap, h)

    return {
        "peak_cumecs": peak,
        "ratio": peak / reported if reported else None,
        "pct_error": (peak - reported) / reported * 100.0 if reported else None,
        "log_error": abs(math.log10(peak / reported)) if peak > 0 and reported else None,
        "released_mcm": float(np.trapezoid(q, t * 3600.0)) / 1e6,
        "inflow_volume_mcm": inflow_mcm,
        "stored_mcm": ev["storage_mcm"],
        "time_of_peak_min": float(t[int(q.argmax())]) * 60.0,
        "envelope_low_cumecs": envelope[0],
        "envelope_high_cumecs": envelope[1],
        "reported_in_envelope": envelope[0] <= reported <= envelope[1],
        "independent_regressions": {k: round(v, 1) for k, v in regs.items()},
        **extra,
    }


def grade(ratio: float | None) -> str:
    """How close is close, said once so every row is graded the same way.

    The bands are the ones dam-break practice actually uses. A factor of two on
    peak breach discharge is a GOOD result in this field - Wahl (2004,
    'Uncertainty of Predictions of Embankment Dam Breach Parameters', ASCE J.
    Hydraulic Engineering 130(5)) found the standard breach regressions carry
    prediction intervals of roughly -0.5 to +1 order of magnitude on peak
    outflow. Anything inside a factor of 2 is at the good end of the published
    scatter; a factor of 3 is still inside it.
    """
    if ratio is None:
        return "n/a"
    r = ratio if ratio >= 1 else 1 / ratio
    if r <= 1.25:
        return "excellent"
    if r <= 2.0:
        return "good"
    if r <= 3.0:
        return "acceptable"
    if r <= 10.0:
        return "poor"
    return "wrong"


def run_all() -> list[dict]:
    rows = []
    for ev in EVENTS:
        out = simulate(ev)
        out["grade"] = grade(out["ratio"])
        rows.append({"event": ev, "result": out})
    return rows


def summarise(rows: list[dict]) -> dict:
    ratios = [r["result"]["ratio"] for r in rows if r["result"]["ratio"]]
    errs = [abs(r["result"]["pct_error"]) for r in rows if r["result"]["pct_error"] is not None]
    within = lambda f: sum(1 for x in ratios if 1 / f <= x <= f)  # noqa: E731
    blind = [r for r in rows if not r["event"]["in_regression_dataset"]]
    blind_ratios = [r["result"]["ratio"] for r in blind if r["result"]["ratio"]]

    # The geometric mean is the right average for a ratio: the arithmetic mean
    # of 0.5 and 2.0 is 1.25, which says the model runs high when in fact those
    # two errors are the same size in opposite directions.
    gmean = math.exp(statistics.fmean(math.log(x) for x in ratios)) if ratios else None

    return {
        "events": len(rows),
        "within_factor_1_25": within(1.25),
        "within_factor_2": within(2.0),
        "within_factor_3": within(3.0),
        "outside_factor_3": len(ratios) - within(3.0),
        "median_abs_pct_error": round(statistics.median(errs), 1) if errs else None,
        "mean_abs_pct_error": round(statistics.fmean(errs), 1) if errs else None,
        "geometric_mean_ratio": round(gmean, 3) if gmean else None,
        "bias": (
            "runs high" if gmean and gmean > 1.15
            else "runs low" if gmean and gmean < 0.87
            else "no systematic bias"
        ),
        "blind_events": len(blind),
        "blind_within_factor_2": sum(1 for x in blind_ratios if 0.5 <= x <= 2.0),
        "reported_inside_our_envelope": sum(
            1 for r in rows if r["result"]["reported_in_envelope"]
        ),
    }


def print_table(rows: list[dict], summary: dict) -> None:
    print()
    print("=" * 112)
    print("  HISTORICAL VALIDATION - peak breach outflow against seven documented failures")
    print("=" * 112)
    print(f"  {'#':<2} {'dam':<26} {'mode run':<19} {'reported':>10} "
          f"{'ours':>10} {'ratio':>7} {'err %':>8}  grade")
    print(f"  {'-'*2} {'-'*26} {'-'*19} {'-'*10} {'-'*10} {'-'*7} {'-'*8}  {'-'*10}")
    for r in rows:
        e, o = r["event"], r["result"]
        blind = "" if e["in_regression_dataset"] else " *"
        print(f"  {e['no']:<2} {e['name'][:26]:<26} {e['our_mode']:<19} "
              f"{e['reported_peak_cumecs']:>10,.0f} {o['peak_cumecs']:>10,.0f} "
              f"{o['ratio']:>7.2f} {o['pct_error']:>+8.1f}  {o['grade']}{blind}")
    print()
    print("  * out-of-sample: this event is NOT in the datasets the breach")
    print("    regressions were fitted to.")
    print()
    print("  " + "-" * 108)
    print(f"  {summary['within_factor_2']}/{summary['events']} within a factor of 2   "
          f"{summary['within_factor_3']}/{summary['events']} within a factor of 3   "
          f"median |error| {summary['median_abs_pct_error']}%")
    print(f"  geometric mean ratio {summary['geometric_mean_ratio']} - {summary['bias']}")
    print(f"  {summary['blind_within_factor_2']}/{summary['blind_events']} of the "
          f"out-of-sample events within a factor of 2")
    print("  " + "-" * 108)
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m integration.historical_validation")
    ap.add_argument("--md", action="store_true", help="write docs/HISTORICAL_VALIDATION.md")
    ap.add_argument("--json", action="store_true", help="write docs/historical_validation.json")
    args = ap.parse_args(argv)

    rows = run_all()
    summary = summarise(rows)
    print_table(rows, summary)

    if args.json:
        REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
        REPORT_JSON.write_text(
            json.dumps({"source": SOURCE, "summary": summary, "rows": rows},
                       indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  wrote {REPORT_JSON}")

    if args.md:
        from integration._historical_report import write_markdown

        write_markdown(rows, summary, REPORT_MD, SOURCE)
        print(f"  wrote {REPORT_MD}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
