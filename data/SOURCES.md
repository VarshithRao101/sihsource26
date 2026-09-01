# data/SOURCES.md - every external dataset, and which folder it lands in.

Companion to `.env.example`. That file lists things needing a KEY; this file
lists things needing a DOWNLOAD. Between them they are the complete external
dependency list for SIH26161.

Check keys with:  python -m shared.creds


## Inputs (the captain's tier-1/2 list - mostly covered)

| folder       | dataset                          | source                  | key? |
|--------------|----------------------------------|-------------------------|------|
| `dem/`       | FABDEM v1.2 (bare-earth COP30)   | data.bris.ac.uk         | no (CC BY-NC-SA) |
| `dem/`       | SRTM / COP30 / ALOS fallback     | OpenTopography          | yes  |
| `dem/`       | NASADEM, GPM IMERG rainfall      | NASA Earthdata          | yes  |
| `dams/`      | GRanD v1.3 (7,320 dams)          | globaldamwatch.org      | form |
| `rivers/`    | HydroSHEDS / HydroRIVERS         | hydrosheds.org          | no   |
| `roughness/` | ESA WorldCover 2021 -> Manning n | esa-worldcover.org      | no   |
| `exposure/`  | WorldPop 100m India              | worldpop.org            | no   |
| `exposure/`  | GHSL built-up                    | ghsl.jrc.ec.europa.eu   | no   |
| `exposure/`  | Google Open Buildings            | sites.research.google   | no   |
| `roads/`     | OSM road graph (India extract)   | geofabrik.de / Overpass | no   |


## Labels and targets (WAS MISSING - four ML models need these)

Inputs alone do not train a supervised model. Each row below is the thing a
model is fitted against; without it the model has no ground truth and the
number it reports is decoration.

| folder     | dataset                              | feeds            | source |
|------------|--------------------------------------|------------------|--------|
| `labels/`  | Copernicus EMS Rapid Mapping extents  | ML #3 SAR / CSI  | emergency.copernicus.eu/mapping |
| `labels/`  | Global Flood Database events          | ML #3 SAR / CSI  | global-flood-database.cloudtostreet.ai |
| `damage/`  | JRC global depth-damage curves        | ML #4 XGBoost    | publications.jrc.ec.europa.eu |
| `inflow/`  | CWC / India-WRIS reservoir series     | ML #5 LSTM       | indiawris.gov.in |
| `inflow/`  | DAHITI / Hydroweb altimetry levels    | ML #5 LSTM       | dahiti.dgfi.tum.de |
| `observed/`| Sentinel-1 GRD scenes for our sites   | ML #3 SAR        | CDSE or GEE (key) |


## Not a dataset - engines and compute

| what          | where                | note |
|---------------|----------------------|------|
| Delft3D 4 OSS | oss.deltares.nl      | fully open; prefer over FM Suite for scripted runs |
| DualSPHysics  | dual.sphysics.org    | CPU (OpenMP) build works - GPU is faster, not required |
| Kaggle        | kaggle.com           | free GPU hours for the FNO/U-Net surrogate if no local card |


## Rule

Nothing large is committed. `.gitignore` keeps the folder skeleton and small
vector files only. If a teammate needs a dataset to run, either it is small
enough to commit or a cached sample for one site goes in, not the full archive.
