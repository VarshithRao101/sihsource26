// tests/e2e/fixtures.js — the scenario the walkthrough runs, and the helpers.
//
// One dam, chosen for a reason: Lower Manair on the Manair in Telangana is in
// the CWC register with a height and a gross storage, and this repository has
// its scouting DEM, its conditioned DEM at a 30 km reach and its OSM exposure
// already cached under data/. So the run exercises the whole pipeline - river
// trace, terrain, exposure, breach, solver, grids, damage, evacuation,
// uncertainty, validator - without depending on a download completing while a
// juror watches.
//
// Change REACH_KM and the DEM cache misses and the test starts downloading.
// That is not a bug, but it is slow, so it is written down here.

const DAM_ID = "TL47HH0065";
const DAM_NAME = "Lower Manair Dam";
const REACH_KM = 30;

// Four simulated hours, not eight. The solve is real and its wall-clock cost
// swings by a factor of three on a busy machine, which was failing the suite on
// a run that was working perfectly. Four hours still drives every stage - the
// breach forms, the flood reaches named settlements, impact and evacuation both
// produce output - at roughly half the cost. Do not cut it further: below about
// three hours the water has not reached enough villages for impact.json to be
// worth asserting on.
const END_HR = 4;

/** Poll the run status until `pred` holds or we run out of patience. */
// 12 minutes. A 30 km reach over 6 simulated hours solves in about two on an
// idle machine, but the run is real: a contended CPU, a cold DEM cache or a
// bigger reach all make it legitimately slower, and a tight budget here fails
// the suite for a solve that was working perfectly.
async function until(request, runId, pred, { timeoutMs = 15 * 60 * 1000, everyMs = 1500 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    const r = await request.get(`/api/runs/${runId}/status`);
    if (r.ok()) {
      last = await r.json();
      if (pred(last)) return last;
    }
    await new Promise((res) => setTimeout(res, everyMs));
  }
  throw new Error(
    `timed out waiting on ${runId}; last status was ` + JSON.stringify(last)
  );
}

const isTerminal = (s) => s.status === "done" || s.status === "failed";

module.exports = { DAM_ID, DAM_NAME, REACH_KM, END_HR, until, isTerminal };
