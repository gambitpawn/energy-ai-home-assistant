# Energy AI multi-engine contract v1

## Purpose

Energy AI uses one shared decision contract for all battery-control engines so deterministic, adaptive deterministic, neural and hybrid approaches can be evaluated fairly on the same information and later selected by the user without changing the downstream safety layer.

## Permanent baseline

`deterministic_v35` is the permanent baseline engine. It is frozen as the stable reference for challenger performance in active-control operation.

Observed inverter/app behavior is not part of the final engine registry. It remains usable as a historical pre-control reference only for periods where that behavior was actually observed.

Perfect hindsight is an evaluation oracle, not a selectable engine.

## Canonical engine families

- `deterministic_v35` — deterministic DP v3.5, immutable baseline.
- `adaptive_deterministic_v1` — reserved challenger using bounded learned parameters in deterministic optimization.
- `neural_v1` — reserved challenger using a learned policy.
- `hybrid_v1` — reserved challenger combining learned value/model components with deterministic constrained optimization.

## Shared information vintage

Every engine competing at a decision point must receive the same `EngineInput` and therefore the same `information_vintage_id`.

The fingerprint covers:

- generation time;
- decision interval;
- current battery SOC;
- complete load/PV/uncertainty/price horizon;
- price-known/unknown mask;
- physical constraints;
- objective/policy metadata;
- source metadata.

A performance comparison is invalid if competing decisions were not generated from the same information vintage.

## Engine output

`EngineDecision` contains the requested battery action before safety processing. Positive kW means discharge and negative kW means charge.

The engine may additionally return expected SOC, a common-format future action trace, diagnostics and model/training metadata.

## Safety boundary

Decision engines never have physical authority. Their requested action must pass through one common deterministic downstream layer containing physical constraints, stale/fault handling, clamps, hysteresis and write-rate controls.

Engine identity and control mode are separate concepts. The same engine implementation must support shadow evaluation and later active control without a separate active-only policy path.

## Selection and ranking

The contract reserves both manual and future automatic engine selection. No physical engine selection is enabled in contract v1.

Once active control exists, challenger performance is measured primarily against `deterministic_v35`, with mature perfect-hindsight evaluation as an additional oracle benchmark. Historical app/inverter comparisons remain contextual evidence, not the live baseline.
