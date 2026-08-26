from datetime import datetime, timezone

from app.regret_decomposition import _inject_perfect_information


def _dt(s: str):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(second=0, microsecond=0)


def test_perfect_forecast_preserves_historical_price_information():
    horizon = [{
        "start": "2026-08-25T20:00:00+00:00",
        "load_kw": 1.0,
        "pv_kw": 2.0,
        "load_uncertainty_kw": 0.8,
        "pv_uncertainty_kw": 0.6,
        "price_known": False,
        "price_ore_kwh": None,
    }]
    actual = {
        _dt(horizon[0]["start"]): {
            "load_kw": 3.5,
            "pv_kw": 1.2,
            "price_ore_kwh": 177.0,
        }
    }
    rows, missing = _inject_perfect_information(horizon, actual, perfect_prices=False)
    assert missing == []
    assert rows[0]["load_kw"] == 3.5
    assert rows[0]["pv_kw"] == 1.2
    assert rows[0]["load_uncertainty_kw"] == 0.0
    assert rows[0]["pv_uncertainty_kw"] == 0.0
    assert rows[0]["price_known"] is False
    assert rows[0]["price_ore_kwh"] is None


def test_perfect_information_exposes_realized_price():
    horizon = [{
        "start": "2026-08-25T20:00:00+00:00",
        "load_kw": 1.0,
        "pv_kw": 2.0,
        "load_uncertainty_kw": 0.8,
        "pv_uncertainty_kw": 0.6,
        "price_known": False,
        "price_ore_kwh": None,
    }]
    actual = {
        _dt(horizon[0]["start"]): {
            "load_kw": 3.5,
            "pv_kw": 1.2,
            "price_ore_kwh": 177.0,
        }
    }
    rows, missing = _inject_perfect_information(horizon, actual, perfect_prices=True)
    assert missing == []
    assert rows[0]["price_known"] is True
    assert rows[0]["price_ore_kwh"] == 177.0


def test_perfect_information_reports_missing_actual_interval():
    horizon = [{
        "start": "2026-08-25T20:00:00+00:00",
        "load_kw": 1.0,
        "pv_kw": 2.0,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }]
    rows, missing = _inject_perfect_information(horizon, {}, perfect_prices=False)
    assert rows == []
    assert missing == ["2026-08-25T20:00:00+00:00"]
