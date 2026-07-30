# Phase 4 — Retrain and evaluate

Date: 2026-07-30
Status: approved, not yet implemented

## Problem

Workflow 02 ends at a NoOp named `Retrain approved`. A human can now approve a
RETRAIN from their phone and the frozen graph resumes correctly, but nothing
happens as a result. Phase 4 makes the approved action do something: fit a
challenger, compare it against the champion, and promote it only if it wins.

## Three problems found before designing

Each was verified against the running system, not assumed.

**1. There is no training data.** The four seeded tables — `runs`,
`feature_stats`, `registry`, `incidents` — are all *metadata about* models.
`feature_stats` holds five aggregate rows (PSI, null rate, means), not samples.
There are zero feature/label rows to fit a classifier on, so `BUILD_PLAN`'s
"retrains a small classifier on the synthetic data" has nothing to train on.

**2. The container wipes the database on every restart.** The Dockerfile `CMD`
runs `seed_db.py`, which `DROP`s all four tables. Phase 4's own success
criterion — "an approved RETRAIN produces a new row in `runs`" — would survive
only until the next `docker compose up`, and a promoted champion would silently
revert to `v12`.

**3. n8n cannot write to the registry.** The SQLite file lives on the agent's
`/data` volume. n8n has no access to it, so "promotes the challenger in the
registry table" is not implementable as written; it requires an agent endpoint.

**4. `runs` and `registry` do not join.** `runs` carries metrics with no version
column; `registry` carries versions with no metrics. "The champion's F1" is
currently unanswerable, and the whole phase is a comparison against it.

## Goals

1. An approved RETRAIN fits a challenger and records a run.
2. The challenger is promoted only if it beats the champion on F1.
3. Both survive a container restart.

## Non-goals

- No model serving. Nothing reads the promoted artifact; promotion is a registry
  row change. Serving is outside this project's scope.
- No hyperparameter search, no cross-validation. One fit, one comparison.
- No `Split In Batches`. `BUILD_PLAN` suggests it for evaluating across data
  slices, but there is one slice — a batch loop over a single item is canvas
  decoration that would have to be defended in an interview.

## Design

### 1. `seed_db.py` — schema changes

**Add a `samples` table:**

```sql
CREATE TABLE samples (
    model_name TEXT,
    split TEXT,              -- 'train' | 'live'
    monthly_spend REAL,
    sessions_7d REAL,
    tenure_months REAL,
    support_tickets REAL,
    plan_tier REAL,
    churned INTEGER          -- label
);
```

The `live` split is drawn shifted on `monthly_spend` and `sessions_7d` — the
same two features `feature_stats` already reports as drifted, and the same two
workflow 01's drift node flags. The challenger is therefore evaluated against
data that genuinely differs from what the champion was fitted on, so the
retrain responds to the drift the agent diagnosed rather than to a reshuffle.

**Add a `version` column to `runs`,** so a set of metrics can be attributed to a
registry version. Without it, "the champion's F1" cannot be answered, and that
comparison is the entire phase. Seeded rows are attributed `v12` down to `v10`.

**Seeding becomes conditional:** `seed(db_path: str | None = None, if_empty:
bool = False) -> str`. The container `CMD` passes `--if-empty` so restarts
preserve data; `python seed_db.py` with no flag still performs an explicit
reset, which is what `CLAUDE.md` documents it as doing. The existing positional
call `seed(str(db))` used by the tests is unchanged.

### 2. `app/training.py` — a new module

Kept out of `main.py`, which stays a routing layer.

```python
load_samples(model_name: str) -> tuple[list, list, list, list]
    # X_train, y_train, X_live, y_live

train_challenger(model_name: str) -> dict
    # fits LogisticRegression on train, scores on live,
    # writes a runs row, returns champion vs challenger

promote(model_name: str, version: str) -> dict
    # archives the current champion, marks `version` champion
```

`train_challenger` returns:

```json
{
  "model_name": "churn_clf",
  "run_id": "run-015",
  "champion":   {"version": "v12", "f1": 0.826, "accuracy": 0.877, ...},
  "challenger": {"version": "v13", "f1": 0.851, "accuracy": 0.889, ...}
}
```

Champion metrics come from the `runs` row whose `version` matches the current
registry champion. The challenger version is derived from the highest existing
version number plus one.

scikit-learn provides `LogisticRegression`, `train_test_split` and the metric
helpers. It pulls numpy and scipy, adding roughly 150MB to the agent image. This
does not touch `CLAUDE.md` constraint 2, which governs n8n Code nodes running
under a runtime with no package access; the FastAPI service is a normal Python
process and the constraint says so explicitly.

### 3. Endpoints

```
POST /train    {"model_name": "churn_clf"}
POST /promote  {"model_name": "churn_clf", "version": "v13"}
```

Both sit behind the existing `_auth` shared-secret check, like every other
endpoint except `/health`.

### 4. `workflows/03-retrain-and-evaluate.json`

```
Execute Workflow Trigger
  -> POST /train
  -> IF  {{ $json.challenger.f1 }} > {{ $json.champion.f1 }}
       true  -> POST /promote -> Challenger promoted
       false -> Champion held
```

### 5. Workflow 02

Replace the `Retrain approved` NoOp with an Execute Workflow node calling 03,
the same way workflow 01 calls 02: `typeVersion` 1.1 with a `passthrough`
trigger, and `waitForSubWorkflow` inside `options`.

## Why promotion is n8n's decision, not the agent's

`CLAUDE.md` constraint 3: the agent never performs irreversible actions; it
proposes, n8n executes.

Fitting a model and recording a run is not irreversible — nothing `/train` does
changes what would serve traffic, and a discarded challenger leaves only a row
in a history table. **Promotion is** the consequential step, so the comparison
happens in n8n's IF node and n8n calls `/promote` explicitly. The agent computes
and records; n8n decides. Auto-promoting inside `/train` would be simpler and
would quietly move the only irreversible act in this phase into the layer the
constraint exists to keep it out of.

## Verification

**Testable, and will be tested:**

- `seed(if_empty=True)` leaves existing rows alone; `seed()` resets.
- `train_challenger` writes exactly one `runs` row, with a version not already
  present.
- Metrics are in `[0, 1]` and training is deterministic for a fixed seed.
- `promote` leaves exactly one champion, and archives the previous one.
- `promote` rejects a version absent from the registry rather than creating one.
- `/train` and `/promote` reject a missing token, like the other endpoints.

**Not testable:** workflow 03, as with every workflow. Verified by running it.

**Done when:** approving a RETRAIN from Telegram produces a new `runs` row, and
`registry` shows a new champion if and only if the challenger's F1 is higher —
with both surviving `docker compose restart`.

## Rejected alternatives

**Generating training data in memory inside `/train`.** No schema change, fewer
moving parts. Rejected: the model would train on data unrelated to the drift the
agent just diagnosed, making the retrain theatre and awkward to defend when
asked what actually changed between champion and challenger.

**Hand-written logistic regression.** Zero new dependencies, a ~150MB smaller
image, every line explainable. Rejected: it means defending a hand-rolled
classifier in an ML interview instead of the orchestration the project is
actually about, and hand-computed F1 is a quiet place for bugs.

**Auto-promoting inside `/train`.** One fewer endpoint and one fewer n8n node.
Rejected: see the layering section above.

**Keeping the unconditional reseed.** No change to the Dockerfile. Rejected: it
makes the phase's own deliverable unobservable after a restart.
