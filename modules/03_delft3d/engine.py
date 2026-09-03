"""
modules/03_delft3d/engine.py - is a Delft3D solver actually installed?

    python -m modules.03_delft3d.engine

The problem statement asks for Delft3D. Until now the answer "not installed"
was a hardcoded string in integration/compare_engines.py. A hardcoded string is
a claim, not a measurement, and AGENTS.md Part 1 does not allow us to state
things we did not check. This module checks.

    A CORRECTION THIS FILE USED TO GET WRONG, written down because five other
    documents repeated it. "Delft3D" is two different products and only one of
    them needs a licence:

      Delft3D 4 Suite      Delft3D-FLOW - the structured curvilinear model that
      (structured)         has been called Delft3D since the 1990s, and the one
                           NTRO's statement means. GPLv3. Source AND pre-compiled
                           Windows binaries are free from download.deltares.nl
                           after a registration. No licence file.
                           Kernel: d_hydro.exe driving flow2d3d.dll.

      Delft3D FM Suite     D-Flow Flexible Mesh - the newer unstructured model.
      (flexible mesh)      The GUI suite needs a Deltares licence, which we were
                           not granted.
                           Kernel: dimr.exe / dflowfm.exe.

    This file used to search for the FM kernel only, find nothing, and report
    "licence not granted". That was true of FM and false of Delft3D 4, which
    anybody can download. Both are searched for now, and the report says which
    one was found.

It looks for the computational kernel - the thing that actually routes water -
not the GUI and not the licence server. Deltares ships those as separate
downloads and only the kernel solves anything:

    Deltares License Software     FlexNet licence manager (DS_Flex, lmadmin)
    Delft3D 4 / FM Suite (GUI)    the modelling environment
    the kernel                    what we drive from the command line

Finding the licence manager and concluding "Delft3D is installed" is exactly
the kind of mistake that gets caught in front of a juror, so the licence tooling
is reported separately and never counted as an engine.

Search order:
    1. $DELFT3D_HOME, if set
    2. sibling directories of the repo root (where DualSPHysics_v5.4 lives)
    3. the usual Windows install roots

Owner: captain.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# What each flavour of Delft3D looks like on disk
# --------------------------------------------------------------------------

# Delft3D 4 (structured). d_hydro.exe is the launcher; it loads the actual
# engine out of flow2d3d.dll, so d_hydro without the library cannot solve and
# we report the two separately. deltares_hydro.exe is the pre-2012 name and
# trisim is the bare FLOW executable underneath both.
D3D4_KERNEL_NAMES = (
    "d_hydro.exe", "d_hydro",
    "deltares_hydro.exe", "deltares_hydro",
    "trisim.exe", "trisim",
)
D3D4_LIBRARY_NAMES = (
    "flow2d3d.dll", "flow2d3d_sp.dll", "libflow2d3d.so", "libflow2d3d_sp.so",
)
D3D4_LAUNCHER_NAMES = (
    "run_dflow2d3d.bat", "run_dflow2d3d.sh",
    "run_dflow2d3d_parallel.bat", "run_dflow2d3d_parallel.sh",
)

# Delft3D FM. dimr is the modern entry point and drives dflowfm; dflowfm alone
# is still usable.
FM_KERNEL_NAMES = ("dimr.exe", "dimr", "dflowfm-cli.exe", "dflowfm.exe", "dflowfm")
FM_LAUNCHER_NAMES = ("run_dimr.bat", "run_dflowfm.bat", "run_dimr.sh", "run_dflowfm.sh")

# Licence tooling. Present in "Deltares License Software". Not a solver.
LICENCE_NAMES = ("DS_Flex.exe", "lmadmin.exe", "dhsdelft.exe", "lmgrd.exe")

FLAVOURS = {
    "delft3d4": {
        "name": "Delft3D 4 (Delft3D-FLOW, structured)",
        "kernels": D3D4_KERNEL_NAMES,
        "launchers": D3D4_LAUNCHER_NAMES,
        "licence_required": False,
        "licence_note": "GPLv3 open source. No licence file. download.deltares.nl",
    },
    "dflowfm": {
        "name": "Delft3D FM (D-Flow Flexible Mesh)",
        "kernels": FM_KERNEL_NAMES,
        "launchers": FM_LAUNCHER_NAMES,
        "licence_required": True,
        "licence_note": "The FM Suite needs a Deltares licence file. Not granted to us.",
    },
}

DOWNLOAD_URL = "https://download.deltares.nl/open-source-software"

SEARCH_ROOTS = (
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
    Path("C:/Deltares"),
    Path("D:/Deltares"),
)

# Folder names an install plausibly hides under. The OSS binary zip unpacks to
# something like oss_artifacts_x64_65936, which says nothing about Delft, so
# guessing only on "delft" would miss the very download this file tells the
# operator to fetch.
_NAME_HINTS = (
    "delft", "deltares", "dflow", "d-flow", "d3d",
    "oss_artifacts", "flow2d3d",
)

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")


@dataclass
class EngineStatus:
    """What we found on this machine. Every field is observed, none assumed."""

    installed: bool = False
    flavour: str | None = None          # "delft3d4" | "dflowfm"
    kernel: Path | None = None
    library: Path | None = None         # flow2d3d.dll, for Delft3D 4 only
    launcher: Path | None = None
    version: str | None = None
    licence_tooling: list[Path] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)

    @property
    def flavour_name(self) -> str | None:
        if self.flavour is None:
            return None
        return FLAVOURS[self.flavour]["name"]

    @property
    def licence_required(self) -> bool | None:
        if self.flavour is None:
            return None
        return bool(FLAVOURS[self.flavour]["licence_required"])

    @property
    def can_solve(self) -> bool:
        """Delft3D 4's launcher is useless without the engine library beside it."""
        if not self.installed:
            return False
        if self.flavour == "delft3d4" and self.kernel is not None:
            if self.kernel.name.lower().startswith(("d_hydro", "deltares_hydro")):
                return self.library is not None
        return True

    @property
    def summary(self) -> str:
        if self.installed:
            v = f" {self.version}" if self.version else ""
            s = f"{self.flavour_name}{v} kernel found at {self.kernel}"
            if not self.can_solve:
                s += (
                    " - but flow2d3d.dll was NOT found beside it, and d_hydro "
                    "cannot solve without the engine library. Treat as unusable."
                )
            return s
        if self.licence_tooling:
            return (
                "NOT INSTALLED - only the Deltares licence manager is present "
                f"({len(self.licence_tooling)} file(s)). The licence manager is "
                "not a solver; the kernel is a separate download."
            )
        return "NOT INSTALLED - no Delft3D kernel found (neither Delft3D 4 nor FM)."

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "flavour": self.flavour,
            "flavour_name": self.flavour_name,
            "licence_required": self.licence_required,
            "can_solve": self.can_solve,
            "kernel": str(self.kernel) if self.kernel else None,
            "library": str(self.library) if self.library else None,
            "launcher": str(self.launcher) if self.launcher else None,
            "version": self.version,
            "version_source": "the install path" if self.version else None,
            "licence_tooling_only": bool(self.licence_tooling and not self.installed),
            "licence_tooling": [str(p) for p in self.licence_tooling],
            "searched": self.searched,
            "summary": self.summary,
            "download_url": DOWNLOAD_URL,
        }


def _candidate_roots() -> list[Path]:
    """Where a Delft3D install could plausibly live on this machine."""
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
                if any(k in child.name.lower() for k in _NAME_HINTS):
                    roots.append(child)
        except OSError:
            continue
    # Only the Deltares-shaped subfolders of the big install roots. Walking all
    # of C:/Program Files four levels deep takes twenty seconds and the gate has
    # to stay quick; an install always sits in a folder that says so.
    for root in SEARCH_ROOTS:
        try:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                if any(k in child.name.lower() for k in _NAME_HINTS):
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

    Depth-limited on purpose: a Delft3D install is thousands of files and we
    only need to know whether the kernel is there, not to walk all of it.
    """
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    lowered = tuple(n.lower() for n in names)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file() and e.name.lower() in lowered:
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


def _version_from_path(p: Path) -> str | None:
    """Deltares stamps the release into the install path, e.g. delft3d4_4.07.01.

    Read off the path, so to_dict says where it came from. d_hydro.exe will not
    print a version without a case to run, and inventing one is not an option.
    """
    for part in reversed(p.parts):
        m = _VERSION_RE.search(part)
        if m:
            return m.group(1)
    return None


def _fill(st: EngineStatus, flavour: str, kernel: Path, root: Path | None) -> EngineStatus:
    """Record a kernel hit and everything that has to sit beside it."""
    st.installed = True
    st.flavour = flavour
    st.kernel = kernel
    st.version = _version_from_path(kernel)
    scope = root if root is not None else kernel.parent
    st.launcher = _find_first(scope, FLAVOURS[flavour]["launchers"])
    if flavour == "delft3d4":
        st.library = _find_first(scope, D3D4_LIBRARY_NAMES)
    return st


def detect() -> EngineStatus:
    """Look for a Delft3D kernel. Never raises; absence is a normal answer.

    Delft3D 4 wins over FM when both are present: it is the model the problem
    statement names, it is the one we can legally run, and it is the one
    modules/03_delft3d/case.py writes cases for.
    """
    st = EngineStatus()

    # A kernel already on PATH beats any directory search.
    for flavour in ("delft3d4", "dflowfm"):
        for name in FLAVOURS[flavour]["kernels"]:
            found = shutil.which(name)
            if found:
                st.searched.append("PATH")
                return _fill(st, flavour, Path(found), None)

    fm_fallback: tuple[Path, Path] | None = None  # (root, kernel)

    for root in _candidate_roots():
        st.searched.append(str(root))

        kernel = _find_first(root, D3D4_KERNEL_NAMES)
        if kernel is not None:
            return _fill(st, "delft3d4", kernel, root)

        if fm_fallback is None:
            fm_kernel = _find_first(root, FM_KERNEL_NAMES)
            if fm_kernel is not None:
                fm_fallback = (root, fm_kernel)
                continue

        # Not a solver, but worth reporting so the operator knows what they have.
        for lic in LICENCE_NAMES:
            hit = _find_first(root, (lic,), max_depth=2)
            if hit is not None:
                st.licence_tooling.append(hit)

    if fm_fallback is not None:
        root, kernel = fm_fallback
        return _fill(st, "dflowfm", kernel, root)

    return st


def status() -> dict:
    """The dict compare_engines.py, pipeline.py and the API report."""
    return detect().to_dict()


def main() -> int:
    st = detect()
    print("Delft3D engine check\n")
    print(f"  {st.summary}\n")
    if st.kernel:
        print(f"  flavour   {st.flavour_name}")
        print(f"  kernel    {st.kernel}")
        if st.flavour == "delft3d4":
            print(f"  library   {st.library or 'NOT FOUND - d_hydro cannot solve without it'}")
        print(f"  launcher  {st.launcher or 'not found'}")
        print(f"  version   {st.version or 'not stamped in the path'}")
        print(f"  licence   {'required (FM Suite)' if st.licence_required else 'none needed (GPLv3)'}")
    if st.licence_tooling:
        print("  licence tooling found (not a solver):")
        for p in st.licence_tooling:
            print(f"    {p}")
    print("\n  searched:")
    for s in st.searched:
        print(f"    {s}")
    if not st.installed:
        print(
            "\n  To install the solver the problem statement names, download the\n"
            f"  Delft3D 4 Suite OPEN SOURCE Windows binaries from\n    {DOWNLOAD_URL}\n"
            "  (GPLv3, free registration, no licence file), then either unpack it\n"
            "  beside DualSPHysics_v5.4/ or set DELFT3D_HOME to its folder.\n"
            "  You want d_hydro.exe and flow2d3d.dll. The Delft3D FM Suite is a\n"
            "  different product and does need a licence we were not granted."
        )
    return 0 if st.installed else 1


if __name__ == "__main__":
    raise SystemExit(main())
