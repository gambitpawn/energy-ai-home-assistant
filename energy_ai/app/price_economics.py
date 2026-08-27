from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DB_PATH

OPTIONS_PATH = Path("/data/options.json")
PRICING_MODEL = "spot_linked_grid_v1"
CURRENT_ECONOMICS = "current_economics"
HISTORICAL_ECONOMICS = "historical_economics"

# Swedish energy tax from 2026-01-01, excluding VAT. This is the default fixed
# import component; installations can override it in Home Assistant options or
# through the app-owned SQLite parameter store.
DEFAULT_IMPORT_FIXED_INCLUDING_ENERGY_TAX_ORE_KWH = 36.00
DEFAULT_IMPORT_SPOT_PERCENTAGE = 6.86
DEFAULT_EXPORT_FIXED_COMPENSATION_ORE_KWH = 2.84
DEFAULT_EXPORT_SPOT_PERCENTAGE = 6.05


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(default if value in (None, "") else value)
    except (TypeError, ValueError):
        return float(default)


def _legacy_or_default_import_fixed(base: dict[str, Any], opts: dict[str, Any]) -> float:
    explicit = opts.get(
        "import_fixed_including_energy_tax_ore_kwh",
        base.get("import_fixed_including_energy_tax_ore_kwh"),
    )
    if explicit not in (None, ""):
        return _f(explicit, DEFAULT_IMPORT_FIXED_INCLUDING_ENERGY_TAX_ORE_KWH)

    legacy = _f(opts.get("import_overhead_ore_kwh", base.get("import_overhead_ore_kwh", 0.0)))
    if abs(legacy) > 1e-12:
        return legacy
    return DEFAULT_IMPORT_FIXED_INCLUDING_ENERGY_TAX_ORE_KWH


def current_economics_from_options(base_economics: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return active economics with the already-loaded runtime config authoritative.

    `base_economics` comes from load_config(), where SQLite UI overrides have
    already been layered above Supervisor options. Raw /data/options.json is
    therefore only a fallback for callers that do not supply a complete base.
    """
    base = dict(base_economics or {})
    merged = {**_options(), **base}
    import_fixed = _legacy_or_default_import_fixed(merged, {})

    legacy_export_overhead = _f(merged.get("export_overhead_ore_kwh", 0.0))
    explicit_export = merged.get("export_fixed_compensation_ore_kwh")
    export_fixed = _f(
        explicit_export,
        -legacy_export_overhead if legacy_export_overhead else DEFAULT_EXPORT_FIXED_COMPENSATION_ORE_KWH,
    )

    economics = {
        **base,
        "pricing_model": PRICING_MODEL,
        "import_fixed_including_energy_tax_ore_kwh": import_fixed,
        "import_spot_percentage": _f(
            merged.get("import_spot_percentage"), DEFAULT_IMPORT_SPOT_PERCENTAGE
        ),
        "export_fixed_compensation_ore_kwh": export_fixed,
        "export_spot_percentage": _f(
            merged.get("export_spot_percentage"), DEFAULT_EXPORT_SPOT_PERCENTAGE
        ),
        "minimum_arbitrage_margin_ore_kwh": _f(
            merged.get("minimum_arbitrage_margin_ore_kwh"), 20.0
        ),
        "economics_valid_from": str(merged.get("economics_valid_from") or ""),
        "replay_default": CURRENT_ECONOMICS,
        "import_fixed_tax_basis": "energy_tax_2026_excluding_vat",
        # Compatibility values for old/report-only code paths. New economic
        # decisions must call effective_prices() instead of these aliases.
        "import_overhead_ore_kwh": import_fixed,
        "export_overhead_ore_kwh": -export_fixed,
    }
    return economics


def install_current_economics(cfg: dict[str, Any]) -> dict[str, Any]:
    policy = cfg.setdefault("policy", {})
    economics = current_economics_from_options(policy.get("economics") or {})
    policy["economics"] = economics
    return economics


def economics_payload(economics_or_cfg: dict[str, Any]) -> dict[str, Any]:
    economics = (
        ((economics_or_cfg.get("policy") or {}).get("economics") or {})
        if "policy" in economics_or_cfg
        else economics_or_cfg
    )
    if "import_fixed_including_energy_tax_ore_kwh" in economics:
        import_fixed = _f(
            economics.get("import_fixed_including_energy_tax_ore_kwh"),
            DEFAULT_IMPORT_FIXED_INCLUDING_ENERGY_TAX_ORE_KWH,
        )
    else:
        legacy_import = _f(economics.get("import_overhead_ore_kwh", 0.0))
        import_fixed = (
            legacy_import
            if abs(legacy_import) > 1e-12
            else DEFAULT_IMPORT_FIXED_INCLUDING_ENERGY_TAX_ORE_KWH
        )
    return {
        "pricing_model": str(economics.get("pricing_model") or PRICING_MODEL),
        "import_fixed_including_energy_tax_ore_kwh": import_fixed,
        "import_spot_percentage": _f(
            economics.get("import_spot_percentage"), DEFAULT_IMPORT_SPOT_PERCENTAGE
        ),
        "export_fixed_compensation_ore_kwh": _f(
            economics.get(
                "export_fixed_compensation_ore_kwh",
                -_f(economics.get("export_overhead_ore_kwh", -DEFAULT_EXPORT_FIXED_COMPENSATION_ORE_KWH)),
            ),
            DEFAULT_EXPORT_FIXED_COMPENSATION_ORE_KWH,
        ),
        "export_spot_percentage": _f(
            economics.get("export_spot_percentage"), DEFAULT_EXPORT_SPOT_PERCENTAGE
        ),
        "minimum_arbitrage_margin_ore_kwh": _f(
            economics.get("minimum_arbitrage_margin_ore_kwh"), 20.0
        ),
        "import_fixed_tax_basis": str(
            economics.get("import_fixed_tax_basis") or "energy_tax_2026_excluding_vat"
        ),
    }


def economics_signature(economics_or_cfg: dict[str, Any]) -> str:
    raw = json.dumps(economics_payload(economics_or_cfg), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def effective_prices(spot_ore_kwh: float, economics_or_cfg: dict[str, Any]) -> dict[str, float]:
    e = economics_payload(economics_or_cfg)
    spot = float(spot_ore_kwh)
    buy = (
        spot * (1.0 + e["import_spot_percentage"] / 100.0)
        + e["import_fixed_including_energy_tax_ore_kwh"]
    )
    sell = (
        spot * (1.0 + e["export_spot_percentage"] / 100.0)
        + e["export_fixed_compensation_ore_kwh"]
    )
    return {
        "spot_price_ore_kwh": spot,
        "effective_import_price_ore_kwh": buy,
        "effective_export_price_ore_kwh": sell,
        "import_spot_component_ore_kwh": spot * e["import_spot_percentage"] / 100.0,
        "export_spot_component_ore_kwh": spot * e["export_spot_percentage"] / 100.0,
    }


def enrich_price_row(row: dict[str, Any], economics_or_cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if bool(out.get("price_known")) and out.get("price_ore_kwh") is not None:
        out.update(effective_prices(float(out["price_ore_kwh"]), economics_or_cfg))
    else:
        out["effective_import_price_ore_kwh"] = None
        out["effective_export_price_ore_kwh"] = None
    return out


def _init_version_store() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS economics_tariff_version(
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_economics_tariff_validity
                ON economics_tariff_version(valid_from, valid_to);
            '''
        )


def register_current_economics(cfg: dict[str, Any]) -> dict[str, Any]:
    """Persist a new tariff version only when the configured economics changes."""
    _init_version_store()
    payload = economics_payload(cfg)
    signature = economics_signature(payload)
    configured_from = str(
        ((cfg.get("policy") or {}).get("economics") or {}).get("economics_valid_from") or ""
    ).strip()
    valid_from = configured_from or _now()
    with sqlite3.connect(DB_PATH) as c:
        latest = c.execute(
            "SELECT version_id,signature,valid_from,payload_json FROM economics_tariff_version "
            "WHERE valid_to IS NULL ORDER BY version_id DESC LIMIT 1"
        ).fetchone()
        if latest and latest[1] == signature:
            return {
                "version_id": latest[0],
                "signature": signature,
                "valid_from": latest[2],
                "changed": False,
                "economics": json.loads(latest[3]),
            }
        if latest:
            c.execute(
                "UPDATE economics_tariff_version SET valid_to=? WHERE version_id=?",
                (valid_from, latest[0]),
            )
        cur = c.execute(
            "INSERT INTO economics_tariff_version(signature,valid_from,valid_to,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (signature, valid_from, None, json.dumps(payload, sort_keys=True), _now()),
        )
        version_id = int(cur.lastrowid)
    return {
        "version_id": version_id,
        "signature": signature,
        "valid_from": valid_from,
        "changed": True,
        "economics": payload,
    }


def economics_versions(limit: int = 50) -> list[dict[str, Any]]:
    _init_version_store()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT version_id,signature,valid_from,valid_to,payload_json,created_at "
            "FROM economics_tariff_version ORDER BY version_id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [
        {
            "version_id": r[0],
            "signature": r[1],
            "valid_from": r[2],
            "valid_to": r[3],
            "economics": json.loads(r[4]),
            "created_at": r[5],
        }
        for r in rows
    ]


def economics_for_timestamp(
    cfg: dict[str, Any], timestamp: str | None, mode: str = CURRENT_ECONOMICS
) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode != HISTORICAL_ECONOMICS or not timestamp:
        e = economics_payload(cfg)
        return e, {
            "mode": CURRENT_ECONOMICS,
            "signature": economics_signature(e),
            "source": "active_config",
        }
    _init_version_store()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            '''SELECT version_id,signature,payload_json,valid_from,valid_to
               FROM economics_tariff_version
               WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>?)
               ORDER BY valid_from DESC,version_id DESC LIMIT 1''',
            (timestamp, timestamp),
        ).fetchone()
    if not row:
        e = economics_payload(cfg)
        return e, {
            "mode": HISTORICAL_ECONOMICS,
            "signature": economics_signature(e),
            "source": "fallback_active_config",
        }
    return json.loads(row[2]), {
        "mode": HISTORICAL_ECONOMICS,
        "version_id": row[0],
        "signature": row[1],
        "valid_from": row[3],
        "valid_to": row[4],
        "source": "version_store",
    }


def effective_prices_for_row(
    row: dict[str, Any], cfg: dict[str, Any], mode: str = CURRENT_ECONOMICS
) -> tuple[dict[str, float], dict[str, Any]]:
    e, meta = economics_for_timestamp(cfg, str(row.get("start") or ""), mode)
    return effective_prices(float(row["price_ore_kwh"]), e), meta
