# Model Integration Guide

This document defines the required integration contract for adding a new optimization/model engine to Energy AI.

It is intentionally prescriptive. A model is **not integrated** merely because its class exists or because it is listed in the registry. It is integrated only when it participates correctly in the complete production chain:

`shared EngineInput -> model decision -> engine_store -> comparison/scoring -> selector/operator routing -> downstream safety -> UI`

The purpose of this document is to prevent partially integrated challengers, hidden CPU work, stale UI assumptions, and model families that accidentally share an invalid training architecture.

---

## 1. Non-negotiable architectural rules

### 1.1 `deterministic_v35` is immutable

`deterministic_v35` is the permanent frozen baseline and safety reference.

A new model must never:

- modify `deterministic_v35` behaviour,
- patch its solver,
- change its objective in place,
- reuse its engine id,
- silently replace its decisions,
- alter its historical meaning.

If a deterministic improvement is required, create a new challenger engine with a new engine id.

### 1.2 Every challenger competes on the same information vintage

All comparable engines must receive the same ex-ante `EngineInput` for a decision interval.

The common contract is defined in:

- `energy_ai/app/engine_contract.py`
- `energy_ai/app/engine_input_v2.py`

The canonical identity is `EngineInput.information_vintage_id`.

A challenger decision is valid for comparison only if:

- `decision.information_vintage_id == engine_input.information_vintage_id`, and
- `decision.decision_start == engine_input.decision_start`.

`engine_store.insert_engine_run()` enforces these invariants and must remain the normal persistence path.

Never build a challenger input from another engine's output. In particular, horizon input must not contain baseline actions, baseline SOC trajectories, objective values, or other post-decision information.

### 1.3 Training information and inference information must be explicitly separated

For learned models, define two different contracts:

1. **Inference features**: information genuinely available at decision time.
2. **Teacher/target data**: information allowed only after realization for training/evaluation.

Never allow hindsight/perfect-information data into inference features.

A teacher may use realized outcomes, but the resulting target must be learnable from the ex-ante feature set. Before implementation, document why the mapping is learnable and what irreducible uncertainty remains.

### 1.4 A model family owns its feature/label architecture

Do not make a new learned model depend on another learned model's feature schema, training table, target definition, confidence calibration, or qualification artifact merely because reuse is convenient.

Shared infrastructure is acceptable only when it represents a genuinely model-independent contract.

Examples of acceptable shared infrastructure:

- `EngineInput`,
- realized load/PV history,
- common economic scoring,
- common perfect-information evaluator,
- generic model artifact helpers.

Examples that should normally be model-specific:

- feature schema,
- target/label schema,
- normalization,
- model artifact metadata,
- training sample table/schema,
- confidence calibration,
- qualification generation/state.

A new architecture should use a new id/schema, for example `foo_v2`, rather than silently changing the semantics of `foo_v1`.

### 1.5 Physical authority remains downstream

`EngineDecision.requested_action_kw` is a requested pre-safety action.

A model must not bypass:

- selector routing,
- deterministic actuator safety,
- SOC guards,
- grid import/export constraints,
- watchdog/fail-safe logic,
- explicit operating-mode/arming controls.

A model may suggest an action. It does not directly command the inverter.

---

## 2. Required design work before coding

For any non-trivial new model, write the implementation plan first.

The plan must define:

- engine id and version,
- model family,
- why the model should exist,
- exact input information available at inference,
- output semantics,
- training target if applicable,
- expected computational cost,
- persistence/artifact format,
- retraining cadence,
- qualification/promotion logic,
- fallback behaviour,
- retirement/migration strategy,
- tests required at each integration layer.

Then review the plan critically for:

- target leakage,
- shared-training assumptions,
- CPU/RAM cost on Raspberry Pi,
- synchronous heavy work in request/control paths,
- duplicate computation between models,
- selector race inconsistencies,
- stale/manual operator selections,
- startup side effects,
- failure modes when model files are missing or corrupt,
- downgrade/upgrade compatibility.

Heavy training, replay, sweeps, and evaluation should normally run in maintenance, preferably overnight when practical and spread over time.

Do not implement until these risks are addressed.

---

## 3. Engine contract

### 3.1 Descriptor

Add the model to `energy_ai/app/engine_registry.py` using `EngineDescriptor`.

Required fields include:

- `engine_id`
- `engine_version`
- `family`
- `display_name`
- `description`
- `baseline=False`
- `available`
- `trainable`
- `learning_enabled`
- `supports_shadow`
- `supports_active`

`available=True` means the production system can actually produce a valid decision for the engine. Do not set it merely because source code exists.

The registry drives operator choices, model comparison, and ranking. Incorrect registry state can expose a model that cannot actually run.

### 3.2 Family

Current accepted families are defined in `engine_contract.ENGINE_FAMILIES`.

If a genuinely new family is required, update the contract deliberately and add tests. Do not misuse an existing family label just to avoid changing the contract.

### 3.3 Decision implementation

The engine must produce `EngineDecision` with:

- its own `engine_id`,
- its own `engine_version`,
- correct family,
- the shared `information_vintage_id`,
- the shared `decision_start`,
- continuous `requested_action_kw`,
- expected SOC if available,
- status,
- diagnostics,
- model metadata where relevant.

Do not quantize actions unless the model architecture or physical device genuinely requires it.

The engine should be deterministic for a fixed model artifact and identical `EngineInput`, unless stochasticity is itself part of the declared algorithm. If stochastic, persist the seed/scenario identity required for reproducibility.

---

## 4. Runtime integration: the critical hook

This is the most important integration step.

A model must produce a decision on the **same current information vintage** as the other competing engines and persist it through `insert_engine_run()`.

Merely registering a model does not make it appear in behaviour comparison or scoring.

### 4.1 Production entry point

The production add-on entry point is `app.runtime_operator:app` via `run.sh`.

Production model hooks are installed from:

- `energy_ai/app/runtime_operator.py`
- and, where legacy/core behaviour still exists, `energy_ai/app/runtime.py`.

Prefer a small, explicit model-specific runtime adapter such as:

`<model>_runtime.py`

with an idempotent function such as:

`install_<model>_runtime_patch(cfg)`

The patch should wrap the common challenger preparation/selector gateway rather than create an independent planning schedule.

### 4.2 Required runtime behaviour

For every eligible decision vintage:

1. obtain the already-built common `EngineInput`,
2. call the model off the event loop if CPU-bound,
3. produce one `EngineDecision`,
4. persist it with `insert_engine_run(engine_input, [decision])`,
5. record diagnostics without blocking physical control,
6. allow selector/operator routing to consume the stored decision.

CPU-heavy model inference must use `asyncio.to_thread()` or the existing worker architecture as appropriate; never block the FastAPI/event-loop control path with substantial synchronous work.

### 4.3 Wrapper ordering

Wrapper order matters.

`runtime_operator.py` currently installs operator routing before active challenger preparation wrappers so all challenger decisions exist on the same vintage before routing.

When adding a wrapper:

- explicitly document its order,
- add a source-order regression test,
- verify it neither bypasses nor recursively re-enters selector routing,
- ensure it is idempotent if installation can occur more than once in tests/imports.

Never rely on accidental import order.

### 4.4 Failure semantics

A challenger failure must not break baseline planning or physical safety.

On model failure:

- record a failed/unavailable model result where appropriate,
- do not prevent `deterministic_v35` from being stored/routed,
- allow selector fallback,
- do not trigger synchronous retraining in the control path.

---

## 5. Persistence and comparison

### 5.1 Engine decisions

Use `energy_ai/app/engine_store.py`.

`engine_information_vintage` stores the shared input.

`engine_decision` stores each model's decision against that shared input.

`competition_rows()` groups decisions by `decision_start` and `information_vintage_id`. This is the basis for behaviour comparison.

If a model does not write decisions here, the Models page can list it from the registry but will have no behaviour series.

### 5.2 Economic scoring

The selector/evaluation pipeline writes mature daily scores to `engine_daily_score`.

A new engine must be included in the same evaluation semantics as other challengers. Do not introduce a private metric for promotion unless the selector policy is explicitly redesigned.

The comparison invariant is:

> Same information vintage, same realized world, same common economic metric.

Perfect-information/oracle evaluation is a teacher/reference, not an active engine.

### 5.3 Model generation identity

Any trainable model must have an immutable model-generation identity in persisted decision metadata.

At minimum persist:

- model id,
- model revision/generation,
- training timestamp,
- feature schema version,
- target schema/version,
- training sample count,
- relevant validation metrics.

Selector qualification must compare stable model generations. A model must not retrain underneath an active qualification window without either freezing a candidate or resetting qualification deliberately.

---

## 6. Selector and operator integration

### 6.1 Operator choices

`energy_ai/app/engine_operator_selection.py` derives registered engine ids from `registry_status()`.

A new available engine therefore becomes a possible manual operator choice.

Tests must verify:

- the engine appears when available,
- invalid/manual stale engine ids fall back to Auto,
- missing decisions fall back to `deterministic_v35`,
- model health failures cannot bypass fallback.

If display text differs from the engine id, add a `DISPLAY_NAMES` entry in the production runtime or centralize it in the registry.

### 6.2 Auto selector

The robust selector uses model keys/generations, paired mature-day scores, qualification gates, health events, quarantine, and fallback.

For a new engine, verify explicitly that:

- `_current_model_key(engine_id)` can resolve a stable key,
- `_engine_model_key(...)` can recover the key from persisted decisions,
- qualification compares like-for-like generations,
- insufficient model data produces `waiting_model`/equivalent rather than an exception,
- live health checks understand the model's action/SOC semantics,
- fallback always remains `deterministic_v35`.

A new trainable architecture should normally get its own qualification state/artifact rather than reuse another model family's candidate state.

---

## 7. Training and maintenance

### 7.1 No training in the control path

Training, sample construction, replay, feature backfills, hyperparameter search, counterfactual evaluation, and large DB scans must not run synchronously during a quarter decision or HTTP request.

Use the maintenance architecture in:

- `energy_ai/app/runtime_maintenance.py`
- `energy_ai/app/maintenance_coordination.py`

### 7.2 Scheduling

For expensive work:

- prefer nightly execution where possible,
- spread different jobs over separate slots,
- use low-priority coordination,
- avoid quarter boundaries,
- avoid starting multiple CPU-heavy jobs simultaneously,
- cap candidate/sample counts per run,
- make jobs resumable/incremental.

Document expected worst-case duration and memory use on Raspberry Pi hardware.

### 7.3 Training ownership

Each learned model owns its training lifecycle.

A new model must not depend on another model's automatic maintenance loop to keep its dataset alive.

If two models genuinely need a common dataset, promote that dataset generator to a clearly named model-independent module/table with its own schema and tests. Do not hide shared ownership under one model's name.

### 7.4 Automatic retraining

Retraining policy must define:

- minimum samples,
- minimum target/action diversity,
- validation split semantics,
- cadence,
- requirement for new samples,
- artifact atomicity,
- rollback behaviour,
- qualification reset/freeze behaviour.

Never retrain simply because the service restarted.

---

## 8. Learned-model feature and target design

Before implementing a learned model, add a design note covering the following.

### 8.1 Prediction target

State exactly what the model predicts.

Examples:

- continuous battery action kW,
- value function,
- residual correction to a deterministic action,
- forecast-error distribution,
- policy parameters.

Do not use categorical classification for an inherently ordered/continuous target unless there is a specific mathematical justification.

### 8.2 Information boundary

For every feature, answer:

> Was this value actually knowable at `decision_start`?

Persist source timestamps where practical.

### 8.3 Teacher alignment

If hindsight/oracle labels are used, quantify whether multiple materially different realized futures can map the same ex-ante state to different optimal actions.

If this ambiguity is large, reconsider the target. Possible alternatives include:

- value prediction rather than direct action imitation,
- uncertainty-aware policy,
- residual learning around a deterministic optimizer,
- learned optimizer parameters,
- scenario/value distribution prediction.

Do not assume a perfect-information policy is automatically a good supervised target for an ex-ante policy.

### 8.4 Validation metrics

Use metrics that match physical/economic meaning.

For action regression this may include:

- MAE in kW,
- bias,
- direction accuracy,
- within ±1/±2 kW,
- realized economic regret,
- clamp/safety rejection rate.

Accuracy alone is usually insufficient for ordered action decisions.

---

## 9. UI integration

The Models page is backed by `energy_ai/app/ui_models.py` and selector/operator UI extensions.

A correctly integrated model should appear from the registry automatically, but useful charts require persisted data.

Verify both modes:

### Economic

Requires mature `engine_daily_score` rows.

Expected behaviour:

- no score yet -> model still visible with appropriate state,
- score available -> cumulative realized oracle-regret series appears,
- lower regret is correctly described as better.

### Behaviour

Requires `engine_decision` rows for common vintages.

Expected behaviour:

- requested battery action series appears,
- expected SOC appears if model supplies it,
- timestamps align with other models,
- model does not silently disappear because its runtime hook failed to persist decisions.

Add UI/API regression tests that construct at least two engines on the same information vintage and verify both are returned.

---

## 10. Status and diagnostics endpoints

A model-specific status endpoint is optional but recommended for trainable or operationally complex engines.

Useful fields include:

- engine id,
- runtime build,
- model exists,
- model id/revision,
- trained at,
- training samples,
- feature schema,
- target schema,
- validation metrics,
- shadow ready,
- active eligible,
- qualification state,
- last inference error,
- last maintenance error.

Do not expose a `ready` state that differs from what registry/selector actually use.

---

## 11. Model artifact rules

Model files belong under `/data/models` and must survive add-on/container replacement.

Use atomic writes/replacements for:

- model artifacts,
- metadata,
- qualification candidate state,
- training status.

Never overwrite the only known-good model before the replacement artifact and metadata are complete.

For a new architecture use unique names, for example:

- `<engine_id>.joblib`
- `<engine_id>.json`
- `<engine_id>_versions/`
- `<engine_id>_qualification.json`

Do not reuse retired v1 filenames for a new v2 architecture.

---

## 12. Migration and retirement

Every model integration must define how it will later be removed.

A retirement must account for all of the following:

- registry descriptor,
- runtime wrapper,
- maintenance loop,
- training/sample generation,
- qualification hooks,
- selector model keys/state,
- manual operator preference,
- stored decisions/scores,
- model artifacts/version directories,
- UI display names/routes,
- tests that assert the old architecture,
- documentation.

If old history is semantically invalid, explicitly migrate/delete it instead of leaving it to contaminate comparison.

If history remains meaningful, mark the engine historical and prevent it from being selectable.

Never leave a retired model's training loop running merely because its UI entry was removed.

---

## 13. Required test matrix

A new model is not complete until the following layers are covered.

### 13.1 Contract tests

Test:

- descriptor fields,
- unique engine id/version,
- valid family,
- `EngineDecision` construction,
- shared information vintage,
- continuous action semantics,
- expected SOC bounds,
- deterministic/reproducible output for fixed input where applicable.

### 13.2 Runtime hook tests

Test:

- production runtime installs the model hook,
- installation order is correct,
- hook is idempotent,
- a model decision is created for the same vintage as baseline,
- decision is inserted into `engine_store`,
- failure does not prevent baseline decision/routing,
- no heavy synchronous training occurs in the quarter path.

These tests must inspect the **current production entry point**, not an obsolete runtime module.

### 13.3 Store/comparison tests

Test:

- baseline + challenger decisions persist on the same vintage,
- `competition_rows()` returns both,
- Models API behaviour contains both,
- mature scoring creates challenger rows,
- no accidental mixing of different information vintages.

### 13.4 Selector tests

Test:

- manual selection,
- Auto selection,
- missing challenger decision fallback,
- invalid action fallback,
- model generation key,
- qualification accumulation,
- promotion gate,
- quarantine/disqualification,
- restart persistence,
- stale selection after model retirement.

### 13.5 Training tests for learned models

Test:

- no target leakage,
- feature schema width/version,
- sample construction uses only eligible vintages,
- target construction is reproducible,
- minimum sample/diversity guards,
- artifact atomicity,
- corrupt/missing artifact handling,
- retraining cadence,
- no retraining without new data,
- frozen qualification candidate semantics if used.

### 13.6 Maintenance tests

Test:

- job is actually included in `combined_maintenance_loop`,
- slot/cadence is correct,
- heavy jobs use low-priority coordination,
- retired/disabled model jobs are absent,
- repeated startup does not duplicate loops.

### 13.7 UI tests

Test:

- model listed exactly once,
- correct display name/state,
- economic series when scores exist,
- behaviour series when decisions exist,
- empty-data state is explicit,
- retired models do not remain selectable.

### 13.8 Version/migration tests

Test:

- model migrations are idempotent,
- upgrade from previous state is safe,
- stale selector/manual state is repaired,
- unrelated models/data are untouched,
- release version is sourced canonically.

---

## 14. CI search checklist before push

Before pushing a model architecture change, search the entire test suite and runtime source for the old/new engine id and all relevant install hooks.

At minimum search for:

- engine id,
- runtime install function,
- qualification function,
- maintenance function,
- model artifact filename,
- feature schema,
- target/label schema,
- UI display name,
- selector model key.

This is mandatory when adding, renaming, or retiring a model. Source-string regression tests can otherwise continue asserting an obsolete architecture even when production code is correct.

Do not rely only on the first failing CI test.

---

## 15. Versioning rules

When the add-on release version is bumped:

- change the version **only** in `energy_ai/config.yaml`,
- do not hardcode the same release number in runtime modules or tests,
- runtime/UI version must resolve through the canonical release-version mechanism.

A documentation-only change does not require a release bump unless it is intentionally bundled into a release.

Model `engine_version`, feature-schema versions, model artifact revisions, and add-on release versions are separate concepts. Do not conflate them.

---

## 16. Recommended file structure for a new learned model

Example for a hypothetical `policy_v2`:

```text
energy_ai/app/
  policy_v2_engine.py
  policy_v2_features.py
  policy_v2_training.py
  policy_v2_runtime.py
  policy_v2_qualification.py      # only if qualification requires custom state
  policy_v2_cleanup.py            # if migration/retirement needs explicit cleanup

energy_ai/tests/
  test_policy_v2_engine.py
  test_policy_v2_features.py
  test_policy_v2_training.py
  test_policy_v2_runtime.py
  test_policy_v2_selector.py
  test_policy_v2_ui.py
```

Shared model-independent helpers should have model-independent names. Do not put shared infrastructure under another engine's `neural_*`, `gradient_*`, etc. namespace.

---

## 17. Acceptance checklist

A PR adding a new model should not be merged until every applicable item below is true.

- [ ] Design and risk review completed before implementation.
- [ ] `deterministic_v35` unchanged.
- [ ] Unique engine id and explicit engine version.
- [ ] Descriptor added to registry only when runtime is actually available.
- [ ] Engine consumes canonical `EngineInput`.
- [ ] No post-decision/hindsight leakage into inference inputs.
- [ ] Engine produces valid `EngineDecision`.
- [ ] Same `information_vintage_id` as competing engines.
- [ ] Runtime hook produces a decision every eligible quarter.
- [ ] Decision persisted through `engine_store`.
- [ ] Failure cannot break baseline or downstream safety.
- [ ] CPU-heavy inference does not block the event loop.
- [ ] Training/replay not executed in control/request path.
- [ ] Maintenance scheduling is explicit and load-aware.
- [ ] Learned model owns its feature/target/training architecture.
- [ ] Stable model-generation identity persisted.
- [ ] Qualification cannot mix model generations silently.
- [ ] Selector model key works.
- [ ] Manual selection works and has deterministic fallback.
- [ ] Auto ranking/qualification works.
- [ ] Models economic comparison works.
- [ ] Models behaviour comparison works.
- [ ] Status/diagnostics expose useful readiness information.
- [ ] Artifact writes are atomic.
- [ ] Upgrade/migration path tested.
- [ ] Retirement path documented.
- [ ] Entire test suite searched for stale architecture assumptions.
- [ ] Version bumped only in `config.yaml` if a release bump is required.
- [ ] Full CI passes.

---

## 18. Definition of done

A new model is done only when it can be followed end-to-end from one real production decision interval:

1. Energy AI creates one canonical `EngineInput`.
2. `deterministic_v35` and the challenger receive that exact vintage.
3. Both produce `EngineDecision` objects.
4. Both decisions are persisted.
5. The Models behaviour endpoint returns both.
6. Mature evaluation scores both against the same realized outcome/oracle semantics.
7. Selector ranking sees the challenger with the correct model generation.
8. Manual selection can route it and safely fall back if it fails.
9. Auto can qualify/promote it only through the defined gates.
10. The actuator still independently enforces physical safety.
11. Restart/upgrade preserves the intended model state without starting duplicate or unintended heavy work.

If any link in that chain is missing, the model is not integrated.
