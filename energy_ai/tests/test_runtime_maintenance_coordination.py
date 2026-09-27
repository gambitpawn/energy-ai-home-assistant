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


def test_core_optimizer_day_evaluation_uses_reserved_lane():
    source = (ROOT / "app" / "runtime_maintenance.py").read_text(encoding="utf-8")
    block = source[source.index("async def _optimizer_day_loop"):source.index("async def _evaluation_decomposition_loop")]
    assert "lane=LANE_EVALUATION" in block
    assert "timeout_seconds=600" in block


def test_hindsight_decomposition_stays_on_heavy_lane_with_deadline():
    source = (ROOT / "app" / "runtime_maintenance.py").read_text(encoding="utf-8")
    block = source[source.index("async def _evaluation_decomposition_loop"):source.index("async def _selector_loop")]
    assert "lane=LANE_HEAVY" in block
    assert "timeout_seconds=1200" in block
