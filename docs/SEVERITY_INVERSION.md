# Breach severity inversion — Annamayya, 19 November 2021

**This is inverse modelling. It is not validation, not tuning, and not an accuracy figure.**
No number in this document may be quoted as "our model is X% accurate". Read `AGENTS.md`
Part 1 before citing anything here.

Reproduce with:

```
python integration/annamayya_severity.py
```

---

## 1 · The question

The Annamayya (Cheyyeru Project) earthfill embankment failed on 19 November 2021. **Nobody
published its breach parameters.** Every simulation of this event in this repository therefore
assumes a *full reservoir* and a *complete breach* — the worst case, chosen because it is the
defensible assumption when the truth is unknown, not because it is what happened.

That assumption is the leading suspect for the large over-prediction our validation reports:
bias 7.2 against Sentinel-1. So we swept the one unknown — how much of the 63.16 MCM was
released — and asked which value the observation is *consistent with*.

**Four terms this experiment keeps apart:**

| term | question | can we answer it here? |
|---|---|---|
| model validation | does the model reproduce a known event? | **No** — the event's forcing is unknown |
| inverse estimation | given the observation, what forcing is consistent? | This experiment |
| sensitivity analysis | how much does the answer move when an input moves? | By-product, and it worked |
| system accuracy | how well does the system predict in general? | **Not measurable from one event** |

## 2 · Method and controls

Four runs, identical in every respect except reservoir release fraction: **60 m grid, 24 h,
40 km reach, wet ≥ 0.05 m** (the contract threshold, unchanged). Observation is the Sentinel-1
3-scene composite 2021-11-19 … 2021-12-05 at an Otsu threshold of **−11.706 dB**.

Six controls, each of which was capable of invalidating the result:

1. **Runs executed strictly sequentially.** `run_id` is allocated from the folders that exist, so
   concurrent submission collides — it did, earlier in this work, and three runs received the
   same id.
2. **Each run's scenario read back from its own `meta.json`**, not assumed from submission order.
3. **Configuration equality enforced.** Two earlier Annamayya runs (90 m grid; 48 h) were
   *excluded and reported*, not silently dropped.
4. **Grid equality enforced** between each simulation and the observation.
5. **No-data is not dry.** `modules/06`'s `agreement()` compares two boolean masks, so a cell the
   satellite never imaged counts as "observed dry". Here every metric is computed only over cells
   with valid backscatter on **both** dates. 246,181 cells evaluated, **1,959 excluded as
   no-data**, 99.2% of the domain imaged.
6. **Coverage floor of 50%.** An earlier attempt at a single-pass observation returned a
   zero-filled raster at 0.4% coverage and would have produced a confident CSI computed from
   nothing.

Two duplicate runs (`_006`, `_008`) were detected and reported as anomalies rather than averaged in.

## 3 · Result

Over 246,181 valid cells:

| release | peak m³/s | area km² | hits | false | miss | CSI | POD | FAR | bias |
|---|---|---|---|---|---|---|---|---|---|
| 100% | 11,325 | 71.59 | 688 | 19,178 | 2,064 | 0.0314 | 0.250 | 0.965 | 7.22 |
| 75% | 6,407 | 45.16 | 350 | 12,181 | 2,402 | 0.0234 | 0.127 | 0.972 | 4.55 |
| 50% | 2,766 | 21.48 | 65 | 5,896 | 2,687 | 0.0075 | 0.024 | 0.989 | 2.17 |
| 25% | 398 | 10.04 | 56 | 2,730 | 2,696 | 0.0102 | 0.020 | 0.980 | 1.01 |

Every scenario is in the table, including the ones that score worse.

**Is the observation flood-shaped?** (8-connected components)

| | cells | components | largest | median |
|---|---|---|---|---|
| observed (Sentinel-1) | 2,752 | **732** | 178 (6.5%) | **1 cell** |
| simulated (100%) | 19,866 | **13** | 19,843 (**99.9%**) | 1 cell |

**75% of the observed components are 1–2 cells.**

## 4 · Interpretation

**(1) Does reducing severity reduce the over-prediction?** Yes, and cleanly. Bias falls
7.22 → 4.55 → 2.17 → 1.01, monotonically, and reaches 1.01 at 25% release. The area over-prediction
is *entirely explained* by the full-reservoir assumption. As a **sensitivity result this is the
solid finding of the experiment**: the model's flood area is strongly and monotonically controlled
by the release fraction, exactly as the physics requires.

**(2) Do CSI, POD and FAR move consistently?** **No — and this is the important negative.** POD
collapses monotonically (0.250 → 0.020) as severity falls, and CSI is non-monotonic: 0.0314,
0.0234, 0.0075, then back up to 0.0102. FAR stays above 0.96 everywhere. Only bias behaves. A
genuine inversion would show the skill scores peaking at the true severity; here they simply track
"how much of the domain did you flood".

**(3) Which severity is the observation most consistent with?** **The two criteria disagree, and
that disagreement is the result.** Highest CSI is 100% release (0.0314). Best area match is 25%
(bias 1.01). At 25% the simulated wet area is 2,786 cells against 2,752 observed — the same amount
of water to within 1% — yet the two overlap on **56 cells, 2.0%**. Same quantity of water, entirely
different places. **The inversion is not identifiable from this observation.**

**(4) Is the improvement meaningful?** For area, yes: bias 7.22 → 1.01 is a real, physically
interpretable result. For spatial skill, no. Peak CSI is 0.0314 — for reference, an operational
flood model is judged against CSI 0.6–0.8. Moving a score from 0.0075 to 0.0314 is movement inside
the noise floor, and **presenting it as an accuracy improvement would be dishonest**.

**(5) Could Sentinel-1 or monsoon contamination explain this instead?** **Yes, and the connectivity
measurement says it probably does.** A dam-break inundation is one connected corridor: the
simulation puts 99.9% of its water in a single component. The observed mask is **732 separate
blobs with a median size of one cell**, its largest holding only 6.5% of the wet area. That is the
signature of speckle and scattered wet ground — November 2021 was an exceptionally wet
north-east-monsoon month across Rayalaseema, and the composite spans 16 days. The change-detection
mask is dominated by something that is not the dam's flood, which is sufficient on its own to
explain why no severity achieves spatial agreement.

**(6) Does this tell us about the event or about the model?** **About the event's forcing, and
weakly.** It is an attempt to constrain one unknown input from one noisy observation. It says
nothing about how well the hydraulics work — that is measured by the verification suite (Ritter
RMSE 0.218 m, lake-at-rest 2.9 × 10⁻⁶ m, mass error 0.000%), which uses no satellite data at all.

## 5 · Conclusion for a jury

> We simulated the 2021 Annamayya dam failure at four breach severities and compared each against
> Sentinel-1. Reducing the release from 100% to 25% brings the flood **area** into agreement — bias
> falls from 7.22 to 1.01 — which confirms our over-prediction comes from the worst-case
> full-reservoir assumption we adopt when breach parameters are unpublished, not from the
> hydraulics. But the severity that matches the area overlaps the observation on only 2% of its
> cells, and the observed mask is 732 disconnected fragments where a flood would be one corridor.
> **We therefore cannot identify the true breach severity from this data, and we do not claim to.**

**What this establishes:**
- The model's response to reservoir release is monotonic and physically correct — a genuine
  sensitivity result.
- The documented over-prediction is attributable to a stated assumption, not an unexplained defect.
- The Sentinel-1 composite available for this event is not usable as a flood-extent reference.

**What this does NOT establish:**
- Not the actual breach severity at Annamayya. The inversion is unidentifiable.
- Not a validation. Validation needs known forcing; it was never published.
- Not an accuracy figure. Nothing here is a percentage of correctness, and CSI 0.0314 is the
  severity that best matches a noisy composite, not a measure of how well the model works.
- Not evidence the model is wrong either. The observation is too poor to convict or acquit it.

**What would settle it:** a Copernicus EMS rapid-mapping extent for this event, or any documented
dam break with published breach parameters. Both are listed in `docs/VALIDATION.md` §6.
