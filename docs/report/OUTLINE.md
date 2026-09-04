# The written report — structure and sources

**The report's spine is NTRO's five deliverables, in their order, not ours.** A judge marking
against the statement should be able to find each one without hunting. `docs/NTRO_REQUIREMENTS.md`
already maps every clause to the file that implements it and the evidence it works — that mapping
is the report's skeleton, and most of this document is telling you where to expand it.

Read `AGENTS.md` Part 1 first. Every rule in the deck outline about inventing numbers applies here
and matters more, because a report is read slowly.

---

## 0 · Front matter

Problem statement ID, title, organisation, team. One paragraph of abstract, which should be the
same sentence the deck opens with:

> Point it at any of 5,686 Indian dams or 2,229 rivers and it tells you which villages flood, how
> deep, and how long they have — and it states what it does not know.

## 1 · The problem, and what NTRO actually asked for

Quote the Background verbatim from `docs/PROBLEM_STATEMENT.md`. Then the reading that shaped the
build: **four of the five named events are natural dams**, so river blockage is a first-class
scenario. Then the five deliverables, listed, as a promise of the sections that follow.

Also state what the problem statement does **not** ask for: there is no requirement for machine
learning, deep learning or quantum computing anywhere in it. Anything we built there is a bonus and
is presented as one.

## 2 · System architecture

The 20-stage pipeline. Use the `/workflow` screenshot and say that the diagram is generated from
`modules/04_backend/pipeline.py`, so it cannot drift from the code that runs.

Cover the module layout (`shared/`, `01_geodata` … `09_sfincs`), and the design decision worth a
paragraph of its own: **modules exchange run folders on disk, never imports.** One data contract,
one validator, and a run that fails it does not exist. That is `shared/contract.py` and
`shared/validate.py`.

## 3 · Deliverable (i) — the modelling framework

The longest section. Four scenarios, each with its physics and its citation:

| scenario | method | source |
|---|---|---|
| Dam break | breach regression → level-pool depletion → 2D routing | Froehlich (2008), Von Thun & Gillette (1990), MacDonald & Langridge-Monopolis (1984) |
| River blockage | storage read off the DEM → fill time → natural-dam breach | Peng & Zhang (2012) |
| Water release | orifice + broad-crested weir, **no breach regression** | Fread (1988) |
| Loss and damage | depth-damage curves + velocity aggravation | Huizinga et al. (2017) JRC EUR 28552 EN; Clausen & Clark (1990) |

Then the solver itself: 2D shallow water, HLL Riemann, Audusse well-balanced hydrostatic
reconstruction. Cite Harten–Lax–van Leer (1983), Audusse et al. (2004), Toro (2001). State that the
inertial (LISFLOOD-FP) scheme is also implemented and was measured **60 % slow on the front
position** against Ritter, which is why the default is the full equations. That measurement is the
honest version of "we chose a scheme".

**SPH:** DualSPHysics on GPU, coupled by `runner.splice_sph_hydrograph`, agrees with the weir
equation to 5 %. Say plainly that it is near-field only, roughly the first 60 s.

**Delft3D:** see section 8. Do not claim it here.

## 4 · Deliverable (ii) — customisable scenarios and datasets

`RunRequest`'s parameters, and the DEM providers: COP30, SRTM, **ASTER GDEM v3**, NASADEM, ALOS,
FABDEM, CartoDEM. NTRO's dataset link names ASTER *and* SRTM specifically and both are supported by
name — say so explicitly, it is a direct hit on their wording.

Satellite imagery feeds the **model**, not just the validation: ESA WorldCover becomes per-cell
Manning *n* in `modules/01_geodata/roughness.py`. That distinction is worth a sentence, because
"we used satellite data" usually means "we drew a picture with it".

## 5 · Deliverable (iii) — the dashboard

Input, output, the node graph, the 3D scene, and the exports (.shp, .kml, GeoJSON — all verified
live). Two facts worth their own lines:

- **Zero runtime dependencies, no build step.** Babylon.js is vendored and served by our own
  backend; the console renders with the network unplugged.
- **"Supports large volumes of data" is measured, not asserted:** 542,970 cells at 128 MB peak,
  and the browser downloads a **7.2 KB** texture for the whole time-varying flood.
  Source `docs/LOAD_TEST.md` — note in the report that its timings predate the windowed sweep and
  are now an upper bound.

## 6 · Deliverable (iv) — near-real-time via Google Earth Engine

Both directions, which is the part people miss:

- **out:** Sentinel-1 GRD change detection → observed flood extent → CSI/POD/FAR
- **in:** CHIRPS rainfall → SCS curve-number runoff → reservoir inflow nowcast

State the limitation with the capability: no **observed** inflow series has been obtained, so the
hydrological input is modelled rather than measured.

## 7 · Deliverable (v) — any Indian river and dam

The CWC register (5,686 dams, 29 states), the river index (2,229 rivers, 3,109 points, grouped on
name **and basin** — explain the Ghataprabha collision, it is a good paragraph), and D8 tracing,
which is what makes "any river" true: no pre-built river network is needed.

Proven on sites never run before — **Hirakud** and **Annamayya**, both first try, both 0.000 % mass
error.

## 8 · Verification, convergence and validation — three different questions

Give each its own subsection and do not let them blur.

**8.1 Verification — strong.** Ritter RMSE 0.218 m, lake-at-rest 2.9 × 10⁻⁶ m, mass 0.000 %,
23/23 integration checks, 23 solver physics tests.

**8.2 Grid convergence — measured.** Depth converges by 60 m; 90 m carries 3–6 % of pure
discretisation error, which is why the default moved. Area converges more slowly and in a gorge not
at all. `docs/CONVERGENCE.md`.

**8.3 Validation — weak, and published anyway.** CSI 0.0075 in the gorge, **0.0314** on the
floodplain with bias **7.22** (60 m, no-data excluded — reconcile against the two superseded figures
using `docs/VALIDATION.md` §2.2, and use only the headline). Explain the scenario mismatch: full-reservoir worst case at 24 h maximum
extent against one satellite pass days later. **State that no strong observational validation
exists and none is claimed.**

**8.3a Breach severity inversion — a negative result worth a page.** `docs/SEVERITY_INVERSION.md`.
Four severities (100/75/50/25% release), everything else identical. Bias falls monotonically
7.22 → 1.01, so the over-prediction is the full-reservoir assumption and not the hydraulics — a
clean sensitivity result. But the severity that matches the area overlaps the observation on **2%**
of its cells, and the observed mask is **732 disconnected components** where a flood would be one
corridor (the simulation puts 99.9% of its water in a single component). Write it as what it is:
inverse estimation that came back *unidentifiable*, and evidence that this Sentinel-1 composite is
not usable as a flood-extent reference. **It is not an accuracy figure and no cell of that table may
be quoted as one.**

**8.4 Cross-checks that are not validation.** SFINCS at CSI 0.9653, SPH within 5 % of the weir
equation, the surrogate at CSI 0.909 against our own solver. Label each as what it is.

## 9 · Delft3D — status, precisely

Write this carefully; it is the one incomplete deliverable and the report is where a judge will
look for excuses.

- Delft3D 4 (Delft3D-FLOW) is **GPLv3 and free**; Deltares ships kernels as source.
- We **compiled it from source**, and Deltares' own `f34` example runs on the result.
- A case generated by `modules/03_delft3d/case.py` **solves on it** and reads back with the bed
  matching our DEM to within a centimetre.
- The **engine comparison does not run yet** — on a reach with 277 m of relief the model aborts on
  the first timestep, a setup problem in initial and boundary conditions that we did not finish.
- The **FM suite** is a separate product whose licence we requested and never received.

Do not write "licence not granted" as the headline reason. It is true of FM and false of the model
the statement names, and the difference is exactly the kind of thing a reviewer checks.

## 10 · Limitations

Reproduce `AGENTS.md` Part 4 in full. Every row has a number and a file. Resist the urge to soften
any of them; a limitations section that reads as confident is worth more than one that reads as
apologetic.

## 11 · What would make this genuinely validated

Section 6 of `docs/VALIDATION.md`, in order of value: a Copernicus EMS rapid-mapping extent, a
documented dam-break event with known breach parameters, an observed inflow series from
India-WRIS, a hydraulics engineer's review, and Delft3D finished. Each names what it needs from
outside the project.

## 12 · Reproducing everything

Every command, so a marker can re-run the report:

```
integration/run_all.py                      23/23
pytest modules/04_backend/tests             23 tests
shared/validate outputs/<run_id>            the definition of done
integration/compare_routing.py              SFINCS cross-check
integration/grid_convergence.py             the convergence study
```

## Appendices

- **A** — full `NTRO_REQUIREMENTS.md` deliverable mapping
- **B** — the data contract: file-by-file, units, CRS
- **C** — sources: every dataset with its licence (`data/SOURCES.md`)
- **D** — adversarial Q&A (`docs/QA.md`)

---

## Rules for whoever writes this

1. **Every number names its file.** If you cannot point at it, cut it.
2. **Bad results get the same font size as good ones.** CSI 0.027 is in the report at full size.
3. **No accuracy percentage exists.** Do not create one.
4. **An assumption is labelled an assumption** — damage replacement values especially.
5. When you are unsure whether something is a claim we can support, ask the captain. "We did not
   get to that" is a complete sentence and costs almost nothing.
