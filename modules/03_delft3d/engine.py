"""
modules/03_delft3d/engine.py - is a Delft3D FM solver actually installed?

    python -m modules.03_delft3d.engine

The problem statement asks for Delft3D. Until now the answer "not installed"
was a hardcoded string in integration/compare_engines.py. A hardcoded string is
a claim, not a measurement, and AGENTS.md Part 1 does not allow us to state
things we did not check. This module checks.

It looks for the D-Flow FM computational kernel - the thing that actually
routes water - not the GUI and not the licence server. Deltares ships three
separate downloads and only one of them solves anything:

    Deltares License Software     FlexNet licence manager (DS_Flex, lmadmin)
    Delft3D FM Suite (GUI)        the modelling environment
    D-Flow FM kernel / DIMR       the solver we would drive from the command line

Finding the licence manager and concluding "Delft3D is installed" is exactly
the kind of mistake that gets caught in front of a juror, so this reports the
licence tooling separately and never counts it as an engine.

Search order:
    1. $DELFT3D_HOME, if set
    2. sibling directories of the repo root (where DualSPHysics_v5.4 lives)
    3. the usual Windows install roots

Owner: captain.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The kernel executables, in the order we would rather have them. dimr is the
# modern entry point and drives dflowfm; dflowfm alone is still usable.
KERNEL_NAMES = ("dimr.exe", "dimr", "dflowfm-cli.exe", "dflowfm.exe", "dflowfm")

# Launcher scripts that ship beside the kernel in the FM Suite.
LAUNCHER_NAMES = ("run_dimr.bat", "run_dflowfm.bat", "run_dimr.sh", "run_dflowfm.sh")

# Licence tooling. Present in "Deltares License Software". Not a solver.
LICENCE_NAMES = ("DS_Flex.exe", "lmadmin.exe", "dhsdelft.exe", "lmgrd.exe")

SEARCH_ROOTS = (
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
    Path("C:/Deltares"),
    Path("D:/Deltares"),
)


@dataclass
class EngineStatus:
    """What we found on this machine. Every field is observed, none assumed."""

    installed: bool = False
    kernel: Path | None = None
    launcher: Path | None = None
    version: str | None = None
    licence_tooling: list[Path] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.installed:
            return f"D-Flow FM kernel found at {self.kernel}"
        if self.licence_tooling:
            return (
                "NOT INSTALLED - only the Deltares licence manager is present "
                f"({len(self.licence_tooling)} file(s)). The licence manager is "
                "not a solver; the D-Flow FM kernel is a separate download."
            )
        return "NOT INSTALLED - no D-Flow FM kernel found."

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "kernel": str(self.kernel) if self.kernel else None,
            "launcher": str(self.launcher) if self.launcher else None,
            "version": self.version,
            "licence_tooling_only": bool(self.licence_tooling and not self.installed),
            "licence_tooling": [str(p) for p in self.licence_tooling],
            "searched": self.searched,
            "summary": self.summary,
        }


def _candidate_roots() -> list[Path]:
    """Where a D-Flow FM install could plausibly live on this machine."""
    roots: list[Path] = []
    env = os.environ.get("DELFT3D_HOME", "").strip()
    if env:
        roots.append(Path(env))
    # Beside the repo, which is where DualSPHysics_v5.4 was unpacked.
    for parent in (REPO_ROOT, REPO_ROOT.parent):
        try:
            for child in parent.iterdir():
                if not child.is_dir():
                    continue
                name = child.name.lower()
                if any(k in name for k in
                       ("delft", "deltares", "dflow", "d-flow")):
                    roots.append(child)
        except OSError:
            continue
    # Only the Deltares-shaped subfolders of the big install roots. Walking all
    # of C:/Program Files four levels deep takes twenty seconds and the gate has
    # to stay quick; a D-Flow FM install always sits in a folder that says so.
    for root in SEARCH_ROOTS:
        try:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                if any(k in child.name.lower()
                       for k in ("delft", "deltares", "dflow", "d-flow")):
                    roots.append(child)
        except OSError:
            continue
    # Deduplicate, keeping order.
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _find_first(root: Path, names: tuple[str, ...], max_depth: int = 4) -> Path | None:
    """Shallow search for any of `names` under `root`.

    Depth-limited on purpose: an FM Suite install is thousands of files and we
    only need to know whether the kernel is there, not to walk all of it.
    """
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file() and e.name in names:
                    return e
            except OSError:
                continue
        if depth < max_depth:
            for e in entries:
                try:
                    if e.is_dir():
                        stack.append((e, depth + 1))
                except OSError:
                    continue
    return None


def detect() -> EngineStatus:
    """Look for a D-Flow FM kernel. Never raises; absence is a normal answer."""
    st = EngineStatus()

    # A kernel already on PATH beats any directory search.
    for name in KERNEL_NAMES:
        found = shutil.which(name)
        if found:
            st.installed = True
            st.kernel = Path(found)
            st.searched.append("PATH")
            return st

    for root in _candidate_roots():
        st.searched.append(str(root))
        kernel = _find_first(root, KERNEL_NAMES)
        if kernel is not None:
            st.installed = True
            st.kernel = kernel
            st.launcher = _find_first(root, LAUNCHER_NAMES)
            return st
        # Not a solver, but worth reporting so the operator knows what they have.
        for lic in LICENCE_NAMES:
            hit = _find_first(root, (lic,), max_depth=2)
            if hit is not None:
                st.licence_tooling.append(hit)

    return st


def status() -> dict:
    """The dict compare_engines.py and the API report."""
    return detect().to_dict()


def main() -> int:
    st = detect()
    print("Delft3D FM engine check\n")
    print(f"  {st.summary}\n")
    if st.kernel:
        print(f"  kernel    {st.kernel}")
        print(f"  launcher  {st.launcher or 'not found'}")
    if st.licence_tooling:
        print("  licence tooling found (not a solver):")
        for p in st.licence_tooling:
            print(f"    {p}")
    print("\n  searched:")
    for s in st.searched:
        print(f"    {s}")
    if not st.installed:
        print(
            "\n  To install the solver, download the D-Flow FM kernel / DIMR from\n"
            "  https://download.deltares.nl/ and either unpack it beside\n"
            "  DualSPHysics_v5.4/ or set DELFT3D_HOME to its folder."
        )
    return 0 if st.installed else 1


if __name__ == "__main__":
    raise SystemExit(main())
