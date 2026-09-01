# DOCS A — The Deck

| | |
|---|---|
| **You own** | `docs/deck/` and the presentation file |
| **You may read** | everything. **You may write** only in `docs/`. |
| **Never edit** | any code. Found a wrong number? Tell the captain. Do not fix it yourself. |

Read `AGENTS.md` completely before writing a single slide. **Part 1 (honesty) is the pitch.**

---

## The one rule that matters

**Every number on a slide must come from a file in this repository, and you must be able to say
which file.** Not from memory, not from a screenshot someone sent you, not rounded up because it
looks better. If you cannot point at the JSON key it came from, it does not go on the slide.

If a number looks unimpressive, that is not a reason to change it. Several of the numbers in this
project are unimpressive **on purpose**, and knowing why is the strongest thing we have.

---

## Where every number lives

| Slide claim | Command / file |
|---|---|
| Dams available | `python -m modules.01_geodata.dams search --limit 1` → 5,686 |
| Any run's headline results | `outputs/<run_id>/meta.json` → `results` |
| Mass balance | same file → `results.mass_balance_err_pct` |
| Villages, warning times, damage | `outputs/<run_id>/impact.json` |
| Uncertainty band | `outputs/<run_id>/uncertainty.json` |
| Evacuation margins | `outputs/<run_id>/evacuation.json` |
| Satellite validation | `outputs/<run_id>/validation.json` |
| Solver correctness | `python -m pytest modules/04_backend/tests -q` |
| Whole-system health | `python integration/run_all.py` → 16/16 |
| Engine comparison | `python integration/compare_engines.py` |

---

## Suggested arc

**1. The question.** A dam fails. Which villages, how deep, **how long do they have?** A warning
that says "there is a flood" is nearly useless. One that says "Dikchu, seven hours, leave along this
road" saves lives.

**2. Why it matters now.** ~6,281 large dams in India, around 80% over 25 years old. The Dam Safety
Act 2021 legally requires Emergency Action Plans with inundation maps. Most do not exist in usable
form. *Cite the source for these two figures — do not take them from this file.*

**3. What we built.** One screenshot of the console with a real run loaded. Real villages, real
warning times.

**4. It works on any dam.** The picker, 5,686 dams from the CWC register. Then the demonstration:
Hirakud on the Mahanadi — a river the system had never seen — traced, solved and validated on the
first attempt, 0.000% mass error. Same code as the Teesta, which drops 1,019 m over 40 km where
Hirakud drops 27.7 m.

**5. The physics is correct, and here is the proof.** Ritter analytical dam break, RMSE 0.218 m.
Lake at rest stays flat to 2.9e-06 m. Closed basin conserves mass to 0.000000%. These are the slides
a hydrologist reads.

**6. Three engines, compared.** `integration/compare_engines.py`. Be careful and be precise: the
rows measure **different things**. SPH's near-field peak compares to the weir equation; the routed
peak compares to the empirical regressions. Saying otherwise to an NTRO hydrologist ends badly.

**7. What we do not know.** *Do not skip this slide, and do not bury it at the end.* Part 4 of
`AGENTS.md`, as a table. Including that satellite validation in the gorge is inconclusive and we
have the sensitivity sweep that proves it. Including that we declined to build a GNN and an LSTM
because the data to train them honestly does not exist.

**8. What is next.** Delft3D far-field routing (registration pending), WorldPop populations,
validation on a low-gradient reach.

---

## Things that will lose the round

- A number that cannot be traced to a file.
- An accuracy figure we did not measure. **There is no "95% accurate" anywhere in this project.**
- Presenting the ML surrogate as a flood prediction. It is an emulator of *our own solver*, CSI
  0.909 against it, never validated against a real flood.
- Hiding the class-default populations. They are labelled in the data; label them on the slide.
- Averaging the four peak-outflow regressions into one confident number.

## Done means

Every slide's numbers verified against a file this week, and a one-page "sources" appendix mapping
each claim to its file.
