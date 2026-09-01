# FRONTEND B — Panels, Tables, Forms, Reports

| | |
|---|---|
| **You own** | `modules/05_frontend/src/panels/`, `src/views/` |
| **You may read** | everything, including `src/api.ts` and `src/types.ts` |
| **You may NOT edit** | `src/map/`, `src/scene/`, `src/api.ts`, `src/types.ts` — Frontend A owns those |
| **Need an API change?** | Ask Frontend A. Need a backend change? Ask the captain. |

Read `AGENTS.md` first, completely. Especially Part 1 (honesty) and Part 4 (limitations).

---

## PROTOCOL — follow this literally

1. Split the work below into 6–10 small, independently runnable parts. Show the numbered list.
   **Then stop.**
2. Build **Part 1 only**. Show it running. **Then stop and wait for "next".**
3. Before each part, ask what data, keys or endpoints you need, and wait for the answer.

---

## Your job

Everything that is not the map. This is the half a district officer actually reads.

### `[P0]` Scenario form

Drives `POST /api/runs`. Fields: `dam_id` (from the picker), `failure_mode`, `breach_regression`,
`reservoir_level_frac`, `reach_length_km`, `cellsize_m`, `end_hr`, `scheme`, `real_terrain`.
`GET /api/enums` returns the valid values — read them, do not hardcode them.

### `[P0]` The dam picker

Cascading **State → Nearest city → Dam** over the CWC National Register, 5,686 dams:

- `GET /api/dams/states`
- `GET /api/dams/cities?state=`
- `GET /api/dams?state=&city=&q=&limit=`

Selecting a dam fills the form from the register. **Label the middle filter "Nearest city", not
"District"** — NRLD has no district column, and a wrong district in front of a district
administrator is worse than none at all.

### `[P0]` Impact table

From `GET /api/runs/{run_id}` → `impact.json`. Columns: settlement, arrival hours, depth, velocity,
population, hazard class. **Sort by arrival time ascending** — the village with the least warning is
the one that matters, so it belongs at the top.

Show `population_source` beside the population. Most currently read `class_default`, meaning OSM had
no population tag and we substituted a class median. That must be visible, not hidden.

### `[P0]` Results and provenance

`meta.results` and `meta.dem`. Show `mass_balance_err_pct` prominently — a hydrologist looks for it
first. Show `dem.conditioning`, which is a full sentence describing exactly what we did to the
terrain and why.

### `[P1]` The uncertainty panel

`GET /api/runs/{run_id}/uncertainty`. Four independent peak-outflow regressions that disagree by up
to 10×, plus our routed peak. **Present the spread as the answer, not as a defect.** Do not average
them into a single number — that produces a fifth value none of the four methods supports.

### `[P1]` Evacuation table

`GET /api/runs/{run_id}/evacuation`: road-floods-at, walk time, **margin**, route km, status.
Margin is the number people act on. Under one hour should look alarming.

### `[P1]` Export

`GET /api/runs/{run_id}/export?format=kml|shp|geojson`. KML opens in Google Earth, which is what a
district office actually has on the machine.

### `[P2]` Comparison view

`GET /api/compare?run_ids=a,b` — two scenarios side by side.

### `[P2]` Printable report

One page: map thumbnail, impact table, uncertainty, limitations. This is the artefact that leaves
the room. Coordinate with Docs A and Docs B so it matches the deck.

---

## Rules

- **Never display a number the API did not send.** Blank beats invented.
- Every money figure appears with its `damage_curve_source`. The backend validator enforces that
  pairing; do not break it in the UI.
- Anything from `/api/surrogate` is labelled a prediction, never a simulation.
- No new dependency without telling the captain.

## Done means

Pick a dam from a dropdown, press Run, watch progress, read the impact table, export a KML — all
without touching the map code.
