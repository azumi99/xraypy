"""Canonical company codes for assigning access to Print Label."""

import re


def normalize_pjt_code(value: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,39}", code):
        raise ValueError("Kode PJT harus berisi 1–40 huruf, angka, atau tanda hubung.")
    return code


class LabelOwnershipConflict(ValueError):
    """An existing label belongs to a different PJT."""
