import re

# 12-digit Aadhaar-like runs: contiguous or grouped "1234 5678 9012"
_UID_RE = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
# Indian mobile numbers: +91 prefix (optional) + 10 digits starting [6-9]
_PHONE_RE = re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")


def scrub_pii(text: str) -> str:
    """Anonymize Aadhaar-like and Indian mobile numbers in free text."""
    if not isinstance(text, str):
        return text
    text = _UID_RE.sub("[ANONYMIZED_UID]", text)
    text = _PHONE_RE.sub("[ANONYMIZED_PHONE]", text)
    return text
