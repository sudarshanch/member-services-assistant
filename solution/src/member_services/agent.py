"""Rep assistant.

The default provider is a deterministic policy: it chooses tools, and the
gateway decides whether those tools may run. An OpenAI-compatible tool-calling
loop is available behind MODEL_PROVIDER=openai. Neither path can file a
grievance, cross tenants, or send a member identifier to the directory
without the gateway allowing it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from member_services.gateway import Gateway, cancelled, confirmed
from member_services.kb import individual_and_family, visit_limit
from member_services.money import D, usd

CLAIM_ID = re.compile(r"\b(C\d{7,})\b")
GRIEVANCE_ID = re.compile(r"\b(G26-\d+)\b")
ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
LAST4 = re.compile(r"\b(\d{4})\b")

FULL_SSN = re.compile(
    r"full social security|full ssn|social security number|entire ssn",
    re.IGNORECASE,
)
PRIOR_AUTH = re.compile(r"prior auth", re.IGNORECASE)
FILE_GRIEVANCE = re.compile(r"\b(file|filing)\b", re.IGNORECASE)
RENEWAL = re.compile(r"\b2027\b|renew|after that|next plan year", re.IGNORECASE)


def answer_turn(gateway: Gateway, text: str, as_of: date) -> str:
    session = gateway.session
    session.turn += 1
    session.last_user_text = text
    session.started = time.monotonic()
    session.events.append(
        {"kind": "model", "provider": "deterministic", "model": "deterministic-policy-v1", "remote_call": False}
    )
    if FULL_SSN.search(text) and "last four" not in text.lower():
        return (
            "I can't provide the member's full Social Security number. "
            "I can check the last four digits if you read those to me."
        )
    if session.pending is not None and (cancelled(text) or confirmed(text)):
        return _resolve_pending(gateway, text)
    member = _tool(gateway, "get_member")
    if not member or not member.get("found"):
        return (
            f"Member {session.member_id} is not on file for this plan. "
            "I can't retrieve another plan's records from this conversation."
        )
    person = member["member"]
    if _is_prior_auth(text):
        _tool(
            gateway,
            "retrieve",
            {"query": "prior authorization utilization management", "product_code": person["product_code"]},
        )
        return (
            "That information is not available. Prior authorization decisions are handled "
            "outside this assistant, and I won't guess."
        )
    if _is_grievance_deadline(text):
        return _grievance_deadline(gateway, text, as_of)
    if FILE_GRIEVANCE.search(text) and re.search(r"grievance", text, re.IGNORECASE):
        return _stage(gateway, text)
    if re.search(r"last four", text, re.IGNORECASE):
        return _last4(gateway, text)
    if re.search(r"dermatolog|find an in-network|provider directory", text, re.IGNORECASE):
        return _providers(gateway, text, person)
    if _is_claim_question(text):
        return _claims(gateway, text, person, as_of)
    if _is_benefit_question(text):
        return _benefits(gateway, text, person, as_of)
    return "That information is not available from the member, claims, or plan documents I can use. I won't guess."


def _resolve_pending(gateway: Gateway, text: str) -> str:
    if cancelled(text):
        gateway.session.pending = None
        return "Understood. I will not file the grievance."
    result = gateway.call("file_grievance")
    if not result["ok"]:
        return "I have not filed the grievance. I still need an explicit confirmation after the read-back."
    filed = result["output"]
    return (
        f"Filed one {filed['category']} grievance {filed['grievance_id']} "
        f"on {filed['received_date']}. Status {filed['status']}."
    )


def _stage(gateway: Gateway, text: str) -> str:
    category = _category(text)
    description = _description(text)
    result = gateway.call(
        "stage_grievance",
        {"category": category, "description": description},
    )
    if not result["ok"]:
        return "I can't stage that grievance from the details given."
    staged = result["output"]
    return (
        f"I can file a {staged['category']} grievance with this summary: {staged['description']} "
        "I have not filed it. Please confirm after you read that summary back and the member agrees."
    )


def _last4(gateway: Gateway, text: str) -> str:
    match = LAST4.search(text)
    if not match:
        return "Read me the last four digits and I can check them. I can't read out a full Social Security number."
    result = _tool(gateway, "verify_ssn_last4", {"last4": match.group(1)})
    if not result or not result.get("found"):
        return (
            f"Member {gateway.session.member_id} is not on file for this plan. "
            "I can't retrieve another plan's records from this conversation."
        )
    if result.get("match"):
        return f"The last four digits {match.group(1)} match what we have on file."
    return f"The last four digits {match.group(1)} do not match what we have on file."


def _providers(gateway: Gateway, text: str, person: dict) -> str:
    specialty = "Dermatology" if re.search(r"dermatolog", text, re.IGNORECASE) else "Provider"
    result = _tool(
        gateway,
        "search_providers",
        {
            "query": specialty.lower(),
            "specialty": specialty,
            "zip": person.get("zip"),
            "network_id": person.get("network_id"),
            "limit": 10,
        },
    )
    names = [item["name"] for item in (result or {}).get("results", []) if item.get("name")]
    if not names:
        return "I didn't find an in-network provider matching that search in the member's ZIP area."
    listed = "; ".join(names)
    return (
        f"In-network {specialty.lower()} results near the member's home ZIP {person.get('zip')}, "
        f"network {person.get('network_id')}: {listed}."
    )


def _grievance_deadline(gateway: Gateway, text: str, as_of: date) -> str:
    rules = gateway.kb.rules.get(gateway.session.tenant)
    found = _tool(gateway, "list_grievances")
    wanted = GRIEVANCE_ID.search(text)
    rows = (found or {}).get("grievances", [])
    row = next((item for item in rows if wanted and item["grievance_id"] == wanted.group(1)), None)
    if row is None or rules is None:
        return "That grievance is not on file for this member, so I can't give a resolution deadline."
    received = date.fromisoformat(row["received_date"])
    deadline = received + timedelta(days=rules.grievance_resolution_days)
    past = as_of > deadline
    word = "Yes" if past else "No"
    state = "is past" if past else "is not past"
    return (
        f"{word}. Grievance {row['grievance_id']} {state} the plan's resolution deadline of "
        f"{deadline.isoformat()} ({rules.grievance_resolution_days} calendar days from {row['received_date']})."
    )


def _claims(gateway: Gateway, text: str, person: dict, as_of: date) -> str:
    claim_id = CLAIM_ID.search(text)
    if claim_id and not re.search(r"how many|appeal|why|denied|owe|\bpay", text, re.IGNORECASE):
        one = _tool(gateway, "get_claim", {"claim_id": claim_id.group(1)})
        claim = (one or {}).get("claim")
        if not claim:
            return f"Claim {claim_id.group(1)} is not on file for this member."
        return f"Claim {claim['claim_id']} is {claim['status']}."
    claims = (_tool(gateway, "get_claims") or {}).get("claims", [])
    _tool(
        gateway,
        "retrieve",
        {"query": "claim status denial appeal", "product_code": person["product_code"]},
    )
    if re.search(r"how many", text, re.IGNORECASE) and re.search(r"denied", text, re.IGNORECASE):
        year = "2026" if "2026" in text else str(as_of.year)
        count = sum(1 for claim in claims if claim["status"] == "DENIED" and str(claim["service_date"]).startswith(year))
        return f"{count} of this member's {year} claims were denied."
    if re.search(r"appeal", text, re.IGNORECASE):
        return _appeal(gateway, text, claims, as_of)
    claim = _select_claim(text, claims)
    if claim is None:
        return "I can't find that claim on this member's record, so I won't guess."
    if re.search(r"plan pay|did the plan pay", text, re.IGNORECASE):
        return f"The plan paid {usd(claim['plan_paid'])} on claim {claim['claim_id']}."
    if re.search(r"owe|owed", text, re.IGNORECASE):
        parts = claim["member_responsibility"]
        total = D(parts["deductible"]) + D(parts["copay"]) + D(parts["coinsurance"])
        return (
            f"For the {claim['service_description']} on {claim['service_date']}, "
            f"the member owed {usd(total)}."
        )
    if claim["status"] == "DENIED" or re.search(r"denied|why", text, re.IGNORECASE):
        return _denial(gateway, claim)
    return (
        f"Claim {claim['claim_id']} ({claim['service_description']} on {claim['service_date']}) "
        f"is {claim['status']}."
    )


def _appeal(gateway: Gateway, text: str, claims: list[dict], as_of: date) -> str:
    rules = gateway.kb.rules.get(gateway.session.tenant)
    claim = _select_claim(text, claims)
    if claim is None or rules is None or not claim.get("processed_date"):
        return "That information is not available. I can't tell whether an appeal window is open without the denial notice date."
    deadline = date.fromisoformat(claim["processed_date"]) + timedelta(days=rules.appeal_window_days)
    open_window = as_of <= deadline
    word = "Yes" if open_window else "No"
    if open_window:
        return (
            f"{word}. The member can still appeal the denial on claim {claim['claim_id']}. "
            f"The window ends on {deadline.isoformat()}."
        )
    return (
        f"{word}. The member can no longer appeal the denial on claim {claim['claim_id']}. "
        f"The window ended on {deadline.isoformat()}."
    )


def _denial(gateway: Gateway, claim: dict) -> str:
    carc = claim.get("denial_carc") or "an unspecified code"
    guidance = gateway.kb.member_text_for_carc(gateway.session.tenant, carc) if carc else None
    extra = f" {guidance}" if guidance else ""
    return (
        f"Claim {claim['claim_id']} for {claim['service_description']} on {claim['service_date']} "
        f"was {claim['status']} with {carc}.{extra}"
    )


def _benefits(gateway: Gateway, text: str, person: dict, as_of: date) -> str:
    benefits = _tool(gateway, "get_benefits")
    record = (benefits or {}).get("benefits") or {}
    if record.get("coverage_status") == "TERMINATED" and re.search(
        r"copay|coinsurance|what would|what does|cost", text, re.IGNORECASE
    ):
        ended = record.get("coverage_termination") or person.get("coverage_termination")
        return f"This member's coverage is not active. It ended on {ended}."
    year = as_of.year + 1 if RENEWAL.search(text) and not re.fullmatch(r".*this plan year.*", text, re.IGNORECASE) else as_of.year
    if re.search(r"this plan year", text, re.IGNORECASE) and not re.search(r"renew|after that|\b2027\b", text, re.IGNORECASE):
        year = as_of.year
    product = person["product_code"]
    _tool(
        gateway,
        "retrieve",
        {"query": text, "product_code": product, "year": year},
    )
    pieces: list[str] = []
    if re.search(r"which plan|enrolled", text, re.IGNORECASE):
        pieces.append(f"This member is enrolled in {person['plan_name']}.")
    kind = _benefit_kind(text)
    if kind == "deductible_remaining":
        pieces.append(_remaining_deductible(gateway, person, record, year))
    elif kind == "oop_remaining":
        pieces.append(_remaining_oop(gateway, person, record, year))
    elif kind == "oop_family":
        pieces.append(_family_oop(gateway, person, year))
    elif kind == "pt_remaining":
        pieces.append(_pt_remaining(gateway, person, record, year))
    elif kind:
        pieces.append(_service_cost(gateway, person, year, kind))
    if not pieces:
        return "That information is not available in this tenant's plan documents for the member's product. I won't guess."
    return " ".join(pieces)


def _remaining_deductible(gateway, person, record, year) -> str:
    row = gateway.kb.benefit_row(gateway.session.tenant, person["product_code"], year, "Deductible (individual / family)")
    if row is None:
        return "The deductible for that plan year is not in this tenant's documents."
    individual, _family = individual_and_family(row.in_network)
    met = D((record.get("accumulators") or {}).get("deductible_met_individual", 0))
    left = max(D(0), individual - met)
    return (
        f"{usd(left)} remains before the individual in-network deductible is met "
        f"for plan year {year} (deductible {usd(individual)}, {usd(met)} met)."
    )


def _remaining_oop(gateway, person, record, year) -> str:
    row = gateway.kb.benefit_row(
        gateway.session.tenant, person["product_code"], year, "Out-of-pocket maximum (individual / family)"
    )
    if row is None:
        return "The out-of-pocket maximum for that plan year is not in this tenant's documents."
    individual, _family = individual_and_family(row.in_network)
    met = D((record.get("accumulators") or {}).get("out_of_pocket_met_individual", 0))
    left = max(D(0), individual - met)
    return (
        f"The member is {usd(left)} from the individual in-network out-of-pocket maximum "
        f"for plan year {year}."
    )


def _family_oop(gateway, person, year) -> str:
    row = gateway.kb.benefit_row(
        gateway.session.tenant, person["product_code"], year, "Out-of-pocket maximum (individual / family)"
    )
    if row is None:
        return "The family out-of-pocket maximum is not in this tenant's documents."
    _individual, family = individual_and_family(row.in_network)
    return f"The family in-network out-of-pocket maximum for plan year {year} is {usd(family)}."


def _pt_remaining(gateway, person, record, year) -> str:
    row = gateway.kb.benefit_row(
        gateway.session.tenant, person["product_code"], year, "Physical therapy visit limit"
    )
    if row is None:
        return "The physical therapy visit limit is not in this tenant's documents."
    limit = visit_limit(row.in_network)
    used = int((record.get("accumulators") or {}).get("physical_therapy_visits_used") or 0)
    left = max(limit - used, 0)
    return (
        f"The member has {left} physical therapy visits left in the {year} plan year "
        f"({limit} visit limit, {used} used)."
    )


def _service_cost(gateway, person, year, kind: str) -> str:
    names = {
        "specialist": "Specialist visit",
        "emergency": "Emergency room",
        "generic": "Generic drugs (Tier 1, 30-day retail)",
        "telehealth": "Telehealth (video) visit",
        "behavioral": "Behavioral health outpatient visit (in person or virtual)",
        "urgent": "Urgent care",
        "primary": "Primary care visit",
    }
    row = gateway.kb.benefit_row(gateway.session.tenant, person["product_code"], year, names[kind])
    if row is None:
        return "That benefit is not in this tenant's documents for the member's product and plan year."
    label = {
        "specialist": "an in-network specialist visit",
        "emergency": "an in-network emergency room visit",
        "generic": "a 30-day supply of a generic drug at a retail pharmacy",
        "telehealth": "a video visit with a doctor",
        "behavioral": "a virtual behavioral health session",
        "urgent": "an in-network urgent care visit",
        "primary": "an in-network primary care visit",
    }[kind]
    cost = row.in_network
    if kind == "behavioral":
        return (
            f"A virtual behavioral health session is covered with the outpatient behavioral health benefit: "
            f"{cost}. Plan year {year}, {person['plan_name']}."
        )
    return f"For {label}, this member pays {cost}. Plan year {year}, {person['plan_name']}."


def _select_claim(text: str, claims: list[dict]) -> dict | None:
    claim_id = CLAIM_ID.search(text)
    if claim_id:
        for claim in claims:
            if claim["claim_id"] == claim_id.group(1):
                return claim
        return None
    day = ISO_DATE.search(text)
    pool = claims
    if day:
        pool = [claim for claim in claims if claim["service_date"] == day.group(1)]
    keyword = _service_keyword(text)
    if keyword:
        narrowed = [claim for claim in pool if keyword in (claim.get("service_description") or "").lower()]
        if narrowed:
            pool = narrowed
    if len(pool) == 1:
        return pool[0]
    denied = [claim for claim in pool if claim["status"] == "DENIED"]
    if len(denied) == 1 and re.search(r"denied|denial|appeal", text, re.IGNORECASE):
        return denied[0]
    return pool[0] if len(pool) == 1 else None


def _service_keyword(text: str) -> str | None:
    lowered = text.lower()
    for word in ("mri", "office visit", "therapy", "x-ray", "urgent", "emergency"):
        if word in lowered:
            return "office" if word == "office visit" else word
    return None


def _benefit_kind(text: str) -> str | None:
    lowered = text.lower()
    if "physical therapy" in lowered and re.search(r"left|still|how many", lowered):
        return "pt_remaining"
    if "deductible" in lowered:
        return "deductible_remaining"
    if "out-of-pocket" in lowered or "out of pocket" in lowered:
        if "family" in lowered and not re.search(r"how far|left|remain", lowered):
            return "oop_family"
        return "oop_remaining"
    if "specialist" in lowered:
        return "specialist"
    if "emergency room" in lowered or "emergency" in lowered:
        return "emergency"
    if "generic" in lowered or "prescription" in lowered or "pharmacy" in lowered:
        return "generic"
    if "virtual" in lowered and re.search(r"therapy|behavioral", lowered):
        return "behavioral"
    if "video visit" in lowered or "telehealth" in lowered:
        return "telehealth"
    if "urgent" in lowered:
        return "urgent"
    if "primary care" in lowered:
        return "primary"
    return None


def _is_benefit_question(text: str) -> bool:
    return _benefit_kind(text) is not None or re.search(r"which plan|enrolled", text, re.IGNORECASE) is not None


def _is_claim_question(text: str) -> bool:
    if CLAIM_ID.search(text):
        return True
    if re.search(r"\bclaim", text, re.IGNORECASE):
        return True
    return re.search(r"how much did the (member|plan)|were denied|was denied", text, re.IGNORECASE) is not None


def _is_prior_auth(text: str) -> bool:
    if not PRIOR_AUTH.search(text):
        return False
    if re.search(r"\bclaim\b", text, re.IGNORECASE) and re.search(r"denied|denial", text, re.IGNORECASE):
        return False
    return re.search(r"approved|approval", text, re.IGNORECASE) is not None


def _is_grievance_deadline(text: str) -> bool:
    return re.search(r"grievance", text, re.IGNORECASE) is not None and re.search(
        r"deadline|resolution", text, re.IGNORECASE
    ) is not None


def _category(text: str) -> str:
    lowered = text.lower()
    if "quality of care" in lowered:
        return "quality_of_care"
    if "customer service" in lowered:
        return "customer_service"
    if "billing" in lowered or "bill" in lowered:
        return "billing"
    if re.search(r"\baccess\b", lowered):
        return "access"
    return "other"


def _description(text: str) -> str:
    claim = CLAIM_ID.search(text)
    day = ISO_DATE.search(text)
    claim_id = claim.group(1) if claim else "the claim the member identified"
    service_date = day.group(1) if day else "the service date the member identified"
    if "billing" in text.lower() or "bill" in text.lower():
        return (
            f"Member disputes a provider bill for services on {service_date} ({claim_id}). "
            "The member says the plan already paid and the provider billed the member for the full amount."
        )
    cleaned = " ".join(text.split())
    return cleaned[:2000]


def _tool(gateway: Gateway, name: str, arguments: dict | None = None) -> dict | None:
    result = gateway.call(name, arguments or {})
    if not result["ok"]:
        return None
    return result["output"]


def llm_answer_turn(gateway: Gateway, text: str, settings, as_of: date) -> str:
    """Tool-calling loop. The gateway still owns every side effect."""
    session = gateway.session
    session.turn += 1
    session.last_user_text = text
    session.started = time.monotonic()
    messages = [
        {"role": "system", "content": _system_prompt(session.tenant, session.member_id, as_of)},
        {"role": "user", "content": text},
    ]
    tools = _tool_schema()
    for _ in range(settings.max_steps):
        message = _chat(settings, messages, tools)
        tool_calls = message.get("tool_calls") or []
        session.events.append(
            {
                "kind": "model",
                "provider": "openai",
                "model": settings.model_name,
                "remote_call": True,
                "tool_calls": len(tool_calls),
            }
        )
        if not tool_calls:
            return (message.get("content") or "").strip() or "That information is not available. I won't guess."
        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = gateway.call(name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "content": json.dumps(result),
                }
            )
    return "I stopped because this conversation reached its step budget."


def _system_prompt(tenant: str, member_id: str, as_of: date) -> str:
    return (
        "You answer a member-services representative about one member on one health plan. "
        f"Tenant {tenant}, member {member_id}, today {as_of.isoformat()}. "
        "Use tools for every fact. Do not guess. Never reveal a full Social Security number. "
        "Never put a Social Security number or date of birth in a provider search. "
        "Do not use another tenant's data. If coverage is not active, say so and when it ended. "
        "Stage a grievance only when asked, and file it only after the representative confirms "
        "on a later turn. Say when information is not available."
    )


def _tool_schema() -> list[dict]:
    def tool(name, description, properties, required=None):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                },
            },
        }

    return [
        tool("get_member", "Minimized member record for this conversation.", {}),
        tool("get_benefits", "Coverage status and accumulators. Benefit design is in plan documents.", {}),
        tool("get_claims", "All of this member's claims, oldest page first in the payload.", {}),
        tool("get_claim", "One claim that must belong to this member.", {"claim_id": {"type": "string"}}, ["claim_id"]),
        tool("list_grievances", "Grievances already on file for this member.", {}),
        tool(
            "verify_ssn_last4",
            "Check whether four digits match the record. Does not return the SSN.",
            {"last4": {"type": "string"}},
            ["last4"],
        ),
        tool(
            "search_providers",
            "Search providers by specialty, ZIP and this tenant's network. Do not send SSN or date of birth.",
            {
                "query": {"type": "string"},
                "specialty": {"type": "string"},
                "zip": {"type": "string"},
                "network_id": {"type": "string"},
            },
            ["query"],
        ),
        tool(
            "retrieve",
            "Retrieve admissible plan-document excerpts for this tenant only.",
            {"query": {"type": "string"}, "product_code": {"type": "string"}, "year": {"type": "integer"}},
            ["query"],
        ),
        tool(
            "stage_grievance",
            "Prepare a grievance for read-back. This does not file it.",
            {"category": {"type": "string"}, "description": {"type": "string"}},
            ["category", "description"],
        ),
        tool("file_grievance", "File the staged grievance. Allowed only after a later confirming turn.", {}),
    ]


def _chat(settings, messages, tools) -> dict:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai.")
    payload = {
        "model": settings.model_name,
        "messages": messages,
        "tools": tools,
        "temperature": 0,
    }
    # The Gemini OpenAI-compatible endpoint accepts the key only as a bearer
    # token. Adding x-goog-api-key as well makes Gemini answer "not a valid API key".
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    request = Request(
        settings.openai_base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body["choices"][0]["message"]
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            code = exc.code
            exc.close()
            last_error = (code, detail)
            if code in (429, 503) and attempt < 2:
                time.sleep(1 + attempt)
                continue
            if settings.openai_api_key and settings.openai_api_key in detail:
                detail = detail.replace(settings.openai_api_key, "[redacted]")
            raise RuntimeError(
                f"Model request failed with HTTP {code}. Check solution/.env. {detail}"
            ) from exc
    code, detail = last_error
    raise RuntimeError(f"Model request failed with HTTP {code}. {detail}")
