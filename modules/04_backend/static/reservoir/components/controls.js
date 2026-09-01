/**
 * components/controls.js - transport bar and the parameter sliders.
 *
 * The sliders are generated from CONTROL_GROUPS in config/constants.js, so
 * adding a parameter to the model means adding one line there, not editing
 * HTML. Each slider writes straight into the controller's config; controls
 * marked `reset: true` redefine the reservoir and therefore restart the run.
 */

import { CONTROL_GROUPS, SPEEDS, DEFAULT_SPEED_INDEX } from "../config/constants.js";

export class ControlPanel {
  /**
   * @param {HTMLElement} root container for the slider groups
   * @param {HTMLElement} transport container for start/pause/reset/speed
   * @param {import("../simulation/controller.js").SimulationController} sim
   */
  constructor(root, transport, sim) {
    this.sim = sim;
    this.inputs = {};
    this.buildTransport(transport);
    this.buildGroups(root);
  }

  buildTransport(el) {
    el.innerHTML = `
      <button class="btn btn-primary" data-act="toggle">Start</button>
      <button class="btn" data-act="reset">Reset</button>
      <label class="speed">
        <span class="speed-label">Speed</span>
        <input type="range" min="0" max="${SPEEDS.length - 1}" step="1"
               value="${DEFAULT_SPEED_INDEX}" data-act="speed">
        <output data-speed-out></output>
      </label>`;

    this.toggleBtn = el.querySelector('[data-act="toggle"]');
    this.speedOut = el.querySelector("[data-speed-out]");

    this.toggleBtn.addEventListener("click", () => this.sim.toggle());
    el.querySelector('[data-act="reset"]').addEventListener("click", () => this.sim.reset());
    el.querySelector('[data-act="speed"]').addEventListener("input", (e) => {
      this.sim.setSpeed(SPEEDS[Number(e.target.value)]);
    });
  }

  buildGroups(root) {
    for (const group of CONTROL_GROUPS) {
      const section = document.createElement("section");
      section.className = "control-group";
      section.innerHTML = `<h3>${group.title}</h3>`;

      for (const c of group.controls) {
        const row = document.createElement("label");
        row.className = "control";
        row.innerHTML = `
          <div class="control-head">
            <span class="control-name">${c.label}${c.reset ? '<em title="restarts the run">↺</em>' : ""}</span>
            <output data-out></output>
          </div>
          <input type="range" min="${c.min}" max="${c.max}" step="${c.step}"
                 value="${this.sim.config[c.key]}">
          ${c.hint ? `<div class="control-hint">${c.hint}</div>` : ""}`;

        const input = row.querySelector("input");
        const out = row.querySelector("[data-out]");
        const render = (v) => {
          const digits = c.step < 0.1 ? 2 : c.step < 1 ? 1 : 0;
          out.textContent = `${Number(v).toFixed(digits)} ${c.unit}`.trim();
        };
        render(this.sim.config[c.key]);

        input.addEventListener("input", (e) => {
          const v = Number(e.target.value);
          render(v);
          this.sim.setConfigValue(c.key, v, Boolean(c.reset));
        });

        this.inputs[c.key] = { input, render };
        section.appendChild(row);
      }
      root.appendChild(section);
    }
  }

  /** Called on every tick - keeps the button label and speed readout truthful. */
  update(frame) {
    this.toggleBtn.textContent = frame.running ? "Pause" : "Start";
    this.toggleBtn.classList.toggle("btn-running", frame.running);
    this.speedOut.textContent =
      frame.speed >= 3600
        ? `${frame.speed / 3600} sim-hour / s`
        : frame.speed >= 60
        ? `${frame.speed / 60} sim-min / s`
        : `${frame.speed}× real time`;
  }

  /** Push config values back into the sliders (used after a config fetch). */
  syncFromConfig(config) {
    for (const key in this.inputs) {
      if (config[key] === undefined) continue;
      this.inputs[key].input.value = config[key];
      this.inputs[key].render(config[key]);
    }
  }
}
