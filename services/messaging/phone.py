"""Normalize WhatsApp phone numbers for owner checks."""

from __future__ import annotations


def normalize_wa_digits(phone: str) -> str:
    return str(phone or "").lstrip("+").replace(" ", "").replace("-", "")


def wa_phones_match(a: str, b: str) -> bool:
    """True when numbers match exactly or one is the other with country code (e.g. 91…)."""
    da = normalize_wa_digits(a)
    db = normalize_wa_digits(b)
    if not da or not db:
        return False
    if da == db:
        return True
    if len(da) >= 10 and len(db) >= 10 and (da.endswith(db) or db.endswith(da)):
        return True
    return False
