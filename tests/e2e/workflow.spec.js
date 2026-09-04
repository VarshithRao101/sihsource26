// tests/e2e/workflow.spec.js
//
// The complete user journey, in the order a juror would walk it:
//
//   open Workflow  ->  read the graph  ->  expand a box  ->  press PLAY
//   ->  watch the real backend execute  ->  watch every node change state
//   ->  pause the solver for real  ->  read the final result
//   ->  check every number on it against the API that produced it
//
// Every assertion is against something the backend actually produced. Where a
// number appears on screen the test fetches the same number from the API and
// compares, so a page that renders a plausible-looking value it invented fails
// here rather than in front of somebody.

const { test, expect } = require("@playwright/test");
const F = require("./fixtures");

test.describe.configure({ mode: "serial" });

let MANIFEST = null;
let RUN_ID = null;

/* Never leave a solve running behind us.
 *
 * A test that times out or fails mid-run abandons its solve, and the backend
 * carries on burning a core on it for as long as the server lives. That is not
 * a hypothetical: an abandoned run made every later solve in this suite three
 * times slower, and the suite then blamed the code. Cancel whatever is still in
 * flight after each test, so one failure cannot poison the ones after it. */
test.afterEach(async ({ request }) => {
  try {
    const r = await request.get("/api/runs");
    if (!r.ok()) return;
    for (const active of (await r.json()).active || []) {
      await request.post(`/api/runs/${active.run_id}/cancel`);
      console.log(`[cleanup] cancelled abandoned run ${active.run_id}`);
    }
  } catch {
    // The server may already be gone. Nothing to clean up in that case.
  }
});

// ---------------------------------------------------------------------------
// 1. The graph is generated from the backend, not drawn by hand
// ---------------------------------------------------------------------------

test("the pipeline manifest describes real stages", async ({ request }) => {
  const res = await request.get("/api/pipeline");
  expect(res.ok()).toBeTruthy();
  MANIFEST = await res.json();

  expect(MANIFEST.nodes.length).toBeGreaterThan(10);
  const ids = new Set(MANIFEST.nodes.map((n) => n.id));

  for (const n of MANIFEST.nodes) {
    // A box with no explanation is a box nobody can defend in Q&A.
    expect(n.does.length, `${n.id} has no description`).toBeGreaterThan(80);
    expect(n.module, `${n.id} names no code`).toBeTruthy();
    expect(n.title).toBeTruthy();
  }
  // Every arrow connects two boxes that exist.
  for (const e of MANIFEST.edges) {
    expect(ids.has(e.from), `edge from unknown ${e.from}`).toBeTruthy();
    expect(ids.has(e.to), `edge to unknown ${e.to}`).toBeTruthy();
  }

  // The stages that matter to the problem statement are all present.
  for (const required of [
    "catalogue", "river", "terrain", "exposure", "breach",
    "sph", "solve", "grids", "impact", "evacuation",
    "uncertainty", "gee", "delft3d", "validate", "result",
  ]) {
    expect(ids.has(required), `missing stage ${required}`).toBeTruthy();
  }
});

test("Delft3D is reported from a probe and never claimed", async ({ request }) => {
  const m = MANIFEST || (await (await request.get("/api/pipeline")).json());
  const d = m.engines.delft3d;
  expect(d).toBeTruthy();
  // Whatever the answer is, it has to come with the evidence for it.
  expect(d.summary.length).toBeGreaterThan(10);
  if (!d.installed) {
    expect(d.summary.toUpperCase()).toContain("NOT INSTALLED");
    expect(Array.isArray(d.detail.searched)).toBeTruthy();
    expect(d.detail.searched.length).toBeGreaterThan(0);
  }
  // SFINCS is a different model and must not be presented as Delft3D.
  expect(m.nodes.find((n) => n.id === "sfincs").does).toContain("NOT");
});

// ---------------------------------------------------------------------------
// 2. The page draws it, and a box explains itself when you open it
// ---------------------------------------------------------------------------

test("the workflow page draws one box per stage, all waiting", async ({ page }) => {
  await page.goto("/workflow");
  await expect(page.locator(".node").first()).toBeVisible();

  const manifest = MANIFEST || (await (await page.request.get("/api/pipeline")).json());
  await expect(page.locator(".node")).toHaveCount(manifest.nodes.length);

  // Stages the live request will drive start on WAITING. Engine probes start on
  // what the probe found, because a box that sits on WAITING all demo reads as
  // broken rather than as honest.
  for (const n of manifest.nodes) {
    const state = await page.locator(`.node[data-id="${n.id}"]`).getAttribute("data-s");
    if (n.engine) expect(["skipped", "absent", "complete"]).toContain(state);
    else expect(state).toBe("waiting");
  }
  await expect(page.locator('.node[data-id="delft3d"]')).toHaveAttribute("data-s", "absent");
});

test("clicking a box expands it into what that stage does", async ({ page }) => {
  await page.goto("/workflow");
  await page.locator('.node[data-id="solve"]').click();

  const drawer = page.locator("#drawer");
  await expect(drawer).toHaveClass(/open/);
  await expect(drawer).toContainText("What this stage does");
  await expect(drawer).toContainText("shallow-water");
  await expect(drawer).toContainText("modules/04_backend/solver.py");
  await expect(drawer).toContainText("Takes in");
  await expect(drawer).toContainText("Puts out");
  await expect(drawer).toContainText("HLL Riemann solver");

  // And the absent engine explains its absence rather than hiding it.
  await page.locator('.node[data-id="delft3d"]').click();
  await expect(drawer).toContainText("Paths searched");
  await expect(drawer).toContainText("cannot turn green");
});

test("hovering a box previews what it does without opening it", async ({ page }) => {
  await page.goto("/workflow");
  await page.locator('.node[data-id="breach"]').hover();
  const tip = page.locator("#tip");
  await expect(tip).toBeVisible();
  await expect(tip).toContainText("Breach and outflow");
  await expect(tip).toContainText("click to expand");
});

// ---------------------------------------------------------------------------
// 3. PLAY runs the actual backend pipeline
// ---------------------------------------------------------------------------

test("PLAY executes the real pipeline and every node changes state", async ({ page, request }) => {
  await page.goto("/workflow");
  await expect(page.locator(".node").first()).toBeVisible();

  // Pick the dam the way an operator does: state, then dam. Nobody types a
  // latitude - every physical number comes from the CWC register.
  await page.selectOption("#fstate", "Telangana");
  await expect
    .poll(async () => page.locator("#fdam option").count(), { timeout: 20000 })
    .toBeGreaterThan(1);
  await page.selectOption("#fdam", F.DAM_ID);
  await page.selectOption("#mode", "overtopping");
  await page.fill("#reach", String(F.REACH_KM));
  await page.fill("#endhr", String(F.END_HR));

  // Record every state each box passes through, straight off the DOM, from
  // before the run starts. Polling the API cannot do this: by the time the
  // first poll lands, the early stages have already been and gone.
  await page.evaluate(() => {
    window.__seen = {};
    const note = (el) => {
      const id = el.dataset.id, st = el.dataset.s;
      const list = (window.__seen[id] = window.__seen[id] || []);
      if (list[list.length - 1] !== st) list.push(st);
    };
    document.querySelectorAll(".node").forEach(note);
    new MutationObserver((muts) => {
      for (const m of muts) if (m.attributeName === "data-s") note(m.target);
    }).observe(document.getElementById("world"), {
      subtree: true, attributes: true, attributeFilter: ["data-s"],
    });
  });

  // PLAY must hit the API, not animate a picture of one.
  const [post] = await Promise.all([
    page.waitForRequest(
      (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
      { timeout: 30000 }
    ),
    page.click("#bPlay"),
  ]);
  const sent = JSON.parse(post.postData());
  expect(sent.dam_id).toBe(F.DAM_ID);
  expect(sent.real_terrain).toBe(true);
  // The page sends what the operator chose and nothing it invented.
  expect(sent.reach_length_km).toBe(F.REACH_KM);

  await expect(page.locator("#cRun")).not.toHaveText("no run", { timeout: 30000 });
  RUN_ID = (await page.locator("#cRun").textContent()).trim();
  expect(RUN_ID).toMatch(/^[a-z0-9]+_[a-z]+_[a-z]+_\d+$/);

  // The solve node must actually run - the page is following a real solver.
  await expect(page.locator('.node[data-id="solve"]')).toHaveAttribute(
    "data-s", "running", { timeout: 4 * 60 * 1000 }
  );
  // ...and while it does, the strip shows the solver's own streamed numbers.
  // The labels are uppercased by CSS, so match the text that is in the DOM.
  await expect(page.locator("#strip")).toHaveClass(/on/, { timeout: 60000 });
  await expect(page.locator("#strip")).toContainText(/wet cells/i, { timeout: 60000 });
  await expect(page.locator("#strip")).toContainText(/volume in/i);

  // Follow the run on the API as well, so the page and the backend are checked
  // against each other rather than the page being checked against itself.
  const final = await F.until(request, RUN_ID, F.isTerminal);
  expect(final.status, `run failed: ${final.error}`).toBe("done");

  // WAITING -> RUNNING -> COMPLETE, as the operator saw it happen.
  const seen = await page.evaluate(() => window.__seen);
  for (const id of ["river", "terrain", "breach", "solve", "grids", "validate", "result"]) {
    expect(seen[id], `no transitions recorded for ${id}`).toBeTruthy();
    expect(seen[id][0], `${id} did not start on waiting`).toBe("waiting");
    expect(seen[id]).toContain("running");
    expect(seen[id][seen[id].length - 1], `${id} did not complete`).toBe("complete");
  }
  // The engine we do not have never moved at all.
  expect(seen.delft3d).toEqual(["absent"]);

  // Nothing is left hanging: every box reaches a state that means something.
  for (const [id, node] of Object.entries(final.nodes)) {
    expect(
      ["complete", "skipped", "absent", "failed"],
      `${id} finished on ${node.status}`
    ).toContain(node.status);
  }
  // And the engine we do not have still has not run.
  expect(final.nodes.delft3d.status).toBe("absent");

  // The page agrees with the API.
  await expect(page.locator("#cStatus")).toContainText("done", { timeout: 60000 });
  for (const id of ["solve", "grids", "impact", "validate", "result"]) {
    await expect(page.locator(`.node[data-id="${id}"]`)).toHaveAttribute(
      "data-s", "complete", { timeout: 60000 }
    );
  }
});

// ---------------------------------------------------------------------------
// 4. The final node carries the answer, and the answer is the API's
// ---------------------------------------------------------------------------

test("the final node shows the real flood, damage and evacuation result", async ({ page, request }) => {
  test.skip(!RUN_ID, "needs the run from the PLAY test");
  await page.goto("/workflow");
  await page.click("#bLast");
  await expect(page.locator("#drawer")).toHaveClass(/open/, { timeout: 60000 });

  const api = await (await request.get(`/api/runs/${RUN_ID}`)).json();
  const r = api.meta.results;

  const drawer = page.locator("#drawer");
  await expect(drawer).toContainText("Flood");
  // Headline physics, checked value by value against the run folder.
  await expect(drawer).toContainText(String(r.flood_area_km2));
  await expect(drawer).toContainText(String(r.max_depth_m));
  await expect(drawer).toContainText(String(r.max_velocity_ms));

  if (api.impact && api.impact.totals) {
    const t = api.impact.totals;
    await expect(drawer).toContainText("Damage and loss");
    await expect(drawer).toContainText(String(t.settlements_affected));
    await expect(drawer).toContainText(String(t.damage_inr_crore));
    // Named places with the warning time each one gets.
    await expect(drawer).toContainText("Warning time by settlement");
    await expect(drawer).toContainText(api.impact.settlements[0].name);
    // The damage numbers travel with the source of their curves.
    await expect(drawer).toContainText("Huizinga");
  }

  await expect(drawer).toContainText("Evacuation");
  // The exports the problem statement names explicitly.
  await expect(drawer.locator('a[href*="format=kml"]')).toBeVisible();
  await expect(drawer.locator('a[href*="format=shp"]')).toBeVisible();
});

// ---------------------------------------------------------------------------
// 5. The hidden half: what each stage actually depends on
// ---------------------------------------------------------------------------

test("every stage declares the dependencies it really has", async ({ request }) => {
  const m = MANIFEST || (await (await request.get("/api/pipeline")).json());

  for (const n of m.nodes) {
    expect(n.deps, `${n.id} declares no dependencies at all`).toBeTruthy();
  }
  const by = Object.fromEntries(m.nodes.map((n) => [n.id, n.deps]));

  // Spot-checks against what the code genuinely imports and calls. If somebody
  // swaps a library out and forgets the manifest, this is where it shows up.
  expect(by.solve.code.join(" ")).toMatch(/numba/);
  expect(by.terrain.code.join(" ")).toMatch(/rasterio/);
  expect(by.terrain.services.join(" ")).toMatch(/OpenTopography/);
  expect(by.terrain.services.join(" ")).toMatch(/OPENTOPOGRAPHY_API_KEY/);
  expect(by.exposure.services.join(" ")).toMatch(/Overpass/);
  expect(by.exposure.data.join(" ")).toMatch(/WorldPop/);
  expect(by.impact.code.join(" ")).toMatch(/xgboost/);
  expect(by.evacuation.code.join(" ")).toMatch(/networkx/);
  expect(by.uncertainty.code.join(" ")).toMatch(/scikit-learn/);
  expect(by.surrogate.code.join(" ")).toMatch(/torch/);
  expect(by.gee.services.join(" ")).toMatch(/Earth Engine/);
  expect(by.inflow.data.join(" ")).toMatch(/CHIRPS/);
  expect(by.grids.code.join(" ")).toMatch(/rasterio/);
  expect(by.result.code.join(" ")).toMatch(/geopandas/);

  // The engines are declared as engines, not quietly listed as libraries.
  expect(by.sph.engines.join(" ")).toMatch(/DualSPHysics/);
  expect(by.sfincs.engines.join(" ")).toMatch(/SFINCS/);

  // And the one we do not have says so in the place a juror will look.
  //
  // The wording changed on 2026-09-03 and the assertion moved with it, because
  // the old text was wrong. It said the Delft3D licence was "NOT GRANTED",
  // which is true of Delft3D FM and false of Delft3D 4 - the structured model
  // the problem statement actually names, which is GPLv3 and simply ships as
  // source we did not compile. Both kernels are now declared separately, and
  // this checks that each still states its own real reason.
  const d3 = by.delft3d.engines.join(" ");
  expect(d3, "Delft3D 4 must say the kernel was not built").toMatch(/NOT BUILT/);
  expect(d3, "Delft3D FM must say the kernel is not installed").toMatch(/NOT INSTALLED/);
  expect(d3, "the GPLv3 / source-only fact must be stated").toMatch(/GPLv3/);
  expect(d3, "the FM licence must still be named as ungranted").toMatch(/not granted/i);
});

test("a box shows its dependency counts and expands to the full list", async ({ page }) => {
  await page.goto("/workflow");
  await expect(page.locator(".node").first()).toBeVisible();

  // The counts are on the card, so the supply chain is visible without clicking.
  const chips = page.locator('.node[data-id="terrain"] .deps span');
  await expect(chips.first()).toBeVisible();
  expect(await chips.count()).toBeGreaterThan(1);

  // Clicking opens the real list, grouped by what kind of dependency it is.
  await page.locator('.node[data-id="terrain"]').click();
  const drawer = page.locator("#drawer");
  await expect(drawer).toContainText("Network services and credentials");
  await expect(drawer).toContainText("OPENTOPOGRAPHY_API_KEY");
  await expect(drawer).toContainText("Datasets and files");
  await expect(drawer).toContainText("Copernicus GLO-30");
  await expect(drawer).toContainText("Python packages");
  await expect(drawer).toContainText("rasterio");

  // The absent engine is flagged on its own card, in red, before anyone clicks.
  await expect(page.locator('.node[data-id="delft3d"] .deps span.eng')).toBeVisible();
  await page.locator('.node[data-id="delft3d"]').click();
  await expect(drawer).toContainText("External engines");
  await expect(drawer).toContainText("NOT INSTALLED");
});

test("the page is the graph and nothing else", async ({ page }) => {
  await page.goto("/workflow");
  await expect(page.locator("#canvasWrap")).toBeVisible();
  // The 3D pane is gone, and so is the API link.
  await expect(page.locator("#babylon")).toHaveCount(0);
  await expect(page.locator("#sceneNote")).toHaveCount(0);
  await expect(page.locator('nav.nav a[href="/docs"]')).toHaveCount(0);
  // The controls the operator drives it with are all still here.
  for (const id of ["#bPlay", "#bPause", "#bReset", "#bLast", "#fstate", "#fdam", "#mode"]) {
    await expect(page.locator(id)).toBeVisible();
  }
  // The engine probe log is on screen - it is how we show what is installed.
  await expect(page.locator("#log")).toContainText(/emulator|SFINCS|DualSPHysics/i,
    { timeout: 20000 });
});

test("the finished run fills the metric strip from the run folder", async ({ page, request }) => {
  test.skip(!RUN_ID, "needs the run from the PLAY test");
  await page.goto("/workflow");
  await page.click("#bLast");
  await expect(page.locator("#strip")).toHaveClass(/on/, { timeout: 60000 });

  const api = await (await request.get(`/api/runs/${RUN_ID}`)).json();
  const r = api.meta.results;
  const shown = await page.evaluate(() =>
    Object.fromEntries([...document.querySelectorAll("#strip .metric")].map((m) => [
      m.querySelector(".k").textContent.trim(),
      parseFloat(m.querySelector(".v").textContent.replace(/[^0-9.\-]/g, "")),
    ]))
  );
  expect(shown["flooded area"]).toBeCloseTo(r.flood_area_km2, 1);
  expect(shown["max depth"]).toBeCloseTo(r.max_depth_m, 1);
  expect(shown["max velocity"]).toBeCloseTo(r.max_velocity_ms, 1);
});

// ---------------------------------------------------------------------------
// 6. PAUSE and RESET reach the solver, not just the picture
// ---------------------------------------------------------------------------

test("PAUSE holds the solver and RESET stops it", async ({ page, request }) => {
  await page.goto("/workflow");
  await expect(page.locator(".node").first()).toBeVisible();
  await page.selectOption("#fstate", "Telangana");
  await expect
    .poll(async () => page.locator("#fdam option").count(), { timeout: 20000 })
    .toBeGreaterThan(1);
  await page.selectOption("#fdam", F.DAM_ID);
  await page.fill("#reach", String(F.REACH_KM));
  await page.fill("#endhr", String(F.END_HR));
  await page.click("#bPlay");

  await expect(page.locator("#cRun")).not.toHaveText("no run", { timeout: 30000 });
  const runId = (await page.locator("#cRun").textContent()).trim();

  // Wait until the solver is genuinely stepping, so pausing means something.
  await F.until(request, runId, (s) => (s.pct || 0) > 8, { timeoutMs: 4 * 60 * 1000 });

  await page.click("#bPause");
  await expect(page.locator("#bPause")).toContainText("RESUME");
  await expect(page.locator("#cStatus")).toContainText("paused");

  const before = (await (await request.get(`/api/runs/${runId}/status`)).json()).pct;
  await new Promise((r) => setTimeout(r, 9000));
  const after = (await (await request.get(`/api/runs/${runId}/status`)).json()).pct;

  // The solver blocks inside its own progress callback, so at most the one
  // timestep that was already in flight gets through. Anything more means the
  // CPU carried on behind a frozen picture.
  expect(after - before, `solve advanced ${after - before}% while paused`).toBeLessThan(2);

  await page.click("#bPause");
  await expect(page.locator("#cStatus")).toContainText("running");
  await new Promise((r) => setTimeout(r, 6000));
  const resumed = (await (await request.get(`/api/runs/${runId}/status`)).json()).pct;
  expect(resumed).toBeGreaterThan(after);

  // RESET cancels the run for real and empties the workspace.
  await page.click("#bReset");
  const stopped = await F.until(request, runId, F.isTerminal, { timeoutMs: 60000 });
  expect(stopped.status).toBe("failed");
  expect(stopped.error).toContain("cancelled");

  await expect(page.locator("#cRun")).toHaveText("no run");
  await expect(page.locator("#cStatus")).toHaveText("idle");
  await expect(page.locator('.node[data-id="solve"]')).toHaveAttribute("data-s", "waiting");
  await expect(page.locator('.node[data-id="result"]')).toHaveAttribute("data-s", "waiting");
  // ...but a stage we never ran still tells the truth about itself.
  await expect(page.locator('.node[data-id="delft3d"]')).toHaveAttribute("data-s", "absent");
});
