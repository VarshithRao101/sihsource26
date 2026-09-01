/**
 * charts/timeseries.js - a small canvas line chart. y(t) and V(t) use one each.
 *
 * Deliberately not a charting library: the whole project runs on zero frontend
 * dependencies (AGENTS.md rule 7), and a time series with an auto-scaled axis
 * is about eighty lines. It plots whatever array the controller recorded, at
 * the controller's sampling interval, so the chart cannot drift from the
 * simulation state.
 */

const PAD = { left: 54, right: 12, top: 14, bottom: 26 };

/** 1 / 2 / 5 / 10 ... - the tick spacing a human would have chosen. */
function niceStep(range, target = 4) {
  if (range <= 0) return 1;
  const raw = range / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1;
  return step * mag;
}

export class TimeSeriesChart {
  /** @param {{colour: string, unit: string, refLines?: Array}} opts */
  constructor(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts;
    this.resize();
    window.addEventListener("resize", () => this.resize());
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.w = Math.max(rect.width, 240);
    this.h = Math.max(rect.height, 120);
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /**
   * @param {number[]} ts hours
   * @param {number[]} vs values
   * @param {Array<{value:number,colour:string,label:string}>} refLines
   */
  draw(ts, vs, refLines = []) {
    const ctx = this.ctx;
    const { w, h } = this;
    const plotW = w - PAD.left - PAD.right;
    const plotH = h - PAD.top - PAD.bottom;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0e161f";
    ctx.fillRect(0, 0, w, h);

    if (!ts.length) return;

    const tMax = Math.max(ts[ts.length - 1], 0.05);
    let vMin = Math.min(...vs, ...refLines.map((r) => r.value));
    let vMax = Math.max(...vs, ...refLines.map((r) => r.value));
    if (vMax - vMin < 1e-6) {
      vMax += 1;
      vMin -= 1;
    }
    const padV = (vMax - vMin) * 0.08;
    vMin = Math.max(0, vMin - padV);
    vMax = vMax + padV;

    const px = (t) => PAD.left + (t / tMax) * plotW;
    const py = (v) => PAD.top + plotH - ((v - vMin) / (vMax - vMin)) * plotH;

    // --- grid + axes -----------------------------------------------------
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.strokeStyle = "rgba(95, 113, 131, 0.22)";
    ctx.fillStyle = "#5f7183";
    ctx.lineWidth = 1;

    const vStep = niceStep(vMax - vMin);
    ctx.textAlign = "right";
    for (let v = Math.ceil(vMin / vStep) * vStep; v <= vMax; v += vStep) {
      const y = py(v);
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(w - PAD.right, y);
      ctx.stroke();
      ctx.fillText(v >= 100 ? v.toFixed(0) : v.toFixed(2), PAD.left - 6, y + 3);
    }

    const tStep = niceStep(tMax);
    ctx.textAlign = "center";
    for (let t = 0; t <= tMax + 1e-9; t += tStep) {
      const x = px(t);
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, PAD.top + plotH);
      ctx.stroke();
      ctx.fillText(t.toFixed(t < 10 ? 1 : 0) + " h", x, h - 8);
    }

    // --- reference lines -------------------------------------------------
    for (const ref of refLines) {
      if (ref.value < vMin || ref.value > vMax) continue;
      ctx.save();
      ctx.strokeStyle = ref.colour;
      ctx.globalAlpha = 0.6;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(PAD.left, py(ref.value));
      ctx.lineTo(w - PAD.right, py(ref.value));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = ref.colour;
      ctx.textAlign = "left";
      ctx.fillText(ref.label, PAD.left + 4, py(ref.value) - 3);
      ctx.restore();
    }

    // --- the series ------------------------------------------------------
    const colour = this.opts.colour;

    ctx.beginPath();
    ctx.moveTo(px(ts[0]), py(vs[0]));
    for (let i = 1; i < ts.length; i++) ctx.lineTo(px(ts[i]), py(vs[i]));

    // fill under the line
    ctx.save();
    ctx.lineTo(px(ts[ts.length - 1]), PAD.top + plotH);
    ctx.lineTo(px(ts[0]), PAD.top + plotH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, PAD.top, 0, PAD.top + plotH);
    grad.addColorStop(0, colour + "44");
    grad.addColorStop(1, colour + "05");
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.restore();

    ctx.beginPath();
    ctx.moveTo(px(ts[0]), py(vs[0]));
    for (let i = 1; i < ts.length; i++) ctx.lineTo(px(ts[i]), py(vs[i]));
    ctx.strokeStyle = colour;
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // head marker
    const hx = px(ts[ts.length - 1]);
    const hy = py(vs[vs.length - 1]);
    ctx.fillStyle = colour;
    ctx.beginPath();
    ctx.arc(hx, hy, 3, 0, Math.PI * 2);
    ctx.fill();

    // --- current value ---------------------------------------------------
    ctx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillStyle = colour;
    ctx.textAlign = "right";
    const last = vs[vs.length - 1];
    ctx.fillText(
      `${last >= 100 ? last.toFixed(0) : last.toFixed(2)} ${this.opts.unit}`,
      w - PAD.right,
      PAD.top + 9
    );
  }
}
