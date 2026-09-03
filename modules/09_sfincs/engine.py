"""
modules/09_sfincs/engine.py - is the SFINCS solver installed, and which build?

    python -m modules.09_sfincs.engine

SFINCS is Deltares' open-source flood model (GPL-3.0; the precompiled binaries
ship under the Deltares Freeware licence). It needs no licence key AND it ships
compiled, which is the entire reason it is here: NTRO's statement asks for
Delft3D, we have neither kernel - the FM licence we requested was never answered
and Delft3D 4 ships as source we have not compiled - and a hydrodynamic framework
that can only run its own solver has not proved much.

    WHAT THIS IS NOT. SFINCS is not Delft3D and it is reduced-physics. Nothing
    in this repository may present it as Delft3D, and compare_engines.py keeps
    the Delft3D row reported as absent whether SFINCS is installed or not.

What it does prove is that our data contract accepts an independent third-party
solver. That is the harder engineering claim, and it makes the Delft3D gap a
procurement fact rather than an excuse.

Same detection pattern as modules/03_delft3d/engine.py: absence is measured, not
asserted.

Owner: captain.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXE_NAMES = ("sfincs.exe", "sfincs")

SEARCH_ROOTS = (
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
    Path("C:/Deltares"),
    Path("D:/Deltares"),
)

_NAME_HINTS = ("sfincs",)


@dataclass
class SfincsStatus:
    """What we found. Every field observed, none assumed."""

    installed: bool = False
    exe: Path | None = None
    version: str | None = None
    build_date: str | None = None
    searched: list[str] | None = None

    @property
    def summary(self) -> str:
        if not self.installed:
            return "NOT INSTALLED - no sfincs executable found."
        v = self.version or "unknown build"
        return f"SFINCS {v} at {self.exe}"

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "exe": str(self.exe) if self.exe else None,
            "version": self.version,
            "build_date": self.build_date,
            "searched": self.searched or [],
            "summary": self.summary,
            "is_delft3d": False,
            "note": (
                "SFINCS is Deltares' open-source reduced-physics flood model. It "
                "is NOT Delft3D and must never be presented as Delft3D."
            ),
        }


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("SFINCS_HOME", "").strip()
    if env:
        roots.append(Path(env))
    for parent in (REPO_ROOT, REPO_ROOT.parent):
        try:
            for child in parent.iterdir():
                if child.is_dir() and any(h in child.name.lower() for h in _NAME_HINTS):
                    roots.append(child)
        except OSError:
            continue
    for root in SEARCH_ROOTS:
        try:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and any(h in child.name.lower() for h in _NAME_HINTS):
                    roots.append(child)
        except OSError:
            continue
    seen, out = set(), []
    for r in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _find_exe(root: Path, max_depth: int = 4) -> Path | None:
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
                if e.is_file() and e.name.lower() in EXE_NAMES:
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


def _probe_version(exe: Path) -> tuple[str | None, str | None]:
    """Run the binary in an empty directory and read its banner.

    With no sfincs.inp present it prints its build header and exits. That is a
    cheap, side-effect-free way to record exactly which build produced a result.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [str(exe)], cwd=tmp, capture_output=True, text=True, timeout=60
            )
        text = (out.stdout or "") + (out.stderr or "")
    except Exception:
        return None, None

    ver = re.search(r"Build-Revision:\s*\$?Rev:?\s*(.+)", text)
    date = re.search(r"Build-Date:\s*\$?Date:?\s*([0-9-]+)", text)
    return (
        ver.group(1).strip().rstrip("$").strip() if ver else None,
        date.group(1).strip() if date else None,
    )


def detect(probe: bool = True) -> SfincsStatus:
    """Look for the SFINCS binary. Never raises; absence is a normal answer."""
    st = SfincsStatus(searched=[])

    for name in EXE_NAMES:
        found = shutil.which(name)
        if found:
            st.installed, st.exe = True, Path(found)
            st.searched.append("PATH")
            break

    if not st.installed:
        for root in _candidate_roots():
            st.searched.append(str(root))
            exe = _find_exe(root)
            if exe is not None:
                st.installed, st.exe = True, exe
                break

    if st.installed and probe and st.exe is not None:
        st.version, st.build_date = _probe_version(st.exe)
    return st


def status(probe: bool = True) -> dict:
    return detect(probe=probe).to_dict()


def main() -> int:
    st = detect()
    print("SFINCS engine check\n")
    print(f"  {st.summary}\n")
    if st.installed:
        print(f"  executable  {st.exe}")
        print(f"  version     {st.version or 'unknown'}")
        print(f"  build date  {st.build_date or 'unknown'}")
        print("\n  NOTE: SFINCS is not Delft3D. It is a separate Deltares model,")
        print("  reduced-physics, and must never be presented as Delft3D.")
    else:
        print("  searched:")
        for s in st.searched or []:
            print(f"    {s}")
        print(
            "\n  Download the Windows build from https://download.deltares.nl/en/sfincs/\n"
            "  and unpack it beside DualSPHysics_v5.4/, or set SFINCS_HOME."
        )
    return 0 if st.installed else 1


if __name__ == "__main__":
    raise SystemExit(main())
