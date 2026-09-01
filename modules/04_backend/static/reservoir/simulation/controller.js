/**
 * simulation/controller.js - who owns time.
 *
 * The rule this file exists to enforce: **the timestep is fixed**. The speed
 * control changes how many simulated seconds pass per real second, never dt.
 * A frame that arrives late runs more steps of the same size; it never runs
 * one bigger step. So the trajectory on screen at 3600x is the same trajectory
 * as at 1x, and the same one pytest checks against reservoir.py.
 *
 * The animation frame is only a clock source. Rendering subscribes to `tick`
 * and reads whatever the latest sample is - it cannot influence the physics.
 */

import {
  MAX_STEPS_PER_FRAME,
  CHART_SAMPLE_S,
  CHART_MAX_POINTS,
  SPEEDS,
  DEFAULT_SPEED_INDEX,
} from "../config/constants.js";
import { initialState, sampleOf, step, capacityM3, massBalanceErrorPct } from "./model.js";

/** Real seconds allowed to accumulate in one frame. Stops a backgrounded tab
 *  from dumping a minute of catch-up into a single frame. */
const MAX_REAL_DT_S = 0.1;

export class SimulationController {
  constructor(config) {
    this.config = { ...config };
    this.speed = SPEEDS[DEFAULT_SPEED_INDEX];
    this.running = false;
    this.listeners = [];
    this.reset();
  }

  // -- lifecycle ----------------------------------------------------------

  reset() {
    this.state = initialState(this.config);
    this.startVolume = this.state.volume;
    this.accumulator = 0;
    this.lastFrameMs = null;
    this.stepCount = 0;
    this.history = { t: [], y: [], v: [] };
    this.nextChartSampleS = 0;
    this.latest = sampleOf(this.state, this.config);
    this.peak = { level: this.latest.level, inflow: this.latest.inflow, outflow: this.latest.outflow };
    this.recordHistory(this.latest);
    this.emit();
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.lastFrameMs = null;
    this.emit();
  }

  pause() {
    this.running = false;
    this.emit();
  }

  toggle() {
    this.running ? this.pause() : this.start();
  }

  setSpeed(multiplier) {
    this.speed = multiplier;
    this.emit();
  }

  /**
   * Change one config field while the model is live.
   *
   * `structural` fields (capacity, height, exponent, initial fill) redefine the
   * reservoir itself, so they restart the run. Everything else - inflow,
   * release, spillway - takes effect on the very next step, which is the whole
   * point of the dashboard: turn the inflow up and watch V, and therefore y,
   * respond.
   */
  setConfigValue(key, value, structural = false) {
    this.config = { ...this.config, [key]: value };
    if (structural) {
      const wasRunning = this.running;
      this.reset();
      if (wasRunning) this.start();
    } else {
      // Volume is still valid, but capacity-relative readings are not.
      this.state.volume = Math.min(this.state.volume, capacityM3(this.config));
      this.latest = sampleOf(this.state, this.config);
      this.emit();
    }
  }

  // -- the loop -----------------------------------------------------------

  attach() {
    const frame = (nowMs) => {
      this.advance(nowMs);
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  /** One animation frame's worth of simulated time, in fixed dt steps. */
  advance(nowMs) {
    if (this.lastFrameMs === null) this.lastFrameMs = nowMs;
    const realDt = Math.min((nowMs - this.lastFrameMs) / 1000, MAX_REAL_DT_S);
    this.lastFrameMs = nowMs;

    if (this.running) {
      this.accumulator += realDt * this.speed;
      const dt = this.config.dt_s;
      let steps = 0;
      while (this.accumulator >= dt && steps < MAX_STEPS_PER_FRAME) {
        this.latest = step(this.state, this.config, dt);
        this.accumulator -= dt;
        steps += 1;
        this.stepCount += 1;
        this.recordHistory(this.latest);
      }
      // Anything left is dropped rather than folded into an oversized step:
      // the clock falls behind, the physics stays correct.
      if (steps >= MAX_STEPS_PER_FRAME) this.accumulator = 0;

      this.peak.level = Math.max(this.peak.level, this.latest.level);
      this.peak.inflow = Math.max(this.peak.inflow, this.latest.inflow);
      this.peak.outflow = Math.max(this.peak.outflow, this.latest.outflow);
    }

    this.emit();
  }

  recordHistory(sample) {
    if (sample.tSeconds + 1e-9 < this.nextChartSampleS) return;
    this.nextChartSampleS = sample.tSeconds + CHART_SAMPLE_S;
    const h = this.history;
    h.t.push(sample.tHours);
    h.y.push(sample.level);
    h.v.push(sample.volumeMcm);
    if (h.t.length > CHART_MAX_POINTS) {
      h.t.shift();
      h.y.shift();
      h.v.shift();
    }
  }

  // -- subscription -------------------------------------------------------

  onTick(fn) {
    this.listeners.push(fn);
  }

  emit() {
    const frame = {
      sample: this.latest,
      config: this.config,
      history: this.history,
      running: this.running,
      speed: this.speed,
      steps: this.stepCount,
      peak: this.peak,
      massErrorPct: massBalanceErrorPct(this.state, this.startVolume),
    };
    for (const fn of this.listeners) fn(frame);
  }
}
