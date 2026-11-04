"""Demo pricing policy.

Every value here is a **demo assumption**, not a contest rule. Keep it in one place
so a quote can always name the ``policy_version`` that produced its numbers.
"""
import os
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
    # Compliance gate: a PMA part is never chosen silently, even when the customer
    # did not state a certificate requirement — a human accepts it or picks another.
    "pma_requires_review": True,
}

# Demo approval: lets the full "human approves -> history written -> final document"
# loop be shown on the demo data. It is explicit (env var or this object), every
# artifact it produces is stamped 演示批准, and the default policy stays fail-closed.
DEMO_APPROVAL_ENV = "RFQ_DEMO_APPROVAL"
DEMO_APPROVED_POLICY: dict = {
    **DEFAULT_POLICY,
    "policy_version": POLICY_VERSION + "+demo-approval",
    "commercial_approval_enabled": True,
    "approval_mode": "demo",
    "approval_label": "演示批准",
}


def active_policy() -> dict:
    """DEFAULT_POLICY unless RFQ_DEMO_APPROVAL=1 explicitly opts into demo approval."""
    if os.environ.get(DEMO_APPROVAL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return DEMO_APPROVED_POLICY
    return DEFAULT_POLICY

# Autonomy presets: same pipeline, different resolution of blocking review items.
AUTONOMY_PRESETS = ("interactive", "auto_draft")


def q2(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(value) -> str:
    return str(q2(value))


def dec(value) -> Decimal:
    return Decimal(str(value))


def line_amount(unit_price, quantity) -> Decimal:
    """The one rounding rule for a line total: q2(unit price x priced quantity).

    Pricing writes it and evaluation re-derives it with this same function, so the
    two sides can never disagree on fractional quantities (IN / FT lines).
    """
    return q2(dec(unit_price) * dec(quantity))
