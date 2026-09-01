# FRONTEND A — Map, Scene, Timeline

| | |
|---|---|
| **You own** | `modules/05_frontend/src/map/`, `src/scene/`, `src/api.ts`, `src/types.ts` |
| **You may read** | everything. **You may write** only in the paths above. |
| **Frontend B owns** | `src/panels/`, `src/views/` — do not edit those. They may not edit yours. |
| **Backend is finished** | Every endpoint you need already works. You are not blocked on anyone. |

Read `AGENTS.md` first, completely. Especially Part 1 (honesty) and Part 4 (limitations).

---

## PROTOCOL — follow this literally

1. Split the work below into 6–10 small, independently runnable parts. Show the numbered list to
   the human. **Then stop.**
2. Build **Part 1 only**. Show it running in a browser. **Then stop and wait for "next".**
3. Before each part, ask: *"Part N needs these endpoints / keys / assets: `<list>`. Supply them now,
   or should I stub?"* Wait for the answer.
4. Never batch two parts because they look small.

---

## What exists already

`modules/05_frontend/index.html` is a working single-file console with **no dependencies and no
build step**. It already talks to every endpoint. Open it and use it before you write anything —
you are replacing its presentation, not its wiring.

Start the backend:

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

---

## Your job

Turn the map half into something a district administrator would trust.

### `[P0]` The map

- MapLibre GL from npm. Basemap: the MapTiler key is in `.env` as `MAPTILER_API_KEY`, but
  **fall back to OSM raster tiles if it is absent** — the demo must work offline.
- Flood extent from `GET /api/runs/{run_id}/extent` (GeoJSON, EPSG:4326).
- Settlement markers from `impact.json`, coloured by `hazard_class`
  (`low` / `moderate` / `significant` / `extreme`).
- Dam marker at `meta.site.lat` / `meta.site.lon`.

### `[P0]` The depth raster

`GET /api/runs/{run_id}/file/packed.png` is an RGBA texture on the run's grid, georeferenced by
`meta.domain.bbox`. Channels:

| Channel | Value | Decode |
|---|---|---|
| R | arrival time | `R/255 * meta.time.end_hr` hours |
| G | time of peak | `G/255 * meta.time.end_hr` |
| B | max depth | `B/255 * meta.results.packed_depth_max_m` metres |
| A | duration | `0` means never wet — skip the pixel |

### `[P0]` The timeline

A scrub bar over `0 … meta.time.end_hr`. At time *t*, show a pixel only if `arrival <= t`. The
existing console does this on a 2D canvas in about 20 lines — read `renderFlood()` first.

**Label it honestly.** This is a *reconstruction from the arrival-time and peak-depth grids*, not
frame-by-frame solver output. The existing console says so under the canvas. Keep that sentence.

### `[P1]` Evacuation routes

`GET /api/runs/{run_id}/evacuation` returns `route` as `[[lon,lat], …]` per settlement. Draw them.
Three states, and they must look different: `route_found`, `no_safe_route` (**red — these people
need helicopters**), `access_not_inundated`.

### `[P1]` Live run

`POST /api/runs`, then `ws://localhost:8000/ws/runs/{run_id}`. Progress messages carry `pct`,
`t_hr`, `wet_cells`, `max_depth_m` and a live volume ledger (`volume_in_mcm`,
`volume_stored_mcm`). Showing mass balance updating live is worth more to a hydrologist than any
animation.

### `[P2]` Instant what-if

`POST /api/surrogate` answers in about 20 ms — fast enough to drive from a slider's `input` event.
**It is a neural-network prediction, not a simulation.** The response carries `is_emulated: true`
and a `warning` string. Both must be visible on screen whenever surrogate output is shown.

---

## Rules

- **The SYNTHETIC banner is not decoration.** If `meta.is_fake` is true it must be impossible to
  miss. Never remove it.
- **Never display a number the API did not send.** Blank beats invented.
- Keep the offline fallback. Conference wifi is not a dependency we accept.
- Do not add a dependency without telling the captain.
- `npm run dev` on port 5173; the backend already allows that origin via CORS.

## Done means

The map renders a real run, the timeline scrubs, routes draw, and all of it works with the network
cable pulled out.
