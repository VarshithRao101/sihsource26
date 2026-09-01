/**
 * main.js - wiring only. No physics, no formatting, no drawing.
 *
 * Order of events, once per animation frame:
 *   controller.advance()  ->  fixed-dt Euler steps  ->  emit(frame)
 *   this file             ->  hand `frame` to the dam view, the cards, the charts
 *
 * Rendering is a subscriber. It can be slow, it can be skipped, and the
 * numbers stay right either way.
 */

import { DEFAULTS, STATUS } from "./config/constants.js";
import { SimulationController } from "./simulation/controller.js";
import { spillwayCrestM, capacityM3 } from "./simulation/model.js";
import { DamView } from "./components/dam.js";
import { MetricsPanel } from "./components/metrics.js";
import { ControlPanel } from "./components/controls.js";
import { TimeSeriesChart } from "./charts/timeseries.js";

const CHART_REDRAW_MS = 100;

async function loadBackendConfig() {
  // The page works standalone; the backend is authoritative when it is up.
  try {
    const res = await fetch("/api/reservoir/config", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const body = await res.json();
    return { config: { ...DEFAULTS, ...body.defaults }, source: "backend" };
  } catch (err) {
    return { config: { ...DEFAULTS }, source: "built-in defaults" };
  }
}

async function boot() {
  const { config, source } = await loadBackendConfig();
  document.querySelector("[data-config-source]").textContent = source;

  const sim = new SimulationController(config);

  const dam = new DamView(document.getElementById("dam-canvas"));
  const metrics = new MetricsPanel(
    document.getElementById("metrics"),
    document.getElementById("status-banner")
  );
  const controls = new ControlPanel(
    document.getElementById("controls"),
    document.getElementById("transport"),
    sim
  );
  controls.syncFromConfig(sim.config);

  const levelChart = new TimeSeriesChart(document.getElementById("chart-level"), {
    colour: "#5ec8ff",
    unit: "m",
  });
  const volumeChart = new TimeSeriesChart(document.getElementById("chart-volume"), {
    colour: "#8f7dff",
    unit: "MCM",
  });

  const massEl = document.querySelector("[data-mass-error]");
  let lastChartMs = 0;

  sim.onTick((frame) => {
    const now = performance.now();
    dam.draw(frame, now);
    metrics.update(frame);
    controls.update(frame);

    if (now - lastChartMs > CHART_REDRAW_MS) {
      lastChartMs = now;
      const cfg = frame.config;
      levelChart.draw(frame.history.t, frame.history.y, [
        { value: cfg.dam_height_m, colour: STATUS.overflow.colour, label: "crest H" },
        { value: spillwayCrestM(cfg), colour: STATUS.high.colour, label: "spillway" },
        { value: cfg.low_frac * cfg.dam_height_m, colour: STATUS.low.colour, label: "low guide" },
      ]);
      volumeChart.draw(frame.history.t, frame.history.v, [
        { value: capacityM3(cfg) / 1e6, colour: STATUS.overflow.colour, label: "V_max" },
      ]);
      massEl.textContent = `${frame.massErrorPct.toExponential(1)} %`;
    }
  });

  sim.attach();

  // Handle for the browser console and for scripted checks. `advance(ms)` can
  // be called by hand to step the model without the animation frame, which is
  // how the UI gets verified in a headless tab (rAF does not fire when the
  // page is not painting).
  window.reservoirSim = sim;

  document.addEventListener("keydown", (e) => {
    if (e.target instanceof HTMLInputElement) return;
    if (e.code === "Space") {
      e.preventDefault();
      sim.toggle();
    } else if (e.key.toLowerCase() === "r") {
      sim.reset();
    }
  });
}

boot();
