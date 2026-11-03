"""Local, proposal-only Word output; never evaluates external source support."""

from __future__ import annotations

import re
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.text.run import Run

from .artifacts import atomic_write_bytes
from .ingest import Chapter
from .security import SafetyError, read_approved_bytes, sha256_file, validate_docx_archive

NOTICE = (
    "Offline manuscript inventory only. Source support was not evaluated. "
    "No source articles, live metadata, or external links were accessed. "
    "All queries require human review; no edits are accepted and publication is not authorized."
)


def _exact_runs(paragraph, exact: str):
    """Split plain text runs while retaining formatting; refuse complex XML and ambiguity."""
    if not exact or paragraph.text.count(exact) != 1:
        return None
    allowed = {qn("w:pPr"), qn("w:r"), qn("w:commentRangeStart"), qn("w:commentRangeEnd")}
    if any(child.tag not in allowed for child in paragraph._p):
        return None
    for run in paragraph.runs:
        if any(child.tag not in {qn("w:rPr"), qn("w:t")} for child in run._r):
            return None
    start = paragraph.text.index(exact)
    end = start + len(exact)
    offset = 0
    selected = []
    for run in list(paragraph.runs):
        text = run.text
        run_end = offset + len(text)
        if run_end <= start or offset >= end:
            offset = run_end
            continue
        a, b = max(0, start - offset), min(len(text), end - offset)
        if a == 0 and b == len(text):
            selected.append(run)
        else:
            # Clone complete runs so attributes survive only the necessary boundary splits.
            previous = run._r
            for left, right in ((0, a), (a, b), (b, len(text))):
                if left == right:
                    continue
                element = deepcopy(run._r)
                fragment = Run(element, paragraph)
                fragment.text = text[left:right]
                previous.addnext(element)
                previous = element
                if left == a and right == b:
                    selected.append(fragment)
            run._r.getparent().remove(run._r)
        offset = run_end
    return selected or None


def _paragraph_map(document, chapter):
    result = {}
    for block in chapter.blocks:
        index = block.paragraph_index
        if block.locator.startswith("body.") and index is not None and 0 <= index < len(document.paragraphs):
            paragraph = document.paragraphs[index]
            if paragraph.text.strip() == block.text:
                result[block.locator] = paragraph
    return result


def _literal_markdown(text: str) -> str:
    """Fence untrusted text with a delimiter it cannot close."""
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "\n" + text + ("" if text.endswith("\n") else "\n") + fence


def _text_document(payload: bytes, chapter: Chapter):
    try:
        source_text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SafetyError("Text chapters must be UTF-8 encoded.") from exc
    source_lines = re.split(r"\r\n|\n|\r", source_text)
    document = Document()
    for line in source_lines:
        paragraph = document.add_paragraph(line)
        paragraph.paragraph_format.space_after = 0
        paragraph.paragraph_format.space_before = 0
    paragraphs = {}
    for block in chapter.blocks:
        # Transformed or multiline parser blocks cannot safely identify a source line.
        matches = [index for index, line in enumerate(source_lines) if line == block.text]
        if len(matches) == 1 and "\n" not in block.text and "\r" not in block.text:
            paragraphs[block.locator] = document.paragraphs[matches[0]]
    return document, paragraphs


def export_offline_author_packet(chapter: Chapter, inventory: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Write reviewable Word/query artifacts. Caller writes inventory and decision ledger.

    deterministic_findings share finding_id, location, exact_text, author_query,
    citation_ids, reference_ids and evidence with the caller's evidence records.
    Unsupported, ambiguous or complex anchors are explicitly appendix-only.
    """
    source = Path(chapter.source_path)
    payload = read_approved_bytes(source, chapter.sha256)
    if chapter.extension == ".docx":
        validate_docx_archive(source)
        document = Document(BytesIO(payload))
        paragraphs = _paragraph_map(document, chapter)
    else:
        document, paragraphs = _text_document(payload, chapter)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "author-review.docx"
    queries = output_dir / "author-queries.md"
    if source.resolve() in {output.resolve(), queries.resolve()}:
        raise SafetyError("Author output must not overwrite the manuscript.")
    findings = inventory.get("deterministic_findings", [])
    ids = [item["finding_id"] for item in findings]
    if len(set(ids)) != len(ids):
        raise SafetyError("Author findings must have unique decision identifiers.")
    anchors = []
    for item in findings:
        paragraph = paragraphs.get(item.get("location"))
        runs = _exact_runs(paragraph, item.get("exact_text", "")) if paragraph is not None else None
        status = "appendix_only"
        reason = "No unique exact plain-text anchor at the recorded location."
        if runs:
            try:
                document.add_comment(
                    runs,
                    text=f"{item['finding_id']}\n{item.get('author_query', 'Please review with the editor.')}\nOffline manuscript observation; human decision pending.",
                    author="Editorial Review",
                    initials="ER",
                )
                status, reason = "exact_text_comment", "Unique exact text at the recorded location."
            except (ValueError, AttributeError):
                reason = "Word comment unavailable; see visible appendix."
        anchors.append({"finding_id": item["finding_id"], "location": item.get("location"), "status": status, "reason": reason})
    document.add_page_break()
    document.add_heading("Offline Author Review Notes", level=1)
    document.add_paragraph(NOTICE)
    document.add_paragraph("Parser coverage: " + str(inventory.get("parser_coverage", chapter.parser_coverage)))
    for note in inventory.get("coverage_notes", chapter.coverage_notes):
        document.add_paragraph(str(note))
    lines = ["# Offline Author Queries", "", NOTICE, "", "Inventory/evidence: citation-inventory.json. Decisions are pending human review.", ""]
    coverage = "\n".join(
        ["Parser coverage: " + str(inventory.get("parser_coverage", chapter.parser_coverage)), *[str(note) for note in inventory.get("coverage_notes", chapter.coverage_notes)]]
    )
    lines.extend([_literal_markdown(coverage), ""])
    if not findings:
        message = "No deterministic queries were generated. This does not establish completeness, source support, or editorial readiness."
        document.add_paragraph(message)
        lines.append(message)
    for index, (item, anchor) in enumerate(zip(findings, anchors, strict=True), 1):
        fid = item["finding_id"]
        fields = [
            f"Location: {item.get('location', 'unavailable')}",
            f"Observed text: {item.get('exact_text', '')}",
            item.get("author_query") or "Please review with the editor.",
            f"Anchor: {anchor['status']}. {anchor['reason']}",
            f"Inventory pointer: citation-inventory.json / deterministic_findings / {fid}",
            f"Citation IDs: {', '.join(item.get('citation_ids', [])) or 'none'}; reference IDs: {', '.join(item.get('reference_ids', [])) or 'none'}",
            f"Evidence pointer: citation-inventory.json / deterministic_findings / {fid} / evidence (manuscript observations only)",
            f"Decision ID: {fid}; human decision pending.",
        ]
        document.add_heading(fid, level=2)
        for field in fields:
            document.add_paragraph(field)
        lines.extend([f"## Query {index}", "", _literal_markdown("\n".join([f"Finding ID: {fid}", *fields])), ""])
    buffer = BytesIO()
    document.save(buffer)
    if sha256_file(source) != chapter.sha256:
        raise SafetyError("Source changed while building the author packet.")
    atomic_write_bytes(output, buffer.getvalue())
    atomic_write_bytes(queries, ("\n".join(lines) + "\n").encode("utf-8"))
    return {
        "path": str(output),
        "sha256": sha256_file(output),
        "queries_path": str(queries),
        "queries_sha256": sha256_file(queries),
        "comments_added": sum(a["status"] == "exact_text_comment" for a in anchors),
        "appendix_items": len(findings),
        "anchors": anchors,
        "source_unchanged": True,
        "source_support_evaluated": False,
        "publication_authorized": False,
    }
