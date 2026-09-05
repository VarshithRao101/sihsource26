# Demo runbook

Written so that someone who has never run this project can execute the demo cold, while nervous, on
a network they do not trust.

**Read the failure paths (section 5) before demo day, not during it.** On 2026-09-01 an
OpenTopography fetch timed out mid-run during ordinary development. It will happen on stage too.

---

## 1. The evening before

Run these in order. Every one must pass before you sleep.

```bash
.venv\Scripts\python.exe -m shared.creds
```
Expect: `all tier-1 credentials present`. If anything is missing, nothing below will work.

```bash
.venv\Scripts\python.exe integration\run_all.py
```
Expect: **23/23 passed**, about twenty seconds, and it works with the network unplugged. If this
does not pass, you do not have a demo — fix it or fall back to a recorded video.

```bash
.venv\Scripts\python.exe -m modules.03_delft3d.engine
```
Expect: `NOT INSTALLED`. That is the correct answer today. You are running it so you are not
surprised by the answer on stage.

```bash
.venv\Scripts\python.exe -m shared.validate outputs\chungthangdam_overtop_fast_002
```
Expect: **PASS - 0 error(s), 0 warning(s)**.

### Pre-cache terrain for the dams you might be asked about

This is the single most important preparation step. A dam whose DEM is already on disk runs
offline; a new one needs OpenTopography and can fail.

Pick four or five plausible dams — large, famous, one per region — and run each once the night
before. They will then be cached in `data/dem/` and `data/exposure/`.

```bash
.venv\Scripts\python.exe -m modules.01_geodata.dams search --state Odisha --limit 5
```

---

## 2. Starting the system

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

Console at <http://localhost:8000>, API docs at `/docs`.

**Wait for startup to finish before you touch anything.** The backend deliberately pays two costs at
boot so a juror never watches you pay them: the numba JIT compile, and loading the U-Net checkpoint
into CUDA. Both are reported in the startup log. If you demo the what-if slider before the surrogate
has warmed, the first drag takes 2–5 seconds instead of 20 ms.

**Cold-start budget:** measure this yourself with a stopwatch the night before and write the number
here → `________ seconds from double-click to a flood on screen`.

---

## 3. The runs that are already on disk

All seven validate clean. These are your safety net — every one works with the network unplugged.

| Run | Site | Scenario | Why you would show it |
|---|---|---|---|
| `chungthangdam_overtop_fast_002` | Chungthang, Sikkim | dam break | The primary demo. Full impact, evacuation, SAR validation |
| `latatapovanntpc_blockage_fast_001` | Dhauliganga, Uttarakhand | **river blockage** | The Feb 2021 Chamoli reach — NTRO's own first example |
| `cheyyeruprojectannamayya_gated_fast_001` | Cheyyeru, Andhra Pradesh | **water release** | Controlled gate release, not a failure |
| `cheyyeruprojectannamayya_overtop_fast_001` | Cheyyeru, Andhra Pradesh | dam break | Same dam as above — the scenario comparison |
| `hirakud_overtop_fast_001` | Mahanadi, Odisha | dam break | An unseen river, ran first try |
| `teesta_blockage_fast_002` | Teesta, Sikkim | river blockage | Second blockage case |
| `teesta_overtop_fast_051` | Teesta, Sikkim | dam break | Carries the original SAR validation |

Check any of them:

```bash
.venv\Scripts\python.exe -m shared.validate outputs\latatapovanntpc_blockage_fast_001
```

### The four the console cycles

**Load a stored run** on the console cycles four curated runs, one per click, and tells you which
is next before you press it. They are real runs on COP30 through the same code path as anything
you solve live — not recordings — and each one is there because it shows something the other
three cannot:

| Click | Run | Shows |
|---|---|---|
| 1 | Machchhu II, 1979 — overtopping | A real Indian dam break with a **documented outcome**. The one case in the set where "is this right" has an answer — see [`HISTORICAL_VALIDATION.md`](HISTORICAL_VALIDATION.md) |
| 2 | Lower Manair — spillway blocked | The **time to overtop**: 35.8 hours between the outlets failing and the first water over the crest. No other case produces that number |
| 3 | South Lhonak, 2023 — moraine outburst | A natural dam with no published storage. Also the honest bit: the DEM finds 0.34 MCM behind that moraine against ~25.7 MCM that actually drained, and `meta.json` publishes both |
| 4 | Idukki — foundation failure | A 169 m arch dam, the one class of structure the breach regressions **do not describe**. `meta.json` says no regression was applied |

Rebuild them on a machine with network access:

```bash
.venv\Scripts\python.exe -u -m integration.build_demo_runs
```

The manifest (`data/demo_runs.json`) is committed; the run folders are not, because they are large
and regenerable. `GET /api/demo-runs` returns only what is actually on disk, so on a fresh clone the
button falls back to loading the newest run rather than offering four ids that 404.

---

## 4. The demo, in order

Rehearse until it is boring. Eight minutes.

**0:00 — Open on the Workflow page, not the console.**
<http://localhost:8000/workflow>. Twenty boxes, arrows between them, every one a real stage in
this repository. Take fifteen seconds to say what the shape is — dam register, trace the river,
condition the terrain, find who lives downstream, size the breach, solve the water, write the
grids, cost the damage, plan the evacuation, publish the uncertainty, validate. Then point at the
grey dashed box at the bottom.

> **"Delft3D is named in the problem statement and we do not have the kernel. To be precise about
> why: Delft3D 4, the structured model this statement means, is GPLv3 and free — but Deltares ships
> it as source, so it is a compile we did not do. The FM suite is the one that needs a licence, and
> ours was never answered. That box is a filesystem probe. It lists the paths it searched and it
> never turns green. The Deltares model above it is SFINCS, which is a different model, and we
> label it as one."**
>
> Say this in the first minute, before anyone asks. It buys you the rest of the room.

Click one box — the solver — so they see it expand into what that stage takes in, puts out, which
file does it, and the papers behind it. Do not click more than two; the point is that the detail
is there, not to read it out.

**0:45 — "Name any dam in India." Then press PLAY.**
Open with the question nobody else in the room can take. Use the dam picker: State → dam, from the
CWC National Register of Large Dams, 5,686 entries, plus 63 natural dams. PLAY runs the real
pipeline: each box turns
amber then green as its stage actually finishes, carrying the backend's own words — *"breach 268 m
wide in 5.45 hr, peak 33,865 m³/s"*, *"58 settlements, 363 road segments"*. The solver box streams
simulated time, wet cells and the volume ledger while it works.

If someone asks whether the animation is real, press **PAUSE**. The percentage stops moving,
because the solver thread is blocked between timesteps. Press it again and it carries on.

Then switch to the console for the map. Or stay here and let the 3D scene replay the flood over the
conditioned DEM — but say what it is: *"the ground is the real DEM; the water surface is
reconstructed from the arrival-time, peak-time, depth and duration grids, coloured by velocity.
It is a rendering of solver output, not frame-by-frame solver output."* That sentence is printed on
the page too, so you cannot forget it.

> Say "nearest city", never "district". NRLD has no district column, and a wrong district in front
> of a district administrator is worse than no district.

**2:00 — On the console, point at the water.**
Hover anywhere on the flood: depth, speed and hazard class for that cell, read out of that run's
GeoTIFFs by the API — the hazard class is computed server side from `shared/contract.py`, so the
browser is not carrying a second copy of the thresholds. The drifting streaks are the arrival-time
gradient, which is the direction the front actually travelled, at the speed `max_velocity.tif`
recorded there. Nothing on that map is decorative.

Point at the mass-balance figure while it solves. A hydrologist looks for that number first, and
watching it hold at zero while water moves is worth more than any animation.

**3:00 — The impact table and evacuation routes.**
This is the Humanitarian Assistance and Disaster Relief answer, and it is what NTRO's problem
statement is actually about. Named villages, arrival times, roads cut, walking routes with margins.
Red routes mean **no safe route on foot — those people need helicopters.**

Say the population source out loud. Some are WorldPop measurements, some are class defaults, and the
table says which.

**4:00 — Drag the what-if slider.**
The U-Net surrogate answers in about 20 ms, roughly 900× faster than the solver.

> Say this every single time, without being asked: **"this is a neural network emulating our own
> solver, not a simulation, and it has never been validated against a real flood."** The response
> carries `is_emulated: true` and a warning string, and both are on screen.

**5:00 — Switch to the Chamoli blockage.**
`latatapovanntpc_blockage_fast_001`. A landslide dam on the Dhauliganga, the reach destroyed in
February 2021 — the first event named in NTRO's own background. The lake impounds 6.32 MCM behind
debris, breaches 280 m wide in 8.5 minutes, and reaches the first village in **half a minute**.

Four of the five failures NTRO names are natural dams, not engineered ones. Say so.

**6:00 — The same dam, three ways.**
Annamayya: dam break 11,325 m³/s · gates fully open 8,069 m³/s · gates at 25% 2,609 m³/s. Same
water, a quarter of the peak. That is what a controlled release buys an operator, and it is the
"dam break **or water release**" clause of the problem statement.

**6:30 — Engine comparison, including what is missing.**

```bash
.venv\Scripts\python.exe integration\compare_engines.py --capacity 5 --height 60
```

Our solver, the weir equation, DualSPHysics on GPU, four empirical regressions — and Delft3D
reported absent, never estimated. **Volunteer the gap before anyone asks for it.**

**7:30 — Export.**

```bash
.venv\Scripts\python.exe -m shared.validate outputs\<run_id>
```

Then the KML / Shapefile / GeoJSON buttons. Open the KML in Google Earth if there is a screen for it.
That closes deliverable (iii) in ten seconds.

---

## 5. When it goes wrong

### The network is dead, or OpenTopography times out

**This has already happened once in development.** Symptom: the run fails with
`ConnectionError: HTTPSConnectionPool(host='portal.opentopography.org' ...)`.

The system deliberately does **not** fall back to synthetic terrain, because a run claiming
`dem.source = COP30` must actually be COP30. So it fails loudly instead of lying quietly.

**What you say, out loud, while you switch:**

> "That is the DEM download timing out — we deliberately refuse to substitute fake terrain, because
> a result that claims COP30 has to be COP30. Here is the same pipeline on a dam we cached earlier."

Then load a cached run from section 3. **Do not retry the live run on stage.**

### The basemap does not load

The console falls back to OSM raster tiles when `MAPTILER_API_KEY` is absent or unreachable. Verify
this yourself by pulling the network cable before demo day. Conference wifi is not a dependency this
project accepts.

### Overpass returns nothing

The flood map still renders; the impact table is empty. The exposure layer refuses to cache an empty
download, so it is a transient, not a corruption. Load a cached site instead.

### The surrogate is slow on the first drag

You demoed before startup finished. Say "still warming up", wait, drag again.

### A run fails validation

```bash
.venv\Scripts\python.exe -m shared.validate outputs\<run_id>
```

A run that does not pass does not exist. Do not show it. Load a cached run.

---

## 6. Things to say before you are asked

These are strengths, not confessions. Full detail in `docs/QA.md` and `AGENTS.md` Part 4.

- There is **no accuracy percentage** anywhere in this project, because we never measured one.
- The terrain is 30 m and we have no survey of the riverbed under the water.
- Breach parameter regressions disagree with each other by a factor of four on the same dam. We show
  all of them and average none of them.
- Our satellite validation is **weak on both reaches we tried**, and we publish the numbers anyway.
- Delft3D is **not installed** — a kernel we did not compile, not a door that was shut on us. The
  free Delft3D 4 ships as source; the FM licence we asked for was never answered.
- The ML surrogate emulates **our own solver**, not reality.

---

## 7. What must be true before you walk on stage

- [ ] `run_all.py` prints 23/23
- [ ] Backend started and fully warmed
- [ ] Four or five DEMs pre-cached for likely dams
- [ ] All seven cached runs validate PASS
- [ ] Network cable pulled once, end to end, and it still worked
- [ ] Cold-start time measured and written down
- [ ] You have said the limitations out loud, to a person, at least once
