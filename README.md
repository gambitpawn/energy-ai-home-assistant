# Energy AI for Home Assistant

Energy AI is a Home Assistant app for forecasting, planning and controlling household energy flows around solar PV, a stationary battery, electricity prices and flexible loads.

It is designed as a closed-loop energy controller rather than a collection of independent automations. Measurements from Home Assistant are combined with weather, price and learned consumption/production behaviour. The system forecasts future load and PV production, calculates battery plans, compares competing control engines and — in **Active** mode — sends the selected battery command to a Solinteg inverter through Home Assistant.

The current runtime is version **1.0.98**.

## System objective

The primary objective is to reduce the household's effective electricity cost while respecting physical and operational constraints. Battery charging and discharging can be shifted between quarter-hour intervals when economically justified, while accounting for:

- PV production and forecast uncertainty
- household demand and flexible loads
- quarter-hour electricity prices
- import and export price components
- battery charge/discharge efficiency and degradation cost
- hard and preferred state-of-charge limits
- reserve requirements
- grid import and export limits
- optional demand tariffs
- uncertain future prices and forecast quality

Plans are built on 15-minute intervals and refreshed when new measurements, forecasts or prices become available.

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
  ├─ deterministic_v35       ← frozen reference and fallback
  ├─ adaptive_deterministic_v1
  ├─ neural_v1
  └─ hybrid_v1               ← neural-guided constrained v3.5 optimization
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

Forecasting, model selection and physical actuation are deliberately separated. A learned model can propose an action, but it cannot bypass deterministic actuator safety.

## Data sources

Energy AI reads operational state primarily through Home Assistant. Typical entities include:

- PV power
- house load
- grid power
- battery power
- battery state of charge
- current electricity price
- EV charging power and connection state
- optional vehicle SOC / target SOC / ready-by time
- optional sauna, spa and pool measurements

The default installation profile is configured for a Solinteg inverter and battery, but entity IDs are configurable.

Operational and learning data are persisted in SQLite:

```text
/data/energy_ai.db
```

The database contains measurements, forecasts, optimizer plans, model decisions, selector history, evaluations, settings and actuator events.

## Forecasting

### PV forecast

The PV forecast combines a physical solar-production baseline with a learned residual correction. The physical component uses installation geometry and irradiance. The learned component corrects systematic error using features including irradiance, air temperature, cloud conditions, solar position, time of day and season.

Historical production observations are used for automatic recalibration, making the forecast increasingly specific to the actual installation.

### Load forecast

Household demand is forecast from historical load behaviour and current context. Flexible or identifiable loads can be represented separately so that known events are not treated as random base-load variation.

The system currently includes explicit modelling hooks for EV charging and sauna operation, with entity/configuration support prepared for spa and pool loads.

Flexible-load forecasting and physical device control are separate. The Solinteg battery is currently the commissioned physical actuator; representing another load in the forecast does not automatically give Energy AI authority to switch that device.

## Battery optimizer

The deterministic optimizer produces a battery trajectory over the available forecast horizon. `deterministic_v35` is intentionally frozen and serves as the permanent reference and fallback policy.

The optimizer accounts for:

- effective import and export prices
- battery energy capacity
- maximum charge and discharge power
- charge/discharge efficiency
- battery degradation cost
- hard and preferred SOC limits
- reserve penalties
- grid import/export limits
- uncertain future electricity prices
- terminal SOC continuity

The configured battery power limits are the maximum actions available to deterministic safety. There is no separate temporary 2 kW commissioning cap in the current control chain.

### Live SOC replanning

Normal quarter-hour planning uses a shared information vintage so all engines are compared fairly. Separately, a deterministic live replanning path can react when measured SOC deviates materially from the expected trajectory.

This path is operational safety/receding-horizon control and is excluded from challenger comparison.

## Control engines

All engines use the same `EngineInput` / `EngineDecision` contract and receive the same ex-ante information vintage for a decision interval.

### deterministic_v35

Frozen deterministic dynamic programming policy. It is the permanent baseline, fallback and performance reference.

### adaptive_deterministic_v1

A deterministic challenger with bounded learned policy/risk parameters. It preserves the constrained optimizer structure while allowing selected policy parameters to adapt from historical performance.

### neural_v1

A learned battery-policy challenger trained from historical information vintages and perfect-information teacher labels. It predicts an action class from system state, forecast context, tariff context and future horizon features.

The neural model has no direct physical authority. Its requested action still passes through the selector and deterministic actuator safety.

### hybrid_v1

`hybrid_v1` combines the frozen v3.5 constrained dynamic program with the action-probability distribution produced by `neural_v1`.

The neural model does not choose an unconstrained battery target. Instead, its distribution is converted into a confidence-weighted prior on the **first feasible DP transition**. SOC transitions, reserve rules, grid constraints and terminal handling remain deterministic.

The learned prior is deliberately bounded:

- maximum neural-prior strength: **6 öre per decision**
- maximum accepted deterioration versus the deterministic backbone: **5 öre per decision**
- if the guided path exceeds that regret guard, hybrid returns the ordinary v3.5 action

This allows learned information to break economically close deterministic ties without bypassing the deterministic optimization envelope.

## Continuous training versus qualification

Neural training and selector qualification are intentionally separate processes.

The **latest trained neural model** may continue to change as new samples arrive. That model is not automatically substituted into the live race. Instead, Energy AI snapshots one neural revision as a **frozen qualification candidate**. Both `neural_v1` and `hybrid_v1` use that same frozen neural revision for the entire qualification window.

```text
continuous training
      ↓
latest neural revision r18 → r19 → r20 ...
      │
      │ snapshot when a new candidate starts
      ▼
frozen qualification candidate r18
      │
      ├─ neural_v1(r18)
      └─ hybrid_v1(r18)
              ↓
          robust10_v1
```

Daily or weekly retraining therefore does **not** reset the ten-day race.

A frozen candidate remains unchanged until one of these occurs:

- it passes qualification and becomes the selected incumbent
- it completes the robust ten-day qualification without promotion and a newer trained model is available; the latest model is then snapshotted as the next qualification candidate
- it is selected and subsequently disqualified by the live circuit breaker; the selector rolls back and the latest available neural model can become the next candidate
- its feature schema becomes incompatible with the current runtime, requiring a compatible candidate snapshot

A neural or hybrid engine that is already the selected incumbent remains frozen; background training cannot silently replace the model controlling the race.

## Model selector and operator engine choice

The selector evaluates challengers using realized performance over complete historical days. Promotion requires sustained improvement rather than a single good result.

The current `robust10_v1` policy requires, among other checks:

- 10 complete qualification days for the same model revision
- at least 7 daily wins
- at least 92 valid quarter-hour intervals per day
- improvement in mean performance
- improvement in median performance
- no material p90/tail regression
- no material clamp/safety regression

If several challengers pass, the selector chooses the eligible engine with the largest relative improvement; absolute improvement is used as the next ranking criterion.

The comparison is against the current incumbent. `deterministic_v35` remains the permanent fallback. If a selected challenger becomes unhealthy or materially underperforms, the selector can roll back to the deterministic baseline.

The **Models** tab exposes an operator engine selector. `Auto` is the default and follows the robust selector incumbent. An individual engine can also be chosen manually. Manual selection changes only the routing of control decisions: the Auto race, qualification, promotion and rollback evidence continue to be collected in the background. A manually selected engine is still subject to model-health checks and downstream deterministic actuator safety; unavailable, quarantined or unhealthy manual engines fall back to `deterministic_v35`.

The Models tab also shows the current race ranking. This is a descriptive ranking based on current paired realized oracle-regret performance versus the Auto incumbent. A model can therefore lead the interim ranking while still being marked as evaluating; only the full robust qualification gates permit automatic promotion.

Physical actuation is downstream of selector and operator-routing logic, so neither promotion nor manual engine choice changes the actuator safety contract.

## Operating modes

The primary operator control is at the top of **Parameters**.

### Shadow

Energy AI forecasts, plans, learns and evaluates but does not control battery power. The inverter is returned to the configured normal/safe working mode.

### Active

Selecting Active performs the required control transition automatically:

1. preflight validation
2. recovery of any pending safe release
3. zero-power Solinteg handshake / arming when required
4. resolution of the selected engine decision valid for the current interval
5. transition to Active
6. deterministic safety filtering
7. Solinteg command and acknowledgement

If no valid selected-engine decision exists for the current quarter, Energy AI can take control using the deterministic fallback or at 0 kW and wait for the next valid decision. The operator does not need to wait for a quarter boundary before enabling Active.

After an app restart the system deliberately starts in Shadow and must be activated again.

## Decision timing

Every control decision belongs to a specific `decision_start`. Future decisions may be calculated in advance but may not be dispatched early.

The decision-start scheduler keeps future candidates pending and applies them only when their interval begins. At dispatch time the actuator reevaluates safety using fresh actual state.

For example, a plan for 16:45 may already exist at 16:30, but its battery command cannot replace the current command before 16:45.

## Physical Solinteg control

Physical battery control uses Home Assistant entities exposed by the Solinteg/SolaX Modbus integration.

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

No forecasting or machine-learning engine sends commands directly to the inverter. Before physical dispatch the actuator uses current measurements to calculate the safe action envelope.

Safeguards include:

- configuration freshness checks
- actual-state freshness limits
- current SOC/load/PV availability
- hard SOC limits with guard margin
- configured battery max charge/discharge power
- configured grid import/export limits
- candidate validity and expiry
- exact `decision_start` timing
- Solinteg mode and target acknowledgement
- target readback tolerance
- watchdog supervision while Active
- persistent retry of failed safe release
- zero-target and safe-mode release on normal shutdown

### Important limitation

The current Solinteg/Home Assistant path has no verified inverter-native command-expiry/deadman function. Energy AI can release safely on normal faults, mode changes, restarts and clean shutdown, but cannot guarantee autonomous return to zero after a total host/process/power failure in which no further command can be sent.

## Economics

Energy AI separates Nord Pool spot price from the effective marginal import/export economics of the household. Configuration supports fixed per-kWh components and spot-linked percentage components on both import and export.

Demand tariffs are modelled separately and can be enabled when the configured tariff structure is ready for production use.

## Web interface

The app runs through Home Assistant Ingress. Main views include:

- **Overview** — current energy state, active model and plan
- **Live** — measurements and forecasts
- **Parameters** — installation, economics, optimizer and actuator settings, plus Shadow/Active mode
- **Models** — Auto/manual engine selection, current Auto race ranking, engine comparisons and selector state
- **Evaluation** — historical performance and replay analysis

Settings saved in Parameters are persisted in SQLite and override Home Assistant app defaults. The Models engine selection is also persisted in SQLite; new installations default to `Auto`. Settings affecting startup-configured actuator semantics require a restart before physical control is allowed.

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
GET  /engines/hybrid/status
GET  /engines/selector/status
GET  /engines/selector/control/latest
GET  /optimizer/replanning/status
GET  /economics/status

GET  /ui/model-control
POST /ui/model-control
GET  /ui/model-ranking
```

Low-level actuator endpoints remain available for diagnostics, but ordinary operation is intended to use the Shadow/Active control in Parameters and the engine selector in Models.

## Installation

Add the repository to the Home Assistant App Store:

```text
https://github.com/gambitpawn/energy-ai-home-assistant
```

Install **Energy AI**, configure the required Home Assistant entities and open the app through Ingress.

Before physical Solinteg control, verify that:

- the Working Mode entity resolves correctly
- the EMS BattCtrl charge/discharge target resolves correctly
- the configured safe release mode is the desired inverter mode when Energy AI is not controlling it
- battery and grid limits match the installation
- SOC/load/PV measurements use the expected units and sign conventions

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
- separate continuous model training from frozen qualification revisions
- keep automatic model selection and manual engine routing separate from physical safety
- validate physical actions against current measured state
- never dispatch a future decision before `decision_start`
- fail toward safe release rather than silently continuing after actuator faults
- require measured replay/evaluation evidence before a learned engine replaces the Auto incumbent

These constraints allow forecasting and control models to evolve without turning each model change into a new physical safety implementation.
