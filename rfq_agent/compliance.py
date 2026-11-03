"""Business-rule vocabulary shared by ingest, pricing and evaluation.

Kept deliberately small and deterministic: the customer's own column text decides
which certificates are acceptable, a unit is compared as written (no conversion),
and a part number is matched exactly first, then by a documented normalisation.
Anything that is not an exact, verifiable match is surfaced for a human, never
silently accepted.
"""
from __future__ import annotations

import re

# Release certificates that prove airworthiness for a new/overhauled part.
AIRWORTHINESS_CERTS = ("EASA Form 1", "FAA 8130-3")

# Same unit written differently — not a conversion. PCS/SET are *not* folded into EA.
UNIT_SYNONYMS = {"EACH": "EA", "EA": "EA"}


def normalize_pn(value) -> str:
    """Case, whitespace and hyphen insensitive form of a part number."""
    return re.sub(r"[\s\-]+", "", str(value or "")).upper()


def match_type(candidate_pn, line) -> str | None:
    """How a reference-table part number relates to an RFQ line.

    exact      — identical string to the RFQ part number
    normalized — equal after normalize_pn (case / space / hyphen differences)
    alias      — equals the line's MPN or manual part number instead
    """
    raw = "" if candidate_pn is None else str(candidate_pn).strip()
    if not raw:
        return None
    if raw == line.part_number:
        return "exact"
    key = normalize_pn(raw)
    if key and key == normalize_pn(line.part_number):
        return "normalized"
    for alias in (getattr(line, "mpn", None), getattr(line, "manual_pn", None)):
        if alias and key == normalize_pn(alias):
            return "alias"
    return None


def lookup_keys(line) -> set[str]:
    """Every normalised part number a reference row may legitimately carry for this line."""
    keys = {normalize_pn(line.part_number)}
    for alias in (getattr(line, "mpn", None), getattr(line, "manual_pn", None)):
        if alias:
            keys.add(normalize_pn(alias))
    return {key for key in keys if key}


def required_certificates(text: str | None) -> list[str]:
    """Which release certificates the customer's requirement text demands.

    A named certificate narrows the set; a generic "CERT REQUIRED" / 适航证书 means
    any airworthiness release certificate. PMA and CoC never satisfy it.
    """
    upper = (text or "").upper()
    if not upper.strip():
        return []
    named = []
    if "EASA" in upper or "FORM 1" in upper or "FORM1" in upper:
        named.append("EASA Form 1")
    if "8130" in upper:
        named.append("FAA 8130-3")
    if named:
        return named
    if re.search(r"CERT|AIRWORTH|适航|证书", upper):
        return list(AIRWORTHINESS_CERTS)
    return []


def is_special_requirement(text: str | None) -> bool:
    """A requirement flag whose content the customer did not spell out."""
    upper = (text or "").upper()
    return bool(upper.strip()) and ("SPECIAL" in upper or "特殊" in upper)


def certificate_satisfies(certificate: str | None, required: list[str]) -> bool:
    if not required:
        return True
    return (certificate or "").strip() in required


def is_pma(certificate: str | None) -> bool:
    return "PMA" in (certificate or "").upper()


def unit_key(unit) -> str:
    text = str(unit or "").strip().upper()
    return UNIT_SYNONYMS.get(text, text)


def same_unit(a, b) -> bool:
    return bool(unit_key(a)) and unit_key(a) == unit_key(b)
