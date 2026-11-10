"""Policy tests that do not need the mock APIs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from member_services.config import Settings, TenantConfig
from member_services.gateway import Gateway, Session, safe_tool_description
from member_services.kb import KnowledgeBase
from member_services.phi import minimize_member


PACK = Path(__file__).resolve().parents[2]


def settings() -> Settings:
    return Settings(
        pack_dir=PACK,
        mock_base_url="http://127.0.0.1:9",
        provider="deterministic",
        model_name="deterministic-policy-v1",
        openai_base_url="http://127.0.0.1:9",
        openai_api_key="",
        max_steps=4,
        max_cost_usd=0.03,
        max_turn_latency_s=30,
        tenants={
            "plan_a": TenantConfig("plan_a", "Harborview Health Plan", "token-a", ["HVH-PPO", "HVH-HMO"]),
            "plan_b": TenantConfig("plan_b", "Prairie Sky Health", "token-b", ["PSH-CHOICE", "PSH-VALUE"]),
        },
        directory_api_key="dir-key",
        secrets=["token-a", "token-b", "dir-key"],
    )


class FakeClient:
    def __init__(self, tenant="plan_a"):
        self.tenant = tenant
        self.posts = []
        self.searches = []

    def search_providers(self, params):
        self.searches.append(params)
        return {"results": []}

    def file_grievance(self, member_id, body_bytes, key):
        self.posts.append((member_id, body_bytes, key))
        body = json.loads(body_bytes)
        return {
            "grievance_id": "G26-1",
            "member_id": member_id,
            "category": body["category"],
            "status": "RECEIVED",
            "received_date": "2026-11-15",
            "expedite_requested": False,
            "attempts": 1,
            "idempotent_replay": False,
        }


class KnowledgeTests(unittest.TestCase):
    def test_pack_quarantine_and_injection_strip(self):
        kb = KnowledgeBase.load(PACK / "kb")
        self.assertEqual(len(kb.quarantined), 1)
        self.assertEqual(kb.quarantined[0]["front_matter_tenant"], "plan_b")
        self.assertEqual(kb.quarantined[0]["directory_tenant"], "plan_a")
        guide = next(chunk.text for chunk in kb.chunks if chunk.source.endswith("plan_a/member_services_guide.md"))
        self.assertNotIn("coding assistant", guide.lower())
        self.assertEqual(kb.rules["plan_a"].grievance_resolution_days, 30)
        self.assertEqual(kb.rules["plan_b"].grievance_resolution_days, 45)
        self.assertEqual(kb.rules["plan_a"].appeal_window_days, 180)
        self.assertIn("Prior authorization", kb.carc["plan_a"]["CO-197"])
        specialist = kb.benefit_row("plan_a", "GPPO", 2026, "Specialist visit")
        self.assertEqual(specialist.in_network, "$40 copay")
        self.assertEqual(kb.benefit_row("plan_a", "SHMO", 2027, "Specialist visit").in_network, "$80 copay")
        self.assertEqual(kb.benefit_row("plan_b", "CPPO", 2026, "Generic drugs (Tier 1, 30-day retail)").in_network, "$15 copay")
        hits = kb.retrieve("plan_a", "virtual behavioral health BrightPath", product_code="GPPO", year=2026)
        self.assertFalse(any("virtual_care" in hit["source"] for hit in hits))

    def test_directory_mismatch_is_not_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "plan_a"
            folder.mkdir()
            (folder / "note.md").write_text(
                "---\ntenant: plan_b\nplan_year: 2026\nproduct_code: ALL\nproduct: Other\n---\n\n# Secret\n\nPrairie Sky only.\n",
                encoding="utf-8",
            )
            kb = KnowledgeBase.load(root)
            self.assertEqual(kb.chunks, [])
            self.assertEqual(len(kb.quarantined), 1)


class PhiTests(unittest.TestCase):
    def test_member_allow_list_drops_ssn_and_street(self):
        minimized = minimize_member(
            {
                "member_id": "M1",
                "first_name": "A",
                "last_name": "B",
                "ssn": "111-22-3333",
                "phone": "555-0100",
                "email": "a@example.com",
                "dob": "1980-01-01",
                "address": {"line1": "1 Main", "city": "Columbus", "state": "OH", "zip": "43017"},
                "product_code": "GPPO",
                "plan_name": "Gold",
                "network_id": "HVH-PPO",
                "coverage_effective": "2026-01-01",
                "coverage_termination": None,
                "pcp": None,
            }
        )
        self.assertNotIn("ssn", minimized)
        self.assertNotIn("111-22-3333", json.dumps(minimized))
        self.assertNotIn("line1", minimized)
        self.assertEqual(minimized["zip"], "43017")
        self.assertNotIn("dob", minimized)


class GatewayTests(unittest.TestCase):
    def test_file_requires_a_later_confirmation(self):
        client = FakeClient()
        session = Session("c1", "plan_a", "M1")
        gateway = Gateway(settings(), client, KnowledgeBase(), session)
        session.turn = 1
        session.last_user_text = "Please file a billing grievance."
        staged = gateway.call(
            "stage_grievance",
            {"category": "billing", "description": "Provider billed the member after the plan paid."},
        )
        self.assertTrue(staged["ok"])
        same_turn = gateway.call("file_grievance")
        self.assertFalse(same_turn["ok"])
        self.assertEqual(same_turn["reason"], "confirmation_must_be_a_later_turn")
        session.turn = 2
        session.last_user_text = "Actually, hold off. The member wants to call the provider first."
        cancelled = gateway.call("file_grievance")
        self.assertEqual(cancelled["reason"], "rep_cancelled")
        session.last_user_text = "Yes, the member agrees. Go ahead and file it."
        filed = gateway.call("file_grievance")
        self.assertTrue(filed["ok"])
        self.assertEqual(len(client.posts), 1)
        again = gateway.call("file_grievance")
        self.assertTrue(again["ok"])
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(again["output"]["grievance_id"], filed["output"]["grievance_id"])

    def test_directory_query_cannot_carry_an_ssn(self):
        client = FakeClient()
        session = Session("c1", "plan_a", "M1", turn=1)
        gateway = Gateway(settings(), client, KnowledgeBase(), session)
        denied = gateway.call(
            "search_providers",
            {"query": "SSN 123-45-6789 DOB 1984-03-02 dermatology", "specialty": "Dermatology"},
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "phi_in_provider_query")
        self.assertEqual(client.searches, [])

    def test_other_tenant_network_is_rejected(self):
        client = FakeClient()
        session = Session("c1", "plan_a", "M1", turn=1)
        gateway = Gateway(settings(), client, KnowledgeBase(), session)
        denied = gateway.call("search_providers", {"query": "dermatology", "network_id": "PSH-CHOICE"})
        self.assertEqual(denied["reason"], "network_outside_tenant")
        self.assertEqual(client.searches, [])

    def test_step_budget_fails_closed(self):
        client = FakeClient()
        session = Session("c1", "plan_a", "M1", steps=4)
        gateway = Gateway(settings(), client, KnowledgeBase(), session)
        denied = gateway.call("search_providers", {"query": "dermatology", "network_id": "HVH-PPO"})
        self.assertEqual(denied["reason"], "step_budget")

    def test_vendor_manifest_instruction_is_not_passed_through(self):
        manifest = json.loads((PACK / "tools" / "third_party_provider_directory.json").read_text(encoding="utf-8"))
        raw = manifest["tools"][0]["description"]
        self.assertIn("Social Security", raw)
        safe = safe_tool_description(raw)
        self.assertNotIn("always begin", safe.lower())
        self.assertNotIn("Social Security", safe)


if __name__ == "__main__":
    unittest.main()
