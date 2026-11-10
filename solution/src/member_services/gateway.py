"""Tool gateway. Every tool call, including retrieval, comes through here.

The model cannot choose the tenant, attach a credential, or skip confirmation.
Write calls also carry a gateway-owned idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from member_services.api_client import ApiError, IsolationError, MemberClient
from member_services.config import Settings
from member_services.kb import KnowledgeBase
from member_services.phi import minimize


class PolicyDenied(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

SSN_QUERY = re.compile(
    r"\bssn\b|social security|\bdob\b|date of birth|\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b",
    re.IGNORECASE,
)
CANCEL = re.compile(
    r"hold off|do not|don't|cancel|never mind|not now|call the provider",
    re.IGNORECASE,
)
CONFIRM = re.compile(
    r"\b(yes|confirmed|go ahead|file it|agrees|agreed)\b",
    re.IGNORECASE,
)
CATEGORIES = {"billing", "quality_of_care", "access", "customer_service", "other"}

TOOL_COST = {
    "get_member": 0.0002,
    "get_benefits": 0.0002,
    "get_claims": 0.0010,
    "get_claim": 0.0008,
    "list_grievances": 0.0002,
    "verify_ssn_last4": 0.0002,
    "search_providers": 0.0003,
    "retrieve": 0.0001,
    "stage_grievance": 0.0001,
    "file_grievance": 0.0005,
}


@dataclass
class PendingGrievance:
    category: str
    description: str
    body_bytes: bytes
    idempotency_key: str
    staged_on_turn: int


@dataclass
class Session:
    conversation_id: str
    tenant: str
    member_id: str
    turn: int = 0
    last_user_text: str = ""
    pending: PendingGrievance | None = None
    filed: dict | None = None
    steps: int = 0
    cost_usd: float = 0.0
    started: float = field(default_factory=time.monotonic)
    events: list[dict] = field(default_factory=list)


class Gateway:
    def __init__(self, settings: Settings, client: MemberClient, kb: KnowledgeBase, session: Session):
        self.settings = settings
        self.client = client
        self.kb = kb
        self.session = session
        if client.tenant != session.tenant:
            raise IsolationError("Client tenant and session tenant differ.")

    def call(self, name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        price = TOOL_COST.get(name, 0.001)
        budget = self._budget(price)
        if budget:
            return self._finish(name, False, "deny", budget, {})
        member_argument = arguments.get("member_id")
        if member_argument and member_argument != self.session.member_id:
            return self._finish(name, False, "deny", "member_mismatch", {})
        try:
            output = self._dispatch(name, arguments)
        except PolicyDenied as exc:
            return self._finish(name, False, "deny", exc.reason, {})
        except IsolationError:
            return self._finish(name, False, "deny", "tenant_isolation", {})
        except ApiError as exc:
            return self._finish(name, False, "error", exc.code, {"status": exc.status})
        self.session.steps += 1
        self.session.cost_usd = round(self.session.cost_usd + price, 6)
        return self._finish(name, True, "allow", "ok", output)

    def _dispatch(self, name: str, arguments: dict) -> dict:
        member_id = self.session.member_id
        if name == "get_member":
            found = self.client.get_member(member_id)
            return {"found": found is not None, "member": found}
        if name == "get_benefits":
            found = self.client.get_benefits(member_id)
            return {"found": found is not None, "benefits": found}
        if name == "get_claims":
            return {"claims": self.client.list_claims(member_id)}
        if name == "get_claim":
            claim = self.client.get_claim(str(arguments["claim_id"]))
            if claim and claim.get("member_id") != member_id:
                return {"found": False, "claim": None}
            return {"found": claim is not None, "claim": claim}
        if name == "list_grievances":
            rows = [
                item
                for item in self.client.list_grievances(member_id)
                if item.get("member_id") == member_id
            ]
            return {"grievances": [_public_grievance(item) for item in rows]}
        if name == "verify_ssn_last4":
            return self.client.verify_ssn_last4(member_id, str(arguments.get("last4", "")))
        if name == "search_providers":
            return self._search_providers(arguments)
        if name == "retrieve":
            hits = self.kb.retrieve(
                self.session.tenant,
                str(arguments.get("query", "")),
                arguments.get("product_code"),
                arguments.get("year"),
            )
            return {"hits": hits}
        if name == "stage_grievance":
            return self._stage(arguments)
        if name == "file_grievance":
            return self._file()
        return {"error": "unknown_tool"}

    def _search_providers(self, arguments: dict) -> dict:
        blob = " ".join(str(value) for value in arguments.values())
        if SSN_QUERY.search(blob):
            raise PolicyDenied("phi_in_provider_query")
        network = arguments.get("network_id") or ""
        allowed = set(self.settings.tenants[self.session.tenant].networks)
        if network and network not in allowed:
            raise PolicyDenied("network_outside_tenant")
        params = {
            "query": arguments.get("query") or arguments.get("specialty") or "provider",
            "specialty": arguments.get("specialty"),
            "zip": arguments.get("zip"),
            "network_id": network or None,
            "limit": min(int(arguments.get("limit", 10)), 10),
        }
        if SSN_QUERY.search(str(params["query"])):
            raise PolicyDenied("phi_in_provider_query")
        payload = self.client.search_providers(params)
        results = []
        for item in payload.get("results", []):
            if network and network not in item.get("networks", []):
                continue
            results.append(
                {
                    "name": item.get("name"),
                    "npi": item.get("npi"),
                    "specialty": item.get("specialty"),
                    "zip": (item.get("address") or {}).get("zip"),
                    "networks": [net for net in item.get("networks", []) if net in allowed],
                    "accepting_new_patients": item.get("accepting_new_patients"),
                }
            )
        return {"results": results}

    def _stage(self, arguments: dict) -> dict:
        category = arguments.get("category")
        description = " ".join(str(arguments.get("description", "")).split())
        if category not in CATEGORIES:
            raise ApiError(422, "invalid_category")
        if not description or len(description) > 2000:
            raise ApiError(422, "invalid_description")
        body = {
            "category": category,
            "description": description,
            "expedite_requested": False,
        }
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key_src = f"{self.session.tenant}|{self.session.member_id}|{category}|{description}"
        key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:32]
        self.session.pending = PendingGrievance(
            category=category,
            description=description,
            body_bytes=raw,
            idempotency_key=key,
            staged_on_turn=self.session.turn,
        )
        return {
            "staged": True,
            "filed": False,
            "category": category,
            "description": description,
            "read_back_required": True,
        }

    def _file(self) -> dict:
        pending = self.session.pending
        if pending is None:
            raise PolicyDenied("no_staged_grievance")
        if self.session.turn <= pending.staged_on_turn:
            raise PolicyDenied("confirmation_must_be_a_later_turn")
        if CANCEL.search(self.session.last_user_text):
            raise PolicyDenied("rep_cancelled")
        if not CONFIRM.search(self.session.last_user_text):
            raise PolicyDenied("confirmation_required")
        if self.session.filed is not None:
            return dict(self.session.filed)
        record = self.client.file_grievance(
            self.session.member_id,
            pending.body_bytes,
            pending.idempotency_key,
        )
        public = _public_grievance(record)
        public["attempts"] = record.get("attempts")
        public["idempotent_replay"] = record.get("idempotent_replay")
        self.session.filed = public
        return public

    def _budget(self, price: float) -> str | None:
        if self.session.steps >= self.settings.max_steps:
            return "step_budget"
        if self.session.cost_usd + price > self.settings.max_cost_usd + 1e-9:
            return "cost_budget"
        if time.monotonic() - self.session.started > self.settings.max_turn_latency_s:
            return "latency_budget"
        return None

    def _finish(self, name: str, ok: bool, decision: str, reason: str, output: dict) -> dict:
        safe_output = minimize(output, self.settings.secrets)
        event = {
            "kind": "gateway",
            "tool": name,
            "decision": decision,
            "reason": reason,
            "output": safe_output,
        }
        self.session.events.append(event)
        return {"ok": ok, "decision": decision, "reason": reason, "output": safe_output}


def confirmed(text: str) -> bool:
    return CONFIRM.search(text) is not None and CANCEL.search(text) is None


def cancelled(text: str) -> bool:
    return CANCEL.search(text) is not None


def safe_tool_description(description: str) -> str:
    if SSN_QUERY.search(description or ""):
        return (
            "Search providers by specialty, ZIP and this tenant's network. "
            "Do not include member identifiers in the query."
        )
    return description


def _public_grievance(record: dict) -> dict:
    return {
        "grievance_id": record.get("grievance_id"),
        "member_id": record.get("member_id"),
        "category": record.get("category"),
        "status": record.get("status"),
        "received_date": record.get("received_date"),
        "expedite_requested": record.get("expedite_requested"),
    }
