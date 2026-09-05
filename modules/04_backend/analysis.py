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


class RunAnalysis(BaseModel):
    """The briefing. Every field is rendered in the console."""

    headline: str = Field(description="One sentence an officer could read aloud.")
    severity: Literal["low", "moderate", "significant", "extreme"]
    severity_basis: str = Field(
        description="Which payload figures drove that severity word."
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
        "site": meta.get("site"),
        "scenario": meta.get("scenario"),
        "domain": meta.get("domain"),
        "dem": meta.get("dem"),
        "time": meta.get("time"),
        "results": meta.get("results"),
    }

    for name in ("impact", "uncertainty", "evacuation", "validation"):
        if (run_dir / f"{name}.json").exists():
            payload[name] = read_json(run_dir, f"{name}.json")

    # Settlement lists get long and repetitive. Keep the worst-affected ones,
    # which is what a briefing is about, and say how many were dropped so the
    # model cannot imply it saw all of them.
    impact = payload.get("impact") or {}
    settlements = impact.get("settlements")
    if isinstance(settlements, list) and len(settlements) > 15:
        ordered = sorted(
            settlements,
            key=lambda s: (s.get("population") or 0),
            reverse=True,
        )
        impact["settlements"] = ordered[:15]
        impact["settlements_omitted"] = len(settlements) - 15

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

    text_parts = [
        analysis.headline,
        analysis.severity_basis,
        analysis.population_note,
        *[f.statement for f in analysis.findings],
        *analysis.priority_actions,
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
