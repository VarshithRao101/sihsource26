/**
 * components/dam.js - the dam cross-section.
 *
 * Everything in this drawing is a function of the simulation state. The water
 * height is y from the model, the valley shape is the storage curve
 * V = V_max (y/H)^k drawn sideways, and the jets appear exactly when the
 * corresponding term of Q_out(y) is non-zero. Nothing here is keyframed - set
 * k = 1 and the banks become vertical because that really is a prismatic tank.
 *
 * The only cosmetic liberty is the ripple on the water surface, which is a
 * sine of wall-clock time and carries no information.
 */

import { STATUS } from "../config/constants.js";
import { spillwayCrestM, outletInvertM } from "../simulation/model.js";

const PAD = { left: 52, right: 26, top: 22, bottom: 34 };

export class DamView {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.resize();
    window.addEventListener("resize", () => this.resize());
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.w = Math.max(rect.width, 320);
    this.h = Math.max(rect.height, 240);
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  draw(frame, nowMs) {
    const { sample: s, config: cfg } = frame;
    const ctx = this.ctx;
    const w = this.w;
    const h = this.h;

    const bedY = h - PAD.bottom;
    const topY = PAD.top;
    const hMax = cfg.dam_height_m * 1.08; // headroom above the crest
    const yPix = (level) => bedY - (level / hMax) * (bedY - topY);

    const xDam = PAD.left + (w - PAD.left - PAD.right) * 0.7;
    const damBaseHalf = 22;
    const damCrestHalf = 9;

    // Valley half-width at a given level, from the storage curve. The exponent
    // is the model's k, so the picture and the equation cannot disagree.
    const wMax = xDam - damBaseHalf - PAD.left - 8;
    const wMin = wMax * 0.16;
    const bankX = (level) => {
      const f = Math.pow(Math.min(Math.max(level, 0) / cfg.dam_height_m, 1), cfg.storage_exponent - 1);
      return xDam - damBaseHalf - (wMin + (wMax - wMin) * f);
    };

    ctx.clearRect(0, 0, w, h);

    // --- background ------------------------------------------------------
    const sky = ctx.createLinearGradient(0, topY - 14, 0, bedY);
    sky.addColorStop(0, "#0d151f");
    sky.addColorStop(1, "#111c27");
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    // --- valley walls ----------------------------------------------------
    ctx.beginPath();
    ctx.moveTo(0, bedY);
    ctx.lineTo(0, yPix(cfg.dam_height_m * 1.02));
    for (let i = 0; i <= 40; i++) {
      const level = (cfg.dam_height_m * 1.02 * (40 - i)) / 40;
      ctx.lineTo(bankX(level), yPix(level));
    }
    ctx.lineTo(xDam - damBaseHalf, bedY);
    ctx.closePath();
    ctx.fillStyle = "#1b2530";
    ctx.fill();

    // river bed downstream
    ctx.fillStyle = "#1b2530";
    ctx.fillRect(xDam + damBaseHalf, bedY - 6, w - xDam - damBaseHalf, h - bedY + 6);
    ctx.fillStyle = "#141d27";
    ctx.fillRect(0, bedY, w, h - bedY);

    // --- water -----------------------------------------------------------
    const yw = Math.max(s.level, 0);
    const surfaceY = yPix(yw);
    if (yw > 0.01) {
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(bankX(0), bedY);
      for (let i = 0; i <= 40; i++) {
        const level = (yw * i) / 40;
        ctx.lineTo(bankX(level), yPix(level));
      }
      // rippled surface, cosmetic only
      const amp = Math.min(2.2, 0.35 + s.inflow / 900);
      for (let x = bankX(yw); x <= xDam - damCrestHalf; x += 6) {
        const wave = Math.sin(x * 0.06 + nowMs * 0.0022) * amp;
        ctx.lineTo(x, surfaceY + wave);
      }
      ctx.lineTo(xDam - damCrestHalf, surfaceY);
      ctx.lineTo(xDam - damBaseHalf, bedY);
      ctx.closePath();

      const water = ctx.createLinearGradient(0, surfaceY, 0, bedY);
      water.addColorStop(0, "#3fa9f5");
      water.addColorStop(0.55, "#1f6fb2");
      water.addColorStop(1, "#123a5e");
      ctx.fillStyle = water;
      ctx.fill();

      ctx.strokeStyle = "rgba(147, 214, 255, 0.85)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.restore();
    }

    // --- dam body --------------------------------------------------------
    const crestY = yPix(cfg.dam_height_m);
    ctx.beginPath();
    ctx.moveTo(xDam - damBaseHalf, bedY);
    ctx.lineTo(xDam - damCrestHalf, crestY);
    ctx.lineTo(xDam + damCrestHalf, crestY);
    ctx.lineTo(xDam + damBaseHalf, bedY);
    ctx.closePath();
    const concrete = ctx.createLinearGradient(xDam - damBaseHalf, 0, xDam + damBaseHalf, 0);
    concrete.addColorStop(0, "#67717d");
    concrete.addColorStop(0.5, "#4d5661");
    concrete.addColorStop(1, "#39424c");
    ctx.fillStyle = concrete;
    ctx.fill();
    ctx.strokeStyle = "#8a949f";
    ctx.lineWidth = 1;
    ctx.stroke();

    // --- reference levels ------------------------------------------------
    this.refLine(yPix(cfg.dam_height_m), xDam + damBaseHalf, "CREST  H", "#ff4d5e");
    this.refLine(yPix(spillwayCrestM(cfg)), xDam + damBaseHalf, "SPILLWAY", "#ff8c42");
    this.refLine(yPix(cfg.low_frac * cfg.dam_height_m), xDam + damBaseHalf, "LOW GUIDE", "#f5b301");

    // --- jets ------------------------------------------------------------
    // Each one leaves the downstream face at the elevation of the structure it
    // comes from, and only while its term of Q_out(y) is carrying water.
    const faceX = (py) =>
      xDam + damCrestHalf +
      ((py - crestY) / Math.max(bedY - crestY, 1)) * (damBaseHalf - damCrestHalf);

    if (s.spillway > 0.01) {
      const py = yPix(spillwayCrestM(cfg));
      this.jet(ctx, faceX(py), py, bedY, s.spillway, nowMs, "#7fd4ff");
    }
    if (s.gate > 0.01) {
      const py = yPix(outletInvertM(cfg));
      this.jet(ctx, faceX(py), py, bedY, s.gate, nowMs, "#4aa8e0");
    }
    if (s.overflow > 0.01) {
      this.jet(ctx, faceX(crestY), crestY, bedY, s.overflow, nowMs, "#ff6b7a");
    }

    // downstream water surface, thickness from total discharge
    const dsDepth = Math.min(18, 3 + Math.pow(s.outflow + s.overflow, 0.42));
    ctx.fillStyle = "rgba(63, 169, 245, 0.55)";
    ctx.fillRect(xDam + damBaseHalf, bedY - dsDepth, w - xDam - damBaseHalf, dsDepth);

    // --- axis ------------------------------------------------------------
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillStyle = "#5f7183";
    ctx.strokeStyle = "rgba(95, 113, 131, 0.28)";
    ctx.lineWidth = 1;
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const level = (cfg.dam_height_m * i) / ticks;
      const py = yPix(level);
      ctx.beginPath();
      ctx.moveTo(PAD.left - 6, py);
      ctx.lineTo(w - PAD.right, py);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.fillText(level.toFixed(0) + " m", PAD.left - 10, py + 4);
    }

    // --- current level marker -------------------------------------------
    const colour = STATUS[s.status].colour;
    ctx.strokeStyle = colour;
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(PAD.left - 6, surfaceY);
    ctx.lineTo(bankX(yw), surfaceY);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = colour;
    const label = `y = ${s.level.toFixed(2)} m`;
    ctx.textAlign = "left";
    ctx.font = "600 12px ui-monospace, SFMono-Regular, Menlo, monospace";
    const lx = Math.min(bankX(yw) + 8, w - PAD.right - ctx.measureText(label).width - 4);
    ctx.fillText(label, lx, surfaceY - 7);

    // --- footer ----------------------------------------------------------
    // The geometry caption is dropped on narrow screens so the two captions
    // never collide; the discharge readout is the one that has to survive.
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillStyle = "#5f7183";
    if (w > 620) {
      ctx.textAlign = "left";
      ctx.fillText(
        `valley section drawn from V = V_max (y/H)^${cfg.storage_exponent.toFixed(1)}`,
        PAD.left - 6,
        h - 12
      );
    }
    ctx.textAlign = "right";
    ctx.fillText(`downstream  ${(s.outflow + s.overflow).toFixed(0)} m³/s`, w - PAD.right, h - 12);
  }

  refLine(py, xFrom, text, colour) {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 0.55;
    ctx.setLineDash([3, 5]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(PAD.left - 6, py);
    ctx.lineTo(this.w - PAD.right, py);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 0.9;
    ctx.fillStyle = colour;
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "right";
    ctx.fillText(text, this.w - PAD.right, py - 4);
    ctx.restore();
  }

  /** A falling stream whose width comes from the discharge it represents. */
  jet(ctx, fromX, fromY, bedY, discharge, nowMs, colour) {
    const thickness = Math.min(16, 1.5 + Math.pow(discharge, 0.36));
    const drop = Math.max(bedY - fromY, 1);
    const reach = 16 + Math.min(26, Math.pow(discharge, 0.3) * 4);
    ctx.save();
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 0.8;
    ctx.lineWidth = thickness;
    ctx.lineCap = "round";
    ctx.setLineDash([14, 7]);
    ctx.lineDashOffset = -(nowMs * 0.12) % 21;
    ctx.beginPath();
    ctx.moveTo(fromX, fromY);
    ctx.quadraticCurveTo(fromX + reach * 0.55, fromY + drop * 0.6, fromX + reach, bedY - 4);
    ctx.stroke();
    ctx.restore();
  }
}
