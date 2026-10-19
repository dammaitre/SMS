"""Phone number normalisation, so inbox, sent and contacts agree on one key."""

import re

DEFAULT_CC = "33"


def normalize(number: str, cc: str = DEFAULT_CC) -> str:
    """'06 12 34 56 78', '0033612345678', '+33 6…' -> '+33612345678'.

    Alphanumeric senders (AIRBNB, BANQUE POP) and short codes are kept as-is.
    """
    raw = number.strip()
    if not raw or re.search(r"[A-Za-z]", raw):
        return raw
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return f"+{cc}{digits[1:]}"
    return digits  # short codes (e.g. 36179) and anything unusual


def display(number: str) -> str:
    """'+33612345678' -> '+33 6 12 34 56 78' (French layout only)."""
    if number.startswith("+33") and len(number) == 12:
        rest = number[3:]
        return "+33 " + rest[0] + " " + " ".join(rest[i:i + 2] for i in range(1, 9, 2))
    return number
