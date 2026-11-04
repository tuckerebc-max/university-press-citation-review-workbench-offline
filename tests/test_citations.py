import unittest

from upress_workbench.citations import build_inventory
from upress_workbench.ingest import Block, Chapter


def inventory(body, refs):
    blocks = [Block("body.p0001", body, "body", False, 0)]
    blocks += [Block(f"body.p{i + 2:04d}", t, "reference_list", True, i + 1) for i, t in enumerate(refs)]
    return build_inventory(Chapter("CH001", "", "", ".txt", 0, "", blocks, "high", []))


class CitationRegressionTests(unittest.TestCase):
    def test_narrative_spans_keep_original_author_whitespace(self):
        cases = [
            ("Smith and Jones (2020)", "Smith and Jones (2020)", 0, "Smith, A., & Jones, B. (2020). Title."),
            ("Smith   and   Jones (2020)", "Smith   and   Jones (2020)", 0, "Smith, A., & Jones, B. (2020). Title."),
            ("Smith\tand\tJones (2020)", "Smith\tand\tJones (2020)", 0, "Smith, A., & Jones, B. (2020). Title."),
            ("Smith\nand\nJones (2020)", "Smith\nand\nJones (2020)", 0, "Smith, A., & Jones, B. (2020). Title."),
            ("Recent work by Smith   and   Jones (2020).", "Smith   and   Jones (2020)", 15, "Smith, A., & Jones, B. (2020). Title."),
            ("Recent work by van   der   Waals (2020).", "van   der   Waals (2020)", 15, "van der Waals, A. (2020). Title."),
            ("See World   Health\tOrganization (2020).", "World   Health\tOrganization (2020)", 4, "World Health Organization. (2020). Report."),
            ("Smith,   Jones,   and   Li (2020).", "Smith,   Jones,   and   Li (2020)", 0, "Smith, A., Jones, B., & Li, C. (2020). Title."),
        ]
        for body, exact, start, reference in cases:
            with self.subTest(body=body):
                [citation] = inventory(body, [reference])["citations"]
                self.assertEqual(citation["candidate_reference_ids"], ["R001"])
                self.assertEqual(citation["char_start"], start)
                self.assertEqual(citation["char_end"], start + len(exact))
                self.assertEqual(citation["raw_text"], exact)
                [finding] = [f for f in inventory(body, [])["deterministic_findings"] if f["category"] == "missing_reference"]
                self.assertEqual(finding["exact_text"], exact)
                self.assertEqual(body[finding["char_start"]:finding["char_end"]], exact)

    def test_caption_with_markdown_emphasis_matches_only_local_target(self):
        for caption in ("**Table 4.** Result", "__Table 4__: Result", "### Table 4: Result"):
            result = inventory(f"See Table 4 and Table 5.\n{caption}", [])
            missing = [f["exact_text"] for f in result["deterministic_findings"] if f["category"] == "cross_reference_missing_target"]
            self.assertEqual(missing, ["Table 5"])

    def test_title_possessive_after_preposition_retains_author_span(self):
        body = "Compare the editions of Rao\u2019s *A public atlas* (2018, 4th ed.)."
        result = inventory(body, ["Rao, T. (2018). A public atlas (3rd ed.). Publisher."])
        [finding] = [f for f in result["deterministic_findings"] if f["category"] == "edition_mismatch"]
        self.assertEqual(finding["exact_text"], "Rao\u2019s *A public atlas* (2018, 4th ed.)")
        self.assertEqual(body[finding["char_start"]:finding["char_end"]], finding["exact_text"])
    def test_three_author_narratives_and_group_conjunction_identity(self):
        result = inventory(
            "Smith, Jones, and Li (2020). Research and Development Council (2021).",
            ["Smith, A., Jones, B., & Li, C. (2020). Title.", "Research and Development Council. (2021). Report."],
        )
        self.assertEqual([c["candidate_reference_ids"] for c in result["citations"]], [["R001"], ["R002"]])
        self.assertEqual(result["references"][1]["authors_or_group"], ["research and development council"])

    def test_grouped_findings_have_individual_source_spans(self):
        body = "See (Smith, 2020; Jones, 2021)."
        result = inventory(body, [])
        self.assertEqual([f["exact_text"] for f in result["deterministic_findings"]], ["Smith, 2020", "Jones, 2021"])
        for finding in result["deterministic_findings"]:
            self.assertEqual(body[finding["char_start"] : finding["char_end"]], finding["exact_text"])

    def test_possessive_title_edition_yields_local_mismatch(self):
        result = inventory("Smith's *A Book Title* (2020, 2nd ed.)", ["Smith, A. (2020). A Book Title (3rd ed.)."])
        self.assertTrue(any(f["category"] == "edition_mismatch" for f in result["deterministic_findings"]))
        result = inventory("Smith's *Different Title* (2020, 2nd ed.)", ["Smith, A. (2020). A Book Title (3rd ed.)."])
        self.assertTrue(any(f["category"] == "edition_identity_uncertainty" for f in result["deterministic_findings"]))

    def test_explosive_ambiguous_candidate_graph_stops_explicitly(self):
        from upress_workbench.security import SafetyError

        with self.assertRaisesRegex(SafetyError, "candidate"):
            inventory("(Smith, 2020) " * 2000, ["Smith, A. (2020). Same title."] * 2000)

    def test_numeric_style_does_not_flood_orphan_queries(self):
        result = inventory("Sources [1\u20133].", ["Smith, A. (2020). One.", "Jones, B. (2021). Two."])
        self.assertFalse(any(f["category"] == "orphan_reference" for f in result["deterministic_findings"]))
        self.assertTrue(all(row["relationship_status"] == "not_checked" for row in result["ledger_entries"]))
        import json
        from pathlib import Path

        import jsonschema

        schema = json.loads((Path(__file__).parents[1] / "vendor/reference-citation-integrity/schemas/ledger-entry.schema.json").read_text(encoding="utf-8"))
        for row in result["ledger_entries"]:
            jsonschema.validate(row, schema)

    def test_no_reference_heading_yields_inventory_query(self):
        chapter = Chapter(
            "CH001",
            "",
            "",
            ".txt",
            0,
            "",
            [Block("body.p0001", "(Smith, 2020; Jones, 2021)", "body", False, 0)],
            "partial",
            ["No explicit References or Bibliography heading was detected."],
        )
        result = build_inventory(chapter)
        self.assertEqual([f["category"] for f in result["deterministic_findings"]], ["reference_inventory_unavailable"])

    def test_initials_compound_and_international_names(self):
        for author, ref in [("Smith", "Smith, J."), ("van der Waals", "van der Waals, J."), ("\u738b", "\u738b, \u5c0f\u660e"), ("O\u2019Neill", "O\u2019Neill, A.")]:
            with self.subTest(author=author):
                result = inventory(f"({author}, 2020)", [f"{ref} (2020). A title."])
                self.assertEqual(result["citations"][0]["candidate_reference_ids"], ["R001"])

    def test_organization_suffix_and_narrative(self):
        result = inventory(
            "World Health Organization (2020a) reports this (World Health Organization, 2020b).",
            ["World Health Organization. (2020a). One.", "World Health Organization. (2020b). Two."],
        )
        self.assertEqual([c["candidate_reference_ids"] for c in result["citations"]], [["R001"], ["R002"]])

    def test_no_date_and_multiple_years(self):
        result = inventory("(Smith, n.d.; Jones, 2019, 2020)", ["Smith, J. (n.d.). One.", "Jones, A. (2019). Two.", "Jones, A. (2020). Three."])
        self.assertEqual([c["candidate_reference_ids"] for c in result["citations"]], [["R001"], ["R002"], ["R003"]])

    def test_ambiguity_doi_and_missing_year_are_findings(self):
        result = inventory(
            "(Smith, 2020; Absent, 2021)",
            ["Smith, A. (2020). One. https://doi.org/10.1234/ABC(12).", "Smith, B. (2020). Two. doi:10.1234/abc(12)", "Jones, J. Title without date. 2nd ed."],
        )
        categories = {f["category"] for f in result["deterministic_findings"]}
        self.assertTrue({"ambiguous_match", "missing_reference", "orphan_reference", "duplicate_doi", "missing_year", "edition_version_uncertainty"} <= categories)
        self.assertEqual(result["dois"], ["10.1234/abc(12)"])
        self.assertEqual(result["citations"][0]["paragraph_index"], 0)

    def test_coverage_and_cross_reference_defects(self):
        result = inventory("See Figure 9. Error! Reference source not found. Sources [1\u20133].", [])
        categories = {f["category"] for f in result["deterministic_findings"]}
        self.assertIn("cross_reference_defect", categories)
        self.assertIn("unsupported_style", categories)
        self.assertEqual(result["parser_coverage"], "partial")

    def test_year_only_parentheses_are_not_citations(self):
        result = inventory("The survey (2020) has 2021 participants.", [])
        self.assertEqual(result["citations"], [])

    def test_suffix_uncertainty_and_exact_source_spans(self):
        body = "As Smith (2020) argues (Jones, 2021, pp. 2\u20134)."
        result = inventory(body, ["Smith, A. (2020a). One.", "Smith, A. (2020b). Two."])
        self.assertEqual(result["citations"][0]["candidate_reference_ids"], ["R001", "R002"])
        self.assertEqual(result["citations"][1]["locator"], "pp. 2\u20134")
        self.assertEqual(result["ledger_entries"][0]["relationship_status"], "ambiguous_match")
        for finding in result["deterministic_findings"]:
            if finding["location"] == "body.p0001":
                self.assertEqual(body[finding["char_start"] : finding["char_end"]], finding["exact_text"])

    def test_doi_raw_preserved_and_nd_spacing(self):
        result = inventory("(Smith, N. D.)", ["Smith, J. (n.d.). One. https://doi.org/10.1234/ABC%2812%29."])
        self.assertEqual(result["citations"][0]["candidate_reference_ids"], ["R001"])
        self.assertEqual(result["references"][0]["identifiers"][0]["raw"], "10.1234/ABC%2812%29.")

    def test_narrative_after_prose_and_et_al(self):
        result = inventory("Recent work by Smith (2020) and Jones et al. (2021) agrees.", ["Smith, A. (2020). One.", "Jones, B., & Li, Q. (2021). Two."])
        self.assertEqual([c["candidate_reference_ids"] for c in result["citations"]], [["R001"], ["R002"]])

    def test_explicit_edition_mismatch_and_control(self):
        result = inventory("(Smith, 2020, 2nd ed.; Jones, 2021, 3rd edition)", ["Smith, A. (2020). Title (3rd ed.).", "Jones, B. (2021). Other (3rd edition)."])
        mismatches = [f for f in result["deterministic_findings"] if f["category"] == "edition_mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["reference_ids"], ["R001"])

    def test_caption_targets_controls_and_external_scope(self):
        body = "See Figure 2.1 and Table 9. See Figure 8 in Chapter 3.\nFigure 2.1: Sample caption."
        result = inventory(body, [])
        missing = [f for f in result["deterministic_findings"] if f["category"] == "cross_reference_missing_target"]
        self.assertEqual([f["exact_text"] for f in missing], ["Table 9"])
        self.assertIn("within this chapter", missing[0]["author_query"])

    def test_large_repetitive_text_and_reference_inventory(self):
        result = inventory("ordinary words " * 22000, [])
        self.assertEqual(result["citations"], [])
        refs = [f"Author{i}, A. (2020). Title {i}." for i in range(2000)]
        body = " ".join(f"(Author{i}, 2020)" for i in range(2000))
        result = inventory(body, refs)
        self.assertEqual(len(result["citations"]), 2000)
        self.assertEqual(result["unmatched_citation_ids"], [])

    def test_two_authors_narrative_edition_and_need_markers(self):
        body = "Smith and Jones (2020, 2nd ed., p. 4) agrees (Smith & Jones, 2020). [citation needed] [reference needed]"
        result = inventory(body, ["Smith, A., & Jones, B. (2020). Title (3rd ed.)."])
        self.assertEqual([c["candidate_reference_ids"] for c in result["citations"]], [["R001"], ["R001"]])
        self.assertEqual(result["citations"][0]["locator"], "p. 4")
        mismatch = next(f for f in result["deterministic_findings"] if f["category"] == "edition_mismatch")
        self.assertEqual([e["location"] for e in mismatch["evidence"]], ["body.p0001", "body.p0002"])
        self.assertEqual(len([f for f in result["deterministic_findings"] if f["category"] == "missing_citation_marker"]), 2)
