import copy
import unittest

from tests.test_core import StubClient
from upress_workbench.citations import build_inventory
from upress_workbench.ingest import Block, Chapter
from upress_workbench.review import author_items, load_skill_policy, run_rci, run_sei


class ReviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.chapter = Chapter(
            "CH001",
            "",
            "chapter.txt",
            ".txt",
            0,
            "a" * 64,
            [Block("body.p0002", "The intervention proved the claim (Smith, 2020).", "body", False, 0), Block("body.p0004", "Smith, J. (2021). Title.", "reference_list", True, 1)],
            "high",
            [],
        )
        self.inventory = build_inventory(self.chapter)
        self.rci_policy = load_skill_policy("reference-citation-integrity")
        self.sei_policy = load_skill_policy("scholarly-editorial-integrity")

    def run_pair(self, mutate=None):
        class MutatingStub(StubClient):
            def complete_json(inner, *args):
                result, usage = super().complete_json(*args)
                if mutate:
                    mutate(result, "rci" if "rci" in args[2] else "sei")
                return copy.deepcopy(result), usage

        client = MutatingStub()
        rci, rw = run_rci(client, self.rci_policy, self.chapter, self.inventory, [], "test", "baseline")
        sei, sw = run_sei(client, self.sei_policy, self.chapter, self.inventory, rci, [], "Editor", "public")
        return rci, rw, sei, sw

    def test_legacy_valid_rci_retained_invalid_sei_excerpt_quarantined(self):
        rci, rw, sei, sw = self.run_pair()
        self.assertEqual(len(rci["findings"]), 1)
        self.assertEqual(rci["findings"][0]["status"], "needs_review")
        self.assertEqual(sei["findings"], [])
        self.assertTrue(sw["quarantined_findings"])
        self.assertEqual([i["family"] for i in author_items(rci, rw, sei, sw)], ["RCI"])

    def test_rci_cannot_certify_external_evidence_or_retrieval(self):
        def mutate(result, stage):
            if stage == "rci":
                result["findings"][0]["status"] = "verified"
                result["findings"][0]["evidence"][0]["source_id"] = "FABRICATED-ARTICLE"

        rci, rw, _, _ = self.run_pair(mutate)
        self.assertEqual(rci["findings"], [])
        self.assertTrue(rw["quarantined_findings"])

    def test_positive_scholarly_and_closure_statuses_never_author_facing(self):
        for status in ["verified", "supported_with_scope", "resolved"]:

            def mutate(result, stage, status=status):
                if stage == "sei":
                    result["findings"][0].update(status=status, exact_text="The intervention proved the claim", source_access_status="live")

            rci, rw, sei, sw = self.run_pair(mutate)
            self.assertEqual(sei["findings"], [], status)
            self.assertFalse(any(i["family"] == "SEI" for i in author_items(rci, rw, sei, sw)))

    def test_grounded_source_gap_preserved_but_source_access_machine_controlled(self):
        def mutate(result, stage):
            if stage == "sei":
                result["findings"][0].update(exact_text="The intervention proved the claim", source_access_status="author_supplied")

        rci, rw, sei, sw = self.run_pair(mutate)
        self.assertEqual(sei["findings"][0]["source_access_status"], "inaccessible")
        self.assertEqual(sei["findings"][0]["status"], "source_unavailable")
        self.assertEqual(len(author_items(rci, rw, sei, sw)), 2)

    def test_positive_verdict_cannot_be_laundered_through_signal_prose(self):
        def mutate(result, stage):
            if stage == "sei":
                result["findings"][0].update(exact_text="The intervention proved the claim", status="signal", reason="The source confirms this claim.")

        _, _, sei, sw = self.run_pair(mutate)
        self.assertEqual(sei["findings"], [])
        self.assertTrue(sw["quarantined_findings"])

    def test_verified_local_rci_is_proposal_without_invented_retrieval(self):
        def mutate(result, stage):
            if stage == "rci":
                result["findings"][0]["status"] = "verified"

        rci, _, _, _ = self.run_pair(mutate)
        self.assertEqual(rci["findings"][0]["status"], "needs_review")
        self.assertIsNone(rci["findings"][0]["evidence"][0]["retrieved_at"])

    def test_wrong_locator_even_with_real_excerpt_is_quarantined(self):
        def mutate(result, stage):
            if stage == "rci":
                result["findings"][0]["locator"] = "body.p0004"

        rci, rw, _, _ = self.run_pair(mutate)
        self.assertEqual(rci["findings"], [])
        self.assertEqual(rci["release_status"], "not_ready")
        self.assertTrue(rw["quarantined_findings"])

    def test_provider_metadata_timestamp_comes_only_from_supplied_record(self):
        class MetadataStub(StubClient):
            def complete_json(inner, *args):
                result, usage = super().complete_json(*args)
                result["findings"][0]["evidence"] = [
                    {
                        "source_id": "CROSSREF-10.1234/example",
                        "locator": "https://api.crossref.org/works/10.1234/example",
                        "relevance": "Candidate metadata comparison",
                        "access_status": "available",
                    }
                ]
                return result, usage

        record = {
            "doi": "10.1234/example",
            "status": "candidate_available",
            "provider_locator": "https://api.crossref.org/works/10.1234/example",
            "queried_at": "2026-01-01T00:00:00Z",
        }
        rci, _ = run_rci(MetadataStub(), self.rci_policy, self.chapter, self.inventory, [record], "test", "baseline")
        evidence = rci["findings"][0]["evidence"][0]
        self.assertEqual(evidence["retrieved_at"], record["queried_at"])
        self.assertIn("candidate_provider_metadata", evidence["relevance"])

    def test_guarded_results_conform_to_pinned_schemas_offline(self):
        import json
        from pathlib import Path

        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        def mutate(result, stage):
            if stage == "sei":
                result["findings"][0]["exact_text"] = "The intervention proved the claim"

        rci, _, sei, _ = self.run_pair(mutate)
        for name, result in [("reference-citation-integrity", rci), ("scholarly-editorial-integrity", sei)]:
            root = Path(__file__).parents[1] / "vendor" / name / "schemas"
            schemas = [json.loads(p.read_text(encoding="utf-8")) for p in root.glob("*.schema.json")]
            registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
            schema = json.loads((root / "run-result.schema.json").read_text(encoding="utf-8"))
            Draft202012Validator(schema, registry=registry).validate(result)
