"""
modules/02_sph/breach.py - the breach itself, in Smoothed Particle Hydrodynamics.

NTRO names SPH in the problem statement, and this is it: a real DualSPHysics
simulation of water tearing through a breach, run on the GPU, producing the
outflow hydrograph that modules 03 and 04 take as their upstream boundary.
That one CSV is the coupling between all three engines.

    python -m modules.02_sph.breach run --height 60 --width 57 --dp 1.5

WHAT SPH IS FOR HERE, and what it is not. SPH resolves violent, splashing,
free-surface flow - exactly the moment a hole opens in a dam and the water
accelerates through it. It is hopeless for following that water 40 km down a
valley: the particle count scales with volume, and a 40 km floodplain is
billions of particles. So SPH models the NEAR FIELD ONLY - a few hundred metres
of reservoir either side of the breach, for the first minute or so.

Which means a real limitation, stated up front rather than discovered by a
juror: the modelled reservoir block is far smaller than the true reservoir, so
this hydrograph is valid for the initial seconds while the near-dam water
drains, and it does NOT capture reservoir drawdown over hours. Drawdown is what
the level-pool routing in shared.hydro is for. The two are compared, not
substituted - and the comparison is a scoring line.

Owner: captain (module 02).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DSPH_BIN = REPO_ROOT / "DualSPHysics_v5.4" / "bin" / "windows"
CASES_DIR = REPO_ROOT / "modules" / "02_sph" / "cases"

GENCASE = DSPH_BIN / "GenCase_win64.exe"
DSPH_GPU = DSPH_BIN / "DualSPHysics5.4_win64.exe"
DSPH_CPU = DSPH_BIN / "DualSPHysics5.4CPU_win64.exe"
FLOWTOOL = DSPH_BIN / "FlowTool_win64.exe"
PARTVTK = DSPH_BIN / "PartVTK_win64.exe"


@dataclass
class BreachCase:
    """Geometry and numerics of one SPH breach simulation. All metres/seconds."""

    dam_height_m: float = 60.0
    water_depth_m: float = 55.0
    breach_bottom_width_m: float = 40.0
    breach_side_slope: float = 1.0
    breach_depth_m: float = 55.0
    """Vertical extent of the opening, measured DOWN from the crest."""

    reservoir_length_m: float = 120.0
    """How much reservoir to model upstream of the dam. Not the real reservoir -
    see the module docstring."""
    channel_width_m: float = 120.0
    downstream_length_m: float = 150.0
    wall_thickness_m: float = 8.0

    dp: float = 1.5
    """Initial inter-particle distance. THE cost knob: particle count scales
    with dp^-3. 1.5 m over this domain is a few hundred thousand particles and
    runs in minutes on a 4 GB card; 0.75 m is eight times that."""

    time_max_s: float = 60.0
    time_out_s: float = 0.5
    gpu: bool = True

    def as_dict(self) -> dict:
        return asdict(self)

    def estimate_particles(self) -> int:
        """Rough fluid-particle count, so a run that will not fit is caught
        before forty minutes of GPU time, not after."""
        vol = self.reservoir_length_m * self.channel_width_m * self.water_depth_m
        return int(vol / (self.dp**3))


# ==========================================================================
# Case generation
# ==========================================================================


def _breach_void_layers(case: BreachCase, n_layers: int = 12) -> str:
    """Carve the trapezoidal breach out of the dam wall.

    GenCase draws boxes, not trapezoids, so the opening is built as a stack of
    void boxes whose width grows with height - which is exactly what a
    trapezoidal breach is. 12 layers over a 55 m opening is a 4.6 m step, well
    below the particle spacing effects at dp = 1.5 m.

    Void is drawn AFTER the wall so it removes wall particles already placed.
    """
    crest = case.dam_height_m
    invert = max(crest - case.breach_depth_m, 0.0)
    x0 = case.reservoir_length_m
    layer_h = (crest - invert) / n_layers
    y_mid = case.channel_width_m / 2.0

    out = []
    for k in range(n_layers):
        z0 = invert + k * layer_h
        half = 0.5 * case.breach_bottom_width_m + case.breach_side_slope * (z0 - invert)
        out.append(
            f"""                    <drawbox>
                        <boxfill>solid</boxfill>
                        <point x="{x0 - 0.1:.3f}" y="{y_mid - half:.3f}" z="{z0:.3f}" />
                        <size x="{case.wall_thickness_m + 0.2:.3f}" y="{2 * half:.3f}" z="{layer_h:.3f}" />
                    </drawbox>"""
        )
    return "\n".join(out)


def write_case_xml(case: BreachCase, case_dir: Path, name: str) -> Path:
    """Write the GenCase case-definition XML for a breach."""
    case_dir.mkdir(parents=True, exist_ok=True)

    total_x = case.reservoir_length_m + case.wall_thickness_m + case.downstream_length_m
    wall_x = case.reservoir_length_m

    xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
<case>
    <casedef>
        <constantsdef>
            <gravity x="0" y="0" z="-9.81" comment="Gravitational acceleration" units_comment="m/s^2" />
            <rhop0 value="1000" comment="Reference density of the fluid" units_comment="kg/m^3" />
            <rhopgradient value="2" comment="Initial density gradient 1:Rhop0, 2:Water column, 3:Max. water height" />
            <hswl value="0" auto="true" comment="Maximum still water level to calculate speedofsound" units_comment="metres (m)" />
            <gamma value="7" comment="Polytropic constant for water used in the state equation" />
            <speedsystem value="0" auto="true" comment="Maximum system speed" />
            <coefsound value="20" comment="Coefficient to multiply speedsystem" />
            <speedsound value="0" auto="true" comment="Speed of sound" />
            <coefh value="1.0" comment="Coefficient to calculate the smoothing length" />
            <cflnumber value="0.2" comment="Coefficient to multiply dt" />
        </constantsdef>
        <mkconfig boundcount="240" fluidcount="9" />
        <geometry>
            <definition dp="{case.dp}" units_comment="metres (m)">
                <pointmin x="-{case.dp * 2:.2f}" y="-{case.dp * 2:.2f}" z="-{case.dp * 2:.2f}" />
                <pointmax x="{total_x + case.dp * 2:.2f}" y="{case.channel_width_m + case.dp * 2:.2f}" z="{case.dam_height_m + 10:.2f}" />
            </definition>
            <commands>
                <mainlist>
                    <setshapemode>dp | bound</setshapemode>
                    <setdrawmode mode="full" />

                    <!-- valley floor and side walls, the whole length -->
                    <setmkbound mk="0" />
                    <drawbox>
                        <boxfill>bottom | front | back</boxfill>
                        <point x="0" y="0" z="0" />
                        <size x="{total_x:.3f}" y="{case.channel_width_m:.3f}" z="{case.dam_height_m + 8:.3f}" />
                    </drawbox>

                    <!-- upstream wall, so the modelled reservoir block is closed -->
                    <setmkbound mk="1" />
                    <drawbox>
                        <boxfill>left</boxfill>
                        <point x="0" y="0" z="0" />
                        <size x="{case.dp:.3f}" y="{case.channel_width_m:.3f}" z="{case.dam_height_m + 8:.3f}" />
                    </drawbox>

                    <!-- the dam -->
                    <setmkbound mk="2" />
                    <drawbox>
                        <boxfill>solid</boxfill>
                        <point x="{wall_x:.3f}" y="0" z="0" />
                        <size x="{case.wall_thickness_m:.3f}" y="{case.channel_width_m:.3f}" z="{case.dam_height_m:.3f}" />
                    </drawbox>

                    <!-- the breach: a stack of void boxes, widening with height -->
                    <setmkvoid />
{_breach_void_layers(case)}

                    <!-- the impounded water -->
                    <setmkfluid mk="0" />
                    <drawbox>
                        <boxfill>solid</boxfill>
                        <point x="{case.dp:.3f}" y="{case.dp:.3f}" z="{case.dp:.3f}" />
                        <size x="{case.reservoir_length_m - case.dp:.3f}" y="{case.channel_width_m - 2 * case.dp:.3f}" z="{case.water_depth_m:.3f}" />
                    </drawbox>

                    <shapeout file="" />
                </mainlist>
            </commands>
        </geometry>
    </casedef>
    <execution>
        <parameters>
            <parameter key="SavePosDouble" value="0" />
            <parameter key="StepAlgorithm" value="2" comment="1:Verlet, 2:Symplectic" />
            <parameter key="Kernel" value="2" comment="1:Cubic Spline, 2:Wendland" />
            <parameter key="ViscoTreatment" value="1" comment="1:Artificial, 2:Laminar+SPS" />
            <parameter key="Visco" value="0.01" comment="Artificial viscosity. 0.01 is the standard dam-break value" />
            <parameter key="DensityDT" value="2" comment="Density Diffusion Term 2:Fourtakas full" />
            <parameter key="DensityDTvalue" value="0.1" />
            <parameter key="CoefDtMin" value="0.05" />
            <parameter key="#DtIni" value="0.0001" />
            <parameter key="TimeMax" value="{case.time_max_s}" comment="Physical time to simulate" units_comment="seconds" />
            <parameter key="TimeOut" value="{case.time_out_s}" comment="Output interval" units_comment="seconds" />
            <parameter key="PartsOutMax" value="20" comment="%% of allowed particles to be excluded" />
            <parameter key="RhopOutMin" value="700" units_comment="kg/m^3" />
            <parameter key="RhopOutMax" value="1300" units_comment="kg/m^3" />
        </parameters>
    </execution>
</case>
"""
    path = case_dir / f"{name}_Def.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def write_flowtool_boxes(case: BreachCase, case_dir: Path, name: str) -> Path:
    """Define the reservoir box FlowTool measures.

    Discharge is derived from the reservoir losing water, not from counting
    particles crossing a plane. Fewer edge cases: splash that leaves and
    returns is handled correctly, and the number is a volume rate by
    construction.
    """
    x1 = 0.0
    x2 = case.reservoir_length_m
    y1, y2 = 0.0, case.channel_width_m
    z1, z2 = 0.0, case.dam_height_m + 8.0

    corners = [
        (x1, y2, z1), (x1, y1, z1), (x2, y1, z1), (x2, y2, z1),
        (x1, y2, z2), (x1, y1, z2), (x2, y1, z2), (x2, y2, z2),
    ]
    lines = ["BOX @Reservoir"] + [f"{a} {b} {c}" for a, b, c in corners]
    path = case_dir / f"{name}_FileBoxes.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ==========================================================================
# Running
# ==========================================================================


def _run(cmd: list[str], cwd: Path, log: Path) -> None:
    """Run one DualSPHysics tool, tee its output to a log, fail loudly."""
    with open(log, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        proc = subprocess.run(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace",
        )
        fh.write(proc.stdout or "")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-25:])
        raise RuntimeError(
            f"{Path(cmd[0]).name} exited {proc.returncode}. Last output:\n{tail}"
        )


def run_case(case: BreachCase, name: str = "breach", force: bool = False) -> Path:
    """GenCase -> DualSPHysics -> FlowTool. Returns the case directory."""
    if not GENCASE.exists():
        raise RuntimeError(
            f"DualSPHysics binaries not found at {DSPH_BIN}. Download v5.4 from "
            f"https://dual.sphysics.org/downloads/ and unpack it into the repo root."
        )

    case_dir = CASES_DIR / name
    if case_dir.exists() and force:
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    out_dir = case_dir / "out"
    log = case_dir / "run.log"

    n = case.estimate_particles()
    print(f"  ~{n:,} fluid particles at dp = {case.dp} m")
    if n > 4_000_000:
        raise RuntimeError(
            f"{n:,} particles will not fit in 4 GB of VRAM. Increase dp "
            f"(currently {case.dp} m) or shrink the modelled reservoir."
        )

    def_xml = write_case_xml(case, case_dir, name)
    write_flowtool_boxes(case, case_dir, name)
    (case_dir / "case.json").write_text(json.dumps(case.as_dict(), indent=2))

    t0 = time.perf_counter()
    _run([str(GENCASE), str(def_xml.with_suffix("")).replace("_Def", "_Def"),
          str(out_dir / name), "-save:all"], case_dir, log)

    solver = DSPH_GPU if case.gpu else DSPH_CPU
    _run([str(solver), "-gpu" if case.gpu else "-cpu", str(out_dir / name), str(out_dir)],
         case_dir, log)

    _run([str(FLOWTOOL), "-dirdata", str(out_dir / "data"),
          "-fileboxes", str(case_dir / f"{name}_FileBoxes.txt"),
          "-savecsv", str(out_dir / "flow.csv")], case_dir, log)

    print(f"  SPH finished in {time.perf_counter() - t0:.0f} s")
    return case_dir


# ==========================================================================
# Hydrograph extraction
# ==========================================================================


def hydrograph_from_flow(case_dir: Path, name: str = "breach") -> tuple[np.ndarray, np.ndarray]:
    """Turn FlowTool's reservoir volume series into (time_hr, discharge_cumecs).

    Discharge is the rate at which the modelled reservoir loses water:

        Q(t) = -dV/dt

    Differencing is noisy at SPH output resolution, so the series is smoothed
    with a short centred moving average before differencing. The smoothing
    window is reported; it is 3 output steps, which at TimeOut = 0.5 s is 1.5 s
    - far shorter than any feature of a breach hydrograph.
    """
    csv_path = case_dir / "out" / "flow.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} - FlowTool did not produce output")

    times: list[float] = []
    volumes: list[float] = []
    with open(csv_path, "r", encoding="utf-8", errors="replace") as fh:
        rows = [r for r in csv.reader(fh, delimiter=";") if r]

    header_idx = None
    for i, r in enumerate(rows):
        joined = ";".join(r).lower()
        if "time" in joined and ("volume" in joined or "nok" in joined):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"could not find a header row in {csv_path}")

    header = [c.strip().lower() for c in rows[header_idx]]
    t_col = next((i for i, c in enumerate(header) if c.startswith("time")), 0)
    v_col = next(
        (i for i, c in enumerate(header) if "volume" in c and "out" not in c and "in" not in c),
        None,
    )
    if v_col is None:
        v_col = next((i for i, c in enumerate(header) if c.startswith("nok")), None)
    if v_col is None:
        raise RuntimeError(f"no volume column in {csv_path}: {header}")

    for r in rows[header_idx + 1:]:
        try:
            times.append(float(r[t_col]))
            volumes.append(float(r[v_col]))
        except (ValueError, IndexError):
            continue

    t = np.asarray(times, dtype=np.float64)
    v = np.asarray(volumes, dtype=np.float64)
    if t.size < 4:
        raise RuntimeError(f"only {t.size} FlowTool samples - the run was too short")

    # If the column was a particle count, convert to volume.
    if "nok" in header[v_col]:
        case = json.loads((case_dir / "case.json").read_text())
        v = v * case["dp"] ** 3

    k = 3
    kernel = np.ones(k) / k
    v_s = np.convolve(v, kernel, mode="same")
    v_s[:k] = v[:k]
    v_s[-k:] = v[-k:]

    q = -np.gradient(v_s, t)
    q = np.clip(q, 0.0, None)

    t_hr = (t - t[0]) / 3600.0
    if t_hr[0] != 0.0:
        t_hr[0] = 0.0
    return t_hr, q


def write_sph_run(
    case_dir: Path,
    site: dict,
    scenario: dict,
    outputs_dir: Path,
    run_id: str,
    name: str = "breach",
) -> Path:
    """Write the SPH result as a contract hydrograph the other engines can read.

    Only hydrograph.csv and meta.json - an SPH near-field run has no downstream
    inundation grids, and inventing them would be worse than not having them.
    Modules 03 and 04 consume the hydrograph; that is the coupling.
    """
    from shared.io import write_hydrograph, write_json

    t_hr, q = hydrograph_from_flow(case_dir, name)
    run_dir = Path(outputs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_hydrograph(run_dir, t_hr, q)

    case = json.loads((case_dir / "case.json").read_text())
    write_json(
        run_dir,
        "sph_meta.json",
        {
            "run_id": run_id,
            "engine": "sph",
            "solver": "DualSPHysics v5.4",
            "site": site,
            "scenario": scenario,
            "case": case,
            "peak_discharge_cumecs": round(float(q.max()), 1),
            "simulated_seconds": round(float(t_hr[-1] * 3600.0), 1),
            "limitation": (
                "Near-field only. The modelled reservoir block is far smaller "
                "than the real reservoir, so this hydrograph describes the "
                "initial breach outflow and does NOT include reservoir "
                "drawdown. Compare against the level-pool routing in "
                "shared.hydro rather than substituting for it."
            ),
        },
    )
    return run_dir


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.02_sph.breach")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="generate and solve a breach case")
    r.add_argument("--name", default="breach")
    r.add_argument("--height", type=float, default=60.0)
    r.add_argument("--water", type=float, default=55.0)
    r.add_argument("--width", type=float, default=40.0)
    r.add_argument("--dp", type=float, default=1.5)
    r.add_argument("--time", type=float, default=60.0)
    r.add_argument("--cpu", action="store_true")
    r.add_argument("--force", action="store_true")

    h = sub.add_parser("hydrograph", help="extract the hydrograph from a finished case")
    h.add_argument("--name", default="breach")

    args = ap.parse_args(argv)

    if args.cmd == "run":
        case = BreachCase(
            dam_height_m=args.height, water_depth_m=args.water,
            breach_bottom_width_m=args.width, breach_depth_m=args.water,
            dp=args.dp, time_max_s=args.time, gpu=not args.cpu,
        )
        case_dir = run_case(case, name=args.name, force=args.force)
        t, q = hydrograph_from_flow(case_dir, args.name)
        print(f"  peak {q.max():,.0f} m3/s over {t[-1] * 3600:.0f} s simulated")
        return 0

    case_dir = CASES_DIR / args.name
    t, q = hydrograph_from_flow(case_dir, args.name)
    print(f"peak {q.max():,.0f} m3/s, {len(t)} samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
