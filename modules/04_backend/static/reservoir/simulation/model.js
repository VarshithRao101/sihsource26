/**
 * simulation/model.js - the equations. No DOM, no timers, no state of its own.
 *
 * This is a line-for-line mirror of modules/04_backend/reservoir.py, which is
 * the reference implementation and the one under pytest. If you change an
 * equation here, change it there and re-run:
 *
 *     python -m pytest modules/04_backend/tests/test_reservoir.py -q
 *
 *     dV/dt = Q_in(t) - Q_out(y)
 *     V(y)  = V_max (y/H)^k        ->  y = H (V/V_max)^(1/k)
 *
 * Every function is pure: given a config and a state it returns a number. That
 * is what lets the controller step it at any speed and the tests pin it down.
 */

import { GRAVITY, ORIFICE_CD, WEIR_C_SI, MCM } from "../config/constants.js";

// ---------------------------------------------------------------------------
// Derived geometry
// ---------------------------------------------------------------------------

export const capacityM3 = (cfg) => cfg.capacity_mcm * MCM;
export const spillwayCrestM = (cfg) => cfg.spillway_crest_frac * cfg.dam_height_m;
export const outletInvertM = (cfg) => cfg.outlet_invert_frac * cfg.dam_height_m;

/** Gate area that delivers target_release_cumecs at full supply level. */
export function outletAreaM2(cfg) {
  const head = Math.max(cfg.dam_height_m - outletInvertM(cfg), 1e-6);
  return cfg.target_release_cumecs / (ORIFICE_CD * Math.sqrt(2 * GRAVITY * head));
}

// ---------------------------------------------------------------------------
// V <-> y
// ---------------------------------------------------------------------------

/** y from V. Inverse of the storage curve. */
export function levelM(volumeM3, cfg) {
  const vMax = capacityM3(cfg);
  if (volumeM3 <= 0 || vMax <= 0) return 0;
  const frac = Math.min(volumeM3 / vMax, 1);
  return cfg.dam_height_m * Math.pow(frac, 1 / cfg.storage_exponent);
}

/** V from y. */
export function volumeM3(level, cfg) {
  if (level <= 0) return 0;
  const frac = Math.min(level / cfg.dam_height_m, 1);
  return capacityM3(cfg) * Math.pow(frac, cfg.storage_exponent);
}

/**
 * dV/dy at this level, m2 - the water surface area.
 * At k = 1 this is the constant A of the y = V/A tank model.
 */
export function surfaceAreaM2(level, cfg) {
  const h = cfg.dam_height_m;
  const k = cfg.storage_exponent;
  if (h <= 0) return 0;
  if (level <= 0) return k === 1 ? (capacityM3(cfg) * k) / h : 0;
  return (capacityM3(cfg) * k * Math.pow(Math.min(level, h), k - 1)) / Math.pow(h, k);
}

// ---------------------------------------------------------------------------
// Q_in(t) and Q_out(y)
// ---------------------------------------------------------------------------

/** Q_in(t): steady base flow plus an optional synthetic Gaussian flood wave. */
export function inflowCumecs(tSeconds, cfg) {
  let q = cfg.base_inflow_cumecs;
  if (cfg.flood_peak_cumecs > 0 && cfg.flood_duration_hr > 0) {
    const tHr = tSeconds / 3600;
    const sigma = cfg.flood_duration_hr / 2;
    const z = (tHr - cfg.flood_peak_time_hr) / sigma;
    q += cfg.flood_peak_cumecs * Math.exp(-0.5 * z * z);
  }
  return q;
}

/**
 * Q_out(y): controlled outlet + uncontrolled spillway, both head-driven.
 *
 *   outlet    Q = Cd A sqrt(2 g (y - y_invert))
 *   spillway  Q = C L (y - y_crest)^1.5
 */
export function outflowCumecs(level, cfg) {
  const outletHead = Math.max(level - outletInvertM(cfg), 0);
  const gate = ORIFICE_CD * outletAreaM2(cfg) * Math.sqrt(2 * GRAVITY * outletHead);

  const weirHead = Math.max(level - spillwayCrestM(cfg), 0);
  const spillway = WEIR_C_SI * cfg.spillway_length_m * Math.pow(weirHead, 1.5);

  return { gate, spillway, total: gate + spillway };
}

// ---------------------------------------------------------------------------
// Warning state
// ---------------------------------------------------------------------------

export function statusOf(level, cfg, overflowing) {
  if (overflowing || level >= cfg.dam_height_m) return "overflow";
  if (level >= spillwayCrestM(cfg)) return "high";
  if (level <= cfg.low_frac * cfg.dam_height_m) return "low";
  return "normal";
}

// ---------------------------------------------------------------------------
// State and the Euler step
// ---------------------------------------------------------------------------

export function initialState(cfg) {
  return {
    tSeconds: 0,
    volume: cfg.initial_volume_frac * capacityM3(cfg),
    inflowVolume: 0,
    outflowVolume: 0,
    overflowVolume: 0,
  };
}

/** The reading an instrument would take right now. No integration here. */
export function sampleOf(state, cfg, overflowCumecs = 0, substeps = 0) {
  const y = levelM(state.volume, cfg);
  const qIn = inflowCumecs(state.tSeconds, cfg);
  const out = outflowCumecs(y, cfg);
  const overflowing = overflowCumecs > 0 || state.volume >= capacityM3(cfg);
  return {
    tSeconds: state.tSeconds,
    tHours: state.tSeconds / 3600,
    volume: state.volume,
    volumeMcm: state.volume / MCM,
    volumeFrac: state.volume / capacityM3(cfg),
    level: y,
    levelFrac: cfg.dam_height_m ? y / cfg.dam_height_m : 0,
    area: surfaceAreaM2(y, cfg),
    inflow: qIn,
    outflow: out.total,
    gate: out.gate,
    spillway: out.spillway,
    overflow: overflowCumecs,
    status: statusOf(y, cfg, overflowing),
    substeps,
  };
}

/**
 * Advance one timestep. Mutates `state`, returns the sample it produced.
 *
 *     V_next = V + (Q_in - Q_out) dt,   clamped to 0 <= V <= V_max
 *
 * Both clamps are reported rather than hidden: the surplus above V_max leaves
 * as overflow, and an empty reservoir cannot release water it does not have.
 */
export function step(state, cfg, dtSeconds) {
  const dt = dtSeconds === undefined ? cfg.dt_s : dtSeconds;
  const vMax = capacityM3(cfg);

  // Euler is first order, so subdivide any step that would move too much of
  // the reservoir at once.
  const y0 = levelM(state.volume, cfg);
  const net = inflowCumecs(state.tSeconds, cfg) - outflowCumecs(y0, cfg).total;
  const budget = cfg.max_volume_frac_per_step * vMax;
  let nSub = 1;
  if (budget > 0 && Math.abs(net) * dt > budget) {
    nSub = Math.min(Math.ceil((Math.abs(net) * dt) / budget), 1000);
  }
  const h = dt / nSub;

  let v = state.volume;
  let overVolume = 0;

  for (let i = 0; i < nSub; i++) {
    const y = levelM(v, cfg);
    const qIn = inflowCumecs(state.tSeconds, cfg);
    let qOut = outflowCumecs(y, cfg).total;

    let vNext = v + (qIn - qOut) * h;
    if (vNext > vMax) {
      overVolume += vNext - vMax;
      state.overflowVolume += vNext - vMax;
      vNext = vMax;
    } else if (vNext < 0) {
      qOut = v / h + qIn; // drain what exists, no more
      vNext = 0;
    }

    state.inflowVolume += qIn * h;
    state.outflowVolume += qOut * h;
    state.tSeconds += h;
    v = vNext;
  }

  state.volume = v;
  return sampleOf(state, cfg, dt > 0 ? overVolume / dt : 0, nSub);
}

/** Storage change minus (in - out - overflow), as a percentage of inflow. */
export function massBalanceErrorPct(state, startVolume) {
  const stored = state.volume - startVolume;
  const accounted = state.inflowVolume - state.outflowVolume - state.overflowVolume;
  return (100 * (stored - accounted)) / Math.max(state.inflowVolume, 1e-9);
}
