from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .production_state import scheduled_overrides


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def build_override_aware_forecast(base_fn: Callable[[dict[str, Any], list[datetime]], dict[str, Any]]):
    def wrapped(cfg: dict[str, Any], starts: list[datetime]) -> dict[str, Any]:
        result = base_fn(cfg, starts)
        rows = result.get("rows") or []
        overrides = scheduled_overrides()
        ev_max = float(((cfg.get("policy") or {}).get("ev") or {}).get("max_power_kw", 11.0))
        sauna_nominal = float(((cfg.get("policy") or {}).get("sauna") or {}).get("nominal_peak_kw", 6.0))
        applied: list[dict[str, Any]] = []

        for row in rows:
            try:
                stamp = _dt(row["start"])
            except Exception:
                continue
            ev_kw = float(row.get("ev_forecast_kw") or 0.0)
            sauna_kw = float(row.get("sauna_forecast_kw") or 0.0)
            row_applied: list[int] = []
            for item in overrides:
                if item.get("status") != "active":
                    continue
                start = _dt(item["starts_at"])
                end = _dt(item["ends_at"]) if item.get("ends_at") else None
                if stamp < start or (end is not None and stamp >= end):
                    continue
                kind = item.get("kind")
                if kind == "sauna":
                    sauna_kw = sauna_nominal
                    row_applied.append(int(item["override_id"]))
                elif kind == "ev_charge_now":
                    # Existing EV model is provisional and assumes two-hour
                    # persistence. Charge-now makes that persistence explicit,
                    # using configured EV max power when no live draw exists.
                    ev_kw = max(ev_kw, ev_max)
                    row_applied.append(int(item["override_id"]))
            row["ev_forecast_kw"] = round(max(0.0, ev_kw), 4)
            row["sauna_forecast_kw"] = round(max(0.0, sauna_kw), 4)
            row["flexible_load_forecast_kw"] = round(max(0.0, ev_kw) + max(0.0, sauna_kw), 4)
            if row_applied:
                row["user_override_ids"] = row_applied

        for item in overrides:
            applied.append({
                "override_id": item.get("override_id"),
                "kind": item.get("kind"),
                "starts_at": item.get("starts_at"),
                "ends_at": item.get("ends_at"),
            })
        result["user_overrides"] = applied
        return result

    return wrapped
