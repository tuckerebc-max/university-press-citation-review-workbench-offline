from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import unquote

from .ingest import Chapter
from .security import SafetyError

YEAR_RE = re.compile(r"\b((?:1[5-9]|20)\d{2}[a-z]?|n\.\s*d\.)(?!\w)", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"]+", re.IGNORECASE)
EDITION_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:ed\.|edition\b)", re.I)
TARGET_RE = re.compile(r"\b(Figure|Table)\s+(\d+(?:\.\d+)*[a-z]?)\b", re.I)
CAPTION_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?[*_]{0,2}(Figure|Table)\s+(\d+(?:\.\d+)*[a-z]?)[*_]{0,2}\s*[.:\u2014-][*_]{0,2}[ \t]+\S", re.I | re.M)
NUMERIC_RE = re.compile(r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\]")
_PAGE_NUMBER = r"\d+\w*(?:\s*[\u2013-]\s*\d+\w*)?"
LOCATOR_RE = re.compile(
    rf"\b(?:pp\.\s*{_PAGE_NUMBER}(?:\s*,\s*{_PAGE_NUMBER})*|(?:p\.|para\.|chapter|table|figure)\s*\d+[\w\u2013-]*)",
    re.I,
)


def _citation_years(text: str) -> list[re.Match[str]]:
    """Keep dates outside explicit locator spans, retaining original offsets."""
    locators = [match.span() for match in LOCATOR_RE.finditer(text)]
    return [
        year for year in YEAR_RE.finditer(text)
        if not any(start <= year.start() and year.end() <= end for start, end in locators)
    ]


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in value.casefold()).split())


def _first_author(value: str) -> str:
    value = re.sub(r"\bet\s+al\.?", "", value, flags=re.IGNORECASE).strip()
    value = re.split(r"[,;]", value, maxsplit=1)[0]
    tokens = _normalized(value).split()
    return " ".join(tokens)


def normalize_doi(value: str) -> str:
    """Normalize syntax only; preserve balanced suffix parentheses."""
    value = unquote(value).strip().rstrip(".,;:")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value.casefold()


def _date_key(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _title_key(value: str | None) -> str:
    # Compare only observed title text, excluding a printed numeric edition.
    # Publisher, title variations and translations are not inferred.
    value = re.split(r"\(?\b\d+(?:st|nd|rd|th)?\s*(?:ed\b\.?|edition\b)", value or "", maxsplit=1, flags=re.I)[0]
    return _normalized(value)


def _findings(chapter: Chapter, citations: list[dict], references: list[dict]) -> list[dict]:
    findings = []
    blocks = {b.locator: b for b in chapter.blocks}
    reference_by_id = {r["reference_id"]: r for r in references}
    numeric_coverage = any(NUMERIC_RE.search(b.text) for b in chapter.blocks if not b.in_references)
    reference_inventory_unavailable = not references and any("no explicit" in note.casefold() and "heading" in note.casefold() for note in chapter.coverage_notes)
    captions: dict[tuple[str, str], list[str]] = {}
    for block in chapter.blocks:
        if not block.in_references:
            for caption in CAPTION_RE.finditer(block.text):
                captions.setdefault((caption[1].casefold(), caption[2].casefold()), []).append(block.locator)

    def add(category, record, query, citation_ids=None, reference_ids=None):
        block = blocks[record["location"]]
        exact = record.get("raw_text", block.text)
        start = record.get("char_start", block.text.find(exact))
        evidence = [
            {
                "source": "manuscript",
                "location": record["location"],
                "text": exact,
                "char_start": start if start >= 0 else None,
                "char_end": record.get("char_end", start + len(exact) if start >= 0 else None),
            }
        ]
        for rid in reference_ids or []:
            ref = reference_by_id[rid]
            if ref["location"] != record["location"] or ref["raw_text"] != exact:
                evidence.append(
                    {"source": "manuscript", "reference_id": rid, "location": ref["location"], "text": ref["raw_text"], "char_start": 0, "char_end": len(ref["raw_text"])}
                )
        findings.append(
            {
                "finding_id": f"PARSE-{chapter.chapter_id}-{len(findings) + 1:03d}",
                "category": category,
                "location": record["location"],
                "paragraph_index": block.paragraph_index,
                "char_start": start if start >= 0 else None,
                "char_end": record.get("char_end", start + len(exact) if start >= 0 else None),
                "exact_text": exact,
                "citation_ids": citation_ids or [],
                "reference_ids": reference_ids or [],
                "evidence": evidence,
                "author_query": query,
                "status": "needs_human_review",
            }
        )

    for citation in citations:
        candidates = citation["candidate_reference_ids"]
        if citation.get("title_identity_uncertain"):
            add(
                "edition_identity_uncertainty",
                citation,
                "Please identify the reference and edition intended by this possessive author/title/date expression. Its title/author identity was not reliably established automatically; compare the source consulted with the reference edition.",
                [citation["citation_id"]],
                candidates,
            )
            continue
        if not candidates and not reference_inventory_unavailable:
            add(
                "missing_reference",
                citation,
                "Please supply or identify the reference corresponding to this citation; no local author-date candidate was found.",
                [citation["citation_id"]],
            )
        elif len(candidates) > 1 or citation.get("match_uncertain"):
            add("ambiguous_match", citation, "Please identify the intended reference and confirm any author/year disambiguation.", [citation["citation_id"]], candidates)
        if len(candidates) == 1 and not citation.get("match_uncertain"):
            reference = reference_by_id[candidates[0]]
            if citation.get("edition") and reference.get("edition") and citation["edition"] != reference["edition"]:
                add(
                    "edition_mismatch",
                    citation,
                    f"The citation specifies edition {citation['edition']}, while its local reference specifies edition {reference['edition']}. Please confirm the edition consulted and reconcile these records; neither edition has been checked externally.",
                    [citation["citation_id"]],
                    candidates,
                )
    cited = {rid for c in citations for rid in c["candidate_reference_ids"]}
    doi_refs: dict[str, list[dict]] = {}
    for ref in references:
        block = blocks[ref["location"]]
        ref.update(paragraph_index=block.paragraph_index, char_start=0, char_end=len(block.text))
        rid = ref["reference_id"]
        if rid not in cited and not numeric_coverage:
            add(
                "orphan_reference",
                ref,
                "Please confirm where this reference is cited, whether it is intentionally uncited, or whether extraction missed its citation.",
                reference_ids=[rid],
            )
        if not ref["date"]:
            add("missing_year", ref, "Please confirm the publication date or explicit no-date designation; no supported date was parsed.", reference_ids=[rid])
        if re.search(r"\b(?:edition|version|revised)\b|\b(?:rev|ed)\.", ref["raw_text"], re.I):
            add(
                "edition_version_uncertainty",
                ref,
                "Please confirm the cited edition/version against the source consulted. The manuscript alone does not verify it.",
                reference_ids=[rid],
            )
        for identifier in ref["identifiers"]:
            doi_refs.setdefault(identifier["normalized"], []).append(ref)
    for doi, refs in doi_refs.items():
        if len(refs) > 1:
            add(
                "duplicate_doi",
                refs[0],
                f"These references contain the same normalized DOI ({doi}). Please confirm whether they describe the same source; no external metadata was checked.",
                reference_ids=[r["reference_id"] for r in refs],
            )
    if reference_inventory_unavailable and citations:
        add(
            "reference_inventory_unavailable",
            citations[0],
            "No explicit reference-list heading was extracted. Please identify the reference-list boundary or supply its location before judging individual missing-reference links.",
            [c["citation_id"] for c in citations],
        )
    for block in chapter.blocks:
        if block.in_references:
            continue
        record = {"location": block.locator, "raw_text": block.text}
        for match in re.finditer(r"\[(?:citation|reference)\s+needed\]", block.text, re.I):
            add(
                "missing_citation_marker",
                {**record, "raw_text": match.group(), "char_start": match.start(), "char_end": match.end()},
                "Please supply the intended citation and corresponding reference for this unresolved manuscript marker.",
            )
        caption_starts = {m.start(1) for m in CAPTION_RE.finditer(block.text)}
        for match in TARGET_RE.finditer(block.text):
            if match.start() in caption_starts:
                continue
            # Explicit other-chapter pointers are outside this local check.
            before = block.text[max(0, match.start() - 45) : match.start()]
            after = block.text[match.end() : match.end() + 45]
            if re.search(r"\bchapter\s+[\w.]+\s*[,:'\u2019s -]*$", before, re.I) or re.match(r"\s+(?:in|of|from)\s+chapter\b", after, re.I):
                continue
            targets = captions.get((match[1].casefold(), match[2].casefold()), [])
            span_record = {**record, "raw_text": match.group(), "char_start": match.start(), "char_end": match.end()}
            if not targets:
                add(
                    "cross_reference_missing_target",
                    span_record,
                    "Please identify or supply the labeled target within this chapter, or specify its external chapter. No supported caption label was extracted locally; this does not establish absence from the volume or unextracted objects.",
                )
            elif len(targets) > 1:
                add(
                    "cross_reference_ambiguous_target",
                    span_record,
                    "Multiple caption labels match this pointer within this chapter. Please confirm and distinguish the intended target.",
                )
        for match in re.finditer(r"Error!\s*(?:Reference source not found|Bookmark not defined)\.?", block.text, re.I):
            add(
                "cross_reference_defect",
                {**record, "raw_text": match.group(), "char_start": match.start(), "char_end": match.end()},
                "Please repair this broken manuscript cross-reference and confirm its target.",
            )
        if NUMERIC_RE.search(block.text):
            add(
                "unsupported_style",
                record,
                "Numeric citation-like text was detected. Please inventory this citation style manually; the author-date parser cannot establish its reference links.",
            )
    return findings


def _reference_record(text: str, locator: str, number: int) -> dict[str, Any]:
    year_match = YEAR_RE.search(text)
    year = year_match.group(1) if year_match else None
    edition = EDITION_RE.search(text)
    author_text = text[: year_match.start()] if year_match else text.split(".", 1)[0]
    author = _first_author(author_text.strip(" .(\t"))
    remainder = text[year_match.end() :] if year_match else text
    title_bits = [part.strip(" .()") for part in remainder.split(".") if part.strip(" .()")]
    title = title_bits[0] if title_bits else None
    identifiers = []
    seen_dois = set()
    for match in DOI_RE.finditer(text):
        raw = match.group(0)
        doi = normalize_doi(raw)
        if doi not in seen_dois:
            identifiers.append({"scheme": "doi", "raw": raw, "normalized": doi, "status": "syntax_only"})
            seen_dois.add(doi)
    return {
        "reference_id": f"R{number:03d}",
        "raw_text": text,
        "location": locator,
        "source_type": "unknown_or_ambiguous",
        "authors_or_group": [author] if author else [],
        "personal_author_syntax": bool(re.search(r",\s*[^\W\d_]\.", author_text)),
        "date": year,
        "title": title,
        "container_title": None,
        "version": None,
        "edition": edition[1] if edition else None,
        "identifiers": identifiers,
        "metadata_provenance": [],
        "parse_confidence": 0.75 if author and year else 0.45,
    }


def _citation_record(raw: str, locator: str, number: int, form: str) -> dict[str, Any]:
    year = YEAR_RE.search(raw)
    before = raw[: year.start()] if year else raw
    author = _first_author(before)
    locator_match = re.search(r"\b(?:p{1,2}\.|para\.|chapter|table|figure)\s*\d+[A-Za-z-]*", raw, re.IGNORECASE)
    return {
        "citation_id": f"C{number:03d}",
        "raw_text": raw,
        "location": locator,
        "context_type": "body",
        "citation_form": form,
        "authors_or_group": [author] if author else [],
        "author_expression": before.strip(" (,"),
        "year": year.group(1) if year else None,
        "edition": None,
        "locator": locator_match.group(0) if locator_match else None,
        "quotation_detected": False,
        "candidate_reference_ids": [],
        "parse_confidence": 0.8 if author and year else 0.5,
    }


def build_inventory(chapter: Chapter) -> dict[str, Any]:
    references = []
    for block in chapter.blocks:
        heading_candidate = block.text.casefold().strip().lstrip("#").strip().rstrip(":").strip()
        if block.in_references and heading_candidate not in {"references", "bibliography", "works cited", "reference list", "literature cited"}:
            references.append(_reference_record(block.text, block.locator, len(references) + 1))

    citations = []
    for block in chapter.blocks:
        if block.in_references:
            continue
        extracted = []
        for match in re.finditer(r"\(([^()]{1,500})\)", block.text):
            inside = match.group(1)
            for piece_match in re.finditer(r"[^;]+", inside):
                piece = piece_match.group()
                # Explicit sample sizes can resemble author/year citations when
                # the count is in the year range. Exclude only that notation.
                if re.fullmatch(r"\s*[nN]\s*=\s*\d+\s*", piece):
                    continue
                years = _citation_years(piece)
                if not years:
                    continue
                author = piece[: years[0].start()].strip(" ,")
                if not author or not any(c.isalpha() for c in author):
                    continue
                for year in years:
                    record = _citation_record(f"({author}, {year.group(0)})", block.locator, 0, "parenthetical")
                    segment_start = match.start(1) + piece_match.start() + len(piece) - len(piece.lstrip())
                    segment_end = match.start(1) + piece_match.end() - len(piece) + len(piece.rstrip())
                    record["raw_text"] = block.text[segment_start:segment_end]
                    locator = LOCATOR_RE.search(piece)
                    record["locator"] = locator.group(0) if locator else None
                    edition = EDITION_RE.search(piece)
                    record["edition"] = edition[1] if edition else None
                    record["char_start"], record["char_end"] = segment_start, segment_end
                    extracted.append(record)
        # Narrative author strings begin at an uppercase word. Lowercase particles
        # are retained; arbitrary sentence prefixes are not treated as group names.
        narrative = re.compile(r"(?<![\w])([^\W\d_][\w\u2019'\u2013-]*(?:,?\s+(?:[^\W\d_][\w\u2019'\u2013-]*|&|et|al\.))*)\s*$", re.UNICODE)
        for date_match in re.finditer(r"\(([^()]{1,500})\)", block.text):
            years = _citation_years(date_match.group(1))
            if not years or date_match.group(1)[: years[0].start()].strip():
                continue
            prefix = block.text[max(0, date_match.start() - 250) : date_match.start()]
            possessive = re.search(r"\b([^\W\d_][\w\u2019'-]*(?:\s+[^\W\d_][\w'-]*)?)[\u2019']s\s+(.{1,160}?)\s*$", prefix)
            if possessive and EDITION_RE.search(date_match[1]):
                start = date_match.start() - len(prefix) + possessive.start()
                author = possessive[1]
                parts = author.split()
                if len(parts) > 1 and parts[0].casefold() in {"of", "in", "from", "with", "by", "see", "consult", "compare"}:
                    start += author.index(parts[1], len(parts[0]))
                    author = parts[1]
                record = _citation_record(f"{author} ({years[0][0]})", block.locator, 0, "possessive_title")
                record.update(
                    raw_text=block.text[start : date_match.end()],
                    char_start=start,
                    char_end=date_match.end(),
                    title_identity_uncertain=True,
                    observed_title=possessive[2],
                    edition=EDITION_RE.search(date_match[1])[1],
                )
                extracted.append(record)
                continue
            match = narrative.search(prefix)
            if not match:
                continue
            author = match.group(1)
            word_spans = list(re.finditer(r"\S+", author))
            words = [word.group() for word in word_spans]
            first_word = 0
            while words and words[0].casefold() in {"as", "see", "in", "according", "to", "also"}:
                words.pop(0)
                first_word += 1
            if not words:
                continue
            # A lower-case prose token bounds the author phrase on its left.
            # Retain particles and et al. within names rather than reducing them
            # to the last token as the original parser did.
            particles = {"van", "von", "de", "der", "del", "da", "la", "et", "al.", "and", "&"}
            boundary = max((i for i, w in enumerate(words) if w[0].islower() and w.casefold() not in particles), default=-1)
            words = words[boundary + 1 :]
            first_word += boundary + 1
            if not words:
                continue
            # Trim prose before the first capitalized author token, retaining
            # conventional surname particles. This remains a bounded heuristic.
            start_word = next((i for i, w in enumerate(words) if w[0].isupper()), 0)
            while start_word and words[start_word - 1].casefold() in {"van", "von", "de", "der", "del", "da", "la"}:
                start_word -= 1
            author = " ".join(words[start_word:])
            if any(w[0].islower() and w.casefold() not in particles for w in author.split()):
                continue
            # Match identity uses normalized words, but evidence offsets must
            # retain their original whitespace and complete author tokens.
            start = date_match.start() - len(prefix) + match.start(1) + word_spans[first_word + start_word].start()
            for year in years:
                record = _citation_record(f"{author} ({year.group(0)})", block.locator, 0, "narrative")
                record["raw_text"] = block.text[start : date_match.end()]
                record["char_start"], record["char_end"] = start, date_match.end()
                edition = EDITION_RE.search(date_match.group(1))
                record["edition"] = edition[1] if edition else None
                locator = LOCATOR_RE.search(date_match.group(1))
                record["locator"] = locator.group(0) if locator else None
                extracted.append(record)
        for record in sorted(extracted, key=lambda c: (c["char_start"], c["char_end"])):
            record["citation_id"] = f"C{len(citations) + 1:03d}"
            record["context_type"] = block.context_type
            record["paragraph_index"] = block.paragraph_index
            citations.append(record)

    ref_by_key: dict[tuple[str, str], list[str]] = {}
    for ref in references:
        key = ((ref["authors_or_group"] or [""])[0], _date_key(ref["date"]))
        ref_by_key.setdefault(key, []).append(ref["reference_id"])

    unmatched = []
    candidate_edges = 0
    for citation in citations:
        key = ((citation["authors_or_group"] or [""])[0], _date_key(citation["year"]))
        candidates = ref_by_key.get(key, [])
        if not candidates:
            lead_author = _normalized(re.split(r"[,&;]|\s+and\s+", citation["author_expression"], maxsplit=1, flags=re.I)[0])
            candidates = [rid for rid in ref_by_key.get((lead_author, key[1]), []) if references[int(rid[1:]) - 1]["personal_author_syntax"]]
        if not candidates and re.fullmatch(r"\d{4}", key[1]):
            candidates = [rid for (author, date), ids in ref_by_key.items() if author == key[0] and re.fullmatch(key[1] + "[a-z]", date) for rid in ids]
            citation["match_uncertain"] = bool(candidates)
        if citation.get("title_identity_uncertain"):
            title_candidates = [rid for rid in candidates if _title_key(references[int(rid[1:]) - 1]["title"]) == _title_key(citation.get("observed_title"))]
            if len(title_candidates) == 1 and not citation.get("match_uncertain") and _title_key(citation.get("observed_title")):
                candidates = title_candidates
                citation["title_identity_uncertain"] = False
                citation["match_method"] = "author_year_title"
        citation["candidate_reference_ids"] = candidates
        candidate_edges += len(candidates)
        if candidate_edges > 25000:
            raise SafetyError("Local citation candidate graph exceeds 25000 links; split the chapter or manually resolve repeated ambiguous author-date records before retrying.")
        if not candidates:
            unmatched.append(citation["citation_id"])

    ledgers = []
    cited_reference_ids = {rid for citation in citations for rid in citation["candidate_reference_ids"]}
    citation_by_ref: dict[str, list[dict]] = {}
    for citation in citations:
        for rid in citation["candidate_reference_ids"]:
            citation_by_ref.setdefault(rid, []).append(citation)
    numeric_coverage = any(NUMERIC_RE.search(b.text) for b in chapter.blocks if not b.in_references)
    for index, ref in enumerate(references, 1):
        linked = citation_by_ref.get(ref["reference_id"], [])
        citation_ids = [c["citation_id"] for c in linked]
        status = "orphan_reference" if not citation_ids else "matched"
        if not citation_ids and numeric_coverage:
            status = "not_checked"
        if any(len(c["candidate_reference_ids"]) > 1 or c.get("match_uncertain") for c in linked):
            status = "ambiguous_match"
        ledgers.append(
            {
                "ledger_entry_id": f"L{index:03d}",
                "source_record_id": ref["reference_id"],
                "citation_ids": citation_ids,
                "reference_ids": [ref["reference_id"]],
                "relationship_status": status,
                "match_method": ("author_year_title" if any(c.get("match_method") == "author_year_title" for c in linked) else "exact_author_year")
                if status == "matched"
                else "none",
                "candidate_source_record_ids": [],
                "confidence": 0.85 if status == "matched" else 0.45,
                "evidence_ids": [f"MANUSCRIPT-{chapter.chapter_id}"],
                "exception_id": None,
                "decision_log_id": None,
            }
        )

    findings = _findings(chapter, citations, references)
    dois = sorted({identifier["normalized"] for ref in references for identifier in ref["identifiers"]})
    return {
        "schema_version": 1,
        "chapter_id": chapter.chapter_id,
        "citations": citations,
        "references": references,
        "ledger_entries": ledgers,
        "unmatched_citation_ids": unmatched,
        "orphan_reference_ids": sorted(ref["reference_id"] for ref in references if ref["reference_id"] not in cited_reference_ids and not numeric_coverage),
        "dois": dois,
        "parser_coverage": "partial",
        "coverage_notes": [
            *chapter.coverage_notes,
            "Citation extraction is heuristic author-date parsing, not a complete style or scholarly verification. Numeric, note-only, short-title, nested citations, initials disambiguation, and group abbreviations may require manual inventory.",
            "Candidate author-year links are manuscript-local possibilities, not verified source identities. Reference entries must be segmented correctly upstream.",
            "Organization conjunctions are retained. Multi-author fallback requires personal-author syntax in the observed reference. Possessive title/edition expressions require a unique local author/year/title candidate for edition comparison; unresolved identities receive manual queries. Numeric-style chapters suppress orphan claims because links are not checked. Candidate graphs stop safely above 25000 links.",
            "Caption checks cover extracted Figure/Table labels with numeric or multipart numbers and explicit colon/period/dash captions within this chapter only. Other label styles, implicit external targets, images and unextracted objects require review. Edition comparisons cover explicit numeric editions only.",
        ],
        "deterministic_findings": findings,
    }
