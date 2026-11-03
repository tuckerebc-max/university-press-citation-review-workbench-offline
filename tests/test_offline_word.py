from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree

from upress_workbench.ingest import extract_chapter
from upress_workbench.offline_export import _exact_runs, export_offline_author_packet
from upress_workbench.security import SourceFile, sha256_file

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


class OfflineWordTests(unittest.TestCase):
    def test_concise_entries_keep_action_excerpt_and_record_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.txt"
            path.write_text("A short source.", encoding="utf-8")
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, sha256_file(path)), "CH001")
            finding = {
                "finding_id": "F001",
                "category": "edition_mismatch",
                "location": "body.p0001",
                "exact_text": "long observed text " * 80,
                "author_query": "Please confirm the edition.",
            }
            export_offline_author_packet(chapter, {"deterministic_findings": [finding]}, root / "out")
            doc = Document(root / "out" / "author-review.docx")
            entry = next(i for i, p in enumerate(doc.paragraphs) if p.text == "1. Confirm the edition")
            paragraphs = doc.paragraphs[entry : entry + 5]
            self.assertEqual(paragraphs[1].text, "Please confirm the edition.")
            self.assertLess(len(paragraphs[2].text), 210)
            self.assertIn("Appendix only", paragraphs[3].text)
            self.assertIn("F001", paragraphs[4].text)
            self.assertIn("evidence-ledger.json", paragraphs[4].text)
            self.assertTrue(all(p.paragraph_format.keep_with_next for p in paragraphs[:-1]))
            self.assertFalse(paragraphs[-1].paragraph_format.keep_with_next)
            markdown = (root / "out" / "author-queries.md").read_text(encoding="utf-8")
            self.assertLessEqual(max(map(len, markdown.splitlines())), 100)

    def test_precise_anchor_and_visible_fallback_preserve_structure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.docx"
            doc = Document()
            p = doc.add_paragraph("Before ")
            p.add_run("(Smith, ").bold = True
            p.add_run("2020)").italic = True
            p.add_run(" after.")
            doc.add_table(rows=1, cols=1).cell(0, 0).text = "Table content"
            doc.save(path)
            digest = sha256_file(path)
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, digest), "CH001")

            def finding(fid, exact):
                return {
                    "finding_id": fid,
                    "location": "body.p0001",
                    "exact_text": exact,
                    "author_query": "Please confirm the intended reference.",
                    "evidence": [{"source_id": "MANUSCRIPT-CH001"}],
                    "citation_ids": ["C001"],
                    "reference_ids": [],
                }

            inventory = {
                "deterministic_findings": [finding("F001", "(Smith, 2020)"), finding("F002", "missing")],
                "parser_coverage": "partial",
                "coverage_notes": ["Notes omitted."],
            }
            receipt = export_offline_author_packet(chapter, inventory, root / "out")
            self.assertEqual(receipt["comments_added"], 1)
            self.assertEqual(receipt["appendix_items"], 2)
            self.assertEqual(receipt["anchors"][1]["status"], "appendix_only")
            reviewed = Document(root / "out" / "author-review.docx")
            self.assertEqual(reviewed.paragraphs[0].text, "Before (Smith, 2020) after.")
            self.assertEqual(reviewed.tables[0].cell(0, 0).text, "Table content")
            with zipfile.ZipFile(root / "out" / "author-review.docx") as z:
                tree = etree.fromstring(z.read("word/document.xml"))
                p = tree.find(".//w:body/w:p", NS)
                selected = []
                active = False
                for child in p:
                    if child.tag.endswith("commentRangeStart"):
                        active = True
                    elif child.tag.endswith("commentRangeEnd"):
                        active = False
                    elif active:
                        selected.extend(child.xpath(".//w:t/text()", namespaces=NS))
                self.assertEqual("".join(selected), "(Smith, 2020)")
            content = "\n".join(p.text for p in reviewed.paragraphs)
            self.assertIn("Source support was not evaluated", content)
            self.assertIn("Notes omitted.", content)
            self.assertEqual(sha256_file(path), digest)
            self.assertIn("F002", (root / "out" / "author-queries.md").read_text(encoding="utf-8"))

    def test_repeated_quote_does_not_guess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.txt"
            path.write_text("same same", encoding="utf-8")
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, sha256_file(path)), "CH001")
            receipt = export_offline_author_packet(
                chapter, {"deterministic_findings": [{"finding_id": "F001", "location": "body.p0001", "exact_text": "same", "author_query": "Please review."}]}, root / "out"
            )
            self.assertEqual(receipt["comments_added"], 0)
            self.assertEqual(receipt["anchors"][0]["status"], "appendix_only")

    def test_empty_inventory_does_not_claim_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.txt"
            path.write_text("A chapter with no citations.", encoding="utf-8")
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, sha256_file(path)), "CH001")
            receipt = export_offline_author_packet(chapter, {}, root / "out")
            self.assertFalse(receipt["source_support_evaluated"])
            text = (root / "out" / "author-queries.md").read_text(encoding="utf-8")
            self.assertIn("does not establish completeness", text)
            self.assertIn("Parser coverage: partial", text)

    def test_stale_locator_does_not_attach_to_wrong_paragraph(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.docx"
            doc = Document()
            doc.add_paragraph("First paragraph")
            doc.add_paragraph("Second paragraph")
            doc.save(path)
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, sha256_file(path)), "CH001")
            finding = {"finding_id": "F001", "location": "body.p0001", "exact_text": "Second paragraph", "author_query": "Please review."}
            receipt = export_offline_author_packet(chapter, {"deterministic_findings": [finding]}, root / "out")
            self.assertEqual(receipt["comments_added"], 0)
            self.assertEqual(receipt["appendix_items"], 1)

    def test_markdown_manuscript_fields_render_as_literal_code(self):
        from markdown_it import MarkdownIt

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.txt"
            path.write_text("ordinary", encoding="utf-8")
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, sha256_file(path)), "CH001")
            attack = '<img src="https://example.invalid/tracker">\n![remote](https://example.invalid/image)\n```\n[link](javascript:alert(1))'
            finding = {"finding_id": attack, "location": attack, "exact_text": attack, "author_query": attack}
            export_offline_author_packet(chapter, {"deterministic_findings": [finding], "coverage_notes": [attack]}, root / "out")
            rendered = MarkdownIt("commonmark", {"html": True}).render((root / "out" / "author-queries.md").read_text(encoding="utf-8"))
            self.assertNotIn("<img ", rendered)
            self.assertNotIn("<a ", rendered)
            self.assertIn("&lt;img", rendered)

    def test_text_lines_preserve_whitespace_and_multiline_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.md"
            text = "  indented  \n\n\tcontinued\nlast line  \n"
            path.write_bytes(text.encode("utf-8"))
            chapter = extract_chapter(SourceFile(path, path.name, path.stat().st_size, sha256_file(path)), "CH001")
            finding = {"finding_id": "F001", "location": chapter.blocks[-1].locator, "exact_text": "continued", "author_query": "Review"}
            receipt = export_offline_author_packet(chapter, {"deterministic_findings": [finding]}, root / "out")
            doc = Document(root / "out" / "author-review.docx")
            self.assertEqual([p.text for p in doc.paragraphs[:5]], text.split("\n"))
            self.assertEqual(receipt["comments_added"], 0)

    def test_anchor_reuses_unaffected_runs_and_preserves_attributes(self):
        doc = Document()
        p = doc.add_paragraph()
        before = p.add_run("before ")
        target = p.add_run("prefix TARGET suffix")
        target._r.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rsidR", "00AA0001")
        after = p.add_run(" after")
        before_xml = etree.tostring(before._r)
        after_xml = etree.tostring(after._r)
        selected = _exact_runs(p, "TARGET")
        self.assertEqual(etree.tostring(before._r), before_xml)
        self.assertEqual(etree.tostring(after._r), after_xml)
        self.assertIs(before._r.getparent(), p._p)
        self.assertIs(after._r.getparent(), p._p)
        self.assertEqual(selected[0]._r.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rsidR"), "00AA0001")


if __name__ == "__main__":
    unittest.main()

