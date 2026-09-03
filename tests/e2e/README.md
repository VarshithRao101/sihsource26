# End-to-end walkthrough

The complete user journey, executed and checked: open **Workflow**, press **PLAY**, watch the real
backend run, watch every node change state, pause the solver for real, read the final result — and
compare every number on screen against the API that produced it.

It is also the demo script. `npm run demo` runs the same assertions with a visible browser and a
slow-motion delay, so the thing being shown to a juror is the thing being checked.

```bash
cd tests/e2e
npm install          # @playwright/test only; Chromium is already on this machine
npm test             # headless, the whole journey
npm run demo         # headed and slowed down, for showing somebody
npx playwright show-report
```

The backend is started for you if it is not already running (`playwright.config.js` → `webServer`).
It takes about twenty seconds to boot because it warms the solver JIT and loads the ML surrogate
before serving anything.

**This is a dev dependency and it lives outside `modules/05_frontend` on purpose.** The shipped
frontend still has zero runtime dependencies and no build step; nothing in this folder changes
that. `node_modules/` is gitignored.

---

## What it runs, and why

| Spec | Test | What it proves |
|---|---|---|
| `workflow` | the pipeline manifest describes real stages | Every box names code and explains itself; every arrow joins two boxes that exist |
| `workflow` | Delft3D is reported from a probe and never claimed | The absent engine reports the paths it searched, and SFINCS is never presented as Delft3D |
| `workflow` | the page draws one box per stage, all waiting | The graph is generated from `/api/pipeline`, not drawn by hand |
| `workflow` | clicking a box expands it | The detail is the stage's real inputs, outputs, code path and sources |
| `workflow` | hovering a box previews it | Hover gives the summary without opening the drawer |
| `workflow` | **PLAY executes the real pipeline** | POST `/api/runs` is observed on the wire; the solve node genuinely enters RUNNING; every node's WAITING → RUNNING → COMPLETE sequence is recorded from the API and checked; nothing is left hanging |
| `workflow` | the final node shows the real result | Flood area, depth, velocity, settlements and damage on screen are the run folder's own values |
| `workflow` | every stage declares the dependencies it really has | Each node's libraries, datasets, services and engines are spot-checked against what the code imports and calls |
| `workflow` | a box shows its counts and expands to the full list | The supply chain is visible on the card and grouped in the drawer; Delft3D is flagged red before anyone clicks |
| `workflow` | the page is the graph and nothing else | No 3D pane, no API link, all the controls present, engine probe log on screen |
| `workflow` | the finished run fills the metric strip | Flooded area, depth and velocity on screen equal the run folder's values |
| `workflow` | **PAUSE holds the solver and RESET stops it** | `pct` advances by less than 2 % over nine paused seconds, moves again on resume, and RESET marks the run cancelled |
| `console` | the form asks only for what an operator decides | Demo sites and the advanced physics drawer are gone; the fields somebody fills in are still there |
| `console` | the form follows the failure mode | Gate opening, debris height and reservoir level appear and disappear correctly |
| `console` | **pointing at the flood reports depth, speed and risk** | The hover values equal `/api/runs/{id}/probe`, and the hazard class comes from `shared.contract` server side |
| `console` | the flow streaks follow measured direction and speed | Every sampled particle is moving towards ground the water reached *later* — the arrival-time gradient, not decoration |
| `console` | the time slider replays the flood spreading | More water at t = end than near t = 0 |
| `console` | the run's honesty flags reach the screen | DEM source, conditioning, failure mode, uncertainty block, and the synthetic banner tied to `meta.is_fake` |
| `console` | the outputs export as .shp and .kml | Both formats download and parse; GeoJSON is a valid FeatureCollection |
| `console` | the run is contract-valid | `shared.validate` passes on the run everything above was read from |

---

## The scenario it uses

`fixtures.js` pins one dam, for a reason: **Lower Manair Dam** (`TL47HH0065`) on the Manair in
Telangana is in the CWC register with a height and a gross storage, and this repository already
caches its scouting DEM, its conditioned DEM **at a 30 km reach**, and its OSM exposure under
`data/`. So the run exercises the whole pipeline — river trace, terrain, exposure, breach, solver,
grids, damage, evacuation, uncertainty, validator — without waiting on a download while somebody
watches.

Change `REACH_KM` and the DEM cache misses and the test starts downloading. That is not a bug, but
it is slow, which is why it is written down.

## Notes

- The suite is serial. Two of the tests start real solves, and the API registry is in-process.
- `requestAnimationFrame` is throttled in a background tab, so the flow-streak test drives
  `flowStep()` directly and then asserts on what it drew.
