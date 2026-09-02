// tests/e2e/console.spec.js
//
// The operator console: only the fields somebody actually fills in, a map that
// answers questions when you point at it, and the exports the problem statement
// names. Runs against whatever the newest run on disk is, so it exercises real
// output rather than a fixture.

const { test, expect } = require("@playwright/test");

// Not serial: each of these loads the newest run for itself, so one failure
// should not hide the others.

/** Load the newest run into the console and wait for the map to be ready. */
async function loadNewestRun(page) {
  await page.goto("/");
  await page.click("#loadlast");
  await expect
    .poll(async () => page.evaluate(() => !!(PACKED && VIEW && FIELDSMETA)),
          { timeout: 90000 })
    .toBeTruthy();
  return page.evaluate(() => RUN);
}

test("the form asks only for what an operator decides", async ({ page }) => {
  await page.goto("/");

  // Demo data is gone: no canned sites to mistake for a result.
  await expect(page.locator("#preset")).toHaveCount(0);
  // So is the advanced physics drawer - breach regression, cell size, numerical
  // scheme and the terrain toggle are the values we validate against and are
  // not operator decisions. They live in the API defaults now.
  await expect(page.locator("#adv")).toHaveCount(0);
  await expect(page.locator("#regression")).toHaveCount(0);
  await expect(page.locator("#cell")).toHaveCount(0);
  await expect(page.locator("#scheme")).toHaveCount(0);

  // What is left is what somebody genuinely enters.
  for (const id of ["#fstate", "#fcity", "#fq", "#fdam", "#mode", "#level", "#reach", "#endhr"]) {
    await expect(page.locator(id)).toBeVisible();
  }
  // NTRO asks for "any river", so a structure the register does not list is
  // still reachable - just not dressed up as an advanced setting.
  await expect(page.locator("#manual")).toBeVisible();

  // And the workflow page is one click away.
  await expect(page.locator('nav.nav a[href="/workflow"]')).toBeVisible();
});

test("the scenario form follows the failure mode", async ({ page }) => {
  await page.goto("/");
  await page.selectOption("#mode", "gated_release");
  await expect(page.locator("#grp-gate")).toBeVisible();
  await expect(page.locator("#modehelp")).toContainText("does NOT fail");

  await page.selectOption("#mode", "blockage_breach");
  await expect(page.locator("#grp-blockage")).toBeVisible();
  // A landslide dam has no engineered reservoir to be a percentage full.
  await expect(page.locator("#grp-level")).toBeHidden();
});

test("pointing at the flood reports depth, speed and risk from the run's grids",
  async ({ page, request }) => {
    const runId = await loadNewestRun(page);
    expect(runId).toBeTruthy();

    // Aim at a settlement the run says is wet - a point we know carries water.
    const target = await page.evaluate(() => {
      const s = ((typeof IMPACT !== "undefined" && IMPACT && IMPACT.settlements) || [])
        .find((x) => x.max_depth_m > 0.5);
      if (!s) return null;
      const [x, y] = project(s.lon, s.lat, VIEW);
      // The pointer lands on a whole pixel, so round here and use the SAME
      // integer pixel for the mouse move and for the coordinate we check.
      return { name: s.name, x: Math.round(x), y: Math.round(y) };
    });
    test.skip(!target, "newest run has no wet settlement to point at");

    // Watch for the probe the PAGE issues, rather than guessing the coordinate
    // it used - the browser dispatches the pointer at whole client pixels, so
    // aiming at a canvas whose box starts on a fraction lands a cell away.
    const box = await page.locator("#map").boundingBox();
    const probed = page
      .waitForResponse((r) => r.url().includes("/probe?"), { timeout: 15000 })
      .then((r) => r.json());
    await page.mouse.move(box.x + target.x, box.y + target.y);

    const tip = page.locator("#maptip");
    await expect(tip).toBeVisible({ timeout: 10000 });

    // Exactly the three things asked for, and nothing else.
    await expect(tip).toContainText("Water depth");
    await expect(tip).toContainText("Water speed");
    await expect(tip).toContainText("Risk area");
    // Nothing beyond those three - the readout is deliberately not a dump.
    const rows = await tip.locator(".r").count();
    expect(rows).toBe(2);

    // And they are the API's numbers, not the browser's opinion.
    const probe = await probed;
    const text = await tip.innerText();
    expect(probe.inside_domain).toBeTruthy();
    expect(probe.note).toContain("max_depth.tif");
    if (probe.wet) {
      expect(text).toContain(probe.max_depth_m.toFixed(2));
      expect(text).toContain(probe.max_velocity_ms.toFixed(2));
    }
    // The hazard class is computed server side from shared.contract, so the
    // browser is not carrying a second copy of the thresholds.
    expect(text.toLowerCase()).toContain(probe.hazard_class);
    expect(runId).toBeTruthy();
  });

test("the flow streaks follow measured direction and speed", async ({ page }) => {
  await loadNewestRun(page);

  // requestAnimationFrame is throttled in a background tab, so drive the
  // animation directly and then assert on what it drew.
  const flow = await page.evaluate(async () => {
    for (let i = 0; i < 60; i++) flowStep();
    const c = document.getElementById("flow");
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    let lit = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) lit++;
    return {
      particles: PARTICLES.length,
      moving: PARTICLES.filter((p) => p.trail.length > 1).length,
      lit,
      overlaySized: c.width === document.getElementById("map").width,
    };
  });
  expect(flow.particles).toBeGreaterThan(100);
  expect(flow.moving).toBeGreaterThan(flow.particles * 0.4);
  expect(flow.lit).toBeGreaterThan(0);
  expect(flow.overlaySized).toBeTruthy();

  // Direction is the arrival-time gradient, so over several frames a particle
  // must end up on ground the water reached LATER than where it started. That
  // is the claim the page makes about the streaks, and it is the claim tested -
  // not a single step, which can land inside the cell it started in.
  const downstream = await page.evaluate(() => {
    // A trail is cleared whenever a particle respawns, so a particle carrying
    // five or more points has been travelling continuously for five frames.
    // Walk its own trail from end to end: screen pixel -> lon/lat -> grid cell.
    const cellOf = ([px, py]) => {
      const [lon, lat] = canvasToLonLat(px, py);
      const b = VIEW.bbox;
      return [
        ((lon - b[0]) / (b[2] - b[0])) * PACKED.width,
        ((b[3] - lat) / (b[3] - b[1])) * PACKED.height,
      ];
    };
    let checked = 0, correct = 0, gain = 0, worstLoss = 0;
    for (const p of PARTICLES) {
      if (p.trail.length < 5) continue;
      const from = arrivalAt(...cellOf(p.trail[0]));
      const to = arrivalAt(...cellOf(p.trail[p.trail.length - 1]));
      if (from === null || to === null) continue;
      checked++;
      gain += to - from;
      if (to >= from) correct++;
      else worstLoss = Math.max(worstLoss, from - to);
    }
    return { checked, correct, meanGain: gain / Math.max(checked, 1), worstLoss };
  });
  expect(downstream.checked).toBeGreaterThan(10);
  // Streaks travel downstream. Two things stop this being exact for every
  // single particle: advection takes steps shorter than a cell, so one can clip
  // the corner of a neighbour the gradient search did not choose; and on a
  // meandering reach the cell across a narrow neck belongs to the other limb
  // and was reached hours apart. So the claim tested is the one the page
  // actually makes - the population moves down the arrival-time gradient.
  expect(downstream.meanGain, "streaks are not moving downstream").toBeGreaterThan(0);
  expect(downstream.correct / downstream.checked).toBeGreaterThan(0.9);
});

test("the time slider replays the flood spreading", async ({ page }) => {
  await loadNewestRun(page);
  const wetAt = (pct) =>
    page.evaluate((p) => {
      const s = document.getElementById("time");
      s.value = String(p);
      s.dispatchEvent(new Event("input"));
      const c = document.getElementById("map");
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      let blue = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i + 2] > d[i] + 30) blue++;
      return blue;
    }, pct);

  const early = await wetAt(10);
  const late = await wetAt(100);
  expect(late).toBeGreaterThan(early);
  await expect(page.locator("#timelab")).toContainText("t =");
});

test("the run's own honesty flags reach the screen", async ({ page }) => {
  const runId = await loadNewestRun(page);
  const meta = await page.evaluate(() => META);

  // Provenance is on screen: which DEM, what was done to it, which breach
  // regression produced the numbers.
  await expect(page.locator("#prov")).toContainText(meta.dem.source);
  await expect(page.locator("#prov")).toContainText(meta.scenario.failure_mode);
  // Uncertainty is published, not buried.
  await expect(page.locator("#uncert")).not.toHaveText("—", { timeout: 30000 });
  // The synthetic banner is tied to the run's own flag, either way.
  const bannerShown = await page.locator("#banner").isVisible();
  expect(bannerShown).toBe(!!meta.is_fake);
  expect(runId).toBeTruthy();
});

test("the outputs export as .shp and .kml", async ({ page, request }) => {
  const runId = await loadNewestRun(page);
  const kml = await request.get(`/api/runs/${runId}/export?format=kml`);
  expect(kml.ok()).toBeTruthy();
  expect((await kml.body()).length).toBeGreaterThan(500);

  const shp = await request.get(`/api/runs/${runId}/export?format=shp`);
  expect(shp.ok()).toBeTruthy();
  expect(shp.headers()["content-type"]).toContain("zip");

  const gj = await request.get(`/api/runs/${runId}/export?format=geojson`);
  expect(gj.ok()).toBeTruthy();
  const fc = await gj.json();
  expect(fc.type).toBe("FeatureCollection");
});

test("the run that produced all of this is contract-valid", async ({ page, request }) => {
  const runId = await loadNewestRun(page);
  const rep = await (await request.get(`/api/runs/${runId}/validate`)).json();
  expect(rep.ok, `validator errors: ${JSON.stringify(rep.errors)}`).toBeTruthy();
});
