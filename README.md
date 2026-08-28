# Energy AI for Home Assistant

Energy AI is a Home Assistant app for forecasting, planning and controlling household energy flows around a solar PV system, stationary battery, electricity prices and flexible loads.

The system is designed as a closed-loop energy controller rather than a collection of independent automations. It continuously combines measurements from Home Assistant with weather, price and learned consumption/production behaviour, calculates an economically preferred battery trajectory, compares alternative control models and — when operating in **Active** mode — sends the selected battery command to a Solinteg inverter through Home Assistant.

The current runtime is version **1.0.96**.

## What the system is trying to optimize

The primary objective is to reduce the household's effective electricity cost while respecting physical and operational constraints. The controller can use the battery to shift energy between intervals when that is economically justified, while also accounting for:

- PV production and forecast uncertainty
- household load and flexible loads
- quarter-hour electricity prices
- import and export price components
- battery charge/discharge efficiency and degradation cost
- minimum and preferred state-of-charge levels
- grid import and export limits
- optional demand-tariff logic
- the value of retaining battery reserve when future prices or forecasts are uncertain

The control horizon is built from 15-minute intervals. Plans are refreshed as new measurements, forecasts and prices become available.

## System architecture

At a high level, Energy AI follows this chain:

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
model selector
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

The forecasting, model-selection and physical-control layers are deliberately separated. A model can propose an action, but it cannot bypass the deterministic actuator safety layer.

## Data sources

Energy AI reads its operational state primarily through Home Assistant. Typical source entities include:

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

Operational and learning data are persisted in SQLite at:

```text
/data/energy_ai.db
```

This database stores measurements, forecasts, optimizer plans, model decisions, selector history, evaluations, settings and actuator events.

## Forecasting

### PV forecast

The PV forecast combines a physical solar-production baseline with a learned residual correction.

The physical part uses the installation geometry and irradiance estimate. The learned model then corrects systematic errors using features such as irradiance, air temperature, cloud conditions, solar position, time of day and season. This lets the model learn installation-specific behaviour that a generic irradiance calculation does not capture.

Historical observations are used for automatic recalibration. The forecast therefore becomes increasingly specific to the actual PV installation rather than relying only on a generic panel model.

### Load forecast

Household demand is forecast from historical load behaviour and current context. Flexible or identifiable loads can be represented separately so that known events do not have to be treated as random base-load variation.

The system currently has explicit modelling hooks for EV charging and sauna operation, with entity/configuration support prepared for spa and pool loads as well.

Flexible-load modelling and physical device control are separate capabilities: at present the Solinteg battery is the commissioned physical actuator. Other loads can be represented in forecasts and overrides without automatically implying that Energy AI can physically switch those devices.

## Battery optimizer

The deterministic optimizer produces a battery trajectory over the available forecast horizon. The reference implementation is `deterministic_v35`, which is intentionally frozen so that it remains a stable baseline for evaluation and fallback.

The optimizer considers, among other things:

- effective import and export prices
- battery energy capacity
- maximum charge and discharge power
- charge/discharge efficiency
- battery degradation cost
- hard and preferred SOC limits
- reserve penalties
- grid import/export limits
- uncertain future electricity prices
- terminal SOC continuity at the end of the horizon

The configured battery power limits are the maximum physical battery actions available to the deterministic safety layer. There is no separate temporary 2 kW commissioning cap in the current control chain.

### Live SOC replanning

Normal quarter-hour planning uses the shared information vintage required for fair model comparison. Separately, a deterministic live safety/replanning path can react when measured SOC deviates materially from the expected trajectory.

This live replanning is not used to give one model an information advantage in the model-selection evaluation. It is a receding-horizon operational safety mechanism.

## Control engines and model selection

All control engines use a common input/output contract and receive the same ex-ante information vintage for a quarter-hour decision.

### deterministic_v35

The frozen deterministic baseline. It is the reference implementation, the permanent fallback and the benchmark against which challengers are evaluated.

### adaptive_deterministic_v1

A deterministic challenger whose parameters can be learned from historical performance while preserving the same basic optimization structure.

### neural_v1

A learned control challenger trained from historical information vintages and teacher/evaluation data. It remains subject to the same downstream physical safety constraints as every other model.

### hybrid_v1

`hybrid_v1` combines the frozen v3.5 constrained dynamic program with the probability distribution produced by `neural_v1`.

The neural model does **not** directly choose an unconstrained battery target. Instead, its probability distribution is converted into a confidence-weighted prior on the **first feasible DP transition**. The rest of the trajectory, all SOC transitions, reserve logic, grid limits and terminal handling remain deterministic v3.5 logic.

The learned prior is intentionally bounded:

- maximum neural-prior strength: **6 öre per decision**
- maximum accepted deterministic-backbone deterioration: **5 öre per decision**
- if the neural-guided path exceeds that regret guard, the hybrid engine returns the ordinary v3.5-backbone action instead

This makes the hybrid engine most influential when several deterministic alternatives are economically close. It can use patterns learned by the neural teacher to break those near-ties without giving the neural network authority to violate the deterministic optimization envelope.

The hybrid model revision is derived from the active `neural_v1` model identity. A neural retrain therefore creates a new hybrid model revision and forces fresh selector qualification before that revision can become the active control engine.

### Selector

The selector evaluates challengers against the baseline over complete historical days. Promotion requires sustained improvement rather than a single good result. The policy considers mean and median performance, daily wins, tail behaviour, coverage and safety/clamp behaviour.

The current robust policy requires a challenger/model revision to qualify over ten complete days, including at least seven daily wins, without material tail or safety regression. `hybrid_v1` enters this same competition automatically once the neural model is shadow-ready; it has no special promotion path.

If a selected challenger becomes unhealthy, the selector falls back to `deterministic_v35`. Physical actuation is downstream of this selection process, so changing the selected model does not change the actuator safety contract.

## Operating modes

The main operator control is at the top of **Parameters** and has two states:

### Shadow

Energy AI forecasts, plans, learns and evaluates, but does not control battery power. The inverter is returned to the configured safe/normal working mode.

### Active

Selecting Active performs the required commissioning checks automatically:

1. preflight validation
2. recovery of any pending safe release
3. zero-power Solinteg handshake / arming when required
4. resolution of the selector decision valid for the current interval
5. transition to Active
6. deterministic safety filtering
7. Solinteg command and acknowledgement

If there is no valid selector decision for the current quarter, the system can take control at 0 kW and wait for the next valid decision. The operator does not need to wait for a quarter boundary before enabling Active.

After an app restart the system deliberately starts in Shadow and must be activated again.

## Decision timing

A control decision belongs to a specific `decision_start` interval. Future decisions may be calculated in advance, but they are not physically dispatched early.

The decision-start scheduler keeps future candidates pending and applies them only when their interval begins. At dispatch time the actuator reevaluates safety using fresh actual state.

This is important because optimizer refreshes often occur shortly before the next quarter. A plan for 16:45 may therefore exist at 16:30, but the 16:45 battery action must not replace the currently active command until 16:45.

## Physical Solinteg control

Physical battery control uses Home Assistant entities exposed by the Solinteg/SolaX Modbus integration.

The normal control path is:

```text
selected action
    ↓
deterministic safety filter
    ↓
Working Mode = EMS BattCtrl
    ↓
battery charge/discharge power target
    ↓
readback / acknowledgement
```

For the Solinteg target used by this project:

- negative battery target = charging
- positive battery target = discharging

When Energy AI releases control, it first sends a zero target and then restores the configured safe working mode, for example `ToU`.

## Deterministic actuator safety

Physical commands are never sent directly from a forecasting or machine-learning model. Before dispatch, the actuator checks the current physical state and calculates the safe action envelope.

The main safeguards are:

- Home Assistant authentication and entity discovery
- configuration freshness checks before arming or Active operation
- actual-state freshness limit
- current SOC, load and PV availability
- hard minimum and maximum SOC constraints with guard margin
- configured battery maximum charge/discharge power
- configured grid import and export limits
- candidate validity and expiry
- exact `decision_start` timing
- Solinteg working-mode and target acknowledgement
- target readback tolerance
- watchdog supervision while Active
- persistent retry of failed safe release
- zero-target and safe-mode release on normal shutdown

The actuator recalculates the grid and SOC envelope from current measurements; the optimizer's requested action is reduced when necessary to remain inside that envelope.

### Important limitation

The current Solinteg/Home Assistant interface does not provide a verified inverter-native command-expiry/deadman function for this control path. Energy AI can release safely on normal faults, mode changes, restarts and clean shutdown, but it cannot guarantee that the inverter will autonomously return to zero after a total host/process/power failure in which no further command can be sent.

## Economics

Energy AI separates market spot price from the effective import/export economics seen by the household. The configuration supports fixed per-kWh components and spot-linked percentage components on both import and export.

This allows the optimizer to make decisions using the actual marginal value of importing, exporting, charging and discharging rather than treating the Nord Pool price alone as the household electricity price.

Demand tariffs are modelled separately and can be enabled when the configured tariff structure is ready for production use.

## Web interface

The app runs through Home Assistant Ingress. The interface is intended both for ordinary operation and for inspecting why the system is behaving as it does.

The main views include:

- **Overview** — current energy state and plan
- **Live** — operational measurements and forecasts
- **Parameters** — installation, economics, optimizer and actuator settings, plus Shadow/Active operating mode
- **Models** — engine status, model comparison and selection
- **Evaluation** — historical performance and replay analysis

Settings saved in Parameters are persisted in SQLite and override Home Assistant add-on defaults. Settings that affect startup-configured actuator semantics require an app restart before they are allowed to control physical hardware.

## API

The UI is backed by a FastAPI service. Useful operational endpoints include:

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
GET  /engines/hybrid/status
GET  /engines/selector/status
GET  /engines/selector/control/latest
GET  /optimizer/replanning/status
GET  /economics/status
```

The low-level actuator endpoints remain useful for diagnostics and development, but normal operation is intended to use the Shadow/Active control in Parameters.

## Installation

Energy AI is packaged as a Home Assistant app/add-on repository.

Add the repository to the Home Assistant App Store:

```text
https://github.com/gambitpawn/energy-ai-home-assistant
```

Install **Energy AI**, configure the required Home Assistant source entities and open the app through Ingress.

For physical Solinteg control, verify at minimum that:

- the Working Mode entity resolves correctly
- the EMS BattCtrl charge/discharge target resolves correctly
- the configured safe release mode is the inverter mode you want when Energy AI is not controlling it
- battery and grid limits match the installation
- SOC/load/PV measurements are fresh and use the expected sign/unit conventions

Use Shadow while configuring or evaluating the system. Select Active only when the physical integration and limits have been verified for the installation.

## Repository structure

```text
energy-ai-home-assistant/
├─ energy_ai/
│  ├─ app/                 Python runtime, forecasts, optimizer and actuator
│  ├─ tests/               regression and policy tests
│  ├─ config.yaml          Home Assistant add-on metadata and defaults
│  ├─ Dockerfile
│  ├─ requirements.txt
│  └─ run.sh
├─ homeassistant/          optional Home Assistant package material
├─ repository.yaml
└─ README.md
```

## Development principles

Several design choices are intentional and should be preserved when extending the system:

- keep `deterministic_v35` frozen as the reference/fallback policy
- give competing models the same information vintage
- keep model selection separate from physical safety
- validate physical commands against current measured state
- never dispatch a future quarter decision before `decision_start`
- fail toward safe release rather than silently continuing after actuator faults
- make learning and promotion measurable through replay/evaluation rather than replacing the baseline ad hoc

These constraints make it possible to improve forecasting and control models without turning every model change into a new physical safety implementation.
