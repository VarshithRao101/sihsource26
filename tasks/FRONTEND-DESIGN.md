# Frontend design — 2 people

You own how it looks. Design only: no backend, no JavaScript, no solver.

Read `AGENTS.md` first, all of it.

---

## What you own

```
modules/05_frontend/theme.css       every visual decision on both pages
modules/05_frontend/assets/         fonts and images you add (create it)
```

**That is the whole list.** You do not edit `index.html`, `workflow.html`,
`config.js`, or anything in `vendor/`.

That is not a restriction on your work, it is what makes it possible. Both pages
keep their styles in inline `<style>` blocks tangled up with the markup and the
JavaScript that drives the solver, and the captain is editing those files daily.
If you worked in them, every push would collide. `theme.css` is loaded **after**
those inline styles on both pages, so anything you set here wins — you get full
control of the appearance without ever touching a file anyone else has open.

Read the top of `theme.css`. It lists the colour tokens both pages use and what
each one means.

## How to work

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

Console at <http://localhost:8000>, the node graph at `/workflow`. Edit
`theme.css`, reload. It is served `no-store`, so a refresh always gets your
latest version — if a change seems to do nothing, it is your CSS, not a cache.

## Three hard rules

**1 · Nothing loads from the internet.** No CDN, no Google Fonts `<link>`, no
framework. This console is demonstrated on conference wifi and is required to
render with the network unplugged. A font fetched from a CDN turns a working
demo into a blank page. Want custom type? Put the font file in `assets/` and
`@font-face` it from disk.

**2 · Three things must stay impossible to miss.** They are honesty features and
the project is judged on them:

- the red **SYNTHETIC DATA** banner (`#banner`)
- the **read-only build** health chip, when the backend cannot solve
- the **disabled PLAY button** and its reason

Restyle them freely. Do not make them subtle.

**3 · Readable beats striking.** This is an emergency-management tool. Depth
figures, arrival times and the villages in the impact table are what someone
acts on. Keep contrast high, keep numbers legible, keep the hazard colours
distinguishable for colour-blind viewers — red/green alone is not enough to
carry meaning.

## What is worth your attention

In rough order of what a judge sees:

1. **The node graph** (`/workflow`) — twenty boxes and the arrows between them.
   The runbook opens the demo here. It should look like a system, not a
   diagram.
2. **The impact table and evacuation rows** — named villages, arrival times,
   roads cut. Rows with no safe route are the ones that matter most.
3. **The map and its legend** — depth ramp, the flood outline, the time
   scrubber.
4. **The scenario form** — the Dam / River toggle, and the panel that appears
   under each.
5. **Density and rhythm** — it is currently a working tool that nobody has
   styled. Spacing and hierarchy will buy more than colour will.

Take a screenshot before you start. You will want the comparison.

---

**Ask the captain before:** adding any dependency, adding a build step, editing
any file not listed above, or changing what a control *says* rather than how it
looks. Found something broken outside `theme.css`? Tell the captain, do not fix
it.
