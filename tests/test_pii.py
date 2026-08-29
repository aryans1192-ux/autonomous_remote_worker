from core.pii import scrub_pii


def test_scrubs_contiguous_aadhaar():
    assert scrub_pii("UID 123456789012 sent") == "UID [ANONYMIZED_UID] sent"


def test_scrubs_grouped_aadhaar():
    assert scrub_pii("uid 1234 5678 9012 ok") == "uid [ANONYMIZED_UID] ok"


def test_scrubs_dashed_aadhaar():
    assert scrub_pii("1234-5678-9012") == "[ANONYMIZED_UID]"


def test_scrubs_phone_with_prefix():
    assert scrub_pii("Call +91 9876543210 now") == "Call [ANONYMIZED_PHONE] now"


def test_scrubs_phone_without_prefix():
    assert scrub_pii("Call 9876543210 now") == "Call [ANONYMIZED_PHONE] now"


def test_leaves_non_pii_alone():
    text = "Merchant MERCHANT_001 had 12 failures in BHIWANI"
    assert scrub_pii(text) == text


def test_leaves_short_numbers_alone():
    assert scrub_pii("ref 1234 5678") == "ref 1234 5678"


def test_handles_non_string():
    assert scrub_pii(None) is None
    assert scrub_pii(12345) == 12345
