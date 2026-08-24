from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

TRAINING_DIR = Path("/data/training")
DATASET_PATH = TRAINING_DIR / "training_dataset_15m.csv"
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def ensure_training_dir() -> Path:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    return TRAINING_DIR


def _safe_name(name: str) -> str:
    base = Path(name or "upload.csv").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip()
    if not cleaned.lower().endswith(".csv"):
        cleaned += ".csv"
    return cleaned or "upload.csv"


def save_upload(filename: str, data: bytes) -> Path:
    ensure_training_dir()
    path = TRAINING_DIR / _safe_name(filename)
    path.write_bytes(data)
    return path


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _dialect(text: str) -> csv.Dialect:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        class Comma(csv.excel):
            delimiter = ","
        return Comma()


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = _read_text(path)
    reader = csv.DictReader(io.StringIO(text), dialect=_dialect(text))
    fieldnames = [str(x).strip() for x in (reader.fieldnames or [])]
    rows = []
    for raw in reader:
        rows.append({str(k).strip(): ("" if v is None else str(v).strip()) for k, v in raw.items() if k is not None})
    return fieldnames, rows


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None", "nan"):
        return None
    text = str(value).strip().replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _offset_timezone(value: str):
    m = re.search(r"UTC\s*([+-])(\d{1,2}):?(\d{2})?", value or "", re.I)
    if not m:
        return None
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    if m.group(1) == "-":
        minutes = -minutes
    return timezone(timedelta(minutes=minutes))


def _parse_iso_or_local(value: str) -> datetime:
    value = (value or "").strip()
    if not value:
        raise ValueError("empty timestamp")
    clean = re.sub(r"\s*\((?:CET|CEST)\)\s*$", "", value, flags=re.I)
    try:
        d = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        d = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
    if d.tzinfo is None:
        d = d.replace(tzinfo=STOCKHOLM)
    return d.astimezone(timezone.utc)


def _bucket_15m(d: datetime) -> datetime:
    d = d.astimezone(timezone.utc)
    return d.replace(minute=(d.minute // 15) * 15, second=0, microsecond=0)


def detect_file(path: Path) -> dict[str, Any]:
    fields, rows = _rows(path)
    low = {f.lower(): f for f in fields}
    kind = "unknown"
    if {"date", "time", "utc_offset", "pv_power_kw"}.issubset(low):
        kind = "solinteg"
    elif "datum" in low and "el kwh" in low:
        kind = "c4_import"
    elif any(k in low for k in ("global_tilted_irradiance", "gti", "gti_w_m2")):
        kind = "irradiance"
    elif "time" in low and any(k in low for k in ("shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation")):
        kind = "irradiance"
    return {"name": path.name, "kind": kind, "columns": fields, "rows": len(rows), "bytes": path.stat().st_size}


def list_training_files() -> list[dict[str, Any]]:
    ensure_training_dir()
    result = []
    for path in sorted(TRAINING_DIR.glob("*.csv")):
        if path.name == DATASET_PATH.name:
            continue
        try:
            result.append(detect_file(path))
        except Exception as exc:
            result.append({"name": path.name, "kind": "error", "error": repr(exc), "bytes": path.stat().st_size})
    return result


def _aggregate_solinteg(path: Path) -> dict[datetime, dict[str, float | None]]:
    fields, rows = _rows(path)
    groups: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        try:
            local = datetime.fromisoformat(f"{row['date']}T{row['time']}")
            tz = _offset_timezone(row.get("utc_offset", "")) or STOCKHOLM
            stamp = local.replace(tzinfo=tz).astimezone(timezone.utc)
        except Exception:
            continue
        bucket = _bucket_15m(stamp)
        for key in ("pv_power_kw", "meter_power_kw", "load_power_kw"):
            value = _number(row.get(key))
            if value is not None:
                groups[bucket][key].append(value)
    out = {}
    for bucket, values in groups.items():
        out[bucket] = {key: (mean(vals) if vals else None) for key, vals in values.items()}
    return out


def _aggregate_c4(path: Path) -> dict[datetime, dict[str, float | None]]:
    _, rows = _rows(path)
    out = {}
    for row in rows:
        try:
            stamp = _parse_iso_or_local(row.get("Datum", ""))
        except Exception:
            continue
        energy = _number(row.get("El kWh"))
        production = _number(row.get("Produktion"))
        bucket = _bucket_15m(stamp)
        out[bucket] = {
            "c4_import_energy_kwh": energy,
            "c4_import_power_kw": None if energy is None else energy * 4.0,
            "c4_production_field": production,
        }
    return out


def _first(row: dict[str, str], names: tuple[str, ...]) -> str | None:
    low = {k.lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in low:
            return low[name.lower()]
    return None


def _aggregate_irradiance(path: Path) -> dict[datetime, dict[str, float | None]]:
    _, rows = _rows(path)
    groups: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        stamp_raw = _first(row, ("time", "timestamp", "timestamp_utc", "date"))
        if not stamp_raw:
            continue
        try:
            stamp = _parse_iso_or_local(stamp_raw)
        except Exception:
            continue
        bucket = _bucket_15m(stamp)
        aliases = {
            "gti_w_m2": ("global_tilted_irradiance", "gti", "gti_w_m2"),
            "shortwave_w_m2": ("shortwave_radiation", "shortwave_w_m2"),
            "dni_w_m2": ("direct_normal_irradiance", "dni", "dni_w_m2"),
            "dhi_w_m2": ("diffuse_radiation", "diffuse_radiation_instant", "dhi", "dhi_w_m2"),
            "cloud_cover_pct": ("cloud_cover", "cloud_cover_pct"),
            "temperature_c": ("temperature_2m", "temperature_c"),
        }
        for target, names in aliases.items():
            value = _number(_first(row, names))
            if value is not None:
                groups[bucket][target].append(value)
    out = {}
    for bucket, values in groups.items():
        out[bucket] = {key: (mean(vals) if vals else None) for key, vals in values.items()}
    return out


def build_dataset() -> dict[str, Any]:
    ensure_training_dir()
    files = list_training_files()
    merged: dict[datetime, dict[str, Any]] = defaultdict(dict)
    source_counts = {"solinteg": 0, "c4_import": 0, "irradiance": 0}

    for info in files:
        path = TRAINING_DIR / info["name"]
        kind = info.get("kind")
        if kind == "solinteg":
            data = _aggregate_solinteg(path)
        elif kind == "c4_import":
            data = _aggregate_c4(path)
        elif kind == "irradiance":
            data = _aggregate_irradiance(path)
        else:
            continue
        source_counts[kind] += len(data)
        for stamp, values in data.items():
            merged[stamp].update(values)

    columns = [
        "timestamp_utc", "pv_power_kw", "meter_power_kw", "load_power_kw",
        "c4_import_energy_kwh", "c4_import_power_kw", "c4_production_field",
        "gti_w_m2", "shortwave_w_m2", "dni_w_m2", "dhi_w_m2",
        "cloud_cover_pct", "temperature_c",
    ]
    with DATASET_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for stamp in sorted(merged):
            row = {"timestamp_utc": stamp.isoformat()}
            row.update(merged[stamp])
            writer.writerow({k: row.get(k) for k in columns})

    overlap_pv_gti = sum(1 for values in merged.values() if values.get("pv_power_kw") is not None and values.get("gti_w_m2") is not None)
    overlap_pv_weather = sum(1 for values in merged.values() if values.get("pv_power_kw") is not None and (values.get("gti_w_m2") is not None or values.get("shortwave_w_m2") is not None))
    pv_rows = sum(1 for values in merged.values() if values.get("pv_power_kw") is not None)
    c4_rows = sum(1 for values in merged.values() if values.get("c4_import_energy_kwh") is not None)

    return {
        "ok": True,
        "dataset": str(DATASET_PATH),
        "rows": len(merged),
        "pv_rows": pv_rows,
        "c4_rows": c4_rows,
        "pv_gti_overlap_rows": overlap_pv_gti,
        "pv_weather_overlap_rows": overlap_pv_weather,
        "source_interval_rows": source_counts,
        "columns": columns,
    }


def dataset_preview(limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    if not DATASET_PATH.exists():
        return {"exists": False, "path": str(DATASET_PATH), "rows": []}
    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append(row)
            if len(rows) >= limit:
                break
    return {"exists": True, "path": str(DATASET_PATH), "rows": rows}


def training_status() -> dict[str, Any]:
    files = list_training_files()
    return {
        "training_dir": str(ensure_training_dir()),
        "dataset_path": str(DATASET_PATH),
        "dataset_exists": DATASET_PATH.exists(),
        "files": files,
        "expected": {
            "solinteg": "5-minute CSV with date,time,utc_offset,pv_power_kw,meter_power_kw,load_power_kw",
            "c4_import": "15-minute CSV with Datum and El kWh",
            "irradiance": "CSV with time/timestamp and global_tilted_irradiance (preferred), optionally shortwave/cloud/temperature",
        },
    }
