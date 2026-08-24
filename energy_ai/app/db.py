import json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

DB_PATH=Path(os.getenv("ENERGY_AI_DB","/data/energy_ai.db"))

CORE_KEYS=("pv_power_kw","house_load_kw","grid_power_kw","battery_power_kw","battery_soc_pct")
LEGACY_MARKERS=(
    "sensor.energy_pv_power",
    "sensor.energy_house_load",
    "sensor.energy_grid_power",
    "sensor.energy_battery_power",
    "sensor.energy_battery_soc",
    "sensor.energy_spot_price",
)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS raw_state(id INTEGER PRIMARY KEY AUTOINCREMENT,collected_at TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state_15m(bucket_start TEXT PRIMARY KEY,collected_at TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS price_15m(area TEXT NOT NULL,start_utc TEXT NOT NULL,end_utc TEXT NOT NULL,price_ore_kwh REAL NOT NULL,source_currency TEXT NOT NULL,source_price_per_mwh REAL NOT NULL,fetched_at TEXT NOT NULL,PRIMARY KEY(area,start_utc));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,event_type TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS llm_explanations(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,model TEXT NOT NULL,request_json TEXT NOT NULL,explanation TEXT NOT NULL);
        ''')
        for marker in LEGACY_MARKERS:
            c.execute("DELETE FROM state_15m WHERE payload_json LIKE ?",(f"%{marker}%",))


def insert_raw(ts,payload):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)",(ts,json.dumps(payload,ensure_ascii=False)))


def upsert_15m(bucket,ts,payload):
    with sqlite3.connect(DB_PATH) as c:
        c.execute('''INSERT INTO state_15m(bucket_start,collected_at,payload_json) VALUES (?,?,?) ON CONFLICT(bucket_start) DO UPDATE SET collected_at=excluded.collected_at,payload_json=excluded.payload_json''',(bucket,ts,json.dumps(payload,ensure_ascii=False)))


def _numeric_state(payload, key):
    try:
        item = payload.get(key) or {}
        if not item.get("available"):
            return None
        value = item.get("state")
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _usable_core_sample(payload):
    return any(_numeric_state(payload,key) is not None for key in CORE_KEYS)


def rebuild_15m_bucket(bucket_start, bucket_end, expected_samples=None):
    with sqlite3.connect(DB_PATH) as c:
        cur=c.execute(
            "SELECT collected_at,payload_json FROM raw_state WHERE collected_at>=? AND collected_at<? ORDER BY collected_at ASC",
            (bucket_start,bucket_end),
        )
        rows=cur.fetchall()

    if not rows:
        return None

    parsed=[(ts,json.loads(payload_json)) for ts,payload_json in rows]
    usable=[(ts,p) for ts,p in parsed if _usable_core_sample(p)]
    if not usable:
        return None

    avg_keys=["pv_power_kw","house_load_kw","grid_power_kw","battery_power_kw","spot_price_ore_kwh"]
    means={}
    mins={}
    maxs={}
    counts={}
    for key in avg_keys:
        values=[v for _,p in usable if (v:=_numeric_state(p,key)) is not None]
        means[key]=mean(values) if values else None
        mins[key]=min(values) if values else None
        maxs[key]=max(values) if values else None
        counts[key]=len(values)

    soc_values=[v for _,p in usable if (v:=_numeric_state(p,"battery_soc_pct")) is not None]
    last_ts,last_payload=usable[-1]
    samples_raw=len(parsed)
    samples_valid=len(usable)
    completeness=None
    if expected_samples:
        completeness=min(1.0,samples_valid/float(expected_samples))

    payload={
        "schema_version":2,
        "bucket_start":bucket_start,
        "bucket_end":bucket_end,
        "samples":samples_valid,
        "samples_raw":samples_raw,
        "expected_samples":expected_samples,
        "completeness":completeness,
        "value_counts":counts,
        "mean":means,
        "min":mins,
        "max":maxs,
        "battery_soc_end_pct":soc_values[-1] if soc_values else None,
        "battery_soc_start_pct":soc_values[0] if soc_values else None,
        "last_sample_at":last_ts,
        "last_state":last_payload,
    }
    upsert_15m(bucket_start,last_ts,payload)
    return payload


def rebuild_recent_15m(poll_seconds, lookback_hours=48):
    expected_samples=max(1,round(900/int(poll_seconds)))
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=lookback_hours)).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        cur=c.execute("SELECT collected_at FROM raw_state WHERE collected_at>=? ORDER BY collected_at ASC",(cutoff,))
        timestamps=[r[0] for r in cur.fetchall()]

    buckets=set()
    for ts in timestamps:
        d=datetime.fromisoformat(ts.replace("Z","+00:00")).astimezone(timezone.utc)
        start=d.replace(minute=(d.minute//15)*15,second=0,microsecond=0)
        buckets.add(start)

    rebuilt=0
    for start in sorted(buckets):
        result=rebuild_15m_bucket(start.isoformat(),(start+timedelta(minutes=15)).isoformat(),expected_samples)
        if result is not None:
            rebuilt+=1
    return rebuilt


def upsert_prices(area, rows, fetched_at):
    with sqlite3.connect(DB_PATH) as c:
        c.executemany('''INSERT INTO price_15m(area,start_utc,end_utc,price_ore_kwh,source_currency,source_price_per_mwh,fetched_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(area,start_utc) DO UPDATE SET end_utc=excluded.end_utc,price_ore_kwh=excluded.price_ore_kwh,source_currency=excluded.source_currency,source_price_per_mwh=excluded.source_price_per_mwh,fetched_at=excluded.fetched_at''',[(area,r['start'],r['end'],r['price_ore_kwh'],r['currency'],r['source_price_per_mwh'],fetched_at) for r in rows])


def get_prices(area, limit=192):
    limit=max(1,min(int(limit),500))
    with sqlite3.connect(DB_PATH) as c:
        cur=c.execute("SELECT area,start_utc,end_utc,price_ore_kwh,source_currency,source_price_per_mwh,fetched_at FROM price_15m WHERE area=? ORDER BY start_utc ASC LIMIT ?",(area,limit))
        names=[d[0] for d in cur.description]
        return [dict(zip(names,row)) for row in cur.fetchall()]


def insert_llm(ts,model,request,text):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO llm_explanations(created_at,model,request_json,explanation) VALUES (?,?,?,?)",(ts,model,json.dumps(request,ensure_ascii=False),text))


def latest_rows(table,limit=50):
    if table not in {"raw_state","state_15m"}: raise ValueError("invalid table")
    order="id" if table=="raw_state" else "bucket_start"; limit=max(1,min(int(limit),1000))
    with sqlite3.connect(DB_PATH) as c:
        cur=c.execute(f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?",(limit,)); names=[d[0] for d in cur.description]
        return [dict(zip(names,row)) for row in cur.fetchall()]
