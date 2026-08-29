# Optimization models

This document describes the optimization models used by the Energy AI system. It is intended as architecture and implementation documentation: what each model is trying to do, how it makes decisions, how learning works where applicable, and how the implementation is structured technically.

The documentation reflects the current code in `main`. It does **not** change model versions, configuration versions, release versions, model revisions, or runtime behavior.

## 1. Scope and model landscape

The runtime engine registry currently contains six optimization engines:

| Engine ID | Family | Trainable | Learning | Main idea |
|---|---|---:|---:|---|
| `deterministic_v35` | deterministic | No | No | Frozen dynamic-programming baseline |
| `adaptive_deterministic_v1` | adaptive deterministic | Yes | Yes | v3.5-style deterministic optimization with learned economic/risk parameters |
| `stochastic_deterministic_v1` | stochastic deterministic | No | No | Scenario-based optimization with common first action and CVaR downside risk |
| `neural_v1` | neural | Yes | Yes | MLP classifier that imitates a perfect-information deterministic teacher |
| `gradient_v1` | gradient boosting | Yes | Yes | Histogram gradient-boosting classifier trained on the same teacher data |
| `hybrid_v1` | hybrid | Indirectly | Yes, through neural dependency | Frozen deterministic backbone guided by a bounded neural prior |

In addition, the system contains a **hindsight/oracle evaluation model** (`optimizer_realized_hindsight_v1`). It is not a selectable runtime engine. It is used to answer a different question: what could an optimizer have done with realized load, PV and price information available with hindsight? That makes it a teacher/evaluation reference rather than a deployable control model.

The permanent architectural rule is that `deterministic_v35` is the immutable reference. Challenger models may learn, be retrained, or be replaced, but the v3.5 baseline must remain unchanged so that comparisons retain a stable meaning.

---

# 2. Common optimization architecture

## 2.1 Separation between decision model and physical authority

The optimizer engines do not directly define the final physical inverter command. They produce an `EngineDecision` containing, among other things:

- requested battery action in kW;
- expected SOC after the first interval;
- optional horizon plan rows;
- model identity and diagnostics;
- the `information_vintage_id` on which the decision was based.

Physical feasibility and operational safety remain downstream. This is especially important for learned models: a neural or gradient model is permitted to propose an action, but it does not get unrestricted physical authority merely because it predicted that action.

This separation allows the system to compare very different optimization approaches under the same physical execution layer.

## 2.2 Sign convention

Throughout the optimization engines:

- positive battery action means **discharge**;
- negative battery action means **charge**;
- zero means idle.

The standard planning interval is 15 minutes (`DT_HOURS = 0.25`).

## 2.3 Shared information vintage

Models are designed to compete on the same information vintage. An `EngineInput` contains the state and forecast information available for a particular decision opportunity, including:

- decision timestamp and generation timestamp;
- initial battery SOC;
- load forecast;
- PV forecast;
- load and PV uncertainty;
- spot prices and whether price is known for each interval;
- installation constraints;
- economic parameters;
- tariff state;
- metadata identifying the input vintage.

This is essential for fair model comparison. A model must not appear better merely because it used a later forecast than another model.

## 2.4 Shared physical concepts

The deterministic-family optimizers work with the same basic physical quantities:

- battery capacity;
- hard minimum and maximum SOC;
- preferred SOC range;
- maximum charge and discharge power;
- charge and discharge efficiency;
- physical grid import limit;
- export limit;
- battery degradation cost;
- dynamic reserve policy;
- known-price and unknown-price horizon treatment.

The deterministic engines represent battery energy as a discrete state grid. A transition between two energy states implies a battery action. Transitions exceeding charge/discharge power or grid constraints are rejected.

## 2.5 Dynamic reserve

Reserve is not a single fixed SOC target. The baseline computes a reserve target between normal reserve and high-uncertainty reserve according to forecast uncertainty. The reserve shortfall objective is piecewise:

1. the lowest, critical SOC zone has the highest penalty;
2. the preferred-minimum zone has a lower but still substantial penalty;
3. the remaining distance to the dynamic reserve target has a smaller penalty.

This is deliberately a soft economic policy rather than a replacement for the hard minimum SOC constraint.

## 2.6 Unknown future prices

The physical forecast horizon can be longer than the published price horizon. v3.5 therefore does not invent future prices. When prices become unknown, it restricts speculative actions and uses a continuation-value mechanism to value stored energy at the known/unknown boundary.

The continuation target depends on factors including:

- forecast deficit in the unknown-price region;
- peak-support need relative to the grid import limit;
- forecast uncertainty;
- dynamic reserve;
- a reference price derived from known prices.

This distinction between known prices and continuation value is inherited, directly or conceptually, by several challenger engines.

---

# 3. `deterministic_v35`

## 3.1 Idé med modellen

`deterministic_v35` är systemets stabila referensmodell. Idén är att lösa batteriets laddnings- och urladdningsproblem som ett deterministiskt dynamiskt programmeringsproblem över en diskret SOC/energigrid.

Modellen är avsiktligt konservativ i arkitekturell mening: den använder explicita fysiska och ekonomiska regler som går att inspektera, och den förändras inte genom maskininlärning.

Dess viktigaste roll är dubbel:

1. den är en fungerande optimizer i egen rätt;
2. den är den permanenta benchmark som alla challengers mäts mot.

## 3.2 Översiktlig funktion

För varje planeringskörning:

1. byggs en gemensam horisont av lastprognos, PV-prognos och prisdata;
2. aktuellt SOC översätts till batterienergi;
3. en diskret energigrid byggs från hard min till hard max och inkluderar uppmätt startenergi;
4. för varje 15-minutersintervall prövas möjliga övergångar mellan energitillstånd;
5. varje övergång omvandlas till laddnings-/urladdningseffekt;
6. fysiskt omöjliga övergångar förkastas;
7. resterande övergångar kostnadssätts;
8. dynamisk programmering väljer den billigaste sammanhängande vägen genom tillståndsrymden;
9. den första åtgärden i den valda planen returneras som optimizerbeslut.

## 3.3 Hur lärande fungerar

Det gör det inte. Modellen är uttryckligen icke-träningsbar och fryst.

Det innebär att förändringar i dess beteende endast får komma från externa indata som prognoser, priser, tariffstatus och konfiguration — inte från att v3.5 själv ändrar sina parametrar baserat på historiska resultat.

Detta är en medveten designprincip. Om baseline kontinuerligt lärde sig skulle jämförelser över tid förlora sitt fasta referensvärde.

## 3.4 Detaljerad teknisk beskrivning

Kärnimplementationen finns i `energy_ai/app/optimizer.py`, medan den rena replay-/engine-adaptern finns i `optimizer_v35_replay.py` och `engine_registry.py`.

### Tillståndsrepresentation

Battery SOC representeras som energi i kWh. `_state_grid()` bygger en diskret grid som alltid inkluderar:

- hard minimum;
- uppmätt startenergi;
- hard maximum.

Gridden segmenteras så att inget artificiellt stort hopp skapas vid start-SOC.

### Övergångar

`_transition_action_kw(e0, e1, ec, ed)` beräknar vilken batterikraft som krävs för att gå från energitillstånd `e0` till `e1` på ett 15-minutersintervall, med hänsyn till laddnings- respektive urladdningsverkningsgrad.

En övergång är möjlig endast om den resulterande effekten ligger inom batteriets laddnings- och urladdningsgränser.

### Intervallkostnad

För kända priser omfattar kostnaden i huvudsak:

- inköpt energi inklusive importpåslag;
- intäkt från export efter exportpåslag;
- batteridegradering som funktion av throughput;
- ett arbitrage-hurdle för diskretionär urladdning.

Modellen skiljer på fysisk urladdning som krävs för att hålla nätimporten under fysisk gräns och diskretionär urladdning för ekonomi.

### Pris okänt

När priset är okänt tillåts inte normal spekulativ nätladdning eller batterieexport. I stället används continuation logic vid gränsen mellan känd och okänd prisperiod.

### Reserve policy

`_dynamic_reserve_kwh()` gör reserve-SOC beroende av summan av last- och PV-osäkerhet. `_reserve_policy_penalty_ore()` lägger en styckvis kostnad på att ligga under kritisk, preferred och target reserve.

### Terminalvillkor

När hela horisonten har kända priser försöker modellen avsluta nära start-SOC inom angiven tolerans. Detta reducerar risken att optimizer “tjänar pengar” artificiellt genom att tömma batteriet i slutet av horisonten utan att värdera den förlorade energin.

När continuation används ersätts detta av ett explicit värde på energi vid övergången till okända priser.

### Beräkningsmetod

Detta är klassisk finite-horizon dynamic programming över diskreta energitillstånd. För varje tidssteg sparas lägsta ackumulerade kostnad för varje nåbart energitillstånd samt parent transition. Efter sista tidssteget backtrackas optimal väg.

---

# 4. `adaptive_deterministic_v1`

## 4.1 Idé med modellen

Den adaptiva deterministiska modellen behåller den transparenta och fysiskt explicita strukturen från den deterministiska optimeraren, men låter vissa **policy- och riskparametrar läras från utfallet**.

Grundtanken är att många fel i ekonomisk batteristyrning inte kräver en helt ny beslutsarkitektur. Det kan räcka att lära systemet exempelvis:

- hur konservativt PV-prognosen bör behandlas;
- hur konservativt lastprognosen bör behandlas;
- hur mycket lagrad terminal energi är värd;
- hur stor marginal som bör krävas före diskretionär urladdning;
- hur högt reserve energy ska värderas;
- hur stor hurdle nätladdning bör ha;
- hur starkt cycling ska straffas.

På så sätt kan modellen lära sig platsens ekonomiska verklighet utan att lära bort hårda fysiska begränsningar.

## 4.2 Översiktlig funktion

`adaptive_deterministic_v1` kör en egen dynamisk programmering som ligger nära v3.5 men med inlärda parametrar.

Före optimeringen riskjusteras prognoserna:

- PV reduceras med `pv_forecast_risk × pv_uncertainty`;
- last ökas med `load_forecast_risk × load_uncertainty`.

Därefter löses en DP där objective dessutom innehåller de inlärda värdena för terminal energy, discharge hurdle, reserve value, charge hurdle och cycling penalty.

## 4.3 Hur lärande fungerar

Lärandet ligger i `adaptive_learning.py` och är replay-baserat.

Nuvarande lärbara parameterfamiljer är:

1. `pv_forecast_risk`;
2. `load_forecast_risk`;
3. `terminal_energy_value_ore_kwh`;
4. `discharge_hurdle_ore_kwh`;
5. `reserve_energy_value_ore_kwh`;
6. `charge_hurdle_ore_kwh`;
7. `cycling_penalty_ore_kwh`.

Varje parameter har ett begränsat tillåtet intervall i runtime och ett separat diskret sökgrid för learning.

### Nattlig feedbackcykel

En learning cycle gör i huvudsak följande:

1. startar från nuvarande `candidate`-parametrar;
2. räknar baseline-score på replay;
3. gör isolerade full-grid sweeps för varje parameter;
4. kombinerar de bästa isolerade värdena;
5. gör en lokal coordinate-descent-pass för att fånga gemensamma effekter;
6. beräknar dagens optimum;
7. blandar endast en del av vägen från gammal candidate till dagsoptimum;
8. sparar både `daily_optimum` och den långsammare `candidate`-uppdateringen.

Candidate learning rate är 0,20. Det betyder att ett enskilt dygn inte omedelbart får flytta produktionskandidaten hela vägen till dagens optimum.

### Objective för learning

Learning-sökningen får en extern evaluator. Den är avsedd att använda fixed realized-cost semantics: parametrarna bedöms på historiska/replay-data snarare än genom att optimera mot sin egen interna surrogate score.

### Persistens och audit trail

SQLite-tabeller sparar:

- parameter states;
- learning runs;
- varje trial;
- learning progress.

Det gör det möjligt att se vilka parameterkombinationer som testats och vilken förbättring de gav.

### Viktig nuvarande begränsning

Den aktuella implementationen lär en **global kandidatvektor**. Den tidigare arkitekturidén om en separat state-dependent/dynamic parameter policy är inte implementerad i den här koden ännu. Dokumentationen ska därför inte beskriva en sådan policy som om den redan kördes.

## 4.4 Detaljerad teknisk beskrivning

Kärnan finns i `adaptive_deterministic.py`.

### Riskjustering

`risk_adjust_rows()` skapar konservativa punktprognoser från prognos + uncertainty. Parametrarna är separata för last och PV, vilket är viktigt eftersom felstrukturen inte behöver vara symmetrisk.

### Objective-komponenter

`_interval_result_adaptive()` beräknar bland annat:

- energy cost;
- cycling penalty;
- discharge hurdle;
- charge hurdle.

Reserve target penalty modifieras genom att den inlärda `reserve_energy_value_ore_kwh` matas in som target-zone rate, medan de hårdare kritiska nivåerna fortfarande kommer från den fysiska/policy-konfigurationen.

### Terminal energy

Om delar av horisonten saknar känt pris appliceras `terminal_energy_value_ore_kwh` vid känd-pris-gränsen som ett explicit värde på lagrad energi.

Om hela horisonten har känt pris används i stället terminal SOC constraint ungefär som i baseline.

### Parameter bounds

Lärbara parametrar är hårt boundsatta. Exempelvis tillåts forecast-risk endast inom 0–2 och cycling penalty inom 0–50 öre/kWh. Detta är ett extra skydd mot instabil eller extrem learning.

---

# 5. `stochastic_deterministic_v1`

## 5.1 Idé med modellen

Den stokastiska deterministiska modellen adresserar en annan svaghet än den adaptiva modellen: att en enda punktprognos kan ge ett överkonfident beslut.

I stället för att först korrigera prognosen till en konservativ punktprognos bygger modellen flera explicita scenarier för last och PV. Den väljer sedan en första åtgärd som måste fungera i samtliga scenarier, samtidigt som framtida åtgärder får anpassa sig till respektive scenario.

Detta är en tvåstegsmodell med **nonanticipativity i första beslutet**.

## 5.2 Översiktlig funktion

Nuvarande modell bygger fem scenarier:

- nominal;
- high load / low PV;
- low load / high PV;
- high load / high PV;
- low load / low PV.

Scenarierna skapas med ±1 prognososäkerhet och vikterna summerar till ett förväntningsvärde som bevarar originalprognosens medelvärde.

För varje möjlig första batteriövergång:

1. samma första action tvingas i alla scenarier;
2. varje scenario får sedan lösa sin optimala recourse-plan för resten av horisonten;
3. expected scenario cost beräknas;
4. upper-tail CVaR beräknas;
5. ett downside-risk premium läggs ovanpå expected cost;
6. action med lägst riskjusterad score väljs.

## 5.3 Hur lärande fungerar

Modellen lär inte från historik i nuvarande implementation. Scenario-definition, `CVAR_ALPHA` och `RISK_AVERSION` är fasta algoritmparametrar.

Det gör den till en challenger för explicit prognososäkerhet, inte en learning model.

## 5.4 Detaljerad teknisk beskrivning

Implementation: `stochastic_engine.py`.

### Scenariomodell

Standardparametrar:

- fem scenarier;
- `SCENARIO_SIGMA = 1.0`;
- `CVAR_ALPHA = 0.80`;
- `RISK_AVERSION = 0.25`.

Scenario load/PV klipps vid noll för att undvika negativa fysiska prognoser.

### Two-stage recourse

För varje kandidat till första energitillstånd löses scenario-DP bakifrån för tidssteg 1…N. Första tillståndet/action är gemensamt, men den fortsatta vägen får skilja mellan scenarier.

Det är centralt: modellen får inte “veta” vid beslutstid vilket scenario som kommer realiseras.

### CVaR

`weighted_cvar()` beräknar den viktade kostnaden i den övre kostnadssvansen. Riskpremien definieras som:

`RISK_AVERSION × max(0, CVaR - expected_cost)`

Den slutliga rankingen använder:

`expected_cost + risk_premium`.

### Fallback/collapse

Om forecast uncertainty är praktiskt noll kollapsar modellen till `deterministic_v35`. Då finns ingen information som motiverar scenarioförgreningen.

### Tie-break

Vid lika riskadjusted score föredras action närmare baseline-action och därefter lägre absolut action. Det reducerar onödiga avvikelser från referensmodellen.

---

# 6. `neural_v1`

## 6.1 Idé med modellen

`neural_v1` försöker lära en direkt policy från systemtillstånd och prognoser till första batteriåtgärd.

Den centrala idén är imitation learning: i stället för att lära från den historiskt faktiskt körda batteristyrningen skapas labels av en **perfect-information v3.5 teacher**. Teachern får historiska realiserade load/PV/prices och beräknar vad v3.5 skulle ha valt om den hade vetat utfallet i förväg.

Neuralmodellen tränas alltså för att svara på frågan:

> Givet den information som faktiskt fanns vid beslutstillfället, vilken första action brukar den perfect-information teacher senare visa hade varit lämplig?

## 6.2 Översiktlig funktion

Runtimeflödet är enkelt:

1. `EngineInput` vektoriseras till ett fast feature-schema;
2. en `StandardScaler` normaliserar features;
3. en MLP-classifier väljer en diskret action class;
4. predicted class returneras som requested battery action;
5. klassannolikheter används för confidence diagnostics;
6. expected SOC räknas fram från predicted action, men markeras som pre-safety.

Action classes är heltalskW från -8 till +8 kW.

## 6.3 Hur lärande fungerar

### Teacher sample construction

Training candidates hämtas från sparade information vintages. För varje 15-minuters decision opportunity väljs en canonical vintage: den färskaste input som låg inom det tillåtna live-fönstret.

En kandidat blir träningsbar först när faktisk data finns för **hela den prognoshorisont** som teachern behöver. Detta är chronological maturity.

Därefter ersätts forecast load/PV/price i teacher-run med realiserade värden och `deterministic_v35` körs med perfect information.

Teacher first action rundas till närmaste diskreta action class.

### Feature-schema

Aktuellt schema är `neural_v1_features_v2`.

Det innehåller tre huvudgrupper:

1. globala state/forecast-features;
2. installations-, policy- och tariff-features;
3. aggregerade block över horisonten.

Blockstrukturen är 18 block × 8 intervall = maximalt 36 timmar, där varje block motsvarar 2 timmar.

Blockfeatures inkluderar bland annat:

- mean load;
- mean PV;
- mean net load;
- mean uncertainty;
- mean known price;
- price-known fraction;
- aktiv andel av consumption tariff;
- aktiv andel av production tariff.

Globala features inkluderar bland annat SOC, tid på dygnet, veckodag, price spread, forecast energy och uncertainty.

Systemfeatures gör modellen generaliserbar över installationens fysiska och ekonomiska konfiguration, inklusive batteristorlek, PV-kapacitet, effekttak, verkningsgrader, SOC-policy, reserve penalties och tariffstatus.

### MLP-arkitektur

Nuvarande classifier:

- `StandardScaler`;
- hidden layer 1: 64 neuroner;
- hidden layer 2: 32 neuroner;
- ReLU;
- Adam;
- L2/alpha = 0.001;
- learning rate = 0.001;
- max 500 iterationer;
- deterministic random seed.

### Minimikrav

Träning kräver minst 64 samples och minst två action classes. En kronologisk 80/20-liknande split används, med minst 12 validation samples när datamängden tillåter det.

### Evaluation metrics

Modellmetadata sparar bland annat:

- validation accuracy;
- action MAE i kW;
- direction accuracy: charge / idle / discharge.

### Model revisions

Varje lyckad träning skapar en ny revision (`neural_v1-rXXXX`) och sparar både versionsfil och metadata. Den aktiva model artifact ersätts atomiskt.

### Nuvarande control status

`shadow_ready` betyder att modellen är tränad och kan jämföras. Det betyder inte automatiskt att den är tillåten som fysisk active controller. Metadata sätter uttryckligen `active_eligible = False` tills tillräcklig closed-loop/head-to-head evidence finns.

## 6.4 Detaljerad teknisk beskrivning

Implementation:

- `neural_features.py` — feature construction;
- `neural_training.py` — teacher generation, dataset and MLP training;
- `neural_training_v2.py` — schema-aware generalized input path;
- `neural_engine.py` — inference.

### Canonical decision samples

För att förhindra leakage och dubletter används en information vintage per decision start. Inputs utanför live timing window förkastas.

### Perfect-information label

Teachern använder samma initial SOC som beslutsögonblicket, men ersätter framtida load, PV och price med observed values. Prognososäkerhet sätts till noll i teacher input. Därmed blir labeln en kontrafaktisk optimal action under full framtidsinformation, inte den action som verkligheten råkade utföra.

### Classification i stället för regression

Modellen klassificerar en diskret action class. Det har flera praktiska fördelar i den här arkitekturen:

- stabilare target space;
- tydliga klassannolikheter;
- enkel confidence-bedömning;
- direkt kompatibilitet med Hybridmodellens probability prior.

Nackdelen är kvantisering: teacherns kontinuerligare DP-action rundas till närmaste heltalskW-klass.

---

# 7. `gradient_v1`

## 7.1 Idé med modellen

`gradient_v1` testar om ett tabulärt gradient-boosting-system kan lära samma teacher policy bättre eller robustare än MLP-modellen.

Det är viktigt att modellen inte tränas på ett annat mål. Neural och gradient får i princip samma supervised problem och samma feature-space. Skillnaden ska främst ligga i model class, inte i teacher eller informationsfördel.

## 7.2 Översiktlig funktion

Runtime är nästan identisk med `neural_v1`:

1. samma feature vector byggs;
2. HistGradientBoostingClassifier predikterar en action class;
3. probability/confidence tas ut när möjligt;
4. predicted action skickas vidare till samma downstream safety architecture.

## 7.3 Hur lärande fungerar

Modellen tränas på det delade perfect-information teacher-datasetet för aktuellt feature-schema.

Nuvarande classifier-konfiguration:

- learning rate 0.06;
- 180 boosting iterations;
- max 15 leaf nodes;
- minimum 8 samples per leaf;
- L2 regularization 1.0;
- ingen early stopping;
- fixed random seed.

Samma huvudmetrics används som för neuralmodellen: accuracy, action MAE och direction accuracy.

### Automatisk retraining

Gradientmodellen har explicit cadence logic:

- under 1000 samples: högst daglig retraining;
- från 1000 samples: högst veckovis retraining;
- retraining sker endast om nya samples tillkommit.

Modellen måste fortfarande kvalificeras separat för active use; metadata kräver `robust10_v1` evidence.

## 7.4 Detaljerad teknisk beskrivning

Implementation:

- `gradient_training.py`;
- `gradient_engine.py`;
- shared `neural_features.py`;
- shared current-schema samples via `neural_training_v2.py`.

HistGradientBoosting är särskilt relevant som challenger eftersom feature-space är tabulärt, heterogent och innehåller både kontinuerliga physical/economic features och sammanfattade tidsblocksfeatures. Modellen kan fånga icke-linjära trösklar och interaktioner utan att kräva explicit scaling pipeline.

Jämförelsen neural vs gradient bör därför tolkas som en empirisk fråga om vilken supervised policy approximator som generaliserar bäst — inte som en jämförelse mellan olika objectives.

---

# 8. `hybrid_v1`

## 8.1 Idé med modellen

Hybridmodellen är byggd för att kombinera två egenskaper som var för sig är attraktiva:

- den neurala modellens förmåga att lära mönster från hindsight teacher-data;
- den deterministiska v3.5-modellens explicita fysiska feasibility och ekonomiska ryggrad.

Neuralmodellen får därför inte ersätta den deterministiska lösaren. Den används som ett **bounded prior** som påverkar rankingen av genomförbara första actions.

## 8.2 Översiktlig funktion

Hybridmodellen gör två lösningar av en v3.5-liknande DP:

1. en ren backbone-lösning utan neural prior;
2. en guided lösning där neural probability distribution lägger en extra penalty på first actions som modellen bedömer som mindre sannolika.

Därefter jämförs guided path med backbone i det **deterministiska backbone-objectivet**.

Om guided path kostar mer än tillåten regret guard avvisas neural guidance och modellen faller tillbaka på backbone-action.

## 8.3 Hur lärande fungerar

`hybrid_v1` tränar inte en separat modell artifact. Dess learning kommer indirekt från den aktiva `neural_v1`-modellen.

När neuralmodellen retränas förändras hybridens prior och därmed hybridens effektiva model identity.

Det innebär att hybridens kontrollstruktur är fast, medan den statistiska guidance-komponenten utvecklas med neuralmodellen.

## 8.4 Detaljerad teknisk beskrivning

Implementation: `hybrid_engine.py`.

### Neural prior

Hybrid laddar `neural_v1`, kräver `shadow_ready`, vektoriserar samma `EngineInput` och läser `predict_proba()`.

Confidence normaliseras mot uniform class probability. Den maximala priorstyrkan är begränsad till 6 öre i selection score.

### Penalty-form

För en DP-candidate action hittas närmaste neural action class. Straffet baseras på log-kvoten mellan toppklassens sannolikhet och kandidatklassens sannolikhet.

Neural penalty appliceras **endast vid t=0**. Den neurala modellen får alltså påverka first-action selection, men inte skriva om hela framtida DP-objectivet.

### Backbone regret guard

Efter att guided och unguided lösning beräknats jämförs deras v3.5-backbone objective.

Default maximum allowed regret är 5 öre. Överskrids den väljs backbone-path.

Det här är en viktig säkerhetsegenskap: neural guidance kan hjälpa till att välja mellan nära ekonomiska alternativ, men får inte driva lösningen långt bort från vad den deterministiska ryggraden bedömer som ekonomiskt rimligt.

### Fysiska constraints

Alla candidate transitions i hybrid-DP går fortfarande genom v3.5:s fysiska feasibility, reserve logic, continuation logic och terminal SOC handling. Neuralmodellen kan därför inte göra en i övrigt infeasible transition feasible.

---

# 9. Hindsight/oracle: `optimizer_realized_hindsight_v1`

## 9.1 Idé med modellen

Hindsight/oracle är inte en modell som ska styra batteriet live. Den finns för utvärdering och counterfactual learning.

Den svarar på frågan:

> Med de faktiska last-, PV- och prisutfallen kända, hur bra hade en optimizer kunnat göra givet samma batteri- och nätbegränsningar?

Det ger ett övre jämförelsetak och en teacher/reference för learning.

## 9.2 Översiktlig funktion

Modellen bygger dygnsdata från realiserade 15-minutersvärden, rekonstruerar ekonomin och löser ett hindsight optimization problem med känd framtid.

Den används tillsammans med realized-cost evaluation för att skilja mellan:

- prognosfel;
- policy-/optimizerfel;
- fysisk clamping;
- ren kostnad som inte gick att undvika.

## 9.3 Hur lärande fungerar

Oraclet lär inte själv. Det producerar signaler som andra modeller kan lära av:

- perfect-information teacher actions för supervised neural/gradient learning;
- counterfactual scores för adaptive parameter search;
- regret benchmarks för model comparison.

## 9.4 Detaljerad teknisk beskrivning

Huvudimplementationen finns i `optimizer_evaluation.py`.

Realiserad load/PV/price hämtas för lokala dygn. Execution kan simuleras med samma hard SOC, power och grid constraints som live-systemet. Hindsight-delen använder ett optimeringsproblem över hela den kända perioden och kan därmed beräkna en kontrafaktisk optimal kostnad.

Det är viktigt att inte blanda ihop hindsight-resultat med live-prestanda. Oraclet har informationsfördel och är därför en benchmark/teacher, inte en rättvis live-competitor.

---

# 10. Förhållandet mellan modellerna

Modellerna löser inte exakt samma epistemiska problem:

- **deterministic_v35** frågar: vad är billigast givet dagens punktprognoser och explicita regler?
- **adaptive_deterministic_v1** frågar: vilka risk- och ekonomiparametrar har historiskt gett bättre realized outcome i samma explicita optimeringsstruktur?
- **stochastic_deterministic_v1** frågar: vilken första action är robust när forecast uncertainty representeras som flera möjliga framtider?
- **neural_v1** frågar: vilken action brukar hindsight-teachern föredra i state/forecast-situationer som liknar denna?
- **gradient_v1** ställer samma supervised fråga som neuralmodellen men med en annan funktionapproximerare.
- **hybrid_v1** frågar: kan den neurala erfarenheten förbättra first-action ranking utan att överge den deterministiska feasibility- och regret-ramen?
- **hindsight/oracle** frågar: vad hade varit möjligt med faktisk framtidsinformation?

Det är därför missvisande att rangordna modeller endast efter exempelvis classification accuracy. Den relevanta huvudmetrik som slutligen avgör kontrollvärde är realized closed-loop economic performance under jämförbar information och samma downstream constraints.

---

# 11. Learning- och evaluation-principer

## 11.1 Ingen leakage från framtiden i live input

Teacher och hindsight får använda realiserad framtid eftersom deras syfte är efterhandsutvärdering. Runtime challenger får däremot endast använda information som fanns i den aktuella information vintage.

## 11.2 Teacher quality och policy quality är olika saker

Neural/gradient accuracy mäter hur väl modellerna imiterar teacherns diskreta action. Det bevisar inte i sig bättre realized economics. Teachern är en learning signal, inte definitionen av live-optimum under osäkra prognoser.

## 11.3 Closed-loop jämförelse är slutprovet

En optimizer kan se bra ut i one-step action accuracy men ändå vara sämre över tid, eftersom batteriets state påverkar kommande beslut. Därför behövs multi-day closed-loop head-to-head evaluation mot den frysta baselinen.

## 11.4 Physical safety får inte läras bort

Learning-parametrar ligger medvetet utanför hårda physical constraints. Learned policy ska kunna påverka ekonomisk preferens och riskbedömning, men inte lära att hard SOC, grid import limit eller batteriets power limit kan ignoreras.

---

# 12. Viktiga kodfiler

| Fil | Roll |
|---|---|
| `energy_ai/app/engine_registry.py` | Registry och metadata för optimizer engines |
| `energy_ai/app/engine_contract.py` | Gemensamt input/output-kontrakt |
| `energy_ai/app/optimizer.py` | Frozen deterministic v3.5 planning logic |
| `energy_ai/app/optimizer_v35_replay.py` | Ren v3.5 solver/replay adapter |
| `energy_ai/app/adaptive_deterministic.py` | Adaptive deterministic solver |
| `energy_ai/app/adaptive_learning.py` | Adaptive parameter learning loop |
| `energy_ai/app/adaptive_replay.py` | Replay/evaluation support för adaptive learning |
| `energy_ai/app/stochastic_engine.py` | Scenario/CVaR optimizer |
| `energy_ai/app/neural_features.py` | Shared generalized feature schema |
| `energy_ai/app/neural_training.py` | Perfect-information teacher dataset och MLP training |
| `energy_ai/app/neural_training_v2.py` | Current-schema/generalized training data path |
| `energy_ai/app/neural_engine.py` | Neural inference engine |
| `energy_ai/app/gradient_training.py` | Gradient-boosting training/retraining |
| `energy_ai/app/gradient_engine.py` | Gradient inference engine |
| `energy_ai/app/hybrid_engine.py` | Neural-guided deterministic hybrid |
| `energy_ai/app/optimizer_evaluation.py` | Realized evaluation och hindsight/oracle |
| `energy_ai/app/historical_closed_loop.py` | Historical closed-loop evaluation |
| `energy_ai/app/model_selector*.py` | Jämförelse/qualification/selection mellan engines |

---

# 13. Sammanfattning av arkitekturen

Systemet har utvecklats från en enda deterministisk optimizer till en modellportfölj där olika challengers angriper olika osäkerhets- och learningproblem.

Den centrala säkerhets- och utvärderingsarkitekturen är dock konstant:

1. `deterministic_v35` förblir fryst baseline;
2. alla engines ska kunna jämföras på samma information vintage;
3. learning får inte modifiera hårda physical constraints;
4. hindsight/oracle används för teacher och evaluation, inte som live competitor;
5. learned models måste kvalificeras på realized closed-loop resultat, inte bara offline fit;
6. downstream safety och fysisk authority hålls separerad från model prediction;
7. hybridisering används för att få statistisk guidance utan att ge upp explicit deterministic feasibility och bounded regret.

Det gör det möjligt att förbättra ekonomisk styrning stegvis utan att förlora en stabil referens, reproducerbarhet eller kontroll över varför ett beslut blev möjligt.