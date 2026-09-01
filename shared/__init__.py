"""
shared/ - the contract, in code.

Five modules import from here and nobody else writes to it. If you think this
package needs a change, stop and ask person 4 / the captain. Do not fork it,
do not copy a function out of it into your module.

    from shared.contract import WET_THRESHOLD_M
    from shared.geo import Grid
    from shared.io import write_grid, write_meta, RunFolder
    from shared.hydro import froehlich_2008, breach_hydrograph
    from shared.validate import validate_run
    from shared.creds import require, optional

Command line:

    python -m shared.fake --run-id synthetic_overtop_fast_001
    python -m shared.validate outputs/synthetic_overtop_fast_001
    python -m shared.creds
"""

from shared.contract import SCHEMA_VERSION, WET_THRESHOLD_M, hazard_class
from shared.geo import Grid, bbox_around, bbox_downstream, haversine_km, utm_epsg

__all__ = [
    "SCHEMA_VERSION",
    "WET_THRESHOLD_M",
    "hazard_class",
    "Grid",
    "bbox_around",
    "bbox_downstream",
    "haversine_km",
    "utm_epsg",
]

__version__ = SCHEMA_VERSION
