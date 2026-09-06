"""
modules/04_backend/analysis.py - the AI briefing over a finished run.

A dam-break run produces six JSON files and four rasters. A district officer
reading it at 3 a.m. wants one paragraph and a list of what to do first. This
module asks Claude for exactly that, and then CHECKS THE ANSWER.

The design rule, which is the whole reason this is defensible in front of NTRO:

    The model never sees the terrain, the register, the internet or its own
    opinion of Indian hydrology. It sees ONE payload built from the run folder
    on disk, and it is told to quote numbers only from that payload. Afterwards
    every number in what it wrote is matched back against the payload
    programmatically. Anything it could not have read is reported as
    `ungrounded_numbers` and the whole briefing is flagged `grounded: false`.

So the failure mode everyone worries about - a language model inventing a
plausible flood statistic - is not argued away here, it is measured. A juror
can ask "how do you know it did not make that up" and the answer is a list.

The briefing is an INTERPRETATION of results the solver computed. It is not a
result. Nothing here is exported, nothing here feeds another module, and the
.shp/.kml the operator downloads are untouched by it.

Owner: captain (module 04).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from shared.io import read_json, read_meta

MODEL = "claude-opus-5"
"""Opus 5. The briefing is short, infrequent and read by an emergency officer;
this is not the place to save a fraction of a cent on a cheaper model."""

MAX_TOKENS = 8000

SYSTEM = """You are briefing a district emergency officer on the results of a \
dam-break inundation simulation. You are the last step in a modelling chain: \
the physics has already been solved and validated, and your job is to explain \
what the numbers mean and what to do first.

Absolute rules, in order of importance:

1. Every number you write MUST appear in the JSON payload you are given. Do not \
compute new numbers - no sums, no percentages, no unit conversions, no \
"roughly", no rounding to a different precision. If you want to express a \
relationship, use words ("most of the affected population", "the larger of the \
two settlements"), not arithmetic.
2. Never state anything the payload does not support. If the payload says a \
figure is an assumption, a default or unmeasured, say so in the same sentence \
that uses it.
3. If `is_fake` is true, the very first thing you say is that this run used \
synthetic terrain and must not be used for any real decision.
4. The `limits` list is not optional and not decoration. Use the payload's own \
limitation, uncertainty and validation fields. Understating what is unknown is \
a worse failure here than being alarming.
5. Warning actions are for the first hours after the breach and must be \
justified by arrival times, depths or the evacuation block in the payload. Do \
not invent local geography, road names, shelters or agencies.
6. The timeline may only use hours that appear in the payload - a settlement's \
arrival_hr, a time_of_peak, or zero. Do not interpolate between two of them \
and do not round one into a friendlier number.
7. `confidence` is set by the payload's own validation, uncertainty and \
knife-edge fields, never by how reasonable the numbers look to you. A run \
whose impounded volume is flagged volume_is_knife_edge cannot be better than \
low. A run with is_fake true is 'not usable'.
8. `verify_before_acting` must name things specific to THIS run - the figure \
that is an assumption, the settlement whose population is a class default, the \
barrier whose volume moved under perturbation. A generic "verify with local \
authorities" is a wasted line.

Write plainly. An officer reads this once, at speed."""


# ==========================================================================
# The structured answer
# ==========================================================================


class Finding(BaseModel):
    """One thing the run shows, with the evidence it came from."""

    statement: str = Field(description="One sentence, plain language.")
    evidence_keys: list[str] = Field(
        description=(
            "Dotted keys from the payload this statement rests on, e.g. "
            "'results.max_depth_m'. At least one."
        )
    )


class TimelineEntry(BaseModel):
    """One moment in the first hours, with the hour it happens at.

    The hour must be an arrival time or a hydrograph time that is IN the
    payload. This is the field most likely to tempt an invented number - "by
    about two hours" - and the grounding check treats it exactly like any
    other, so an interpolated hour is reported as ungrounded.
    """

    hours_after_failure: float = Field(
        description="From the payload. An arrival time, a peak time, or 0."
    )
    what_happens: str = Field(description="One clause. No new geography.")
    who_is_affected: str = Field(
        description=(
            "Named settlements from the payload, or 'the reach' if the payload "
            "names nobody at this hour."
        )
    )


class RunAnalysis(BaseModel):
    """The briefing. Every field is rendered in the console."""

    headline: str = Field(description="One sentence an officer could read aloud.")
    severity: Literal["low", "moderate", "significant", "extreme"]
    severity_basis: str = Field(
        description="Which payload figures drove that severity word."
    )
    alert_text: str = Field(
        description=(
            "Under 160 characters. What an officer would actually send as a "
            "text message in the first minutes: place, hazard, time, action. "
            "No jargon, no unit symbols an SMS will mangle. If the run is "
            "synthetic or ungrounded this must say so first."
        )
    )
    timeline: list[TimelineEntry] = Field(
        description=(
            "The first hours in order, earliest first. Built only from arrival "
            "times and hydrograph times in the payload."
        )
    )
    findings: list[Finding]
    priority_actions: list[str] = Field(
        description="What to do in the first hours, most urgent first."
    )
    population_note: str = Field(
        description=(
            "What the exposure figures do and do not mean, including how the "
            "population was measured where the payload says."
        )
    )
    confidence: Literal["high", "moderate", "low", "not usable"] = Field(
        description=(
            "How much weight this run can carry. Driven by the payload's own "
            "validation, uncertainty and knife-edge fields - NOT by how "
            "plausible the numbers look."
        )
    )
    confidence_basis: str = Field(
        description="The payload fields that set the confidence word."
    )
    verify_before_acting: list[str] = Field(
        description=(
            "The two or three things a human must check against reality before "
            "this is used for a decision. Specific to this run, not generic."
        )
    )
    limits: list[str] = Field(
        description="What this run cannot tell you. Drawn from the payload."
    )
    numbers_cited: list[float] = Field(
        description=(
            "Every numeric value you used anywhere above, exactly as it appears "
            "in the payload."
        )
    )


# ==========================================================================
# The payload - the only thing the model sees
# ==========================================================================


def build_payload(run_dir: Path) -> dict:
    """Everything the briefing may know, read off the run folder.

    Deliberately small. A model given the whole 500 kB of JSON writes about
    whatever is longest; a model given the headline blocks writes about the
    flood.
    """
    meta = read_meta(run_dir)
    payload: dict = {
        "run_id": meta.get("run_id"),
        "is_fake": meta.get("is_fake", True),
        "engine": meta.get("engine"),
        "created_utc": meta.get("created_utc"),
        "site": meta.get("site"),
        "scenario": meta.get("scenario"),
        "domain": meta.get("domain"),
        "dem": meta.get("dem"),
        "time": meta.get("time"),
        "results": meta.get("results"),
        "provenance": meta.get("provenance"),
    }

    # The mode-specific blocks. These were missing, and their absence was a
    # real hole rather than a tidiness issue: `blockage` is where
    # volume_is_knife_edge lives, so the single most important caveat this
    # repository produces - that an impounded volume moves 94% under a
    # quarter-millimetre DEM perturbation - could never reach the briefing at
    # all. A briefing that cannot see the caveat cannot report it.
    for block in ("blockage", "glof_moraine", "spillway_blockage",
                  "river_flood", "sph", "foundation"):
        if meta.get(block):
            payload[block] = meta[block]

    for name in ("impact", "uncertainty", "evacuation", "validation"):
        if (run_dir / f"{name}.json").exists():
            payload[name] = read_json(run_dir, f"{name}.json")

    # What the run did NOT produce, named. Otherwise a missing evacuation.json
    # is indistinguishable from one the briefing simply did not mention, and
    # silence reads as reassurance.
    expected = ["impact.json", "uncertainty.json", "evacuation.json",
                "validation.json", "extent.geojson", "hydrograph.csv"]
    payload["files_absent"] = [f for f in expected if not (run_dir / f).exists()]

    # Settlement lists get long and repetitive. Keep the ones a briefing is
    # about, and say how many were dropped so the model cannot imply it saw all
    # of them.
    #
    # Ordering by population alone was wrong for this purpose. A briefing is
    # read in the first hour, when the question is who gets hit FIRST, and the
    # earliest-warning settlement is routinely a small one that a population
    # sort drops off the end. So the sample is the union of the largest and the
    # earliest, and the payload says that is what it is.
    impact = payload.get("impact") or {}
    settlements = impact.get("settlements")
    if isinstance(settlements, list) and len(settlements) > 15:
        by_pop = sorted(settlements, key=lambda s: (s.get("population") or 0),
                        reverse=True)[:10]
        by_time = sorted(
            (s for s in settlements if s.get("arrival_hr") is not None),
            key=lambda s: s["arrival_hr"],
        )[:10]
        keep, seen = [], set()
        for s in by_time + by_pop:          # earliest first: it is read first
            key = (s.get("name"), s.get("lat"), s.get("lon"))
            if key not in seen:
                seen.add(key)
                keep.append(s)
        impact["settlements"] = keep
        impact["settlements_omitted"] = len(settlements) - len(keep)
        impact["settlements_sample_rule"] = (
            "The ten earliest to be reached and the ten most populous, "
            "deduplicated, earliest first. Not the complete list - "
            "settlements_omitted says how many are not here."
        )

    return payload


def _numbers_in(obj, out: set[float]) -> set[float]:
    """Every numeric value anywhere in the payload, flattened."""
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    elif isinstance(obj, str):
        # Numbers embedded in strings are quotable too: "constant n = 0.06",
        # "power law, k = 2.7", a citation year.
        for m in re.findall(r"-?\d+(?:\.\d+)?", obj):
            try:
                out.add(float(m))
            except ValueError:
                pass
    return out


_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

_UNIT_DIGITS_RE = re.compile(r"(?<=[A-Za-z])\d+")
"""Digits welded to letters are not quantities: the 3 in m3/s, the 2 in km2,
the 30 in COP30, the 2008 in froehlich2008. Stripping them before the scan
keeps the check about numbers the briefing claims, not about spelling."""


def check_grounding(analysis: RunAnalysis, payload: dict) -> dict:
    """Match every number the model wrote against the payload.

    Tolerance is relative and tiny (0.5%) - it exists so that a figure quoted
    as 17.5 against a stored 17.49 is not called a fabrication, not so that a
    wrong number can slide through. Anything unmatched is named.
    """
    known = _numbers_in(payload, set())

    # Every field that carries prose is scanned. Adding a field to RunAnalysis
    # and forgetting it here would create a place in the briefing where a
    # number is not checked, which is the one thing this module exists to
    # prevent - so the list is exhaustive by intent, not by convenience.
    text_parts = [
        analysis.headline,
        analysis.severity_basis,
        analysis.alert_text,
        analysis.population_note,
        analysis.confidence_basis,
        *[f.statement for f in analysis.findings],
        *[t.what_happens for t in analysis.timeline],
        *[t.who_is_affected for t in analysis.timeline],
        *analysis.priority_actions,
        *analysis.verify_before_acting,
        *analysis.limits,
    ]
    written: list[tuple[str, float]] = []
    for part in text_parts:
        for m in _NUM_RE.findall(_UNIT_DIGITS_RE.sub(" ", part)):
            try:
                written.append((part, float(m.replace(",", ""))))
            except ValueError:
                continue
    written += [("numbers_cited", v) for v in analysis.numbers_cited]
    # A timeline hour is a bare float with no prose around it, so it would
    # otherwise escape the scan entirely.
    written += [("timeline.hours_after_failure", float(t.hours_after_failure))
                for t in analysis.timeline]

    def matches(v: float) -> bool:
        for k in known:
            if v == k:
                return True
            scale = max(abs(k), abs(v), 1e-9)
            if abs(v - k) / scale <= 0.005:
                return True
        return False

    ungrounded = []
    for where, v in written:
        if not matches(v):
            ungrounded.append({"value": v, "in": where})

    return {
        "grounded": not ungrounded,
        "checked_values": len(written),
        "payload_values": len(known),
        "ungrounded_numbers": ungrounded,
        "how": (
            "Every number in the briefing text was matched against the values "
            "in the run folder, within 0.5%. Unmatched numbers are listed - "
            "they are numbers the run did not compute."
        ),
    }


# ==========================================================================
# The call
# ==========================================================================


def availability() -> dict:
    """Whether a briefing can be produced at all, and why not if it cannot.

    The console asks this before it draws the panel. Everything else on the
    page works with the network unplugged and must keep working when this is
    unavailable.
    """
    from shared import creds

    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {
            "available": False,
            "reason": "the `anthropic` package is not installed (pip install anthropic)",
        }
    if not creds.get("ANTHROPIC_API_KEY"):
        return {
            "available": False,
            "reason": "ANTHROPIC_API_KEY is not set in .env",
        }
    return {"available": True, "model": MODEL, "reason": ""}


def analyse(run_dir: Path) -> dict:
    """Brief one finished run. Raises RuntimeError with a readable reason."""
    import anthropic

    from shared import creds

    key = creds.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")

    payload = build_payload(run_dir)
    client = anthropic.Anthropic(api_key=key)

    response = client.messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Brief me on this dam-break simulation run. The JSON below "
                    "is the complete record of it and the only source you may "
                    "quote numbers from.\n\n"
                    f"{json.dumps(payload, indent=1, default=str)}"
                ),
            }
        ],
        output_format=RunAnalysis,
    )

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", "") or ""
        raise RuntimeError(f"the model declined to answer: {detail}")

    analysis = response.parsed_output
    if analysis is None:
        raise RuntimeError(f"no structured answer returned (stop {response.stop_reason})")

    return {
        "run_id": payload["run_id"],
        "generated_by": MODEL,
        "is_ai_generated": True,
        "analysis": analysis.model_dump(),
        "grounding": check_grounding(analysis, payload),
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
        "disclaimer": (
            "Written by a language model from this run's own output files. It "
            "is an interpretation of the simulation, not a simulation, not a "
            "forecast, and not an official warning. Every number in it is "
            "checked back against the run - see `grounding`."
        ),
    }
