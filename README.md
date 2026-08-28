# Energy AI for Home Assistant

Energy AI is a Home Assistant app for forecasting, planning and controlling household energy flows around solar PV, a stationary battery, electricity prices and flexible loads.

It is designed as a closed-loop energy controller rather than a collection of independent automations. Measurements from Home Assistant are combined with weather, price and learned consumption/production behaviour. The system forecasts future load and PV production, calculates battery plans, compares competing control engines and — in **Active** mode — sends the selected battery command to a Solinteg inverter through Home Assistant.

The current runtime is version **1.0.100**.

## System objective

The primary objective is to reduce the household's effective electricity cost while respecting physical and operational constraints. Battery charging and discharging can be shifted between quarter-hour intervals when economically justified, while accounting for PV production and uncertainty, household demand, electricity prices, import/export economics, battery efficiency/degradation, SOC reserves, grid limits, optional demand tariffs and uncertain future information.

Plans use 15-minute intervals and are refreshed as measurements, forecasts and prices change.

## Architecture

```text
Home Assistant measurements
        +
weather / irradiance / price data
        ↓
forecasting
  ├─ PV forecast
  ├─ load forecast
  └─ flexible-load context
        ↓
shared information vintage
        ↓
control engines
  ├─ deterministic_v35              ← frozen reference and fallback
  ├─ adaptive_deterministic_v1
  ├─ stochastic_deterministic_v1    ← scenario-based uncertainty optimization
  ├─ gradient_v1                    ← tabular gradient-boosted learned policy
  ├─ neural_v1                      ← neural learned policy
  └─ hybrid_v1                      ← neural-guided constrained v3.5 optimization
        ↓
model selector / operator engine routing
        ↓
selected quarter-hour decision
        ↓
decision_start scheduler
        ↓
deterministic actuator safety
        ↓
Solinteg EMS BattCtrl
        ↓
physical battery
```

Forecasting, model selection and physical actuation are deliberately separated. A challenger can propose an action, but no model can bypass deterministic actuator safety.

## Data sources

Energy AI reads operational state primarily through Home Assistant. Typical entities include PV power, house load, grid power, battery power, battery SOC, electricity price, EV charging state and optional sauna, spa and pool measurements.

Operational and learning data are persisted in:

```text
/data/energy_ai.db
```

The database contains measurements, forecasts, optimizer plans, shared information vintages, engine decisions, selector history, evaluation results, settings and actuator events.

## Forecasting

### PV forecast

The PV forecast combines a physical solar-production baseline with learned residual correction. The physical component uses installation geometry and irradiance. The learned component corrects systematic error using features including irradiance, ambient temperature, cloud conditions, solar position, time of day and season.

Historical production observations are used for automatic recalibration, making the forecast increasingly installation-specific.

### Load forecast

Household demand is forecast from historical load behaviour and current context. Flexible or identifiable loads can be represented separately so known events are not treated as random base-load variation.

The system includes explicit modelling hooks for EV charging and sauna operation, with entity/configuration support prepared for spa and pool loads. Forecast representation and physical device control remain separate; the Solinteg battery is currently the commissioned physical actuator.

## Battery optimizer

`deterministic_v35` produces a battery trajectory over the available forecast horizon and is intentionally frozen as the permanent reference and fallback policy.

Its objective accounts for effective import/export economics, battery capacity and power, efficiency, degradation, hard/preferred SOC limits, reserve penalties, grid limits, uncertain future prices and terminal SOC continuity.

There is no temporary downstream 2 kW commissioning cap in the current control chain. Physical power is bounded by configured battery/grid limits and deterministic actuator safety.

### Live SOC replanning

Normal quarter-hour planning uses a shared information vintage so all engines can be compared fairly. Separately, a deterministic live replanning path can react when measured SOC deviates materially from the expected trajectory. This path is operational receding-horizon safety control and is excluded from challenger comparison.

## Control engines

All engines use the same `EngineInput` / `EngineDecision` contract and receive the same ex-ante information vintage for each decision interval.

### deterministic_v35

Frozen deterministic dynamic-programming policy. It is the permanent baseline, fallback and performance reference.

### adaptive_deterministic_v1

A deterministic challenger with bounded learned policy/risk parameters. It preserves the constrained optimizer structure while allowing selected parameters to adapt from historical performance.

### stochastic_deterministic_v1

The stochastic challenger explicitly uses the load/PV forecast uncertainty in the shared horizon. It creates five symmetric scenarios:

- nominal: 40%
- high load / low PV: 15%
- low load / high PV: 15%
- high load / high PV: 15%
- low load / low PV: 15%

The four uncertainty scenarios use ±1 forecast-uncertainty unit. Their weighted perturbation is symmetric, so expected load and PV remain equal to the source forecast.

The engine is a two-stage stochastic optimizer. Every scenario must accept the same first battery action for the current quarter (**nonanticipativity**). After that action each scenario may follow its own optimal deterministic recourse trajectory.

Candidate first actions are ranked using:

```text
risk-adjusted score
  = expected scenario cost
  + 0.25 × (CVaR80 cost − expected scenario cost)
```

The solver preserves v3.5 battery physics, reserve penalties, grid constraints, terminal-SOC treatment and unknown-price continuation semantics. If load and PV uncertainty are both zero, it collapses exactly to the frozen v3.5 result.

Version 1 explicitly models load/PV uncertainty. Unknown future prices still use the existing v3.5 continuation/risk treatment.

### gradient_v1

`gradient_v1` is a tabular learned policy based on `HistGradientBoostingClassifier`. It is deliberately trained on the **same feature vectors, canonical information vintages and perfect-information teacher labels as `neural_v1`**.

This makes the comparison experimentally useful: gradient and neural see the same problem, while the primary difference is model class.

The current gradient model uses histogram gradient boosting with bounded tree complexity and explicit temporal train/validation splitting. It predicts the same discrete battery action classes as the neural policy and records validation accuracy, action MAE and charge/idle/discharge direction accuracy.

Gradient training reuses the shared teacher-sample table rather than generating its own labels. While the dataset contains fewer than 1000 current-schema samples, a new latest gradient model can be trained at most once per day when new samples exist. At 1000 samples or more, the maximum retraining cadence becomes weekly.

Like every learned challenger, gradient output has no direct physical authority. The requested action must pass model routing, robust selector health checks and deterministic actuator safety.

### neural_v1

A neural learned battery-policy challenger trained from historical information vintages and perfect-information teacher labels. It predicts a discrete action class from current state, forecast context, tariff context and horizon features.

The neural model has no direct physical authority; its requested action passes through the same selector and deterministic actuator safety.

### hybrid_v1

`hybrid_v1` combines the frozen v3.5 constrained dynamic program with the action-probability distribution produced by `neural_v1`.

The neural model does not choose an unconstrained battery target. Its distribution becomes a confidence-weighted prior on the first feasible DP transition. SOC transitions, reserve rules, grid constraints and terminal handling remain deterministic.

The learned prior is bounded:

- maximum neural-prior strength: **6 öre per decision**
- maximum accepted deterioration versus deterministic backbone: **5 öre per decision**
- if the guided path exceeds the regret guard, hybrid returns the ordinary v3.5 action

## Continuous training versus qualification

Training and Auto-selector qualification are separate processes.

### Neural and hybrid

The latest neural model may continue to retrain while one neural revision is frozen as the robust10 qualification candidate. `neural_v1` and `hybrid_v1` share that frozen neural revision during the qualification window.

```text
continuous neural training
      ↓
latest r18 → r19 → r20 ...
      │
      │ snapshot
      ▼
frozen neural candidate r18
      ├─ neural_v1(r18)
      └─ hybrid_v1(r18)
              ↓
          robust10_v1
```

### Gradient

Gradient has an **independent** latest-model stream and an independent frozen qualification candidate:

```text
continuous gradient training
      ↓
latest g05 → g06 → g07 ...
      │
      │ snapshot
      ▼
frozen gradient candidate g05
              ↓
          robust10_v1
```

A newer trained gradient model therefore does not reset an ongoing ten-day race. If the frozen candidate completes robust10 without promotion and a newer latest gradient model exists, that newer model becomes the next candidate. If gradient is the selected incumbent, its controlling candidate remains frozen. Live disqualification can roll it back and open the next candidate generation.

The stochastic engine does not train; its model identity is tied to its scenario/risk-policy definition. Adaptive uses its own deterministic parameter-generation mechanism.

## Model selector and operator engine choice

The selector evaluates challengers using realized performance over complete historical days. The current `robust10_v1` promotion policy requires, among other gates:

- 10 complete qualification days for the same model revision
- at least 7 daily wins
- at least 92 valid quarter-hour intervals per day
- improvement in mean performance
- improvement in median performance
- no material p90/tail regression
- no material clamp/safety regression

If several challengers pass, the eligible engine with the largest relative improvement wins; absolute improvement is the next ranking criterion. Comparison is against the current incumbent, while `deterministic_v35` remains the permanent safety/performance fallback.

The **Models** tab exposes an operator selector. `Auto` follows the robust selector incumbent. Any registered engine can also be selected manually. Manual selection changes only control routing: Auto qualification, promotion and rollback evidence continue in the background. An unavailable, quarantined or unhealthy manually selected engine falls back to `deterministic_v35`.

The Models tab also shows the current race ranking based on paired realized oracle-regret evidence. A challenger can lead the interim ranking without yet being eligible for promotion.

## Operating modes

The primary operator control is at the top of **Parameters**.

### Shadow

Energy AI forecasts, plans, learns and evaluates but does not control battery power. The inverter is returned to its configured safe/normal working mode.

### Active

Selecting Active automatically performs:

1. preflight validation
2. recovery of any pending safe release
3. zero-power Solinteg handshake / arming when required
4. resolution of the selected engine decision valid for the current interval
5. transition to Active
6. deterministic safety filtering
7. Solinteg command and acknowledgement

If no valid selected-engine decision exists for the current quarter, Energy AI can use deterministic fallback or hold 0 kW until the next valid decision. The operator does not need to wait for a quarter boundary before enabling Active.

After an app restart the system deliberately starts in Shadow and must be activated again.

## Decision timing

Every control decision belongs to a specific `decision_start`. Future decisions may be calculated in advance but may not be dispatched early.

The decision-start scheduler keeps future candidates pending and applies them only when their interval begins. At dispatch time the actuator reevaluates safety using fresh actual state.

## Physical Solinteg control

Physical battery control uses Home Assistant entities exposed by the Solinteg/SolaX Modbus integration:

```text
selected action
    ↓
deterministic safety filter
    ↓
Working Mode = EMS BattCtrl
    ↓
battery charge/discharge target
    ↓
readback / acknowledgement
```

Sign convention:

- negative battery target = charging
- positive battery target = discharging

When Energy AI releases control it first sends a zero target and then restores the configured safe working mode, for example `ToU`.

## Deterministic actuator safety

No forecasting or machine-learning engine sends commands directly to the inverter. Before physical dispatch the actuator calculates a safe action envelope from current measurements.

Safeguards include configuration and measurement freshness, hard SOC limits with guard margin, configured battery power limits, grid import/export limits, candidate validity/expiry, exact decision-start timing, Solinteg mode/target acknowledgement, readback tolerance, watchdog supervision, persistent retry of failed safe release and safe zero/release on normal shutdown.

### Important limitation

The current Solinteg/Home Assistant path has no verified inverter-native command-expiry/deadman function. Energy AI can release safely on normal faults, mode changes, restarts and clean shutdown, but cannot guarantee autonomous return to zero after a total host/process/power failure in which no further command can be sent.

## Economics

Energy AI separates Nord Pool spot price from effective marginal import/export economics. Configuration supports fixed per-kWh components and spot-linked percentage components on both import and export. Demand tariffs are modelled separately.

## Web interface

The app runs through Home Assistant Ingress. Main views include:

- **Overview** — current energy state, active model and plan
- **Live** — measurements and forecasts
- **Parameters** — installation, economics, optimizer and actuator settings, plus Shadow/Active mode
- **Models** — Auto/manual engine selection, current race ranking, engine comparisons and selector state
- **Evaluation** — historical performance and replay analysis

Settings saved in Parameters are persisted in SQLite and override Home Assistant app defaults. The Models engine selection is also persistent; new installations default to `Auto`.

## API

Useful operational endpoints include:

```text
GET  /control/operator-mode
POST /control/operator-mode/active
POST /control/operator-mode/shadow

GET  /actuator/status
GET  /actuator/timing/status
POST /actuator/preflight
POST /actuator/arm?confirm=true
POST /actuator/disarm
POST /actuator/run

GET  /engines
GET  /engines/neural/qualification
GET  /engines/gradient/status
GET  /engines/gradient/qualification
GET  /engines/hybrid/status
GET  /engines/stochastic/status
GET  /engines/selector/status
GET  /engines/selector/control/latest
GET  /optimizer/replanning/status
GET  /economics/status

GET  /ui/model-control
POST /ui/model-control
GET  /ui/model-ranking
```

Low-level actuator endpoints remain available for diagnostics, but ordinary operation is intended to use Shadow/Active in Parameters and the engine selector in Models.

## Installation

Add the repository to the Home Assistant App Store:

```text
https://github.com/gambitpawn/energy-ai-home-assistant
```

Install **Energy AI**, configure the required Home Assistant entities and open the app through Ingress.

Before physical Solinteg control, verify that the Working Mode and EMS BattCtrl target entities resolve correctly, the configured safe-release mode is appropriate, physical battery/grid limits match the installation and SOC/load/PV measurements use the expected units/sign conventions.

## Repository structure

```text
energy-ai-home-assistant/
├─ energy_ai/
│  ├─ app/                 runtime, forecasts, engines, selector and actuator
│  ├─ tests/               regression and policy tests
│  ├─ config.yaml          Home Assistant app metadata and defaults
│  ├─ Dockerfile
│  ├─ requirements.txt
│  └─ run.sh
├─ homeassistant/
├─ repository.yaml
└─ README.md
```

## Development principles

The following invariants are intentional:

- keep `deterministic_v35` frozen as reference and fallback
- give competing engines the same ex-ante information vintage
- compare learned model classes on common features/teacher evidence where possible
- separate continuous model training from frozen qualification revisions
- keep automatic model selection and manual routing separate from physical safety
- validate physical actions against current measured state
- never dispatch a future decision before `decision_start`
- fail toward safe release rather than silently continuing after actuator faults
- require measured replay/evaluation evidence before a challenger replaces the Auto incumbent

These constraints allow forecasting and control models to evolve without turning each model change into a new physical safety implementation.
