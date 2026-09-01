/**
 * config/constants.js - every number the simulation uses, in one place.
 *
 * Physical constants are the same ones modules/04_backend/reservoir.py uses,
 * which in turn takes them from shared/. If you change one here, change it
 * there: reservoir.py is the reference implementation and this file mirrors
 * it so the browser can step the model without a round trip per frame.
 *
 * main.js overwrites DEFAULTS from GET /api/reservoir/config when the backend
 * is reachable, so the server stays the single source of truth when it is up
 * and the page still works when opened straight off disk.
 */

/** m/s^2, ISO 80000-3. shared.contract.GRAVITY. */
export const GRAVITY = 9.80665;

/** Sharp-edged orifice discharge coefficient. Fread (1988), BREACH, s.3. */
export const ORIFICE_CD = 0.6;

/** Broad-crested weir coefficient, SI, for Q = C L H^1.5. USACE HEC-RAS. */
export const WEIR_C_SI = 1.7;

/** m3 per million cubic metres. The contract reports volume in MCM. */
export const MCM = 1.0e6;

/** Starting configuration. Chungthang-sized, to match the rest of the project. */
export const DEFAULTS = {
  dam_height_m: 60.0,
  capacity_mcm: 5.0,
  storage_exponent: 2.7,

  base_inflow_cumecs: 60.0,
  flood_peak_cumecs: 0.0,
  flood_peak_time_hr: 6.0,
  flood_duration_hr: 3.0,

  target_release_cumecs: 40.0,
  outlet_invert_frac: 0.05,
  spillway_crest_frac: 0.85,
  spillway_length_m: 60.0,

  initial_volume_frac: 0.5,

  dt_s: 10.0,
  max_volume_frac_per_step: 0.01,

  low_frac: 0.25,
};

/**
 * Simulated seconds advanced per second of wall clock.
 *
 * This is the only thing the speed control touches. It never changes dt, so
 * the numerical answer is identical at every speed - see
 * test_reservoir.test_result_is_insensitive_to_timestep.
 */
export const SPEEDS = [1, 10, 60, 300, 900, 3600];
export const DEFAULT_SPEED_INDEX = 3;

/** Never run more than this many steps in one animation frame. */
export const MAX_STEPS_PER_FRAME = 3000;

/** Record a chart point every this many simulated seconds. */
export const CHART_SAMPLE_S = 60;

/** Ring buffer length for the charts. 4000 points at 60 s is 66 simulated hours. */
export const CHART_MAX_POINTS = 4000;

/** Warning states. The colours are also used by the dam graphic and the banner. */
export const STATUS = {
  low: {
    label: "LOW WATER",
    colour: "#f5b301",
    note: "Below the dead-storage guide level - supply and hydropower at risk.",
  },
  normal: {
    label: "NORMAL",
    colour: "#3ddc84",
    note: "Between the low guide level and the spillway crest.",
  },
  high: {
    label: "HIGH - SPILLING",
    colour: "#ff8c42",
    note: "Above the spillway crest. Water is leaving over the weir.",
  },
  overflow: {
    label: "OVERFLOW / DANGER",
    colour: "#ff4d5e",
    note: "Reservoir is at capacity. Surplus inflow is passing over the crest.",
  },
};

/**
 * The control panel, declared rather than hand-written in HTML.
 * `key` matches a field of the config object exactly.
 */
export const CONTROL_GROUPS = [
  {
    title: "Inflow  Q_in(t)",
    controls: [
      { key: "base_inflow_cumecs", label: "Base inflow", unit: "m³/s", min: 0, max: 3000, step: 5 },
      { key: "flood_peak_cumecs", label: "Flood wave peak", unit: "m³/s", min: 0, max: 5000, step: 25 },
      { key: "flood_peak_time_hr", label: "Flood peak at", unit: "hr", min: 0, max: 48, step: 0.5 },
      { key: "flood_duration_hr", label: "Flood duration", unit: "hr", min: 0.5, max: 24, step: 0.5 },
    ],
  },
  {
    title: "Outflow  Q_out(y)",
    controls: [
      { key: "target_release_cumecs", label: "Gate release at full head", unit: "m³/s", min: 0, max: 2000, step: 5 },
      { key: "spillway_length_m", label: "Spillway crest length", unit: "m", min: 5, max: 400, step: 5 },
      { key: "spillway_crest_frac", label: "Spillway crest", unit: "× H", min: 0.4, max: 1.0, step: 0.01 },
    ],
  },
  {
    title: "Dam geometry",
    controls: [
      { key: "capacity_mcm", label: "Capacity  V_max", unit: "MCM", min: 0.1, max: 100, step: 0.1, reset: true },
      { key: "dam_height_m", label: "Dam height  H", unit: "m", min: 5, max: 300, step: 1, reset: true },
      {
        key: "storage_exponent",
        label: "Storage exponent  k",
        unit: "",
        min: 1.0,
        max: 4.0,
        step: 0.1,
        reset: true,
        hint: "V = V_max (y/H)^k.  k = 1 is a prismatic tank, i.e. exactly y = V/A.",
      },
      { key: "initial_volume_frac", label: "Initial volume", unit: "× V_max", min: 0, max: 1, step: 0.01, reset: true },
    ],
  },
];
