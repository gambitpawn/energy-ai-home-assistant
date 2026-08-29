# Optimization models

This document describes the optimization and optimization-adjacent models used by the Energy AI system. It is intended as a technical architecture reference: why each model exists, how it makes decisions, how learning works where applicable, how the models relate to one another, and how the current implementation maps to code.

The document reflects the current implementation on `main`. It is documentation only. It does **not** change engine versions, model revisions, configuration versions, release versions, training state, model-selection state, or runtime behavior.

---

## 1. Model landscape

The runtime engine registry currently contains six selectable optimization engines:

| Engine ID | Family | Trainable | Learns over time | Core idea |
|---|---|---:|---:|---|
| `deterministic_v35` | deterministic | No | No | Frozen dynamic-programming baseline |
| `adaptive_deterministic_v1` | adaptive deterministic | Yes | Yes | Deterministic DP with learned economic and forecast-risk parameters |
| `stochastic_deterministic_v1` | stochastic deterministic | No | No | Scenario optimization with common first action and CVaR downside risk |
| `neural_v1` | neural | Yes | Yes | MLP classifier that imitates a perfect-information deterministic teacher |
| `gradient_v1` | gradient boosting | Yes | Yes | Histogram gradient-boosting policy trained on the same teacher data |
| `hybrid_v1` | hybrid | Indirectly | Yes, through `neural_v1` | Frozen deterministic backbone with bounded neural guidance |

The system also contains a hindsight/oracle model, `optimizer_realized_hindsight_v1`. It is not a deployable runtime engine. Its role is evaluation and teaching: given realized load, PV and price data, it estimates what an optimizer could have done with perfect hindsight under the same physical and policy constraints.

A permanent architectural rule is that `deterministic_v35` remains immutable. Challenger models may learn, be retrained, be requalified, or eventually be replaced, but v3.5 remains the fixed baseline against which improvement and regret are measured.

---

# 2. Common optimization architecture

## 2.1 Decision model versus physical authority

An optimization engine does not directly issue unrestricted inverter commands. Each engine returns an `EngineDecision`, typically containing:

- the requested battery action in kW;
- expected SOC after the first interval;
- optionally a horizon plan;
- engine identity and model metadata;
- diagnostics;
- the `information_vintage_id` on which the decision was based.

The physical execution layer remains downstream. This separation is especially important for learned engines. A neural model is allowed to propose an action, but its proposal does not bypass battery power limits, SOC limits, inverter limits, grid constraints, actuator timing logic, or other downstream safety mechanisms.

Architecturally, the system therefore separates:

1. **state and forecast construction**;
2. **economic optimization / policy decision**;
3. **model selection**;
4. **physical command validation and actuation**.

This permits heterogeneous models to be compared under a common execution contract.

## 2.2 Sign convention and interval length

Battery action is defined as:

- `action_kw > 0`: battery discharge;
- `action_kw < 0`: battery charge;
- `action_kw = 0`: idle.

The normal planning interval is 15 minutes:

```text
Δt = 0.25 h
```

In code this is `DT_HOURS = 0.25`.

## 2.3 Battery energy state

Let:

- `C` = usable battery capacity in kWh;
- `s_t` = SOC in percent at interval boundary `t`;
- `E_t` = battery energy in kWh at interval boundary `t`.

Then:

```text
E_t = C · s_t / 100
```

The hard battery-energy range is:

```text
E_min = C · SOC_hard_min / 100
E_max = C · SOC_hard_max / 100
```

The deterministic-family optimizers discretize the continuous interval `[E_min, E_max]` into a finite state grid. The measured initial energy is explicitly included in the grid so the optimizer does not introduce an artificial first-step quantization jump.

## 2.4 Battery transition equation

Let:

- `a_t` = battery action in kW;
- `η_c` = charge efficiency;
- `η_d` = discharge efficiency;
- `Δt` = interval length in hours.

The state transition is implemented piecewise.

For discharge (`a_t ≥ 0`):

```text
E_{t+1} = E_t - a_t · Δt / η_d
```

For charge (`a_t < 0`):

```text
E_{t+1} = E_t + (-a_t) · η_c · Δt
```

Conversely, the power required for a transition from `E_t` to `E_{t+1}` is:

```text
if E_{t+1} > E_t:
    a_t = - (E_{t+1} - E_t) / (η_c · Δt)

if E_{t+1} < E_t:
    a_t =   (E_t - E_{t+1}) · η_d / Δt

if E_{t+1} = E_t:
    a_t = 0
```

This is implemented by `_transition_action_kw()` in `optimizer.py` and reused by deterministic-family challengers.

## 2.5 Grid power balance

For each interval, let:

- `L_t` = house load forecast in kW;
- `P_t` = PV forecast in kW;
- `a_t` = battery action in kW.

Net site demand before battery action is:

```text
N_t = L_t - P_t
```

Grid power after battery action is:

```text
G_t = N_t - a_t
```

Hence:

```text
Import_t = max(0, G_t)
RawExport_t = max(0, -G_t)
Export_t = min(RawExport_t, ExportLimit)
Curtailment_t = max(0, RawExport_t - ExportLimit)
```

A discharge reduces grid import or increases export. A charge increases grid import unless it absorbs otherwise-exported PV.

## 2.6 Hard feasibility constraints

The deterministic-family solvers reject transitions that violate physical feasibility. Important constraints include:

```text
-C_max ≤ a_t ≤ D_max
```

where `C_max` is maximum charge power and `D_max` maximum discharge power, together with:

```text
E_min ≤ E_t ≤ E_max
Import_t ≤ GridImportLimit
Export_t ≤ GridExportLimit
```

The export limit is handled partly through export clipping, while the import limit is treated as a hard feasibility constraint in the optimizer.

The engine objective is therefore optimized only over physically admissible transitions.

## 2.7 Known-price energy economics

For an interval with known spot price `p_t`, define effective buy and sell prices:

```text
Buy_t  = p_t + ImportOverhead
Sell_t = max(0, p_t - ExportOverhead)
```

The energy cash-flow term is:

```text
EnergyCost_t = Δt · (Import_t · Buy_t - Export_t · Sell_t)
```

Positive values are costs; negative values are net revenue.

Battery cycling/degradation is represented as a throughput cost:

```text
Degradation_t = |a_t| · Δt · DegradationRate
```

The baseline also distinguishes between discharge that is physically required to avoid exceeding the grid import limit and discretionary discharge.

Required discharge is approximately:

```text
RequiredDischarge_t = max(0, N_t - GridImportLimit)
```

For `a_t > 0`, discretionary discharge is:

```text
DiscretionaryDischarge_t = max(0, a_t - RequiredDischarge_t)
```

The baseline arbitrage hurdle is then:

```text
Hurdle_t = DiscretionaryDischarge_t · Δt · MinimumArbitrageMargin
```

This discourages unnecessary battery cycling for very small apparent price spreads.

## 2.8 Dynamic reserve target

The baseline does not use one fixed reserve SOC. It increases reserve as forecast uncertainty increases.

Let:

```text
U_t = LoadUncertainty_t + PVUncertainty_t
```

Let `U_scale` be the uncertainty level at which the reserve reaches its maximum configured value. Then:

```text
q_t = min(1, U_t / U_scale)
```

and reserve SOC is:

```text
ReserveSOC_t = NormalReserveSOC
             + (HighUncertaintyReserveSOC - NormalReserveSOC) · q_t
```

The corresponding reserve energy is:

```text
ReserveEnergy_t = C · ReserveSOC_t / 100
```

The reserve target is a soft policy objective, not a replacement for the hard SOC minimum.

## 2.9 Piecewise reserve penalty

The reserve penalty uses marginal zones. Let:

- `E_hard` = hard-minimum energy;
- `E_critical` = critical reserve energy;
- `E_preferred` = preferred-minimum energy;
- `E_target` = dynamic reserve target;
- `E` = candidate end-of-interval battery energy.

The helper `_zone_shortfall_kwh(E, low, high)` measures how much of a marginal reserve zone is missing. Conceptually:

```text
Missing(E; low, high) =
    0                                      if E ≥ high
    high - max(E, low)                    otherwise
```

The reserve penalty is:

```text
ReservePenalty_t = Δt · [
      λ_critical  · Missing(E; E_hard, E_critical)
    + λ_preferred · Missing(E; E_critical, E_preferred)
    + λ_target    · Missing(E; E_preferred, E_target)
]
```

with:

```text
λ_critical > λ_preferred > λ_target
```

under normal configuration.

This matters because a simple linear penalty below one reserve threshold would incorrectly value the first kWh below hard-operational comfort the same as a small miss below the high-uncertainty target.

## 2.10 Preferred maximum SOC

The system also has a soft preferred maximum SOC below the hard maximum. Energy above that preferred range can incur:

```text
UpperPenalty_t = max(0, E_{t+1} - E_preferred_max)
               · λ_upper
               · Δt
```

This makes the optimizer indifferent to neither excessive high SOC nor low reserve, while preserving hard SOC bounds separately.

## 2.11 Information vintage

All engines are intended to compete using the same information vintage. An `EngineInput` includes the state and forecast information available for a particular decision opportunity:

- `generated_at`;
- `decision_start`;
- initial SOC;
- load forecast;
- PV forecast;
- load uncertainty;
- PV uncertainty;
- price rows and `price_known` flags;
- physical constraints;
- economic objective parameters;
- installation metadata;
- tariff state;
- `information_vintage_id`.

This is a methodological requirement, not merely a software detail. Comparing two engines that saw different forecasts would confound model quality with information quality.

## 2.12 Unknown future prices and continuation value

The physical forecast horizon can extend beyond the published spot-price horizon. The baseline deliberately does not fabricate future prices.

For unknown-price intervals it restricts speculative behavior. For example, normal grid charging, battery export, and discretionary discharge are not allowed when price is unknown. Physical discharge required to satisfy the import limit may still be necessary.

At the boundary between known and unknown prices, the optimizer assigns continuation value to stored energy.

The continuation target uses several components.

Unknown-horizon net deficit:

```text
Deficit = Σ max(0, L_t - P_t) · Δt
```

Peak-support energy requirement:

```text
PeakSupport = Σ max(0, L_t - P_t - GridImportLimit)
              · Δt / η_d
```

A configured fraction `f_cover` of unknown deficit is converted to battery-energy coverage:

```text
CoveredDeficit = Deficit · f_cover / η_d
```

The target is then broadly:

```text
Target = min(
    PreferredMaxEnergy,
    max(Reserve + CoveredDeficit,
        Reserve + PeakSupport)
)
```

The reference value per stored kWh is derived from known effective buy prices, normally their median:

```text
ReferencePrice = median(KnownBuyPrices)
```

A risk premium increases with unknown-horizon deficit and uncertainty. In current code its structure is:

```text
RiskPremium = RiskMax · [
    0.6 · min(1, Deficit / C)
  + 0.4 · min(1, AverageUnknownUncertainty / U_scale)
]
```

The continuation mechanism therefore values ending the known-price region with sufficient stored energy without pretending to know the actual future spot price.

---

# 3. `deterministic_v35`

## 3.1 Model idea

`deterministic_v35` is the permanent reference optimizer. It solves battery scheduling as a finite-horizon deterministic dynamic-programming problem over a discrete battery-energy grid.

The model intentionally uses explicit physical and economic rules rather than learned policy weights. Its role is twofold:

1. provide a stable and inspectable optimizer;
2. provide an immutable benchmark for all challengers.

The baseline must remain behaviorally frozen. Learning systems are evaluated against it rather than modifying it.

## 3.2 High-level operation

For every planning run, v3.5:

1. constructs a common horizon from load, PV and price data;
2. converts measured SOC to battery energy;
3. constructs a discrete energy-state grid;
4. enumerates feasible transitions between energy states for every 15-minute interval;
5. converts each transition into battery power;
6. rejects physically infeasible transitions;
7. calculates the economic/policy cost of each feasible transition;
8. uses dynamic programming to find the minimum-cost path;
9. backtracks the optimal path;
10. returns the first battery action as the current decision.

The horizon plan is useful for diagnostics and evaluation, but in receding-horizon operation only the current action is authoritative before the system replans with new information.

## 3.3 Learning

There is no learning in `deterministic_v35`.

The engine descriptor explicitly marks it as:

```text
trainable = False
learning_enabled = False
baseline = True
```

Its behavior can change because exogenous inputs change — prices, forecasts, uncertainty, tariff state, SOC, configuration — but it does not alter its own parameters based on historical performance.

This immutability is necessary for meaningful longitudinal comparisons. If the baseline learned continuously, measured challenger improvement would no longer be relative to a fixed reference.

## 3.4 Detailed technical description

Core implementation is in:

- `energy_ai/app/optimizer.py`;
- `energy_ai/app/optimizer_v35_replay.py`;
- `energy_ai/app/engine_registry.py`.

### 3.4.1 State grid

The DP state is battery energy in kWh. `_state_grid()` builds a piecewise-uniform grid that always contains the exact measured initial energy.

If the requested maximum grid step is `δE`, the interval from hard minimum to initial energy and the interval from initial energy to hard maximum are segmented separately. If a segment length is `D`, the number of segments is:

```text
n = ceil(D / δE)
```

and the effective step on that segment is:

```text
δE_eff = D / n
```

This avoids a short or long artificial boundary jump around the initial state.

### 3.4.2 Bellman recursion

Let `S` be the discrete energy-state set. Define `J_t(j)` as the lowest accumulated objective cost of reaching state `j` after interval `t`.

For each candidate transition `i → j`:

```text
J_{t+1}(j) = min_i [ J_t(i) + c_t(i, j) ]
```

where `c_t(i,j)` contains interval economics and policy adjustments.

The implementation stores the minimizing parent state and transition diagnostics for every reachable next state. After the terminal state is selected, the full path is reconstructed by reverse traversal through those parents.

### 3.4.3 Transition cost

For a known-price interval, a simplified representation of the baseline transition cost is:

```text
c_t = EnergyCost_t
    + Degradation_t
    + ArbitrageHurdle_t
    + ReservePenalty_t
    + PreferredMaxPenalty_t
    + ContinuationAdjustment_t
```

Not every term is active in every interval.

### 3.4.4 Required versus discretionary discharge

An important design feature is that physical grid-limit protection is not treated as normal arbitrage.

If net demand exceeds the physical grid import limit:

```text
RequiredDischarge_t = max(0, N_t - GridImportLimit)
```

This portion of discharge is not penalized by the arbitrage hurdle. Only discharge beyond that physical requirement receives the discretionary hurdle.

This prevents the optimizer from economically discouraging battery action that is necessary to respect a physical constraint.

### 3.4.5 Unknown-price intervals

When `price_known = False`, the engine does not assign an invented energy price. Instead, it enforces restrictions:

- no speculative grid charging;
- no battery export;
- no discretionary discharge if it is not physically required.

The current interval energy price term is then zero, while the known/unknown boundary continuation adjustment preserves intertemporal value.

### 3.4.6 Terminal condition when all prices are known

If the entire horizon has known prices, the engine tries to terminate close to initial battery energy.

Let `E_0` be initial energy and `τ` the configured SOC tolerance converted to kWh. Candidate terminal states satisfy:

```text
|E_T - E_0| ≤ τ
```

If none exists because of grid discretization, the nearest reachable energy states are used.

A terminal tie-break cost can further prefer states closer to the start energy:

```text
TerminalTieBreak = |E_T - E_0| · λ_terminal
```

This prevents finite-horizon end effects where the optimizer would otherwise gain artificial value by emptying the battery at the end of the modeled horizon.

### 3.4.7 Terminal treatment when future prices are unknown

If the horizon extends into unknown prices, the fixed terminal-SOC return constraint is not used in the same way. Instead the continuation profile assigns economic value and an energy target at the last known-price interval.

This makes the optimization problem closer to:

```text
min Σ_{t=0}^{K} c_t - V(E_K)
```

where `K` is the last known-price interval and `V(E_K)` is an approximation of the value of stored energy entering the unknown-price region.

### 3.4.8 Computational characteristics

If there are `T` time intervals and `N` discrete battery states, naive transition enumeration is `O(TN²)`. Physical power limits prune many transitions, but the core DP remains a discrete-state shortest-path problem in a layered acyclic graph.

This has several advantages for the baseline:

- deterministic output;
- inspectable objective decomposition;
- straightforward constraint enforcement;
- no model-training dependency;
- reproducibility in replay.

The principal cost is computation compared with a direct policy model.

---

# 4. `adaptive_deterministic_v1`

## 4.1 Model idea

The adaptive deterministic engine preserves the transparent deterministic optimizer structure but allows a bounded set of **economic and forecast-risk policy parameters** to be learned from historical replay.

The hypothesis is that a substantial part of optimizer regret can arise not because dynamic programming is the wrong decision framework, but because fixed policy parameters are imperfect for the installation and its forecast-error distribution.

The model therefore learns how aggressively or conservatively to treat:

- PV uncertainty;
- load uncertainty;
- terminal stored energy;
- discretionary discharge;
- reserve energy;
- grid charging;
- battery cycling.

Hard physical parameters are deliberately excluded from learning.

## 4.2 High-level operation

For a live decision:

1. load the current candidate parameter vector;
2. risk-adjust the load and PV point forecasts;
3. run a deterministic DP using those adjusted forecasts;
4. add learned economic terms to the interval objective;
5. preserve hard physical constraints;
6. return the minimum-cost plan and first action.

The model is therefore a **learned-parameter optimizer**, not a black-box learned policy.

## 4.3 Learnable parameter vector

The implemented parameter vector is:

```text
θ = [
    r_pv,
    r_load,
    v_terminal,
    h_discharge,
    v_reserve,
    h_charge,
    c_cycle
]
```

corresponding to:

- `pv_forecast_risk`;
- `load_forecast_risk`;
- `terminal_energy_value_ore_kwh`;
- `discharge_hurdle_ore_kwh`;
- `reserve_energy_value_ore_kwh`;
- `charge_hurdle_ore_kwh`;
- `cycling_penalty_ore_kwh`.

All parameters are bounded before use. Current runtime bounds are:

```text
0   ≤ r_pv       ≤ 2
0   ≤ r_load     ≤ 2
0   ≤ v_terminal ≤ 500 öre/kWh
0   ≤ h_discharge≤ 100 öre/kWh
0   ≤ v_reserve  ≤ 300 öre/kWh
0   ≤ h_charge   ≤ 100 öre/kWh
0   ≤ c_cycle    ≤ 50 öre/kWh
```

These bounds prevent the learning loop from turning policy parameters into hidden physical overrides.

## 4.4 Forecast-risk transformation

The model converts forecast uncertainty into a conservative point forecast.

For PV:

```text
P'_t = max(0, P_t - r_pv · σ_PV,t)
```

For load:

```text
L'_t = max(0, L_t + r_load · σ_Load,t)
```

where:

- `P_t`, `L_t` are raw forecasts;
- `σ_PV,t`, `σ_Load,t` are uncertainty estimates;
- `r_pv`, `r_load` are learned risk multipliers.

The two multipliers are independent because PV overforecast and load underforecast need not have the same economic consequences or empirical distribution.

## 4.5 Adaptive interval objective

The adaptive interval objective retains energy economics but replaces or augments several baseline fixed terms.

Cycling cost:

```text
CycleCost_t = |a_t| · Δt · c_cycle
```

Discretionary-discharge hurdle:

```text
DischargeHurdle_t = DiscretionaryDischarge_t · Δt · h_discharge
```

Grid-charge hurdle:

```text
ChargeHurdle_t = GridCharge_t · Δt · h_charge
```

The resulting interval term is approximately:

```text
AdaptiveIntervalCost_t = EnergyCost_t
                       + CycleCost_t
                       + DischargeHurdle_t
                       + ChargeHurdle_t
```

Reserve and preferred-max penalties are then added in the DP layer.

## 4.6 Learned reserve value

The hard and preferred reserve-zone penalties remain controlled by system policy. The learned `reserve_energy_value_ore_kwh` replaces the marginal penalty for the upper reserve-target zone.

This is a deliberate separation:

- critical reserve protection remains non-learned;
- preferred minimum remains strongly policy-defined;
- only the marginal value of reaching the dynamic reserve target adapts.

The learned parameter therefore influences economic conservatism without weakening hard low-SOC protection.

## 4.7 Learned terminal-energy value

When the horizon contains an unknown-price region, the adaptive engine adds a learned continuation adjustment at the boundary.

For stored energy `E_boundary`:

```text
ContinuationAdjustment = - E_boundary · v_terminal
```

Because the optimizer minimizes cost, a negative term rewards retaining energy. Higher `v_terminal` therefore makes the optimizer more reluctant to deplete the battery before entering the unknown-price region.

When all prices are known, the normal terminal-SOC return constraint is used instead.

## 4.8 Learning process

Learning is implemented in `adaptive_learning.py` and is replay based.

The core learning objective is external realized replay cost:

```text
Score(θ) = RealizedReplayCost produced when optimizer uses θ
```

The optimizer's own internal objective is not treated as proof that a parameter set is better. Candidate parameters are evaluated against realized historical outcomes.

This is important because otherwise learning could become circular: a parameter vector could appear superior merely because it lowers the same surrogate objective it defines.

## 4.9 Isolated parameter sweeps

For each parameter `θ_j`, the learning system evaluates a predefined search grid while holding every other parameter fixed at the current baseline vector.

Formally, for grid `G_j`:

```text
θ_j* = argmin_{x ∈ G_j} Score(
    θ_1, ..., θ_{j-1}, x, θ_{j+1}, ..., θ_n
)
```

The implementation records every trial, score, and improvement.

The configured grids are intentionally discrete and bounded. They provide reproducibility and limit search complexity.

## 4.10 Coordinate descent refinement

The independently best parameter values are not assumed to form the jointly optimal vector, because parameters interact.

After isolated sweeps, the implementation performs local coordinate descent. For each parameter in order, it searches a small local grid around the current value while all previously updated coordinates remain at their latest values.

For coordinate `j` in a pass:

```text
θ_j ← argmin_{x ∈ LocalGrid_j(θ_j)} Score(θ with θ_j = x)
```

The current implementation performs one local coordinate-refinement pass after the full isolated sweep stage.

## 4.11 Slow candidate update

The best parameter vector for one replay day is called the daily optimum. It is **not** copied directly into the persistent candidate.

Let:

- `θ_old` = current persistent candidate;
- `θ_day` = daily optimum;
- `α = 0.20` = candidate learning rate.

The new candidate is:

```text
θ_new = θ_old + α · (θ_day - θ_old)
```

component by component.

Equivalently:

```text
θ_new = (1 - α) θ_old + α θ_day
```

With `α = 0.20`, one unusual day can move the candidate only 20% of the way toward that day's optimum.

This acts as temporal regularization.

## 4.12 Search grids

Current learning grids include, for example:

```text
pv_forecast_risk:
    0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5

load_forecast_risk:
    0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5

terminal_energy_value_ore_kwh:
    50, 100, 125, 150, 175, 225, 300

cycling_penalty_ore_kwh:
    0, 1, 2, 3, 5, 7.5, 10
```

The runtime bounds are wider than the standard search grid, allowing safe representation of values while keeping normal learning search tractable.

## 4.13 Persistence and auditability

Learning state is persisted in SQLite tables for:

- parameter-state history;
- learning runs;
- individual trials;
- progress state.

For each learning run the system can retain:

- replay date;
- baseline parameter vector;
- baseline score;
- daily-optimum vector;
- daily-optimum score;
- candidate vector;
- trial count;
- diagnostics;
- failure/interruption state.

This makes the adaptive engine much more inspectable than an unconstrained online-learning optimizer.

## 4.14 Current implementation boundary

The current implementation learns one **global parameter vector**.

A state-dependent parameter policy — for example, different optimal reserve value or forecast-risk coefficients as a learned function of season, SOC, price spread, or uncertainty regime — is not currently implemented in the runtime code.

That distinction is important: the architecture can evolve toward a dynamic parameter policy, but the present model should be documented as global adaptive parameter learning.

---

# 5. `stochastic_deterministic_v1`

## 5.1 Model idea

The stochastic deterministic engine addresses forecast uncertainty directly rather than converting it into one conservative point forecast.

Its central question is:

> Which first battery action is robust when several plausible future load/PV realizations are considered, while allowing later actions to adapt after uncertainty resolves?

The model therefore combines:

- deterministic physical constraints;
- a small scenario tree;
- nonanticipativity for the current action;
- scenario-specific recourse after the first action;
- expected cost;
- CVaR downside-risk weighting.

It is stochastic in its treatment of forecast uncertainty, but it is not a learned model.

## 5.2 Scenario construction

The engine builds five symmetric scenarios from load and PV uncertainty.

Current scenario set:

| Scenario | Weight | Load shift | PV shift |
|---|---:|---:|---:|
| nominal | 0.40 | 0σ | 0σ |
| high_load_low_pv | 0.15 | +1σ | -1σ |
| low_load_high_pv | 0.15 | -1σ | +1σ |
| high_load_high_pv | 0.15 | +1σ | +1σ |
| low_load_low_pv | 0.15 | -1σ | -1σ |

For scenario `s`:

```text
L_{t,s} = max(0, L_t + z^L_s · σ_Load,t)
P_{t,s} = max(0, P_t + z^P_s · σ_PV,t)
```

The scenario weights and symmetric perturbations are chosen so that weighted expected load and PV remain centered on the original point forecast.

## 5.3 Two-stage structure and nonanticipativity

The decision is treated as a two-stage problem.

The first action must be identical in every scenario:

```text
a_{0,s} = a_0    for all scenarios s
```

This is the nonanticipativity constraint: at the current decision moment the system cannot know which future scenario will occur.

After that first transition, each scenario is allowed its own optimal recourse path:

```text
a_{t,s},  t ≥ 1
```

Thus the model asks whether a first action leaves the system in a good position across multiple plausible futures, without unrealistically forcing one full horizon plan to be identical across all futures.

## 5.4 Candidate-first-action enumeration

The engine enumerates each reachable first battery-energy state `E_1` from the initial state `E_0`.

Each candidate implies one current action:

```text
a_0 = TransitionPower(E_0, E_1)
```

For every candidate first state, the engine solves an independent deterministic recourse problem in every scenario from interval 1 onward.

A first action is discarded if it is infeasible in **any** scenario.

## 5.5 Scenario recourse

For each scenario `s`, conditional on the selected first state, the engine solves:

```text
C_s(a_0) = min_{a_{1:T,s}} Σ_t c_{t,s}
```

subject to the same battery, grid, reserve, preferred-SOC and continuation logic as the deterministic backbone.

This produces one total scenario cost for each common first action.

## 5.6 Expected cost

For scenario weights `w_s`:

```text
E[C | a_0] = Σ_s w_s · C_s(a_0)
```

The weights sum to 1.

Expected cost alone would make the model risk neutral. To account for downside outcomes, the engine also calculates CVaR.

## 5.7 CVaR

The implementation uses upper-tail Conditional Value at Risk with:

```text
α = 0.80
```

For a cost distribution, `CVaR_0.80` is the weighted mean cost in the worst 20% probability tail.

Conceptually:

```text
CVaR_α(C) = E[C | C is in the upper (1-α) tail]
```

Because there are only five weighted scenarios, the code calculates weighted tail mass explicitly by sorting scenarios from highest to lowest cost and filling the tail probability `1 - α`.

## 5.8 Risk-adjusted score

Current risk aversion is:

```text
ρ = 0.25
```

The engine defines risk premium as:

```text
RiskPremium = ρ · max(0, CVaR_α - ExpectedCost)
```

and scores a first action by:

```text
RiskAdjustedScore = ExpectedCost + RiskPremium
```

The selected first action minimizes this score.

Thus the model does not optimize worst-case cost outright. It remains primarily expectation-oriented but penalizes a large bad-outcome tail.

## 5.9 Tie-breaking against the baseline

If two candidate actions have nearly identical stochastic score, selection prefers the action closest to the deterministic v3.5 first action, then the lower-magnitude action.

Conceptually the sorting key is:

```text
(
    RiskAdjustedScore,
    |a_0 - a_v35|,
    |a_0|
)
```

This gives the challenger a conservative bias toward the proven baseline when the stochastic evidence does not materially distinguish candidates.

## 5.10 Collapse to deterministic baseline

If forecast uncertainty is effectively zero:

```text
max_t sqrt(σ_Load,t² + σ_PV,t²) ≤ ε
```

with a very small numerical epsilon, the scenario model collapses to the deterministic v3.5 solution.

This is both computationally efficient and semantically desirable: if all scenarios are identical, the stochastic model should not invent a different answer.

## 5.11 Learning

There is no parameter learning in the current stochastic engine.

Scenario weights, sigma multipliers, CVaR alpha and risk-aversion coefficient are fixed algorithm constants in `stochastic_engine.py`.

Future calibration could make those parameters empirical, but that is not part of the present implementation.

## 5.12 Technical implementation

Primary code:

- `energy_ai/app/stochastic_engine.py`;
- `energy_ai/app/stochastic_runtime.py`.

The engine internally reuses physical and objective helpers from `optimizer.py` and uses `solve_v35_from_rows()` as a nominal baseline reference.

The computational cost is higher than one deterministic DP because, for each candidate first state, the engine solves recourse across multiple scenarios. However, the scenario set is intentionally small and the first-stage action space is the same discrete battery-state grid used by the deterministic optimizer.

---

# 6. `neural_v1`

## 6.1 Model idea

`neural_v1` is a direct learned policy. Rather than explicitly solving a dynamic program during inference, it learns to map the current information state to a discrete battery action.

The training target is not the action actually taken historically. Instead the model is trained through **imitation learning from a perfect-information deterministic teacher**.

The underlying hypothesis is:

> If v3.5 is given the actual future load, PV and prices, its optimal first action is a useful label for what the live policy should learn to approximate from imperfect forecasts.

This turns historical data into supervised policy-learning examples.

## 6.2 Training sample construction

Each candidate training sample starts from a historical `EngineInput` representing what was actually known at one decision opportunity.

The sample is only mature when actual observations exist for the complete forecast horizon needed by the teacher.

For a candidate input:

1. keep the original decision-time state and information-vintage identity;
2. replace forecast load with realized load;
3. replace forecast PV with realized PV;
4. replace prices with realized prices;
5. set forecast uncertainty to zero for the teacher replay;
6. run frozen v3.5 over this perfect-information horizon;
7. extract the teacher's first action;
8. quantize that action to the nearest allowed action class;
9. pair the label with features derived from the **original decision-time information**, not the hindsight values.

The separation in step 9 is essential. Otherwise the student would receive future information that is unavailable in live operation.

## 6.3 Teacher label

The teacher is:

```text
perfect_information_v35_teacher_v1
```

Let `I_t` denote information available at decision time and `Y_{t:T}` realized future load/PV/price data.

The teacher action is:

```text
a*_teacher = argmin_a J_v35(a | actual future Y_{t:T})
```

The neural model is trained to approximate:

```text
π_φ(I_t) ≈ Quantize(a*_teacher)
```

where `φ` denotes neural-network parameters.

## 6.4 Action classes

Current action classes are integer kW values:

```text
{-8, -7, ..., -1, 0, 1, ..., 7, 8}
```

The continuous teacher action is clipped to this range and rounded to the nearest class.

This makes neural learning a multiclass classification problem rather than continuous regression.

Advantages include:

- stable bounded output space;
- probability distribution over actions;
- interpretable confidence;
- direct alignment with physically reasonable action magnitudes.

The disadvantage is action quantization error.

## 6.5 Feature representation

The current feature schema is:

```text
neural_v1_features_v2
```

Features are built in `neural_features.py`.

The vector contains three broad groups.

### Global decision and horizon features

Examples include:

- initial SOC;
- cyclic hour-of-day encoding;
- cyclic day-of-week encoding;
- horizon completeness;
- price-known fraction;
- minimum known price;
- maximum known price;
- known-price spread;
- forecast load energy;
- forecast PV energy;
- forecast net energy;
- mean load uncertainty;
- mean PV uncertainty.

Time is encoded cyclically:

```text
HourSin = sin(2π · hour / 24)
HourCos = cos(2π · hour / 24)

DowSin = sin(2π · weekday / 7)
DowCos = cos(2π · weekday / 7)
```

This avoids treating, for example, hour 23 and hour 0 as numerically far apart.

### Installation, policy and tariff features

The feature vector also contains system characteristics such as:

- battery capacity;
- PV capacity;
- EV maximum power;
- battery charge/discharge limits;
- grid import/export limits;
- efficiency;
- hard/preferred SOC limits;
- reserve settings;
- reserve penalties;
- arbitrage and degradation values;
- continuation parameters;
- demand tariff settings;
- active tariff state and historical demand peaks.

This allows the policy representation to depend on more than just the immediate forecast.

### Horizon blocks

The horizon is aggregated into:

```text
18 blocks × 8 intervals per block
```

Since each interval is 15 minutes, each block covers:

```text
8 × 0.25 h = 2 h
```

and the maximum represented horizon is:

```text
18 × 2 h = 36 h
```

Each block contains aggregated features including:

- mean load;
- mean PV;
- mean net load;
- mean uncertainty;
- mean known price;
- price-known fraction;
- consumption-demand-tariff active fraction;
- production-demand-tariff active fraction.

This compresses the time series into a fixed-width tabular vector suitable for scikit-learn models.

## 6.6 Neural architecture

The model is a scikit-learn pipeline:

```text
StandardScaler
    → MLPClassifier
```

The MLP currently uses:

```text
hidden_layer_sizes = (64, 32)
activation         = ReLU
solver             = Adam
L2 alpha           = 0.001
learning_rate_init = 0.001
max_iter           = 500
random_state       = 3501
```

Conceptually, for normalized input vector `x`, the network computes:

```text
h1 = ReLU(W1 x + b1)
h2 = ReLU(W2 h1 + b2)
z  = W3 h2 + b3
```

The classifier converts final logits into class probabilities. The predicted battery action is the class with highest predicted probability.

## 6.7 Training split

Samples are ordered chronologically by decision time.

Approximately the first 80% are used for training and the remaining portion for validation, subject to a minimum validation size.

This is intentionally not a random split. A chronological split better approximates the real question: can a model trained on earlier observations generalize to later operating periods?

## 6.8 Minimum data requirements

The current training logic requires at least:

```text
64 samples
```

before a shadow model is trained, and requires at least two action classes in the training data.

It also requires validation data and action diversity within the training subset.

These thresholds are minimum engineering guards, not evidence that 64 samples are statistically sufficient for production control.

## 6.9 Validation metrics

The model records several metrics.

Classification accuracy:

```text
Accuracy = correct action classes / validation samples
```

Action MAE:

```text
MAE = (1/n) Σ |a_pred - a_teacher|
```

Direction accuracy maps each action to:

```text
charge    if a < -0.5
idle      if -0.5 ≤ a ≤ 0.5
discharge if a > 0.5
```

and calculates the fraction of validation examples for which predicted direction matches teacher direction.

Direction accuracy is useful because a 1 kW magnitude error in the correct direction can be less serious than predicting charge when the teacher says discharge.

## 6.10 Inference

At runtime:

1. `EngineInput` is vectorized;
2. the feature vector is passed through the trained pipeline;
3. the predicted action class becomes `requested_action_kw`;
4. if available, `predict_proba()` provides confidence and top action probabilities;
5. expected next SOC is estimated from the requested action using the battery transition equation.

The expected SOC reported by the neural engine is explicitly marked as **pre-safety** because downstream physical logic may clamp or alter the requested action.

## 6.11 Learning and retraining

Learning occurs by accumulating new mature teacher samples and retraining the model.

The model itself is not updated online after every action. Instead, dataset growth and model revisions are separated:

```text
historical information vintages
    → maturity check
    → perfect-information teacher labels
    → training dataset
    → model retraining
    → new model revision
```

Each trained model receives a revision identifier such as:

```text
neural_v1-r0001
neural_v1-r0002
...
```

Versioned model artifacts and metadata are stored so model history remains auditable.

## 6.12 Qualification status

A successfully trained model may be `shadow_ready`, but the metadata explicitly keeps:

```text
active_eligible = False
```

until sufficient head-to-head evidence exists.

Training success is therefore not treated as production qualification.

---

# 7. `gradient_v1`

## 7.1 Model idea

`gradient_v1` is a second direct learned policy, designed to test whether a tree-based tabular learner is better suited to the feature representation than a neural network.

It deliberately uses the **same teacher samples, same labels and same feature schema** as `neural_v1`.

This makes the comparison methodologically clean:

```text
same information
same teacher
same action classes
same target
same validation chronology

only model class differs
```

The model therefore tests model inductive bias rather than changing the learning problem.

## 7.2 Learning data

`gradient_v1` loads the same `neural_training_sample` dataset for the current feature schema.

Its label source remains:

```text
perfect_information_v35_teacher_v1
```

The model is therefore another imitation-learning challenger.

## 7.3 Model architecture

The implementation uses:

```text
HistGradientBoostingClassifier
```

with current hyperparameters:

```text
learning_rate      = 0.06
max_iter           = 180
max_leaf_nodes     = 15
min_samples_leaf   = 8
l2_regularization  = 1.0
early_stopping     = False
random_state       = 3517
```

Gradient boosting builds an additive ensemble:

```text
F_M(x) = F_0(x) + Σ_{m=1}^{M} ν · f_m(x)
```

where:

- `f_m(x)` is the tree added at boosting iteration `m`;
- `ν` is the learning rate;
- the ensemble is optimized iteratively to reduce classification loss.

For multiclass classification, the implementation learns class-score functions that are converted into class probabilities.

## 7.4 Why gradient boosting is a meaningful challenger

The feature representation is mostly tabular and aggregated rather than raw sequential data. Tree ensembles often perform well when:

- nonlinear threshold interactions matter;
- features have different natural scales;
- useful relationships are piecewise rather than globally smooth;
- the dataset is modest in size.

Examples of potentially tree-friendly relationships include:

```text
if SOC < threshold and price spread high and uncertainty low → discharge
if tariff active and historical demand peak near threshold → avoid import
if PV surplus forecast high and SOC moderate → preserve charging headroom
```

An MLP can learn similar relationships, but the sample-efficiency and generalization characteristics can differ materially.

## 7.5 Training and validation

The same chronological train/validation logic and metrics are used as for `neural_v1`:

- classification accuracy;
- action MAE;
- direction accuracy.

The minimum shadow-training threshold is also 64 samples.

## 7.6 Inference

Inference mirrors the neural engine:

```text
EngineInput
    → common feature vector
    → gradient classifier
    → action class
    → requested_action_kw
```

When available, predicted class probabilities are returned for diagnostics.

Expected SOC is computed from the requested action using the same charge/discharge efficiency equations as the neural engine and is marked pre-safety.

## 7.7 Automatic retraining cadence

`gradient_v1` has an explicit automatic retraining policy.

For datasets below 1,000 samples:

```text
cadence = daily
```

For datasets at or above 1,000 samples:

```text
cadence = weekly
```

Retraining is due only when:

- minimum sample count has been reached;
- a model does not yet exist, or the cadence interval has elapsed;
- new samples exist since the active model was trained.

This avoids retraining a model when the underlying dataset has not changed.

## 7.8 Qualification

Like `neural_v1`, a trained gradient model is not automatically eligible for active control.

Its metadata states that robust head-to-head qualification is required before production eligibility.

---

# 8. `hybrid_v1`

## 8.1 Model idea

The hybrid engine combines learned pattern recognition with deterministic constrained optimization.

The neural model does **not** directly choose the final action. Instead it supplies a probabilistic prior over first-action classes. Frozen v3.5 physical and economic logic then solves the optimization problem while applying a bounded penalty to first actions that the neural model considers unlikely.

The core principle is:

> Use the neural model to break or reshape economically close deterministic choices, but do not allow it to override physical feasibility or impose material deterministic regret.

This is intentionally asymmetric: deterministic optimization remains the backbone; learning is advisory.

## 8.2 Neural action prior

The hybrid loads the active `neural_v1` model and obtains class probabilities:

```text
p(a_k | x)
```

for each neural action class `a_k`.

Let:

```text
p_max = max_k p(a_k | x)
```

and let `K` be the number of action classes represented by the model.

A uniform classifier would have probability:

```text
p_uniform = 1 / K
```

The model normalizes top-class confidence as:

```text
ConfidenceNorm = clamp(
    (p_max - p_uniform) / (1 - p_uniform),
    0,
    1
)
```

This means:

- a nearly uniform distribution gives strength near zero;
- a highly concentrated distribution gives strength near one.

## 8.3 Bounded neural prior strength

Maximum neural prior strength is currently:

```text
6 öre
```

The actual strength is:

```text
PriorStrength = 6 · ConfidenceNorm
```

The small absolute scale is intentional. Neural guidance is designed to influence close first-action choices, not dominate the full deterministic horizon objective.

## 8.4 First-action prior penalty

For a deterministic candidate action `a`, the hybrid maps it to the nearest neural action class and looks up probability `p(a)`.

The prior penalty is:

```text
PriorPenalty(a) = PriorStrength · max(
    0,
    ln(p_max / p(a))
)
```

with a small probability floor for numerical safety.

Consequences:

- the top neural class gets zero prior penalty;
- moderately less likely classes get small penalties;
- very unlikely classes get larger penalties;
- penalty magnitude remains capped indirectly by bounded prior strength and the regret guard described below.

The prior penalty is applied **only at `t = 0`**.

Future deterministic transitions are not directly neural-guided.

## 8.5 Guided versus unguided solve

For every decision, the hybrid solves two optimization problems.

### Backbone solve

Frozen v3.5-equivalent DP without neural prior:

```text
J_backbone = min_path J_v35(path)
```

### Guided solve

The same DP with first-step neural prior penalty:

```text
J_guided_score = min_path [
    J_v35(path) + PriorPenalty(a_0)
]
```

Crucially, the engine also keeps the guided path's objective under the **original deterministic objective**, excluding the neural penalty:

```text
J_guided_backbone
```

This allows economic regret to be measured on the common v3.5 scale.

## 8.6 Deterministic regret guard

Define hybrid regret:

```text
Regret = J_guided_backbone - J_backbone
```

The current allowed maximum is:

```text
MaxRegret = 5 öre
```

The neural-guided solution is accepted only if:

```text
Regret ≤ MaxRegret
```

Otherwise the hybrid discards the guided path and returns the pure deterministic backbone action.

This is one of the most important safety/economic design properties of the hybrid model. Neural guidance can change a decision only when the deterministic optimizer judges the economic sacrifice to be very small.

## 8.7 Physical constraints

The hybrid's internal DP mirrors frozen v3.5 physical logic:

- battery energy grid;
- charge/discharge limits;
- efficiencies;
- grid import limit;
- export handling;
- reserve penalties;
- preferred maximum SOC;
- unknown-price continuation;
- terminal SOC handling.

The neural model therefore never creates a physically infeasible transition inside the hybrid optimizer.

Downstream execution safety still applies as an additional layer.

## 8.8 Learning

`hybrid_v1` has no independently trained model.

Its learned component is the active `neural_v1` model. Consequently, the hybrid changes when the neural model is retrained, while the deterministic backbone and hybrid algorithm constants remain fixed.

Hybrid model identity includes a hash derived from:

- hybrid algorithm ID;
- engine version;
- feature schema;
- neural model identity;
- maximum prior strength;
- maximum deterministic regret.

Thus a new neural model revision produces a distinguishable hybrid model identity even without changing the hybrid engine version.

## 8.9 Why this architecture matters

Direct learned policy and deterministic optimization have complementary strengths.

The deterministic optimizer has:

- strong constraint handling;
- explicit economics;
- reproducibility;
- explainability.

The learned model can capture:

- recurring nonlinear patterns;
- interactions not well represented by fixed penalties;
- empirical similarities to hindsight-optimal behavior.

The hybrid is designed so that learned information enters where it is most defensible: preference among already feasible and economically similar first actions.

---

# 9. Hindsight/oracle: `optimizer_realized_hindsight_v1`

## 9.1 Role

The hindsight/oracle model is not a live optimizer. It is an evaluation reference built from realized data.

Its purpose is to answer questions such as:

- What was the minimum feasible cost for this day if actual load, PV and prices had been known in advance?
- How much regret did the live optimizer incur relative to that reference?
- Were losses caused by poor forecasting, poor optimization, or unavoidable physical constraints?
- What action labels should supervised policy models imitate?

It therefore plays two roles:

1. **performance upper-bound / regret reference**;
2. **teacher infrastructure**.

## 9.2 Realized input data

The evaluator reconstructs 15-minute realized intervals from persisted state and price data.

Each usable interval includes, where available:

- realized house load;
- realized PV production;
- realized price;
- battery SOC observations;
- completeness metadata.

Coverage is explicitly measured against the expected number of 15-minute intervals for the local calendar day, including DST effects.

## 9.3 Replaying historical decisions

Historical optimizer plans are mapped to decision intervals using timing constraints.

A decision is considered eligible only if its generation timestamp lies within the configured live decision window around the relevant quarter-hour. Among eligible candidates for an interval, the freshest decision is used.

This prevents hindsight evaluation from crediting the live optimizer with a forecast or plan that was generated too late to have been usable at the actual decision point.

## 9.4 Realized execution simulation

When replaying a requested action, the evaluator clamps it to physical feasibility using battery SOC, battery power and grid constraints.

For requested discharge:

```text
AppliedDischarge ≤ min(
    RequestedDischarge,
    D_max,
    SOCAvailableDischarge,
    ExportFeasibleDischarge
)
```

For requested charge:

```text
AppliedCharge ≤ min(
    RequestedCharge,
    C_max,
    SOCChargeHeadroom,
    GridImportHeadroom
)
```

The resulting SOC follows the same efficiency-aware battery transition equations used elsewhere.

## 9.5 Realized cost

For an applied action, the evaluator computes:

- realized import/export cash cost;
- degradation cost;
- arbitrage hurdle;
- reserve penalty;
- preferred-max penalty;
- throughput;
- clamp status;
- grid-limit exceedance diagnostics.

This makes realized replay comparable to the policy objective used by the optimizers.

## 9.6 Oracle optimization

The hindsight optimizer uses realized future rows rather than forecasts and solves a constrained optimization problem over the day.

Unlike the discrete DP baseline, parts of the hindsight implementation use mathematical programming infrastructure from tariff scenarios, including continuous variables and integer variables where required by mutually exclusive operating modes.

Conceptually, the oracle solves:

```text
min Σ_t [
      RealizedEnergyCost_t
    + Degradation_t
    + PolicyPenalty_t
]
```

subject to:

```text
battery energy balance
hard SOC bounds
charge/discharge power limits
grid import/export limits
operational exclusivity constraints
terminal-energy requirements
```

Because it sees realized future information, it is not a fair live competitor. It is an upper-bound benchmark.

## 9.7 Regret interpretation

For comparable objective semantics:

```text
Regret = Cost_live_policy - Cost_hindsight_oracle
```

A positive regret does not automatically imply the live optimizer is defective. Regret can arise from:

- forecast errors;
- unavailable future prices;
- model-policy error;
- action quantization;
- actuator constraints;
- timing;
- data gaps;
- physical state uncertainty.

The broader evaluation stack should therefore decompose regret rather than treat the oracle gap as one homogeneous model error.

---

# 10. Relationship between the models

The engines can be understood as different answers to the same control problem.

## 10.1 Deterministic baseline

```text
Forecasts + prices + constraints
        ↓
explicit deterministic DP
        ↓
optimal action under fixed policy parameters
```

## 10.2 Adaptive deterministic

```text
Forecasts + uncertainty
        ↓
learned conservative transformation
        ↓
explicit deterministic DP
        ↓
learned economic/risk parameters
```

It learns **how to parameterize** an optimizer.

## 10.3 Stochastic deterministic

```text
Forecasts + uncertainty
        ↓
multiple scenarios
        ↓
common first action + scenario recourse
        ↓
expected cost + CVaR risk
```

It models uncertainty **inside the optimization problem**.

## 10.4 Neural

```text
Decision-time information
        ↓
feature vector
        ↓
MLP classifier trained on hindsight teacher
        ↓
direct action class
```

It learns **the policy mapping itself**.

## 10.5 Gradient boosting

```text
Same decision-time information
        ↓
same feature vector
        ↓
gradient-boosted classifier
        ↓
direct action class
```

It tests a different supervised-learning model class on the same policy-learning problem.

## 10.6 Hybrid

```text
Decision-time information
        ↓
neural probability prior ──────────────┐
                                      ↓
Forecasts + prices + constraints → frozen v3.5 DP
                                      ↓
                         bounded guided first action
                                      ↓
                         deterministic regret guard
```

It learns **preference information** but keeps deterministic optimization as final arbiter.

## 10.7 Hindsight/oracle

```text
Realized future load + PV + prices
        ↓
perfect-information constrained optimization
        ↓
reference optimum / teacher signal
```

It is not deployable because it uses information unavailable at decision time.

---

# 11. Learning taxonomy

The word "learning" means different things across the engines and should not be used generically.

| Engine | What is learned? | Learning signal | Update form |
|---|---|---|---|
| `deterministic_v35` | Nothing | None | Frozen |
| `adaptive_deterministic_v1` | Seven policy/risk parameters | Realized replay score | Grid sweep + coordinate descent + slow blend |
| `stochastic_deterministic_v1` | Nothing currently | None | Fixed scenario model |
| `neural_v1` | Neural classifier weights | Perfect-information v3.5 labels | Supervised retraining |
| `gradient_v1` | Boosted-tree ensemble | Same teacher labels | Supervised retraining |
| `hybrid_v1` | No independent parameters | Inherits neural model | Changes when neural model changes |
| hindsight/oracle | Nothing | Uses realized future directly | Evaluation model |

This distinction is important when interpreting model behavior. For example, the adaptive model may change because its economic coefficients drift, while the neural model may change because a completely new classifier revision has been trained.

---

# 12. Model evaluation principles

## 12.1 Same information vintage

A fair live comparison requires challengers to receive the same information available at the same decision time.

## 12.2 Separate forecast error from optimizer error

A poor live outcome may result from a bad forecast even if the optimizer solved its stated problem correctly. Hindsight replay should therefore be used to distinguish:

```text
forecast regret
optimization/policy regret
execution regret
```

rather than assigning the full oracle gap to the decision engine.

## 12.3 Use realized economics, not internal objective alone

A challenger should not be judged only by the objective value it assigns to its own plan. The primary evidence should be realized or realistically replayed cost under a common objective definition.

## 12.4 Compare physical behavior as well as cost

Relevant metrics include:

- realized cost;
- regret versus v3.5;
- regret versus hindsight;
- battery throughput;
- number of charge/discharge reversals;
- low-SOC exposure;
- reserve shortfall;
- high-SOC exposure;
- import-limit pressure;
- export and curtailment;
- requested-versus-applied action differences;
- action stability between replans.

An optimizer that saves a small amount of money by materially increasing cycling or operational risk may not be preferable.

## 12.5 Training metrics are not control metrics

For direct learned policies, validation classification accuracy is diagnostic but not the final optimization criterion.

Two models with the same classification accuracy can produce different economic outcomes because errors have unequal cost. Confusing `+7 kW` with `+8 kW` is usually less consequential than confusing `-8 kW` with `+8 kW`.

Therefore policy qualification should ultimately rely on closed-loop or high-fidelity replay economics rather than classification accuracy alone.

---

# 13. Main implementation files

| Area | Main files |
|---|---|
| Frozen baseline optimizer | `energy_ai/app/optimizer.py`, `optimizer_v35_replay.py` |
| Engine contract/registry | `engine_contract.py`, `engine_input_v2.py`, `engine_registry.py` |
| Adaptive optimizer | `adaptive_deterministic.py` |
| Adaptive learning | `adaptive_learning.py`, `adaptive_replay.py`, `adaptive_auto.py` |
| Stochastic optimizer | `stochastic_engine.py`, `stochastic_runtime.py` |
| Neural policy | `neural_engine.py` |
| Neural features | `neural_features.py` |
| Neural teacher/training | `neural_training.py`, `neural_training_v2.py`, `neural_teacher_v2.py`, `neural_auto.py` |
| Gradient policy | `gradient_engine.py` |
| Gradient training | `gradient_training.py`, `gradient_runtime.py`, `gradient_qualification.py` |
| Hybrid optimizer | `hybrid_engine.py`, `hybrid_runtime.py` |
| Hindsight/oracle evaluation | `optimizer_evaluation.py` |
| Head-to-head / model selection | `model_selector.py`, `model_selector_robust.py`, `engine_operator_selection.py` |
| Historical replay | `historical_closed_loop.py`, `historical_closed_loop_v2.py`, `monthly_replay.py` |
| Regret analysis | `regret_decomposition.py` |

---

# 14. Design invariants

The following principles are intentional system-level invariants.

### 14.1 v3.5 remains frozen

`deterministic_v35` is the permanent baseline and must not be silently altered to improve its performance.

### 14.2 Learned models do not own physical safety

Model output is a requested action. Physical authority remains downstream.

### 14.3 Teacher information must not leak into student features

Perfect-information future values may define training labels, but decision-time student features must contain only information that would actually have been available live.

### 14.4 Model revisions must be auditable

Retrained learned models are stored with revision metadata rather than silently overwriting model identity.

### 14.5 Learning and qualification are separate

A model being trainable, trained, or `shadow_ready` does not make it production eligible.

### 14.6 Improvement must be measured on a common realized objective

A challenger cannot establish superiority merely by scoring well under an objective function unique to itself.

### 14.7 The hybrid may guide, not dominate

Neural guidance in `hybrid_v1` is explicitly bounded by both prior strength and deterministic regret.

---

# 15. Summary

The optimization stack intentionally contains several model families rather than assuming one algorithm will dominate in all operating regimes.

`deterministic_v35` provides the immutable constrained baseline. `adaptive_deterministic_v1` tests whether the deterministic framework can improve by learning a small number of economically meaningful policy parameters. `stochastic_deterministic_v1` handles uncertainty explicitly with scenarios and downside-risk weighting. `neural_v1` and `gradient_v1` learn direct action policies from perfect-information teacher labels. `hybrid_v1` combines a learned action prior with the frozen deterministic optimizer under a strict regret guard. The hindsight/oracle layer provides the perfect-information reference required for training and rigorous regret analysis.

The resulting architecture deliberately separates four questions:

1. **What is physically feasible?**
2. **What is economically optimal under the information currently available?**
3. **What can historical realized outcomes teach us about better decisions?**
4. **When is a learned challenger sufficiently proven to receive real control authority?**

Keeping those questions separate is central to the design of the Energy AI optimizer stack.