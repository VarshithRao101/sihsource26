# Research, testing and the presentation — 2 people

You own the case we make and the checking of it. No code.

Read `AGENTS.md` first, all of it. Part 1, the honesty policy, is the one that
decides how this is presented, and it outranks anything below.

---

## What you own

```
docs/deck/      the slides
docs/report/    the written submission
docs/qa/        what you found trying to break it
```

Create those folders; they do not exist yet. Write nothing outside them.

---

## 1 · Research

Know the field well enough to place this project in it. The questions a judge
asks come from here.

- **What already exists.** DSS-WISE Lite (NCCHE/FEMA) is the closest analogue to
  what we built and you should be able to say how we differ. HEC-RAS 2D, MIKE
  FLOOD, TUFLOW, LISFLOOD-FP, TELEMAC are the engines people use. In India:
  the Dam Safety Act 2021, CWC's 2018 flood-risk mapping guidelines, and NDSA's
  programme to produce inundation maps for all ~6,600 dams.
- **The events NTRO named** — Rishi Ganga (Feb 2021), Wapriyang (Nov 2021),
  Phuktal/Sumdo (Mar 2015), Kosi (2008). Four of the five are natural dams, not
  engineered failures. Know what happened in each.
- **Where our numbers come from.** Every constant in this repository cites a
  paper. Froehlich (2008), Von Thun & Gillette (1990), MacDonald &
  Langridge-Monopolis (1984) for breach; Huizinga/JRC (2017) for damage;
  Audusse (2004) and Harten-Lax-van Leer (1983) for the solver. You should be
  able to name the source of any figure on a slide.

## 2 · Testing

Break it before a judge does. You do not need to read the code to do this well.

```bash
.venv\Scripts\python.exe integration\run_all.py          # expect 23/23
.venv\Scripts\python.exe -m pytest modules\04_backend\tests -q
.venv\Scripts\python.exe -m shared.validate outputs\chungthangdam_overtop_fast_002
```

Then use the console the way a stranger would and write down everything that
confuses, stalls or looks wrong. Try: a dam with no coordinates, a 500 km reach,
a river with one known point, gates at 0%, the browser with wifi off, a phone.

Record findings in `docs/qa/` as **what you did, what you expected, what
happened**. A finding you cannot reproduce is not yet a finding.

**Report bugs to the captain. Do not fix them.**

## 3 · The deck and the report

Structure them around NTRO's five deliverables — `docs/NTRO_REQUIREMENTS.md`
maps every one to the file that implements it and the evidence it works. That
mapping is the spine of both documents.

Numbers you may use, because they are measured and reproducible:

| | |
|---|---|
| Ritter analytical dam break | RMSE **0.218 m** |
| Lake at rest | **2.9e-06 m** spurious velocity |
| Mass conservation | **0.000%** |
| SFINCS, an independent engine | **CSI 0.9653** agreement |
| Grid convergence | depth converged by 60 m |
| Dams selectable | **5,686** · rivers **2,229** |
| Solver speedup | **2.3–4.5×** |

**Numbers you may NOT use: any we did not compute.** No "95% accurate", no
estimated figures, no rounding a bad result into a good one. Our satellite
validation is weak — CSI 0.027 — and `docs/VALIDATION.md` publishes it at the
same size as the good numbers. The deck does the same. A judge who models dams
knows the uncertainty is there; the only thing being assessed is whether we do.

Source material, already written and current:
`docs/NTRO_REQUIREMENTS.md`, `docs/VALIDATION.md`, `docs/CONVERGENCE.md`,
`docs/QA.md` (the adversarial Q&A), `docs/LOAD_TEST.md`, `docs/DEMO_RUNBOOK.md`.

## 4 · Presenting

`docs/DEMO_RUNBOOK.md` section 4 is the eight-minute run of show. Rehearse it
until it is boring. The Delft3D paragraph in the first minute is not optional —
saying it before anyone asks is worth more than any slide.

---

**Ask the captain before:** adding any number to a slide that is not in this
repository, changing a claim in `docs/`, or answering a technical question you
are not certain of. "We didn't get to that" is a correct answer and costs
almost nothing.
