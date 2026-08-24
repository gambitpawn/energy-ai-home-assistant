import json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

DB_PATH=Path(os.getenv("ENERGY_AI_DB","/data/energy_ai.db"))
STOCKHOLM=ZoneInfo("Europe/Stockholm")
CORE_KEYS=("pv_power_kw","house_load_kw","grid_power_kw","battery_power_kw","battery_soc_pct")
LEGACY_MARKERS=("sensor.energy_pv_power","sensor.energy_house_load","sensor.energy_grid_power","sensor.energy_battery_power","sensor.energy_battery_soc","sensor.energy_spot_price")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS raw_state(id INTEGER PRIMARY KEY AUTOINCREMENT,collected_at TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state_15m(bucket_start TEXT PRIMARY KEY,collected_at TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS price_15m(area TEXT NOT NULL,start_utc TEXT NOT NULL,end_utc TEXT NOT NULL,price_ore_kwh REAL NOT NULL,source_currency TEXT NOT NULL,source_price_per_mwh REAL NOT NULL,fetched_at TEXT NOT NULL,PRIMARY KEY(area,start_utc));
        CREATE TABLE IF NOT EXISTS pv_forecast_15m(generated_at TEXT NOT NULL,start_utc TEXT NOT NULL,forecast_kw REAL NOT NULL,uncertainty_kw REAL NOT NULL,irradiance_w_m2 REAL NOT NULL,cloud_cover_pct REAL,temperature_c REAL,model TEXT NOT NULL,radiation_feature TEXT NOT NULL,payload_json TEXT,PRIMARY KEY(generated_at,start_utc));
        CREATE TABLE IF NOT EXISTS pv_remaining_day_forecast(generated_at TEXT PRIMARY KEY,local_date TEXT NOT NULL,remaining_energy_kwh REAL NOT NULL,p80_kwh REAL,p95_kwh REAL,model TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pv_forecast_eval(start_utc TEXT NOT NULL,horizon_label TEXT NOT NULL,generated_at TEXT NOT NULL,model TEXT NOT NULL,forecast_kw REAL NOT NULL,actual_kw REAL NOT NULL,error_kw REAL NOT NULL,abs_error_kw REAL NOT NULL,lead_minutes REAL NOT NULL,payload_json TEXT,created_at TEXT NOT NULL,PRIMARY KEY(start_utc,horizon_label));
        CREATE TABLE IF NOT EXISTS pv_day_eval(generated_at TEXT PRIMARY KEY,local_date TEXT NOT NULL,model TEXT NOT NULL,forecast_remaining_kwh REAL NOT NULL,actual_remaining_kwh REAL NOT NULL,error_kwh REAL NOT NULL,abs_error_kwh REAL NOT NULL,p80_kwh REAL,p95_kwh REAL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,event_type TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS llm_explanations(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,model TEXT NOT NULL,request_json TEXT NOT NULL,explanation TEXT NOT NULL);
        ''')
        cols={r[1] for r in c.execute("PRAGMA table_info(pv_forecast_15m)").fetchall()}
        if "payload_json" not in cols: c.execute("ALTER TABLE pv_forecast_15m ADD COLUMN payload_json TEXT")
        for marker in LEGACY_MARKERS: c.execute("DELETE FROM state_15m WHERE payload_json LIKE ?",(f"%{marker}%",))


def insert_raw(ts,payload):
    with sqlite3.connect(DB_PATH) as c: c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)",(ts,json.dumps(payload,ensure_ascii=False)))


def upsert_15m(bucket,ts,payload):
    with sqlite3.connect(DB_PATH) as c: c.execute('''INSERT INTO state_15m(bucket_start,collected_at,payload_json) VALUES (?,?,?) ON CONFLICT(bucket_start) DO UPDATE SET collected_at=excluded.collected_at,payload_json=excluded.payload_json''',(bucket,ts,json.dumps(payload,ensure_ascii=False)))


def _numeric_state(payload,key):
    try:
        item=payload.get(key) or {}
        if not item.get("available"): return None
        value=item.get("state"); return float(value) if value is not None else None
    except (TypeError,ValueError,AttributeError): return None


def _component_numeric(payload, component_id):
    try:
        item=(payload.get("load_components") or {}).get(component_id) or {}
        if not item.get("available"): return None
        return float(item.get("state"))
    except (TypeError,ValueError,AttributeError): return None


def _usable_core_sample(payload): return any(_numeric_state(payload,key) is not None for key in CORE_KEYS)


def rebuild_15m_bucket(bucket_start,bucket_end,expected_samples=None):
    with sqlite3.connect(DB_PATH) as c: rows=c.execute("SELECT collected_at,payload_json FROM raw_state WHERE collected_at>=? AND collected_at<? ORDER BY collected_at ASC",(bucket_start,bucket_end)).fetchall()
    if not rows: return None
    parsed=[(ts,json.loads(pj)) for ts,pj in rows]; usable=[(ts,p) for ts,p in parsed if _usable_core_sample(p)]
    if not usable: return None
    avg_keys=["pv_power_kw","house_load_kw","grid_power_kw","battery_power_kw","spot_price_ore_kwh","ev_power_kw"]
    means={}; mins={}; maxs={}; counts={}
    for key in avg_keys:
        values=[v for _,p in usable if (v:=_numeric_state(p,key)) is not None]
        means[key]=mean(values) if values else None; mins[key]=min(values) if values else None; maxs[key]=max(values) if values else None; counts[key]=len(values)
    component_ids=sorted({cid for _,p in usable for cid in (p.get("load_components") or {}).keys()})
    component_means={}; component_counts={}
    for cid in component_ids:
        vals=[v for _,p in usable if (v:=_component_numeric(p,cid)) is not None]
        component_means[cid]=mean(vals) if vals else None; component_counts[cid]=len(vals)
    soc=[v for _,p in usable if (v:=_numeric_state(p,"battery_soc_pct")) is not None]; last_ts,last_payload=usable[-1]
    payload={"schema_version":4,"bucket_start":bucket_start,"bucket_end":bucket_end,"samples":len(usable),"samples_raw":len(parsed),"expected_samples":expected_samples,"completeness":min(1.0,len(usable)/float(expected_samples)) if expected_samples else None,"value_counts":counts,"mean":means,"min":mins,"max":maxs,"component_mean_kw":component_means,"component_value_counts":component_counts,"battery_soc_end_pct":soc[-1] if soc else None,"battery_soc_start_pct":soc[0] if soc else None,"last_sample_at":last_ts,"last_state":last_payload}
    upsert_15m(bucket_start,last_ts,payload); return payload


def rebuild_recent_15m(poll_seconds,lookback_hours=48):
    expected=max(1,round(900/int(poll_seconds))); cutoff=(datetime.now(timezone.utc)-timedelta(hours=lookback_hours)).isoformat()
    with sqlite3.connect(DB_PATH) as c: timestamps=[r[0] for r in c.execute("SELECT collected_at FROM raw_state WHERE collected_at>=? ORDER BY collected_at ASC",(cutoff,)).fetchall()]
    buckets=set()
    for ts in timestamps:
        d=datetime.fromisoformat(ts.replace("Z","+00:00")).astimezone(timezone.utc); buckets.add(d.replace(minute=(d.minute//15)*15,second=0,microsecond=0))
    rebuilt=0
    for start in sorted(buckets):
        if rebuild_15m_bucket(start.isoformat(),(start+timedelta(minutes=15)).isoformat(),expected) is not None: rebuilt+=1
    return rebuilt


def upsert_prices(area,rows,fetched_at):
    with sqlite3.connect(DB_PATH) as c: c.executemany('''INSERT INTO price_15m(area,start_utc,end_utc,price_ore_kwh,source_currency,source_price_per_mwh,fetched_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(area,start_utc) DO UPDATE SET end_utc=excluded.end_utc,price_ore_kwh=excluded.price_ore_kwh,source_currency=excluded.source_currency,source_price_per_mwh=excluded.source_price_per_mwh,fetched_at=excluded.fetched_at''',[(area,r['start'],r['end'],r['price_ore_kwh'],r['currency'],r['source_price_per_mwh'],fetched_at) for r in rows])


def get_prices(area,limit=192):
    limit=max(1,min(int(limit),500))
    with sqlite3.connect(DB_PATH) as c:
        cur=c.execute("SELECT area,start_utc,end_utc,price_ore_kwh,source_currency,source_price_per_mwh,fetched_at FROM price_15m WHERE area=? ORDER BY start_utc ASC LIMIT ?",(area,limit)); names=[d[0] for d in cur.description]; return [dict(zip(names,row)) for row in cur.fetchall()]


def insert_pv_forecast(forecast):
    rows=forecast.get("rows") or []; generated_at=forecast["generated_at"]; model=forecast["model"]; radiation_feature=forecast["radiation_feature"]
    with sqlite3.connect(DB_PATH) as c:
        c.executemany('''INSERT OR REPLACE INTO pv_forecast_15m(generated_at,start_utc,forecast_kw,uncertainty_kw,irradiance_w_m2,cloud_cover_pct,temperature_c,model,radiation_feature,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)''',[(generated_at,r["start"],r["pv_power_forecast_kw"],r["pv_power_uncertainty_kw"],r["irradiance_w_m2"],r.get("cloud_cover_pct"),r.get("temperature_c"),model,radiation_feature,json.dumps(r,ensure_ascii=False)) for r in rows])
        remaining=forecast.get("pv_remaining_energy_today_kwh")
        if remaining is not None:
            g=datetime.fromisoformat(generated_at.replace("Z","+00:00")); local_date=g.astimezone(STOCKHOLM).date().isoformat(); u=forecast.get("pv_remaining_energy_uncertainty") or {}
            c.execute("INSERT OR REPLACE INTO pv_remaining_day_forecast(generated_at,local_date,remaining_energy_kwh,p80_kwh,p95_kwh,model) VALUES (?,?,?,?,?,?)",(generated_at,local_date,float(remaining),u.get("p80_kwh"),u.get("p95_kwh"),model))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=180)).isoformat(); c.execute("DELETE FROM pv_forecast_15m WHERE generated_at < ?",(cutoff,)); c.execute("DELETE FROM pv_remaining_day_forecast WHERE generated_at < ?",(cutoff,))


def latest_pv_forecast(limit=144):
    limit=max(1,min(int(limit),500))
    with sqlite3.connect(DB_PATH) as c:
        row=c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone(); generated_at=row[0] if row else None
        if not generated_at: return {"generated_at":None,"rows":[]}
        cur=c.execute("SELECT start_utc,forecast_kw,uncertainty_kw,irradiance_w_m2,cloud_cover_pct,temperature_c,model,radiation_feature,payload_json FROM pv_forecast_15m WHERE generated_at=? ORDER BY start_utc ASC LIMIT ?",(generated_at,limit)); names=[d[0] for d in cur.description]; rows=[]
        for r in cur.fetchall():
            d=dict(zip(names,r)); payload=d.pop("payload_json",None)
            if payload:
                try: d.update(json.loads(payload))
                except Exception: pass
            rows.append(d)
    return {"generated_at":generated_at,"rows":rows}


def insert_llm(ts,model,request,text):
    with sqlite3.connect(DB_PATH) as c: c.execute("INSERT INTO llm_explanations(created_at,model,request_json,explanation) VALUES (?,?,?,?)",(ts,model,json.dumps(request,ensure_ascii=False),text))


def latest_rows(table,limit=50):
    if table not in {"raw_state","state_15m"}: raise ValueError("invalid table")
    order="id" if table=="raw_state" else "bucket_start"; limit=max(1,min(int(limit),1000))
    with sqlite3.connect(DB_PATH) as c:
        cur=c.execute(f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?",(limit,)); names=[d[0] for d in cur.description]; return [dict(zip(names,row)) for row in cur.fetchall()]
