"""Currency helpers. Amounts stay in Decimal so totals are not binary-float noise."""

from decimal import Decimal, ROUND_HALF_UP


def D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Decimal(f"{float(value):.2f}")


def usd(value) -> str:
    """Format a dollar amount the way the plan documents and eval facts do."""
    amount = D(value)
    if amount == amount.to_integral_value():
        return f"${int(amount):,}"
    return f"${amount:,.2f}"


def parse_dollars(cell: str) -> list[Decimal]:
    import re

    return [Decimal(n.replace(",", "")) for n in re.findall(r"\$([0-9,]+(?:\.[0-9]+)?)", cell)]
