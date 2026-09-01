"""
shared/creds.py - one place that knows where the API keys are.

Nobody calls os.environ directly and nobody hardcodes a key in a notebook. A
key read two different ways is a bug that surfaces on demo day, on somebody
else's laptop, with the projector on.

Reads .env from the repo root, falling back to the real environment so that
CI and a deployed backend work without a file on disk.

Owner: captain / person 4. Everyone imports. Nobody else edits.

    from shared.creds import require, optional
    key = require("OPENTOPOGRAPHY_API_KEY", "01_geodata")

Command line:

    python -m shared.creds          # what do I have, what is still missing
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

# name -> (tier, which module breaks without it)
REGISTRY: dict[str, tuple[int, str]] = {
    "EE_PROJECT_ID": (1, "06_gee_validation"),
    "EE_SERVICE_ACCOUNT_EMAIL": (1, "06_gee_validation"),
    "EE_SERVICE_ACCOUNT_KEY": (1, "06_gee_validation"),
    "OPENTOPOGRAPHY_API_KEY": (1, "01_geodata"),
    "EARTHDATA_USERNAME": (1, "01_geodata"),
    "EARTHDATA_PASSWORD": (1, "01_geodata"),
    "CDSE_CLIENT_ID": (1, "07_ml sar"),
    "CDSE_CLIENT_SECRET": (1, "07_ml sar"),
    "MAPTILER_API_KEY": (1, "05_frontend"),
    "BHUVAN_API_KEY": (2, "01_geodata"),
    "KAGGLE_USERNAME": (2, "07_ml training"),
    "KAGGLE_KEY": (2, "07_ml training"),
    "DEPLOY_BACKEND_URL": (3, "demo"),
}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=value reader. No dependency, no shell semantics, no export."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            values[key.strip()] = value
    return values


_FILE_VALUES = _parse_env_file(ENV_FILE)


def get(name: str, default: str | None = None) -> str | None:
    """Real environment wins over .env, so a deployed backend can override."""
    return os.environ.get(name) or _FILE_VALUES.get(name) or default


def require(name: str, who: str = "") -> str:
    """Fetch a credential or fail loudly, naming the fix."""
    value = get(name)
    if value:
        return value
    tier, owner = REGISTRY.get(name, (0, who or "unknown"))
    raise RuntimeError(
        f"missing credential {name} (needed by {owner}).\n"
        f"  1. copy .env.example to .env\n"
        f"  2. fill in {name}\n"
        f"  3. re-run.  check with: python -m shared.creds"
    )


def optional(name: str, default: str | None = None) -> str | None:
    """For keys with a working fallback - basemap tiles, say."""
    return get(name, default)


def ee_key_path() -> Path:
    """Absolute path to the Earth Engine service-account JSON."""
    path = Path(require("EE_SERVICE_ACCOUNT_KEY"))
    return path if path.is_absolute() else REPO_ROOT / path


def report() -> list[tuple[str, int, str, bool]]:
    """(name, tier, owner, present) for every registered credential."""
    return [
        (name, tier, owner, bool(get(name)))
        for name, (tier, owner) in REGISTRY.items()
    ]


def main() -> int:
    rows = report()
    print(f"env file: {ENV_FILE}  {'found' if ENV_FILE.exists() else 'MISSING'}")
    print()
    for tier in (1, 2, 3):
        in_tier = [r for r in rows if r[1] == tier]
        if not in_tier:
            continue
        label = {1: "blocks a module", 2: "improves a deliverable", 3: "demo day"}
        print(f"  tier {tier} - {label[tier]}")
        for name, _, owner, present in in_tier:
            print(f"    [{'x' if present else ' '}] {name:<32} {owner}")
        print()
    missing = [r[0] for r in rows if r[1] == 1 and not r[3]]
    if missing:
        print(f"{len(missing)} tier-1 credential(s) still missing:")
        print("  " + ", ".join(missing))
        return 1
    print("all tier-1 credentials present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
