#!/usr/bin/env python3
"""Size a spot or linear-perpetual order from a fixed monetary risk budget.

The script accounts for stop distance, estimated round-trip fees/slippage,
venue quantity steps, notional limits, collateral, and leverage. It does not
authorize or submit an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 40


REQUIRED_FIELDS = (
    "side",
    "contract_type",
    "account_equity",
    "free_collateral",
    "risk_fraction",
    "entry_price",
    "stop_price",
    "contract_multiplier",
    "fee_bps_round_trip",
    "slippage_bps_round_trip",
    "quantity_step",
    "min_quantity",
    "min_notional",
    "max_leverage",
)


def decimal_value(payload: dict[str, Any], key: str, errors: list[str]) -> Decimal | None:
    try:
        value = Decimal(str(payload[key]))
    except KeyError:
        errors.append(f"{key} is required")
        return None
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f"{key} must be numeric")
        return None
    if not value.is_finite():
        errors.append(f"{key} must be finite")
        return None
    return value


def optional_decimal(payload: dict[str, Any], key: str, errors: list[str]) -> Decimal | None:
    if key not in payload or payload[key] is None:
        return None
    try:
        value = Decimal(str(payload[key]))
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f"{key} must be numeric when provided")
        return None
    if not value.is_finite():
        errors.append(f"{key} must be finite")
        return None
    return value


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def as_json_number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="Path to sizing JSON; read stdin when omitted")
    args = parser.parse_args()

    try:
        if args.input:
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        else:
            payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [f"Unable to read JSON: {exc}"]}, indent=2))
        return 2

    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "errors": ["Top-level JSON must be an object"]}, indent=2))
        return 1

    errors: list[str] = []
    warnings: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"{field} is required")

    side = payload.get("side")
    if side not in {"long", "short"}:
        errors.append("side must be long or short")

    contract_type = payload.get("contract_type")
    if contract_type not in {"spot", "linear"}:
        errors.append("contract_type must be spot or linear; inverse contracts need a venue-specific formula")
    if contract_type == "spot" and side == "short":
        errors.append("spot short sizing is unsupported; use an authorized margin or derivative contract")

    equity = decimal_value(payload, "account_equity", errors)
    free_collateral = decimal_value(payload, "free_collateral", errors)
    risk_fraction = decimal_value(payload, "risk_fraction", errors)
    entry = decimal_value(payload, "entry_price", errors)
    stop = decimal_value(payload, "stop_price", errors)
    multiplier = decimal_value(payload, "contract_multiplier", errors)
    fee_bps = decimal_value(payload, "fee_bps_round_trip", errors)
    slippage_bps = decimal_value(payload, "slippage_bps_round_trip", errors)
    quantity_step = decimal_value(payload, "quantity_step", errors)
    min_quantity = decimal_value(payload, "min_quantity", errors)
    min_notional = decimal_value(payload, "min_notional", errors)
    max_leverage = decimal_value(payload, "max_leverage", errors)

    max_quantity = optional_decimal(payload, "max_quantity", errors)
    max_notional = optional_decimal(payload, "max_notional", errors)
    liquidity_max_notional = optional_decimal(payload, "liquidity_max_notional", errors)

    numeric_values = {
        "account_equity": equity,
        "free_collateral": free_collateral,
        "risk_fraction": risk_fraction,
        "entry_price": entry,
        "stop_price": stop,
        "contract_multiplier": multiplier,
        "fee_bps_round_trip": fee_bps,
        "slippage_bps_round_trip": slippage_bps,
        "quantity_step": quantity_step,
        "min_quantity": min_quantity,
        "min_notional": min_notional,
        "max_leverage": max_leverage,
    }

    for key, value in numeric_values.items():
        if value is not None and value <= 0 and key not in {"fee_bps_round_trip", "slippage_bps_round_trip"}:
            errors.append(f"{key} must be positive")
    if fee_bps is not None and fee_bps < 0:
        errors.append("fee_bps_round_trip must be non-negative")
    if slippage_bps is not None and slippage_bps < 0:
        errors.append("slippage_bps_round_trip must be non-negative")
    if risk_fraction is not None and risk_fraction > 1:
        errors.append("risk_fraction must be <= 1")

    if entry is not None and stop is not None:
        if side == "long" and stop >= entry:
            errors.append("long stop_price must be below entry_price")
        if side == "short" and stop <= entry:
            errors.append("short stop_price must be above entry_price")

    if errors:
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, indent=2))
        return 1

    assert equity is not None and free_collateral is not None
    assert risk_fraction is not None and entry is not None and stop is not None
    assert multiplier is not None and fee_bps is not None and slippage_bps is not None
    assert quantity_step is not None and min_quantity is not None and min_notional is not None
    assert max_leverage is not None

    price_distance = abs(entry - stop)
    notional_per_quantity = entry * multiplier
    cost_bps = fee_bps + slippage_bps
    cost_per_quantity = notional_per_quantity * cost_bps / Decimal("10000")
    risk_per_quantity = price_distance * multiplier + cost_per_quantity
    risk_budget = equity * risk_fraction

    if risk_per_quantity <= 0:
        print(json.dumps({"ok": False, "errors": ["Calculated risk per quantity must be positive"]}, indent=2))
        return 1

    raw_risk_quantity = risk_budget / risk_per_quantity

    if contract_type == "spot":
        collateral_quantity = free_collateral / notional_per_quantity
        effective_leverage = Decimal("1")
    else:
        collateral_quantity = free_collateral * max_leverage / notional_per_quantity
        effective_leverage = max_leverage

    caps: dict[str, Decimal] = {
        "risk_budget": raw_risk_quantity,
        "collateral": collateral_quantity,
    }
    if max_quantity is not None:
        if max_quantity <= 0:
            errors.append("max_quantity must be positive when provided")
        else:
            caps["max_quantity"] = max_quantity
    if max_notional is not None:
        if max_notional <= 0:
            errors.append("max_notional must be positive when provided")
        else:
            caps["max_notional"] = max_notional / notional_per_quantity
    if liquidity_max_notional is not None:
        if liquidity_max_notional <= 0:
            errors.append("liquidity_max_notional must be positive when provided")
        else:
            caps["liquidity"] = liquidity_max_notional / notional_per_quantity

    if errors:
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, indent=2))
        return 1

    binding_cap_name, capped_raw_quantity = min(caps.items(), key=lambda item: item[1])
    quantity = floor_to_step(capped_raw_quantity, quantity_step)

    if quantity <= 0:
        errors.append("quantity rounds to zero at the venue quantity step")
    if quantity < min_quantity:
        errors.append("quantity is below min_quantity")

    notional = quantity * notional_per_quantity
    if notional < min_notional:
        errors.append("notional is below min_notional")

    price_risk = quantity * price_distance * multiplier
    estimated_cost = quantity * cost_per_quantity
    total_estimated_risk = price_risk + estimated_cost
    final_risk_fraction = total_estimated_risk / equity if equity > 0 else Decimal("0")
    margin_required = notional / effective_leverage

    if total_estimated_risk > risk_budget:
        errors.append("rounded quantity exceeds the risk budget")
    if margin_required > free_collateral:
        errors.append("required margin/cash exceeds free_collateral")

    if binding_cap_name != "risk_budget":
        warnings.append(f"quantity is capped by {binding_cap_name}, below the risk-budget quantity")

    report = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "side": side,
        "contract_type": contract_type,
        "binding_cap": binding_cap_name,
        "calculation": {
            "risk_budget": as_json_number(risk_budget),
            "price_distance": as_json_number(price_distance),
            "notional_per_quantity": as_json_number(notional_per_quantity),
            "estimated_cost_per_quantity": as_json_number(cost_per_quantity),
            "risk_per_quantity": as_json_number(risk_per_quantity),
            "raw_risk_quantity": as_json_number(raw_risk_quantity),
            "capped_raw_quantity": as_json_number(capped_raw_quantity),
            "quantity": as_json_number(quantity),
            "notional": as_json_number(notional),
            "margin_required": as_json_number(margin_required),
            "price_risk": as_json_number(price_risk),
            "estimated_round_trip_cost": as_json_number(estimated_cost),
            "total_estimated_risk": as_json_number(total_estimated_risk),
            "final_risk_fraction": as_json_number(final_risk_fraction),
        },
        "caps": {name: as_json_number(value) for name, value in caps.items()},
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
