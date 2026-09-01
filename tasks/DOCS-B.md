# DOCS B — The Written Report

| | |
|---|---|
| **You own** | `docs/report/` |
| **You may read** | everything. **You may write** only in `docs/`. |
| **Never edit** | any code. Found a wrong number or a broken claim? Tell the captain. |

Read `AGENTS.md` completely first. Your report is the artefact that survives the pitch, so it has to
be right rather than impressive.

---

## Same hard rule as Docs A

**Every number traces to a file in this repository.** Write the filename next to it in your working
draft, even if you strip the references from the final version. If you cannot trace it, it does not
go in.

---

## Structure

### 1. Problem statement and what NTRO asked for

Five deliverables. Address each one explicitly and say honestly where we stand:

| NTRO asked for | Status |
|---|---|
| Framework for dam break **and river blockage**, with **loss and damage** | dam break done; blockage mode exists in the contract (`blockage_breach`); damage done via JRC curves |
| **SPH and Delft3D** | SPH done on GPU (DualSPHysics v5.4); **Delft3D not installed — say so plainly** |
| Customisable tool, different input datasets | done — COP30 / SRTM / NASADEM / ALOS / FABDEM, any bbox on earth |
| Dashboard, large data, **.shp or .kml export** | done — KML, zipped shapefile, GeoJSON |
| **Near-real-time analysis using Google Earth Engine** | done both directions: Sentinel-1 observed extent, and CHIRPS rainfall → inflow nowcast |
| Live demo on any Indian river and dam | done — 5,686 dams; demonstrated on Hirakud, unseen, first try |

### 2. Method

One section per stage, each naming its source. The citations already exist in the code docstrings —
lift them, do not invent new ones.

- **Terrain**: COP30 via OpenTopography. Priority-flood depression filling (Barnes, Lehman & Mulla
  2014). Channel carving (Lindsay 2016). D8 drainage (O'Callaghan & Mark 1984).
- **Breach**: Froehlich (2008), Von Thun & Gillette (1990), MacDonald & Langridge-Monopolis (1984).
- **Hydraulics**: 2D shallow water, HLL Riemann solver, Audusse well-balanced reconstruction.
- **SPH**: DualSPHysics v5.4 (Domínguez et al. 2022).
- **Exposure**: OpenStreetMap via Overpass, ODbL.
- **Damage**: Huizinga et al. (2017), JRC EUR 28552 EN, Asia curves; velocity aggravation after
  Clausen & Clark (1990).
- **Hazard classes**: Australian Disaster Resilience Guideline 7-3 (2017).
- **Validation**: Sentinel-1 GRD, Otsu (1979) thresholding, terrain masking after Twele et al. (2016).
- **Inflow**: CHIRPS (Funk et al. 2015), SCS curve number (USDA SCS 1972), Nash (1957) routing.

### 3. Verification

Distinguish these two words carefully throughout — jurors notice:

- **Verification** — is the code solving the equations correctly? *Ritter analytical solution,
  RMSE 0.218 m. Lake at rest, 2.9e-06 m deviation. Closed-basin mass, 0.000000%.*
- **Validation** — do the equations describe reality? *This is where we are weakest, and the report
  should say so directly. See section 5.*

### 4. Results

The Teesta and Hirakud runs. Tables from `impact.json` and `evacuation.json`. The engine comparison
from `integration/compare_engines.py`, with its caveat that the rows measure different quantities.

### 5. Limitations and uncertainty

**Give this section real space. It is the strongest part of the submission.**

Part 4 of `AGENTS.md` is the checklist. Cover at minimum:

- 30 m terrain, no measured bathymetry, channel carving declared in every `meta.json`
- Breach parameters spread up to 10× — quote the Monte Carlo band, not a single peak
- Populations are class defaults where OSM lacks a tag, and it is labelled per settlement
- SAR validation in a gorge is inconclusive, with the slope-sensitivity sweep as evidence
- SPH is near-field only and does not include reservoir drawdown
- The ML surrogate emulates our solver (CSI 0.909) and has never been tested against a real flood
- Delft3D absent

Then explain **why we declined to build a GNN and an LSTM**: both need labelled real-world data that
does not exist for Indian dam breaks, and training them on our own model's output would have let us
present circular reasoning as learned knowledge. This paragraph is worth more than a results table.

### 6. Reproducibility

Anyone should be able to clone, add credentials, and reproduce. `README.md` has the commands.
State that `integration/run_all.py` passes 16/16 offline.

---

## Style

- Active voice. "We carved the channel", not "the channel was carved".
- No adjective does work a number can do. Not "highly accurate" — give the RMSE.
- Every assumption labelled as one. The code already does this; match it.
- Never write an accuracy figure that is not in a file.

## Done means

A report where a hostile expert reviewer can check every number, and where the limitations section
tells them something they had not already spotted.
