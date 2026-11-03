from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from docx import Document
from docx.table import Table, _Cell
from lxml import etree

from .config import MAX_MODEL_INPUT_CHARS
from .security import SafetyError, SourceFile, sha256_file, validate_docx_archive

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REFERENCE_HEADINGS = {"references", "bibliography", "works cited", "reference list", "literature cited"}


@dataclass(frozen=True)
class Block:
    locator: str
    text: str
    context_type: str
    in_references: bool
    paragraph_index: int | None = None


@dataclass(frozen=True)
class Chapter:
    chapter_id: str
    source_path: str
    relative_path: str
    extension: str
    size: int
    sha256: str
    blocks: list[Block]
    parser_coverage: str
    coverage_notes: list[str]

    def model_text(self) -> str:
        rendered = "\n\n".join(f"[{b.locator} | {b.context_type}]\n{b.text}" for b in self.blocks)
        if len(rendered) > MAX_MODEL_INPUT_CHARS:
            raise SafetyError("Extracted chapter text exceeds the model-input safety limit.")
        return rendered

    def to_record(self) -> dict:
        value = asdict(self)
        value["blocks"] = [asdict(block) for block in self.blocks]
        return value


def _extract_note_part(path: Path, member: str, context_type: str) -> list[Block]:
    blocks: list[Block] = []
    with zipfile.ZipFile(path) as archive:
        try:
            payload = archive.read(member)
        except KeyError:
            return blocks
    root = etree.fromstring(payload, parser=etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False, huge_tree=False))
    index = 0
    for paragraph in root.iter(W_NS + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(W_NS + "t")).strip()
        if text:
            index += 1
            blocks.append(Block(f"{context_type}.p{index:04d}", text, "note", False))
    return blocks


def _extract_docx(source: SourceFile, chapter_id: str) -> Chapter:
    validate_docx_archive(source.path)
    document = Document(source.path)
    blocks: list[Block] = []
    in_references = False
    body_index = 0
    table_index = 0
    paragraph_index = -1
    notes = ["Word coverage includes ordinary body paragraphs, top-level table cells and readable footnote/endnote text. Check fields, text boxes, headers, footers, nested tables, drawings, existing comments and tracked revisions separately."]

    for item in document.iter_inner_content():
        if hasattr(item, "text") and not isinstance(item, Table):
            paragraph_index += 1
            text = item.text.strip()
            if not text:
                continue
            body_index += 1
            normalized = re.sub(r"\s+", " ", text).strip().casefold().rstrip(":")
            if normalized in REFERENCE_HEADINGS:
                in_references = True
            context = "reference_list" if in_references else "body"
            blocks.append(Block(f"body.p{body_index:04d}", text, context, in_references, paragraph_index))
        elif isinstance(item, Table):
            table_index += 1
            for row_no, row in enumerate(item.rows, 1):
                # row.cells expands merged columns and repeats vertically merged
                # text. Walk physical cells once, retaining the logical column.
                cell_no = row.grid_cols_before + 1
                for tc in row._tr.tc_lst:
                    cell = _Cell(tc, item)
                    for para_no, paragraph in enumerate(cell.paragraphs, 1):
                        text = paragraph.text.strip()
                        if text:
                            locator = f"table.{table_index:03d}.r{row_no:03d}.c{cell_no:03d}.p{para_no:03d}"
                            blocks.append(Block(locator, text, "table", in_references))
                    cell_no += tc.grid_span

    footnotes = _extract_note_part(source.path, "word/footnotes.xml", "footnote")
    endnotes = _extract_note_part(source.path, "word/endnotes.xml", "endnote")
    blocks.extend(footnotes)
    blocks.extend(endnotes)
    if not in_references:
        notes.append("No explicit References or Bibliography heading was detected.")
    coverage = "high" if in_references else "partial"
    if not blocks:
        raise SafetyError("No readable text was found in the DOCX file.")
    if sha256_file(source.path) != source.sha256:
        raise SafetyError("Source drifted during extraction.")
    return Chapter(chapter_id, str(source.path), source.relative_path, ".docx", source.size, source.sha256, blocks, coverage, notes)


def _extract_text(source: SourceFile, chapter_id: str) -> Chapter:
    try:
        text = source.path.read_text(encoding="utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise SafetyError("Text and Markdown chapters must be UTF-8 encoded.") from exc
    blocks: list[Block] = []
    in_references = False
    pending: list[str] = []

    def flush() -> None:
        if pending:
            index = len(blocks) + 1
            blocks.append(Block(f"body.p{index:04d}", "\n".join(pending), "reference_list" if in_references else "body", in_references, index - 1))
            pending.clear()

    for line in text.splitlines():
        cleaned = line.strip()
        heading = re.sub(r"^#{1,6}\s+", "", cleaned).casefold().rstrip(":")
        if not cleaned:
            flush()
        elif heading in REFERENCE_HEADINGS:
            flush()
            in_references = True
            pending.append(cleaned)
            flush()
        else:
            # Adjacent author/date entries are common in plain-text bibliographies;
            # keep wrapped titles, publishers and DOI continuations with the entry.
            if in_references and re.match(r"^[^\d(]{1,220}\((?:\d{4}[a-z]?|n\.\s*d\.)\)", cleaned, re.I):
                flush()
            pending.append(cleaned)
    flush()
    if not blocks:
        raise SafetyError("No readable text was found in the chapter.")
    notes = ["Text and Markdown use paragraph and author/date line boundaries. Confirm wrapped entries and the end of the reference list."]
    if not in_references:
        notes.append("No explicit References or Bibliography heading was detected.")
    return Chapter(chapter_id, str(source.path), source.relative_path, source.path.suffix.lower(), source.size, source.sha256, blocks, "high" if in_references else "partial", notes)


def extract_chapter(source: SourceFile, chapter_id: str) -> Chapter:
    if source.path.suffix.lower() == ".docx":
        return _extract_docx(source, chapter_id)
    return _extract_text(source, chapter_id)
