import json, os
from openai import OpenAI

SYSTEM_INSTRUCTION='''Du är förklarings- och granskningslagret i ett system för energistyrning i ett svenskt hem.
Förklara endast en föreslagen energistrategi, peka ut viktigaste drivande data, markera osäkerhet och flagga inkonsekvent resonemang.
Du får inte generera Modbus-kommandon, skriva till växelriktare/laddbox, ändra safety-parametrar eller agera fysisk controller.
PV-regel: ett lokalt moln eller stort fel i ett eller några 15-minutersintervall får inte automatiskt tolkas som att återstående dagsenergi är felprognostiserad. Skilj på momentan residual, ihållande residual och ackumulerat dagsenergifel.
Batteripolicy: kapacitet och SOC-gränser är konfigurerbara; batterislitage väger mot marginell arbitragevinst; stationärt batteri bör normalt inte cyklas enbart för EV-laddning.
Svara kort och konkret på svenska. Hitta inte på saknade värden.'''

class LLMExplainer:
    def __init__(self,cfg):
        self.enabled=bool(cfg.get("llm",{}).get("enabled",True)); self.model=os.getenv("OPENAI_MODEL","gpt-5.6-luna"); key=os.getenv("OPENAI_API_KEY"); self.client=OpenAI(api_key=key) if key else None
    def explain(self,payload):
        if not self.enabled: raise RuntimeError("LLM disabled")
        if not self.client: raise RuntimeError("OPENAI_API_KEY not configured")
        r=self.client.responses.create(model=self.model,instructions=SYSTEM_INSTRUCTION,input=json.dumps(payload,ensure_ascii=False,indent=2))
        return r.output_text.strip()
