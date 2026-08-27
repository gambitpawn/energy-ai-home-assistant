from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .db import DB_PATH

SCHEMA_VERSION = 1
SENSITIVE_EXACT_KEYS = {
    "openai_api_key",
    "ha_access_token",
    "supervisor_token",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_sensitive_key(key: str) -> bool:
    lower = str(key).strip().lower()
    return (
        lower in SENSITIVE_EXACT_KEYS
        or "password" in lower
        or "secret" in lower
        or "token" in lower
        or "api_key" in lower
    )


def init_settings_store() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_setting(
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_app_setting_updated_at
                ON app_setting(updated_at);
            """
        )


def load_setting_overrides() -> dict[str, Any]:
    """Return app-owned parameter overrides.

    The settings table lives in /data/energy_ai.db, which is persistent add-on
    storage. Secrets are deliberately excluded from this store.
    """
    init_settings_store()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        rows = c.execute("SELECT key,value_json FROM app_setting ORDER BY key").fetchall()
    out: dict[str, Any] = {}
    for key, raw in rows:
        try:
            out[str(key)] = json.loads(raw)
        except Exception:
            continue
    return out


def apply_setting_overrides(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Overlay DB settings on Home Assistant/Supervisor options."""
    return {**dict(base or {}), **load_setting_overrides()}


def set_setting_overrides(values: dict[str, Any], *, source: str = "ui") -> dict[str, Any]:
    if not isinstance(values, dict):
        raise TypeError("values must be a dict")
    bad = sorted(str(k) for k in values if _is_sensitive_key(str(k)))
    if bad:
        raise ValueError(f"sensitive settings must remain in Supervisor options: {', '.join(bad)}")

    init_settings_store()
    updated_at = _now()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        c.execute("BEGIN IMMEDIATE")
        for key, value in values.items():
            c.execute(
                """
                INSERT INTO app_setting(key,value_json,updated_at,source,schema_version)
                VALUES (?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at,
                    source=excluded.source,
                    schema_version=excluded.schema_version
                """,
                (
                    str(key),
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    updated_at,
                    str(source),
                    SCHEMA_VERSION,
                ),
            )
    return {"saved": sorted(str(k) for k in values), "updated_at": updated_at}


def delete_setting_overrides(keys: Iterable[str]) -> list[str]:
    clean = sorted({str(k) for k in keys if str(k)})
    if not clean:
        return []
    init_settings_store()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        c.execute("BEGIN IMMEDIATE")
        c.executemany("DELETE FROM app_setting WHERE key=?", [(key,) for key in clean])
    return clean


def settings_status() -> dict[str, Any]:
    init_settings_store()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        rows = c.execute(
            "SELECT key,updated_at,source,schema_version FROM app_setting ORDER BY key"
        ).fetchall()
    return {
        "storage": "sqlite",
        "database": str(DB_PATH),
        "precedence": ["code_default", "home_assistant_options", "db_override"],
        "override_count": len(rows),
        "overrides": [
            {
                "key": str(key),
                "updated_at": str(updated_at),
                "source": str(source),
                "schema_version": int(schema_version),
            }
            for key, updated_at, source, schema_version in rows
        ],
        "secrets_allowed": False,
    }
