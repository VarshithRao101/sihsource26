/**
 * components/metrics.js - the numbers, and the warning banner.
 *
 * Every card reads a field of the sample the controller just produced. There
 * is no separate display state to fall out of sync with the model.
 */

import { STATUS, MCM } from "../config/constants.js";
import { spillwayCrestM, capacityM3 } from "../simulation/model.js";

/** Simulated seconds as d / h / m, the way an operator reads a gauge log. */
export function formatDuration(seconds) {
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const hrs = Math.floor(seconds / 3600);
  if (hrs >= 24) {
    const d = Math.floor(hrs / 24);
    return `${d} d ${String(hrs % 24).padStart(2, "0")} h ${String(m).padStart(2, "0")} m`;
  }
  return `${String(hrs).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const num = (v, digits = 1) =>
  v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** id -> { label, unit, value(frame), sub(frame), tone } */
const CARDS = [
  {
    id: "time",
    label: "Simulation time  t",
    unit: "",
    value: (f) => formatDuration(f.sample.tSeconds),
    sub: (f) => `${num(f.sample.tHours, 2)} hr · dt = ${f.config.dt_s} s · ${f.steps.toLocaleString()} steps`,
  },
  {
    id: "level",
    label: "Water level  y",
    unit: "m",
    value: (f) => num(f.sample.level, 2),
    sub: (f) => `${num(100 * f.sample.levelFrac, 1)} % of H · peak ${num(f.peak.level, 2)} m`,
    tone: (f) => STATUS[f.sample.status].colour,
  },
  {
    id: "volume",
    label: "Water volume  V",
    unit: "MCM",
    value: (f) => num(f.sample.volumeMcm, 3),
    sub: (f) => `${num(100 * f.sample.volumeFrac, 1)} % of V_max · ${num(f.sample.volume / 1000, 0)} × 10³ m³`,
  },
  {
    id: "inflow",
    label: "Inflow  Q_in(t)",
    unit: "m³/s",
    value: (f) => num(f.sample.inflow, 1),
    sub: (f) =>
      f.config.flood_peak_cumecs > 0
        ? `base ${num(f.config.base_inflow_cumecs, 0)} + flood wave`
        : "steady base flow",
    tone: () => "#5ec8ff",
  },
  {
    id: "outflow",
    label: "Outflow  Q_out(y)",
    unit: "m³/s",
    value: (f) => num(f.sample.outflow, 1),
    sub: (f) => `gate ${num(f.sample.gate, 1)} · spillway ${num(f.sample.spillway, 1)}`,
    tone: () => "#ffb265",
  },
  {
    id: "net",
    label: "Net  dV/dt",
    unit: "m³/s",
    value: (f) => {
      const net = f.sample.inflow - f.sample.outflow - f.sample.overflow;
      return (net >= 0 ? "+" : "") + num(net, 1);
    },
    sub: (f) => {
      const net = f.sample.inflow - f.sample.outflow - f.sample.overflow;
      if (Math.abs(net) < 0.05) return "steady state — Q_in = Q_out";
      return net > 0 ? "reservoir filling" : "reservoir drawing down";
    },
    tone: (f) => {
      const net = f.sample.inflow - f.sample.outflow - f.sample.overflow;
      return Math.abs(net) < 0.05 ? "#3ddc84" : net > 0 ? "#5ec8ff" : "#ffb265";
    },
  },
  {
    id: "overflow",
    label: "Overflow over crest",
    unit: "m³/s",
    value: (f) => num(f.sample.overflow, 1),
    sub: (f) =>
      f.sample.overflow > 0.01
        ? "V is clamped at V_max — surplus is passing the crest"
        : "none — V is below capacity",
    tone: (f) => (f.sample.overflow > 0.01 ? "#ff4d5e" : undefined),
  },
  {
    id: "area",
    label: "Water surface  A = dV/dy",
    unit: "km²",
    value: (f) => num(f.sample.area / 1e6, 3),
    sub: (f) =>
      f.config.storage_exponent === 1
        ? "constant — y = V/A exactly"
        : `varies with y  (k = ${f.config.storage_exponent.toFixed(1)})`,
  },
];

export class MetricsPanel {
  constructor(container, bannerEl) {
    this.container = container;
    this.banner = bannerEl;
    this.cells = {};

    for (const card of CARDS) {
      const el = document.createElement("div");
      el.className = "metric";
      el.innerHTML = `
        <div class="metric-label">${card.label}</div>
        <div class="metric-value"><span data-v></span><span class="metric-unit">${card.unit}</span></div>
        <div class="metric-sub" data-s></div>`;
      container.appendChild(el);
      this.cells[card.id] = {
        card,
        value: el.querySelector("[data-v]"),
        sub: el.querySelector("[data-s]"),
      };
    }
  }

  update(frame) {
    for (const id in this.cells) {
      const { card, value, sub } = this.cells[id];
      value.textContent = card.value(frame);
      sub.textContent = card.sub(frame);
      value.style.color = (card.tone && card.tone(frame)) || "";
    }

    const st = STATUS[frame.sample.status];
    const cfg = frame.config;
    this.banner.dataset.status = frame.sample.status;
    this.banner.style.setProperty("--status-colour", st.colour);
    this.banner.querySelector("[data-status-label]").textContent = st.label;
    this.banner.querySelector("[data-status-note]").textContent = st.note;
    this.banner.querySelector("[data-status-detail]").textContent =
      `y = ${num(frame.sample.level, 2)} m of H = ${num(cfg.dam_height_m, 0)} m` +
      ` · spillway crest ${num(spillwayCrestM(cfg), 1)} m` +
      ` · V = ${num(frame.sample.volumeMcm, 3)} / ${num(capacityM3(cfg) / MCM, 2)} MCM`;
  }
}
