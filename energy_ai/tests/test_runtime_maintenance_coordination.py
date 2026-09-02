from __future__ import annotations

from datetime import datetime, timezone

from app import runtime_maintenance as maintenance


class FixedDateTime(datetime):
    current = datetime(2026, 9, 2, 10, 4, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


def test_hourly_maintenance_slot_is_wall_clock_aligned(monkeypatch):
    FixedDateTime.current = datetime(2026, 9, 2, 10, 4, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(maintenance, "datetime", FixedDateTime)
    assert maintenance._seconds_until_slot(minute=5) == 30.0


def test_restart_after_slot_waits_for_next_hour(monkeypatch):
    FixedDateTime.current = datetime(2026, 9, 2, 10, 5, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(maintenance, "datetime", FixedDateTime)
    assert maintenance._seconds_until_slot(minute=5) == 3570.0


def test_six_hour_slot_respects_phase(monkeypatch):
    FixedDateTime.current = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(maintenance, "datetime", FixedDateTime)
    assert maintenance._seconds_until_slot(minute=8, period_hours=6, phase_hour=1) == 3 * 3600 + 8 * 60
