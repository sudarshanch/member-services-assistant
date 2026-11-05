"""Minimum-necessary filtering and log redaction.

The model and the trace see an allow-list. Social Security numbers, street
addresses, phone numbers and email addresses are dropped before that boundary.
"""

import re

SSN_DASHED = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
PHONE = re.compile(r"\b(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}\b")

DROP_KEYS = {"ssn", "social_security_number", "phone", "email", "line1", "notes"}


def scrub_text(value: str, secrets: list[str] | None = None) -> str:
    text = SSN_DASHED.sub("[redacted-ssn]", value)
    text = EMAIL.sub("[redacted-email]", text)
    text = PHONE.sub("[redacted-phone]", text)
    for secret in secrets or []:
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def minimize(value, secrets: list[str] | None = None):
    """Walk a JSON-like object and return a log-safe copy."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key.lower() in DROP_KEYS:
                continue
            if key.lower() == "address" and isinstance(item, dict):
                cleaned["city"] = item.get("city")
                cleaned["state"] = item.get("state")
                cleaned["zip"] = item.get("zip")
                continue
            cleaned[key] = minimize(item, secrets)
        return cleaned
    if isinstance(value, list):
        return [minimize(item, secrets) for item in value]
    if isinstance(value, str):
        return scrub_text(value, secrets)
    return value


def minimize_member(raw: dict) -> dict:
    """Explicit allow-list. A new upstream field does not become model context."""
    address = raw.get("address") or {}
    pcp = raw.get("pcp") if isinstance(raw.get("pcp"), dict) else {}
    return {
        "member_id": raw.get("member_id"),
        "first_name": raw.get("first_name"),
        "last_name": raw.get("last_name"),
        "preferred_language": raw.get("preferred_language"),
        "relationship": raw.get("relationship"),
        "product_code": raw.get("product_code"),
        "plan_name": raw.get("plan_name"),
        "network_id": raw.get("network_id"),
        "coverage_effective": raw.get("coverage_effective"),
        "coverage_termination": raw.get("coverage_termination"),
        "city": address.get("city"),
        "state": address.get("state"),
        "zip": address.get("zip"),
        "pcp_name": pcp.get("name"),
        "pcp_npi": pcp.get("npi"),
    }


def minimize_claim(raw: dict) -> dict:
    denial = raw.get("denial") or None
    responsibility = raw.get("member_responsibility") or {}
    service = raw.get("service") or {}
    provider = raw.get("provider") or {}
    return {
        "claim_id": raw.get("claim_id"),
        "member_id": raw.get("member_id"),
        "service_date": raw.get("service_date"),
        "processed_date": raw.get("processed_date"),
        "status": raw.get("status"),
        "service_code": service.get("code"),
        "service_description": service.get("description"),
        "provider_name": provider.get("name"),
        "provider_npi": provider.get("npi"),
        "billed_amount": raw.get("billed_amount"),
        "allowed_amount": raw.get("allowed_amount"),
        "plan_paid": raw.get("plan_paid"),
        "member_responsibility": {
            "deductible": responsibility.get("deductible", 0),
            "copay": responsibility.get("copay", 0),
            "coinsurance": responsibility.get("coinsurance", 0),
        },
        "denial_carc": denial.get("carc") if isinstance(denial, dict) else None,
        "denial_description": denial.get("description") if isinstance(denial, dict) else None,
    }
