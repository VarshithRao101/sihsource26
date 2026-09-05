# The deck — slide by slide

**Twelve slides, eight minutes, and every number on them comes from a file in this
repository.** Where a figure appears below, the file it came from is named beside it. If you
cannot point at the file, do not put the number on the slide.

Read `AGENTS.md` Part 1 before writing a word of this. It outranks everything here.

---

## The argument the deck has to make

In one sentence, said three different ways across the deck:

> **Point it at any of 5,749 Indian barriers - 5,686 engineered dams and 63 natural ones -
> or 2,229 rivers and it tells you which villages flood,
> how deep, and how long they have — and it tells you what it does not know.**

The second half is not a disclaimer. It is the differentiator. Every team in that room will
show a flood animation. Almost none will show the error bars, and a judge who models dams for a
living is looking for exactly that.

---

## 1 · Title

**Dam Break Inundation Modelling Using Hydrodynamic Modelling of any River**
SIH26161 · NTRO · Disaster Management

One line under it: *5,749 barriers (5,686 engineered + 63 natural) · 2,229 rivers · 8 failure cases · 20-stage automated pipeline · runs live.*

## 2 · The problem, in NTRO's own words

Quote the Background paragraph, then the finding that shapes everything:

> **Four of the five events NTRO names are natural dams, not engineered dam failures** —
> Rishi Ganga (Feb 2021), Wapriyang (Nov 2021), Phuktal (Mar 2015), Kosi (2008).

*Source: `docs/PROBLEM_STATEMENT.md`.* This is why river blockage is a first-class scenario and
not an afterthought, and it is worth saying early because it shows you read the statement rather
than skimmed it.

## 3 · What exists — the pipeline as a picture

Screenshot of `/workflow`. **Twenty stages**, thirteen of which execute in a live run; the other
seven are engine probes and offline comparisons that report their true state.

> Say: *"this diagram is generated from the code that runs it, so it cannot drift."*

*Source: `modules/04_backend/pipeline.py`.*

## 4 · Live demo, not a video

The demo itself. `docs/DEMO_RUNBOOK.md` section 4 is the run of show — rehearse from that, not
from this slide. The slide is just a holding frame with the URL on it.

**Non-negotiable:** the Delft3D sentence in the first minute. See slide 11.

## 5 · One dam, end to end — Chungthang, Sikkim

| | |
|---|---|
| Grid | 330 × 414 at 90 m |
| Peak discharge | **28,010 m³/s** |
| Maximum depth | **40.22 m** |
| Flood area | **10.39 km²** |
| Solve time | **68 s** |
| Mass balance error | **0.000 %** |

*Source: `outputs/chungthangdam_overtop_fast_002/meta.json`.*

Put the mass balance figure on the slide in the same size as the rest. A hydrologist looks for it
first.

## 6 · The answer a district officer acts on

Not depth — **names, times and margins**.

| | |
|---|---|
| Settlements affected | 3 |
| People | **9,126** (all WorldPop 2020 measured, not defaults) |
| Houses | 1,983 |
| Roads cut | 5.36 km |
| Damage | **₹172.11 crore** — buildings 158.71, roads 13.40, cropland 0 |

And one evacuation row in full, because it is the whole HADR argument in one line:

> **Sangkalang** — water arrives in **1.03 h**, the walk out takes **0.02 h**, margin **1.01 h**.

*Sources: `impact.json`, `evacuation.json` in that run folder.*

Say the population source out loud. This run is entirely WorldPop-measured; other runs carry
class defaults and the table says which.

## 7 · What we do not know — the slide nobody else has

| | |
|---|---|
| Breach regressions disagree by | **3.97×** (9,377 – 37,206 m³/s) |
| Our routed peak | 28,010 m³/s, inside that envelope |
| DEM vertical error | **1.7 m RMSE** (COP30, Hawker et al. 2022) |
| Riverbed bathymetry | **not measured at all** |

*Source: `uncertainty.json` — published with every single run, not prepared for this slide.*

> Say: *"we compute all four regressions and average none of them, because averaging would hide
> the disagreement."*

## 8 · Is the maths right? — verification

| Test | Result |
|---|---|
| Ritter analytical dam break | RMSE **0.218 m** |
| Lake at rest (well-balancedness) | **2.9 × 10⁻⁶ m** spurious velocity |
| Closed-basin mass | **0.000 %** |
| Independent engine (SFINCS) | **CSI 0.9653** extent agreement |

*Sources: `integration/run_all.py` (23/23), `docs/VALIDATION.md`.*

An analytical comparison is the strongest evidence a flood model can offer about itself, and most
teams will not have attempted one.

## 9 · How much of the answer is the mesh?

The slide most likely to win a technical judge, because almost nobody does it.

| refinement | max depth |
|---|---|
| 90 → 60 m | −5.9 % / +3.2 % |
| **60 → 45 m** | **+1.1 % / +0.6 %** |

**Depth converges by 60 m** on two independent sites — so we moved the default there. **Flood
area does not converge** as fast, and every area figure carries several percent of grid
dependence. In a gorge it does not converge at all.

*Source: `docs/CONVERGENCE.md`.*

## 10 · Does it match reality? — validation, honestly

| | CSI | POD | bias |
|---|---|---|---|
| Teesta gorge | 0.0075 | 0.013 | 0.70 |
| Annamayya floodplain | 0.0314 | **0.250** | **7.22** |

*60 m grid, no-data cells excluded rather than counted as dry. Two earlier figures for this event
exist (0.0268 at 90 m, 0.0293 unmasked) — `docs/VALIDATION.md` §2.2 reconciles all three. **Put only
this row on the slide.***

**We have no strong observational validation and we claim none.** Detection improved
nineteenfold on the low-gradient reach, which diagnoses the gorge as a *resolution* problem —
independently confirmed by the convergence study, which never touches satellite data.

*Source: `docs/VALIDATION.md`.*

**If a judge pushes on the bias**, this is the answer and it is a strong one: we swept the breach
severity 100/75/50/25% and bias falls monotonically to **1.01** — the over-prediction is the
worst-case assumption we adopt when breach parameters are unpublished, not the hydraulics. But the
severity that matches the area overlaps on only **2%** of cells, and the observed mask is **732
disconnected fragments** where a flood is one corridor. So the satellite composite itself is the
limiting factor. *Source: `docs/SEVERITY_INVERSION.md`.* Do not put this on the slide — it is the
answer to a question, not a bullet.

> Say this before they ask. Being first to your own weakest number is worth more than the number
> costs.

## 11 · Delft3D — the honest slide

The statement names Delft3D. Here is exactly where we are:

- **Delft3D 4 is GPLv3 and free.** Deltares ships the kernels as source, so it is a compile.
- **We compiled it.** `d_hydro.exe` + `flow2d3d.dll`, and Deltares' own `f34` example runs on it.
- **A case our code generates solves on it**, and reads back with the bed matching our DEM to
  within a centimetre.
- **The engine comparison does not run yet.** On a reach with 277 m of relief Delft3D aborts on
  the first timestep, and that is a model-setup problem we have not finished.
- The FM suite is a different product; its licence we requested was never answered.

> If asked "so it does not work?" — ***"The kernel works. Our case format works. The comparison
> on a real reach does not yet, and that is where we stopped."*** Do not dress it up.

*Source: `modules/03_delft3d/`, `integration/compare_delft3d.py`.*

## 12 · What is deliberately not built

- **No GNN for evacuation.** It needs labelled evacuation outcomes, which do not exist for
  Indian dam breaks. We use an exact time-dependent Dijkstra — we do not need to *learn* a
  shortest safe path when we can *compute* it.
- **No LSTM for inflow.** Training on our own runoff model's output would teach it our model and
  let us call the result learned knowledge. Circular.
- **ML is not requested anywhere in this statement.** Four models exist; present them as bonus,
  never as a deliverable.

Close on the sentence the whole deck has been building to:

> **Strongly verified, grid-quantified, weakly validated — and we publish all three.**

---

## Numbers you may NOT use

No accuracy percentage exists in this project because none was measured. No "95 % accurate", no
estimates, no rounding a bad number into a good one. If a slide needs a figure that is not in this
repository, the answer is to measure it or leave it out.

## If you have only 4 slides

3 (the pipeline), 5 (one dam end to end), 7 (what we do not know), 10 (validation, honestly).
That is the whole argument.
