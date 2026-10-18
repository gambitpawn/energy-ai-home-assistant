from datetime import datetime, timedelta, timezone

from app.balboa_heat_scheduler import calculate_heat_plan


def rows(now, values):
    beginning = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return [
        {"start": (beginning + timedelta(minutes=15*i)).isoformat(),
         "price_ore_kwh": value, "price_known": True, "grid_import_kw": 4}
        for i, value in enumerate(values)
    ]


def test_cold_spa_heats_even_when_expensive():
    t=datetime(2026,10,18,18,1,tzinfo=timezone.utc)
    p=calculate_heat_plan(t, 11.5, 39, "off", rows(t,[999]*8))
    assert p["desired"]=="heat" and p["reason"]=="temperature_guard"


def test_cheap_slot_can_heat_and_high_price_can_pause():
    t=datetime(2026,10,18,18,1,tzinfo=timezone.utc)
    assert calculate_heat_plan(t,39,39,"heat",rows(t,[30,90,90,30,90,30,90,30]))["desired"]=="heat"
    assert calculate_heat_plan(t,39,39,"heat",rows(t,[190,40,50,60,70,80,90,30]))["desired"]=="off"


def test_unknown_prices_fail_to_heat():
    t=datetime(2026,10,18,18,1,tzinfo=timezone.utc)
    assert calculate_heat_plan(t,39,39,"off",[])["desired"]=="heat"


def test_bada_nu_overrides_high_price():
    t=datetime(2026,10,18,18,1,tzinfo=timezone.utc)
    assert calculate_heat_plan(t,39,39,"off",rows(t,[999]*8),override=True)["desired"]=="heat"


def test_ev_peak_is_penalized():
    t=datetime(2026,10,18,18,1,tzinfo=timezone.utc)
    r=rows(t,[20,30,40,50,60,70,80,90])
    r[0]["grid_import_kw"]=13
    assert calculate_heat_plan(t,39,39,"heat",r)["desired"]=="off"
