# QA — Verification, the Blessed Run, and the Demo Runbook

| | |
|---|---|
| **You own** | `docs/qa/` |
| **You may read** | everything, and you may **run** anything |
| **You may NOT edit** | any code, `docs/deck/` (Docs A), `docs/report/` (Docs B) |
| **Found something wrong?** | File it in `docs/qa/findings.md` and tell the owner. Never fix it yourself. |

Read `AGENTS.md` completely before you start. **Part 1 (honesty) is your job description** and Part 4
is your starting checklist.

You are the only person on this team whose job is to try to break it. Everyone else is building and
is therefore the worst possible judge of whether it works. Be unpleasant about this. It is cheaper to
find a broken demo today than in front of an NTRO hydrologist.

---

## PROTOCOL — follow this literally

1. Split the work below into 6–10 small, independently runnable parts. Show the numbered list to the
   human. **Then stop.**
2. Do **Part 1 only**. Show the actual terminal output. **Then stop and wait for "next".**
3. Before each part, ask: *"Part N needs these run folders / credentials / files: `<list>`. Supply
   them now, or should I work with what is in `outputs/`?"* Wait for the answer.
4. Never batch two parts because they look small.

---

## One thing to understand before you start

`outputs/` is **gitignored**. Run folders are large and regenerable, so they are not in the repo you
cloned. You either generate them yourself or the captain sends you a folder. Ask before you assume
you are looking at the same data as anyone else.

Running the pipeline writes into `outputs/`. That is allowed — `outputs/` is owned by nobody. The
folder-ownership rule is about *source files*, and it still binds you absolutely everywhere else.

Start the backend:

```bash
.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000
```

---

## Your job

### `[P0]` 1. Sweep every run with the validator

`python -m shared.validate outputs/<run_id>` is **the definition of done** for this project. A run
that does not pass does not exist.

There are 27 folders in `outputs/` right now and they are **not** equivalent. Most are development
leftovers. Produce `docs/qa/run_inventory.md`: one row per run, with

| run_id | validator | impact | evacuation | validation | is_fake | notes |

Paste the validator's real output. Do not summarise a failure into the word "minor".

As of this writing exactly one run — `teesta_overtop_fast_051` — carries impact **and** evacuation
**and** validation. Confirm that, or prove it wrong.

### `[P0]` 2. Nominate the blessed run

Exactly one run goes in the deck, the report and the live demo, and all three must point at the
**same folder**. Pick it against these criteria and write the justification in
`docs/qa/blessed_run.md`:

- validator **PASS**, zero errors, zero warnings
- `meta.is_fake` is **false**
- real terrain, not synthetic
- all four optional files present: `impact.json`, `uncertainty.json`, `evacuation.json`,
  `validation.json`
- `mass_balance_err_pct` you would be happy to show a hydrologist

If nothing meets the bar, **say so** and tell the captain exactly which file is missing. Do not pick
the closest one and hope.

### `[P0]` 3. The demo runbook

`docs/qa/runbook.md`. Written so that a teammate who has never run the project can execute it cold
while nervous.

- Exact commands, in order, copy-pasteable. No "then start the backend".
- **Cold-start timing.** How long from a closed laptop to a flood on screen? Measure it with a
  stopwatch and write the number down.
- **The wifi-is-dead path.** `run_all.py` passes with the network unplugged; the console falls back
  to OSM raster tiles. Verify both yourself, by actually pulling the network, not by reading this
  sentence.
- **Fallback ordering.** If the live run fails on stage, what is shown instead, and what is the exact
  sentence said while switching? Write the sentence.
- What must already be warm before you walk on stage (the surrogate caches its model per process —
  the first call is seconds, later calls are ~20 ms).

### `[P0]` 4. The adversarial question list

`docs/qa/questions.md`. Every question a juror who models dams for a living would ask, each with the
honest answer **and the file the answer comes from**.

Start from `AGENTS.md` Part 4 — every limitation there is a question waiting to be asked. Then add
your own. The ones that will actually be asked:

- "What is your accuracy?" — there is no accuracy percentage in this project, because we did not
  measure one. Know why that is the strong answer.
- "Your SAR validation CSI is 0.02. That is nothing." — correct, and the sensitivity sweep in
  `validation.json` shows why: 30° median slope, a corridor one to three cells wide at 90 m. The fix
  is a low-gradient reach, not a tuned threshold.
- "Hirakud peaks at 265,799 m³/s?" — inside the empirical envelope (38,315 – 380,296) but near the
  top. This one is **not yet resolved** — see item 6.
- "Where did the populations come from?" — currently `class_default`, 1,500 each, because OSM has no
  population tag. Check whether the WorldPop raster has landed before you write the final answer.
- "Is the surrogate a simulation?" — no. It emulates *our own solver* at CSI 0.909 and is not
  validated against a real flood.

**An answer of "we didn't get to that" is a correct answer.** Write those down too.

### `[P1]` 5. Cold-machine rehearsal

Clone the repo somewhere fresh and follow `README.md` setup exactly as written, with no prior
knowledge. Log every step that fails, is ambiguous, or needs a credential you were not told about.

This is the highest-value thing you will do all week. The setup instructions were written by someone
who already had everything installed, which means they are wrong in ways only you can find.

Expected gate at the end: `integration/run_all.py` prints **22/22**.

### `[P1]` 6. Number audit

Every number in `docs/deck/` and `docs/report/` must trace to a file in this repository. Check them.
You **may not edit either** — file discrepancies in `docs/qa/findings.md` with the claimed value, the
file, and the actual value, and tell Docs A or Docs B.

Chase item 6 in `README.md` while you are here: the Hirakud peak needs a hydraulics person's eyes
before it goes on a slide. Finding that person is a legitimate QA task.

### `[P2]` 7. Failure drill

Try to break it on purpose and write down what happens: a dam with no downstream settlements, a
1 m blockage, a bbox in the ocean, `end_hr` of 0.1, a run cancelled mid-WebSocket. Anything that
produces a stack trace on a projector is a P0 finding the moment you find it.

---

## Rules

- **Never edit code, the deck, or the report.** You report. Other people fix. This is not a
  formality — it is what keeps the merges conflict-free.
- **Paste real output.** Never describe what a command printed. Paste it.
- **A failing test is a finding, not an embarrassment.** Reporting it is the entire job.
- **Never fix a number.** If a number is wrong, the code that produced it may be wrong too, and
  editing the slide hides the real bug.
- **Do not invent a test result.** If you did not run it, say you did not run it.

## Done means

There is one blessed run that passes the validator, a runbook a stranger can execute cold with the
network unplugged, an inventory of every run folder, and a question list where **every single answer
names the file it came from**.
