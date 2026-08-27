from __future__ import annotations

from fastapi import Query

from . import dashboard
from .price_economics import (
    CURRENT_ECONOMICS,
    HISTORICAL_ECONOMICS,
    economics_for_timestamp,
    economics_payload,
    economics_signature,
    economics_versions,
    effective_prices,
    install_current_economics,
    register_current_economics,
)
from .price_economics_runtime import install_economics_patches
from .runtime_entry_v178 import app, core

RUNTIME_BUILD = "1.0.79"

# External economics are installed before any v1.79 decisions or training work.
CURRENT_ECONOMICS_CONFIG = install_current_economics(core.cfg)
ECONOMICS_VERSION = register_current_economics(core.cfg)
ECONOMICS_PATCH_STATUS = install_economics_patches(core.cfg)

core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# Keep the Parameters page aligned with the actual runtime economics. The
# add-on option remains one fixed import amount; the label makes explicit that
# the user-entered amount is inclusive of energy tax.
_old_economics_ui = "paramSection('Economics',[['Import overhead',`${n(e.import_overhead_ore_kwh)} öre/kWh`],['Export overhead',`${n(e.export_overhead_ore_kwh)} öre/kWh`],['Minimum arbitrage margin',`${n(e.minimum_arbitrage_margin_ore_kwh)} öre/kWh`],['Battery degradation',`${n(o.battery_degradation_ore_kwh)} öre/kWh throughput`],['Spot-price entity',ent.spot_price||'—','Home Assistant']])"
_new_economics_ui = "paramSection('Economics',[['Fixed import cost incl. energy tax',`${n(e.import_fixed_including_energy_tax_ore_kwh)} öre/kWh`],['Import spot-linked grid fee',`${n(e.import_spot_percentage)} % of spot`],['Fixed export compensation',`${n(e.export_fixed_compensation_ore_kwh)} öre/kWh`],['Export spot-linked compensation',`${n(e.export_spot_percentage)} % of spot`],['Minimum arbitrage margin',`${n(e.minimum_arbitrage_margin_ore_kwh)} öre/kWh`],['Battery degradation',`${n(o.battery_degradation_ore_kwh)} öre/kWh throughput`],['Spot-price entity',ent.spot_price||'—','Home Assistant']])"
if _old_economics_ui in dashboard.DASHBOARD_HTML:
    dashboard.DASHBOARD_HTML = dashboard.DASHBOARD_HTML.replace(_old_economics_ui, _new_economics_ui)


@app.get(
    "/economics/status",
    tags=["economics"],
    summary="Current spot-linked import/export economics and learning epoch",
)
async def economics_status():
    return {
        "ok": True,
        "runtime_build": RUNTIME_BUILD,
        "default_replay_mode": CURRENT_ECONOMICS,
        "historical_replay_mode": HISTORICAL_ECONOMICS,
        "current": economics_payload(core.cfg),
        "signature": economics_signature(core.cfg),
        "registered_version": ECONOMICS_VERSION,
        "runtime_patches": ECONOMICS_PATCH_STATUS,
        "price_formulas": {
            "import": "spot * (1 + import_spot_percentage / 100) + import_fixed_including_energy_tax_ore_kwh",
            "export": "spot * (1 + export_spot_percentage / 100) + export_fixed_compensation_ore_kwh",
            "raw_spot_history_preserved": True,
            "export_price_clamped_to_zero": False,
        },
    }


@app.get(
    "/economics/versions",
    tags=["economics"],
    summary="Versioned grid-energy economics for reproducible historical evaluation",
)
async def economics_version_history(limit: int = Query(50, ge=1, le=500)):
    return {"versions": economics_versions(limit), "default_training_mode": CURRENT_ECONOMICS}


@app.get(
    "/economics/price",
    tags=["economics"],
    summary="Calculate effective import and export value from one raw spot price",
)
async def economics_price(
    spot_ore_kwh: float,
    mode: str = Query(CURRENT_ECONOMICS),
    at: str | None = Query(None),
):
    if mode not in {CURRENT_ECONOMICS, HISTORICAL_ECONOMICS}:
        return {"ok": False, "error": f"mode must be {CURRENT_ECONOMICS} or {HISTORICAL_ECONOMICS}"}
    economics, source = economics_for_timestamp(core.cfg, at, mode)
    return {
        "ok": True,
        "mode": mode,
        "at": at,
        "economics_source": source,
        "economics": economics,
        **effective_prices(spot_ore_kwh, economics),
    }


app.openapi_schema = None
