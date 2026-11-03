from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml.ns import qn

from upress_workbench.ingest import extract_chapter
from upress_workbench.offline import build_private_review, recover_interrupted, select_source
from upress_workbench.private_paths import private_path
from upress_workbench.security import SafetyError, sha256_file


class PrivateReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        source_root = self.root / "inputs"
        source_root.mkdir()
        self.source = source_root / "chapter.md"
        self.source.write_text("A local observation (One, 2020).\n\n# References\nTwo, T. (2021). Title.\nThree, T. (2022). Title.\n    Continued imprint.", encoding="utf-8-sig")
        self.output = self.root / "packets"
        self.output.mkdir()
        self.digest = sha256_file(self.source)

    def run_review(self):
        return build_private_review(self.source, self.output, self.digest, "Test edition", "Editor")

    def test_private_packet_reconciles_to_source_and_zero_network(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("Offline review attempted network access")

        with patch("socket.socket", side_effect=forbidden), patch("socket.getaddrinfo", side_effect=forbidden):
            packet = self.run_review()
        receipt = json.loads((packet / "run-receipt.json").read_text())
        self.assertEqual(receipt["source_sha256_before"], self.digest)
        self.assertEqual(receipt["source_sha256_after"], sha256_file(self.source))
        self.assertEqual(receipt["source_support"], "not_assessed")
        self.assertEqual(receipt["external_model_calls"], 0)
        self.assertFalse(receipt["publication_authorized"])
        for name, expected in receipt["artifacts_sha256"].items():
            self.assertEqual(sha256_file(packet / name), expected)
        inventory = json.loads((packet / "citation-inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(len(inventory["references"]), 2)
        self.assertTrue(inventory["references"][1]["raw_text"].endswith("Continued imprint."))
        ids = {f["finding_id"] for f in inventory["deterministic_findings"]}
        self.assertTrue(ids)
        ledger = json.loads((packet / "decision-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual({row["finding_id"] for row in ledger["decisions"]}, ids)
        evidence = json.loads((packet / "evidence-ledger.json").read_text(encoding="utf-8"))
        locations = {row["location"]: row["text"] for row in evidence["locations"]}
        review_text = "\n".join(p.text for p in Document(packet / "author-review.docx").paragraphs)
        for row in evidence["findings"]:
            self.assertIn(row["exact_text"], locations[row["location"]])
            self.assertIn(row["finding_id"], review_text)
        self.assertTrue(all(row["decision"] is None for row in ledger["decisions"]))

    def test_repeated_runs_create_independent_packets(self):
        first = self.run_review()
        digest = sha256_file(first / "run-receipt.json")
        second = self.run_review()
        self.assertNotEqual(first, second)
        self.assertEqual(sha256_file(first / "run-receipt.json"), digest)

    def test_failed_and_interrupted_exports_do_not_issue_partial_packets(self):
        for failure in (ValueError("bad content"), KeyboardInterrupt()):
            with patch("upress_workbench.offline.export_offline_author_packet", side_effect=failure), self.assertRaises(type(failure)):
                self.run_review()
            self.assertEqual(list(self.output.iterdir()), [])
            self.assertEqual(sha256_file(self.source), self.digest)

    def test_hard_stop_stage_can_be_identified_and_explicitly_discarded(self):
        # Exit at the export boundary, without executing any Python finally blocks.
        code = (
            "import os, sys; import upress_workbench.offline as o; "
            "o.export_offline_author_packet = lambda *args: os._exit(77); "
            "o.build_private_review(*sys.argv[1:])"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(self.source), str(self.output), self.digest, "v1", "Editor"],
            check=False, timeout=30, cwd=Path(__file__).resolve().parents[1],
        )
        self.assertEqual(proc.returncode, 77)
        [stage] = recover_interrupted(self.output)
        self.assertFalse(any(p.name.startswith("PrivateReview-") for p in self.output.iterdir()))
        marker = self.output / stage / "stage.json"
        saved = marker.read_bytes()
        info = json.loads(saved)
        info["pid"] = os.getpid()
        marker.write_text(json.dumps(info), encoding="utf-8")
        with self.assertRaisesRegex(SafetyError, "active"):
            recover_interrupted(self.output, stage)
        marker.write_bytes(saved)
        extra = self.output / stage / "keep.txt"
        extra.write_text("Operator note", encoding="utf-8")
        with self.assertRaisesRegex(SafetyError, "Unrecognized"):
            recover_interrupted(self.output, stage)
        self.assertTrue(extra.is_file())
        extra.unlink()
        self.assertEqual(recover_interrupted(self.output, stage), [stage])
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(sha256_file(self.source), self.digest)

    def test_preselected_and_midrun_drift_block_packet(self):
        self.source.write_text("Changed", encoding="utf-8")
        with self.assertRaisesRegex(SafetyError, "SHA-256"):
            self.run_review()
        self.digest = sha256_file(self.source)
        from upress_workbench.offline_export import export_offline_author_packet

        def modify_after_export(*args):
            result = export_offline_author_packet(*args)
            self.source.write_text("New draft", encoding="utf-8")
            return result

        with patch("upress_workbench.offline.export_offline_author_packet", side_effect=modify_after_export), self.assertRaisesRegex(SafetyError, "changed"):
            self.run_review()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_private_location_checks_worktrees_aliases_and_source_folder(self):
        with self.assertRaises(SafetyError):
            build_private_review(self.source, self.source.parent, self.digest, "v1", "Editor")
        for path in (self.output, self.source.parent):
            marker = path / ".git"
            marker.write_text("gitdir: external-worktree", encoding="ascii")
            with self.assertRaisesRegex(SafetyError, "Git"):
                self.run_review()
            marker.unlink()
        alias = self.root / "copy.md"
        alias.hardlink_to(self.source)
        with self.assertRaisesRegex(SafetyError, "hard-link"):
            select_source(self.source)
        with self.assertRaises(SafetyError):
            private_path(Path(__file__).resolve().parent)

    def test_malformed_and_unsupported_inputs_do_not_issue_packets(self):
        for name, payload in (("bad.docx", b"invalid"), ("invalid.txt", b"\xff"), ("unsupported.pdf", b"x"), ("empty.md", b"")):
            path = self.source.with_name(name)
            path.write_bytes(payload)
            with self.assertRaises(SafetyError):
                build_private_review(path, self.output, sha256_file(path), "v1", "Editor")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_heading_and_hard_wrapped_body_in_markdown(self):
        self.source.write_text("Two lines of prose\non adjacent lines.\nReferences\nOne, A. (2000). A.\nTwo, B. (2001). B.", encoding="utf-8")
        chapter = extract_chapter(select_source(self.source), "CH001")
        self.assertEqual([b.in_references for b in chapter.blocks], [False, True, True, True])
        self.assertIn("\n", chapter.blocks[0].text)

    def test_merged_word_cell_observed_once_and_oversized_grid_blocked(self):
        path = self.source.with_suffix(".docx")
        doc = Document()
        table = doc.add_table(rows=2, cols=3)
        merged = table.cell(0, 0).merge(table.cell(1, 1))
        merged.text = "Merged observation (One, 2001)"
        table.cell(0, 2).text = "Separate column"
        doc.save(path)
        chapter = extract_chapter(select_source(path), "CH001")
        self.assertEqual([b.text for b in chapter.blocks], [merged.text, "Separate column"])
        self.assertTrue(chapter.blocks[1].locator.endswith("c003.p001"))
        merged._tc.tcPr.gridSpan.set(qn("w:val"), "257")
        doc.save(path)
        with self.assertRaisesRegex(SafetyError, "table grid"):
            extract_chapter(select_source(path), "CH001")
