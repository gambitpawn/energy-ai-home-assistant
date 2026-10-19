"""Persistent, default-deny authorization for every Energy AI Balboa write."""
from __future__ import annotations

from .db import connect_db


class BalboaControlDisabled(PermissionError):
    pass


def authorized() -> bool:
    # A new gate deliberately defaults to false even for upgrades that had
    # the legacy spa heat scheduler enabled.
    with connect_db(timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS balboa_write_permission (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT OR IGNORE INTO balboa_write_permission(id,enabled) VALUES(1,0)")
        return bool(db.execute("SELECT enabled FROM balboa_write_permission WHERE id=1").fetchone()[0])


def set_authorized(value: bool) -> None:
    authorized()
    with connect_db(timeout=10) as db:
        db.execute("UPDATE balboa_write_permission SET enabled=? WHERE id=1", (int(bool(value)),))


def require_authorized() -> None:
    if not authorized():
        raise BalboaControlDisabled("Balboa control is manually disabled")
