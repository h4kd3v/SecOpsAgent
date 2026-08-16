"""What a turn cost.

Tokens are a proxy for money and finance asks for money. The rate applied is
stored alongside the amount on every turn, because published rates change and a
cost recomputed later from today's prices is not what was actually spent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.config import get_settings

settings = get_settings()

# Six places: a cheap turn on a cheap model is fractions of a cent, and
# rounding those to zero makes a month of them disappear.
QUANTUM = Decimal("0.000001")


def rates_for(model: str | None) -> tuple[float, float] | None:
    """(input, output) $/1M for a model, or None if it has no configured rate.

    No rate means no cost is recorded. A missing price is a gap an operator can
    see and fill; a guessed one is a number that looks authoritative and is
    wrong.
    """
    table = settings.pricing_table
    if not table:
        return None
    if model and model in table:
        return table[model]
    # Gateways resolve aliases to versioned ids ("gpt-4.1-2025-04-14"), so fall
    # back to the longest configured name that prefixes the served one.
    if model:
        matches = [name for name in table if name != "*" and model.startswith(name)]
        if matches:
            return table[max(matches, key=len)]
    return table.get("*")


def cost_for(model: str | None, usage: dict[str, Any] | None) -> Decimal | None:
    """Cost of one turn, or None when it cannot be known."""
    if not usage:
        return None
    rates = rates_for(model)
    if rates is None:
        return None
    prompt_rate, completion_rate = rates
    prompt = Decimal(int(usage.get("prompt_tokens") or 0))
    completion = Decimal(int(usage.get("completion_tokens") or 0))
    million = Decimal(1_000_000)
    total = (prompt / million) * Decimal(str(prompt_rate)) + (
        completion / million
    ) * Decimal(str(completion_rate))
    return total.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def stamp_rates(model: str | None, usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Record the rates used, so an old turn's cost stays explicable."""
    if usage is None:
        return None
    rates = rates_for(model)
    if rates is None:
        return usage
    return {**usage, "input_rate_per_1m": rates[0], "output_rate_per_1m": rates[1]}
