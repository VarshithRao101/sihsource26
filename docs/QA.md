# Adversarial Q&A

Every question a juror who models dams for a living is likely to ask, the honest answer, and **the
file the answer comes from**. If you cannot point at the file, do not say the number.

The rule that outranks the rest: **"we didn't get to that" is a correct answer.** It costs almost
nothing. A confident wrong answer to an NTRO hydrologist costs the round.

---

## The five they will definitely ask

### "What is the accuracy of your model?"

There is no accuracy percentage anywhere in this project, because we never measured one. What we
have is **verification** — proof we solve the equations correctly — and **validation** against
observed floods that is honestly weak.

Verification, all from `integration/run_all.py`:

| Check | Result |
|---|---|
| Ritter analytical dam break | RMSE **0.218 m** over the rarefaction |
| Lake at rest (well-balancedness) | **2.9e-06 m** spurious velocity |
| Closed-basin mass conservation | **0.000%** |
| Real channel, dam break | **0.000%** |

Validation is in `docs/VALIDATION.md` and the answer there is uncomfortable. We publish it anyway.

> **Why this is the strong answer:** anyone quoting "95% accurate" for a dam break on 30 m terrain
> with no bathymetry is inventing a number. The juror knows that.

---

### "Your satellite validation CSI is 0.027. That's nothing."

Correct, and we say so first. Full detail in `docs/VALIDATION.md`.

We tried two reaches. In the **Teesta gorge** CSI never exceeded 0.02 at any slope threshold, because
median terrain slope is 30° and the flood corridor is one to three cells wide at 90 m — Sentinel-1
physically cannot resolve it. The sensitivity sweep is published in the run's `validation.json`.

So we did the correct thing rather than the flattering one and re-validated on a **low-gradient
reach**: the real Annamayya earthfill failure of 19 November 2021 in Andhra Pradesh, 72 km² wet
against the gorge's 10 km².

| | CSI | POD | bias |
|---|---|---|---|
| Teesta gorge | 0.0075 | 0.013 | 0.70 |
| Annamayya floodplain | 0.0314 | **0.250** | **7.22** |

Detection improved **nineteenfold**, which confirms the gorge failure was a resolution problem. It
does **not** rescue the validation: bias 7.22 means 19,866 simulated wet cells against 2,752
observed. We are comparing a full-reservoir worst case at maximum extent over 24 hours against one
satellite pass days after the event, with the real breach severity unknown.

**We did not tune anything to improve this.** We have no strong observational validation and we do
not claim one.

---

### "Why is Delft3D missing? The problem statement names it."

Because we did not build the kernel. It is a build, not a permission wall — and those are different
answers, so we give the accurate one.

`modules/03_delft3d/engine.py` — the absence is **measured, not asserted**. It searches PATH,
`$DELFT3D_HOME`, beside the repo, and the Deltares-named install folders, and reports what it
actually found:

```bash
.venv\Scripts\python.exe -m modules.03_delft3d.engine
```

Deltares ships three separate downloads and only one solves anything. We have the **licence
manager** (`DS_Flex.exe`, `lmadmin`) — which is not a solver. The **D-Flow FM kernel** requires a
licence file Deltares issues on request; we requested one and had no reply.

**"Delft3D" is two products, and only one of them needs that licence.** Say this before they say it:

| | licence | how you get the kernel |
|---|---|---|
| **Delft3D 4** (Delft3D-FLOW, structured) — the model this statement means | **none, GPLv3** | source only: `d_hydro` + `flow2d3d` are compiled from `github.com/Deltares/Delft3D`, config `d3d4-suite` |
| **Delft3D FM** (D-Flow Flexible Mesh) | required, **we asked and had no reply** | precompiled, behind the licence |

So the honest answer is **not** "Deltares wouldn't let us." It is: the model NTRO names is free and we
did not spend the time compiling it — Intel oneAPI in a Docker devcontainer, against a deadline that
had a working solver in it already. The detector reports both kernels and says which one it found.

`integration/compare_engines.py` prints Delft3D as absent on every run, and a gate check
(`03_delft3d absence is measured`) means we cannot accidentally claim otherwise.

> If asked "so you didn't do it?" — **"No. The FM licence we asked for never came, and the free one
> we'd have had to compile ourselves. We didn't."** Do not dress it up, and do not let it come out
> as "Deltares refused us" — that would be a better story and a false one.

---

### "Where do your population numbers come from?"

`modules/01_geodata/exposure.py`, and each settlement carries its own `population_source` in
`impact.json`, shown in the table:

- `osm:population` — a real OSM census tag. Left untouched.
- `worldpop2020` — measured from WorldPop 2020 constrained 100 m. Every mapped cell is assigned to
  its **nearest** settlement within 2 km, so each person is counted exactly once.
- `class_default` — OSM had no population tag and WorldPop's constrained product has no mapped
  built-up area within 2 km. We substitute a class median and label it.

For Teesta: 16 of 22 settlements are WorldPop measurements, 2 keep real OSM census tags, and 4 —
including Chungthang itself — stay class defaults because WorldPop is blank there. Only 13.8% of
that tile has any data at all; the constrained product only puts people where buildings were
detected, and high-altitude Sikkim villages are poorly mapped.

That gap is real and we leave it visible rather than filling it with a guess.

---

### "Is your ML a simulation?"

No. `modules/07_ml/surrogate.py` is a U-Net that **emulates our own solver**, at CSI 0.909 against
it, in about 20 ms — roughly 900× faster. It has never been validated against a real flood.

The API response carries `is_emulated: true` and a warning string, and the console shows both
whenever surrogate output is on screen.

---

## Physics and method

### "What breach parameter formula did you use?"

All of them, and we average none. `shared/hydro.py`:

- Froehlich (2008), ASCE J. Hydraul. Eng. 134(12) — the primary, fitted to 74 documented failures
- Von Thun & Gillette (1990)
- MacDonald & Langridge-Monopolis (1984)

Peak-outflow regressions from Costa (1985), Froehlich (1995), Hagen (1982), USBR (1982) are computed
independently of our hydraulics.

On a 5 MCM / 60 m dam those regressions span **9,377 – 37,206 m³/s, a 4.0× spread**, and the console
draws that spread as a chart with our routed peak inside it. `uncertainty.json` carries the numbers.

> The spread **is** the answer. A single confident number would be the wrong deliverable.

### "What solver scheme?"

2D shallow water, HLL Riemann solver, Audusse well-balanced reconstruction, with an inertial option.
`modules/04_backend/solver.py`.

### "How do you handle the reservoir storage curve?"

We assume `V(h) = V_full · (h/H)^k` with k = 2.7, typical of a steep valley impoundment. **This is an
assumption and it is labelled one** — in the docstring, in `meta.json` under `scenario.storage_curve`,
and it is one of the parameters the Monte Carlo in `modules/07_ml/montecarlo.py` perturbs. There is
no surveyed storage-elevation curve for an arbitrary Indian dam.

### "What about the riverbed under the water?"

We have no measurement of it. COP30 sees the water surface, not the bed. `meta.json` records
`dem.bathymetry = "estimated"` and `dem.conditioning` is a full sentence describing exactly what we
did to the terrain and why.

### "Is river blockage really different, or did you relabel a dam break?"

Genuinely different, and the difference is where the numbers come from.
`modules/04_backend/blockage.py`: nobody published the storage of a landslide dam, so we read it off
the DEM; the fill time comes from upstream inflow; and the breach uses **natural-dam** regressions
(Peng & Zhang 2012, failure mode per Costa & Schuster 1988), which give a wider, faster breach than
an engineered embankment of the same height.

At Lata Tapovan the debris impounds 6.32 MCM over 0.44 km², breaches 280.3 m wide in 8.5 minutes,
and the flood reaches the first village in **half a minute**.

### "Is 'water release' really different from a dam break?"

Yes, and it used to not be — we found and fixed that. A release is not a failure: the structure stays
intact and **no breach regression is used at all**. `shared/hydro.py::gated_release_hydrograph`:

```
gate      Q = Cd A sqrt(2 g (y - y_invert))    orifice, Fread (1988)
spillway  Q = C L (y - y_crest)^1.5            broad-crested weir
```

Where the capacity comes from is recorded in `meta.json` → `gated_release.capacity_source`. When the
dam is in the CWC register we use its **measured design spillway capacity**; without one we size the
outlet to draw down in 24 h and label the block `ASSUMED`.

Annamayya, same dam, same water:

| Scenario | Peak |
|---|---|
| Dam break, overtopping | 11,325 m³/s |
| Gates fully open | 8,069 m³/s |
| Gates at 25% | 2,609 m³/s |

### "Why no GNN for evacuation routing?"

It would need labelled evacuation outcomes — real routes, real timings — which do not exist for
Indian dam breaks. `modules/07_ml/evacuation.py` uses an exact time-dependent Dijkstra instead. We do
not need to *learn* a shortest safe path when we can *compute* it optimally.

### "Why no LSTM for inflow?"

`modules/07_ml/inflow.py` raises `NotImplementedError` with the reason in its docstring. An inflow
LSTM needs an observed inflow series to learn from. Training it on our own runoff model's output
would teach it our model and let us call the result learned knowledge. That is circular. The
physics-based nowcast (CHIRPS rainfall → SCS runoff → routing) is there instead.

**This is a data-acquisition blocker, not a coding one.** Give us a daily India-WRIS series and it
becomes a real supervised problem in an afternoon.

---

## Engineering and scope

### "Can it really run on any Indian dam, or just your demos?"

5,686 dams from the CWC National Register of Large Dams, parsed from the source PDF by
`modules/01_geodata/dams.py`. Demonstrated on dams we had never run before: **Hirakud** on the
Mahanadi and **Annamayya** in Andhra Pradesh, both first try, both 0.000% mass error.

It needs internet for a genuinely new dam, because the DEM must be fetched and we refuse to
substitute synthetic terrain.

### "How large a dataset can the dashboard handle?"

We have not load-tested this, and it is an explicit clause in deliverable (iii). The largest grid we
have run is 437 × 343 ≈ 150,000 cells. The browser receives the whole time-varying flood as a single
RGBA texture (`packed.png`) rather than per-frame rasters, which is what keeps it responsive — but we
have not measured a ceiling and will not claim one.

### "What happens if a module fails mid-run?"

Depends which, and the behaviour is deliberate:

- **DEM fetch fails** → the run fails loudly. No silent synthetic fallback, because a run claiming
  COP30 must be COP30.
- **Overpass fails** → the flood map still renders without settlement names, and the empty result is
  never cached.
- **Surrogate unavailable** → the endpoint reports it honestly; the solver is unaffected.

### "How do you know a result is valid?"

```bash
.venv\Scripts\python.exe -m shared.validate outputs\<run_id>
```

`shared/validate.py` is the definition of done — grid shapes, CRS, units, mass balance, volume
sanity, fake-data flags. **A run that does not pass does not exist.** All seven runs currently on
disk pass with zero errors and zero warnings.

---

## The awkward ones

### "Your Hirakud peak is 265,799 m³/s. Is that credible?"

It is inside the empirical envelope (38,315 – 380,296 m³/s) but near the top of it, and **it has not
been reviewed by a practising hydraulics engineer.** We flag that rather than defend the number.

### "Have you validated the damage estimates?"

No. The depth-damage curves are real — Huizinga et al. (2017), JRC EUR 28552 EN, Asia continental
functions, with velocity aggravation from Clausen & Clark (1990). The **replacement values** are
project assumptions for rural India, are not measured, and `impact.json` says so in
`damage_curve_source`.

### "Why is your team using AI/ML when the problem statement doesn't ask for it?"

It doesn't, and we checked — the official text (`docs/PROBLEM_STATEMENT.md`) contains no requirement
for ML, AI or quantum computing. The four models are a bonus: an XGBoost damage model, Monte Carlo
with GP uncertainty, the U-Net surrogate, and a Sentinel-1 water classifier. We present them as extra,
never as a deliverable, and we declined to build two more (GNN, LSTM) rather than train them on our
own outputs.

### "What would you do with another month?"

Honest answers, in order: obtain a Delft3D licence and complete the engine comparison; get an
observed inflow series and make the LSTM a real supervised problem; validate against a Copernicus EMS
rapid-mapping extent instead of a raw Sentinel-1 threshold; and have a hydraulics engineer review the
Hirakud result.

---

## What we deliberately did NOT build, and why

Say these out loud if the topic comes up. They are strengths.

| Not built | Why |
|---|---|
| GNN for evacuation routing | No labelled evacuation outcomes exist. Dijkstra computes the optimum exactly |
| LSTM for inflow | No observed inflow series. Training on our own model output would be circular |
| A tuned SAR threshold | The gorge result is inconclusive; tuning until the number flatters us would be dishonest |
| An accuracy percentage | We never measured one |
