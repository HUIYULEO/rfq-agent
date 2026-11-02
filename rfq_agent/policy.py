"""Demo pricing policy.

Every value here is a **demo assumption**, not a contest rule. Keep it in one place
so a quote can always name the ``policy_version`` that produced its numbers.
"""
from decimal import Decimal, ROUND_HALF_UP

POLICY_VERSION = "demo-2026-11-01"

DEFAULT_POLICY: dict = {
    "policy_version": POLICY_VERSION,
    # The supplied reference tables and formula are for demonstration, not an
    # approved commercial price book. An explicit approved policy is required
    # before the service can activate a quote.
    "commercial_approval_enabled": False,
    "default_markup_rate": "0.20",
    "fx_to_cny": {"CNY": "1", "EUR": "7.75", "USD": "7.1"},
}

# Autonomy presets: same pipeline, different resolution of blocking review items.
AUTONOMY_PRESETS = ("interactive", "auto_draft")


def q2(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(value) -> str:
    return str(q2(value))


def dec(value) -> Decimal:
    return Decimal(str(value))
