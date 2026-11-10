"""Conversation service used by the CLI, the HTTP API and the evaluation harness."""

from __future__ import annotations

import time
import uuid
from datetime import date

from member_services.agent import answer_turn, llm_answer_turn
from member_services.api_client import MemberClient
from member_services.config import Settings
from member_services.gateway import Gateway, Session
from member_services.kb import KnowledgeBase
from member_services.phi import scrub_text


class Assistant:
    def __init__(self, settings: Settings, kb: KnowledgeBase | None = None):
        self.settings = settings
        self.kb = kb or KnowledgeBase.load(settings.kb_dir)
        self.sessions: dict[str, Session] = {}
        self.clients: dict[str, MemberClient] = {}
        self.as_of = self._clock()

    def _clock(self) -> date:
        client = self._client(next(iter(self.settings.tenants)))
        payload = client.health()
        return date.fromisoformat(payload["clock"])

    def _client(self, tenant: str) -> MemberClient:
        if tenant not in self.settings.tenants:
            raise KeyError(tenant)
        if tenant not in self.clients:
            config = self.settings.tenants[tenant]
            self.clients[tenant] = MemberClient(
                self.settings.mock_base_url,
                tenant,
                config.token,
                self.settings.directory_api_key,
            )
        return self.clients[tenant]

    def open(self, tenant: str, member_id: str) -> Session:
        if tenant not in self.settings.tenants:
            raise KeyError(tenant)
        session = Session(conversation_id=uuid.uuid4().hex, tenant=tenant, member_id=member_id)
        self.sessions[session.conversation_id] = session
        return session

    def turn(self, conversation_id: str, text: str) -> dict:
        session = self.sessions[conversation_id]
        gateway = Gateway(self.settings, self._client(session.tenant), self.kb, session)
        if self.settings.provider == "deterministic":
            reply = answer_turn(gateway, text, self.as_of)
            model = self.settings.model_name
        elif self.settings.provider == "openai":
            reply = llm_answer_turn(gateway, text, self.settings, self.as_of)
            model = self.settings.model_name
        else:
            raise RuntimeError(f"Unknown MODEL_PROVIDER {self.settings.provider!r}.")
        reply = scrub_text(reply, self.settings.secrets)
        latency_ms = round((time.monotonic() - session.started) * 1000)
        session.events.append({"kind": "reply", "text": reply, "model": model, "latency_ms": latency_ms})
        return {
            "conversation_id": conversation_id,
            "reply": reply,
            "model": model,
            "tenant": session.tenant,
            "member_id": session.member_id,
            "steps": session.steps,
            "cost_usd": session.cost_usd,
            "latency_ms": latency_ms,
            "trace": session.events,
        }
