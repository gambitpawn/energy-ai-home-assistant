from datetime import datetime, timezone

import pytest

from app.optimizer_v36_live import _live_rows, _transition_action_kw
from app.soc_replanning import expected_soc_at


def test_live_rows_preserve_only_remaining_part_of_current_quarter():
    base = [
        {"start": "2026-08-28T10:00:00+00:00", "load_kw": 1.0, "pv_kw": 0.0, "price_known": True, "price_ore_kwh": 100.0},
        {"start": "2026-08-28T10:15:00+00:00", "load_kw": 1.0, "pv_kw": 0.0, "price_known": True, "price_ore_kwh": 100.0},
    ]
    now = datetime(2026, 8, 28, 10, 7, 0, tzinfo=timezone.utc)
    rows = _live_rows(base, now)

    assert len(rows) == 2
    assert rows[0]["partial_interval"] is True
    assert rows[0]["start"] == "2026-08-28T10:07:00+00:00"
    assert rows[0]["end"] == "2026-08-28T10:15:00+00:00"
    assert rows[0]["duration_minutes"] == pytest.approx(8.0)
    assert rows[1]["partial_interval"] is False
    assert rows[1]["duration_minutes"] == pytest.approx(15.0)


def test_transition_power_uses_actual_partial_duration():
    # One kWh battery-energy increase over 7.5 minutes at 95% charge efficiency.
    action = _transition_action_kw(5.0, 6.0, 0.95, 0.95, 0.125)
    assert action == pytest.approx(-(1.0 / 0.95) / 0.125)


def test_expected_soc_interpolates_legacy_v35_interval():
    plan = {
        "initial_soc_pct": 50.0,
        "rows": [
            {"start": "2026-08-28T10:00:00+00:00", "expected_soc_pct": 40.0},
            {"start": "2026-08-28T10:15:00+00:00", "expected_soc_pct": 30.0},
        ],
    }
    at = datetime(2026, 8, 28, 10, 7, 30, tzinfo=timezone.utc)
    assert expected_soc_at(plan, at) == pytest.approx(45.0)


def test_expected_soc_uses_explicit_v36_start_and_end():
    plan = {
        "initial_soc_pct": 36.0,
        "rows": [
            {
                "start": "2026-08-28T10:08:00+00:00",
                "end": "2026-08-28T10:15:00+00:00",
                "duration_hours": 7.0 / 60.0,
                "soc_start_pct": 36.0,
                "expected_soc_pct": 29.0,
            }
        ],
    }
    at = datetime(2026, 8, 28, 10, 11, 30, tzinfo=timezone.utc)
    assert expected_soc_at(plan, at) == pytest.approx(32.5)
