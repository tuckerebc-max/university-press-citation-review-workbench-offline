from __future__ import annotations

import hashlib
import ipaddress
import os
import posixpath
import re
import socket
import stat
import urllib.parse
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from . import config


class SafetyError(ValueError):
    pass


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative_path: str
    size: int
    sha256: str


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    attrs = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attrs & flag)


def sha256_file(path: Path) -> str:
    before = path.lstat()
    if _is_reparse_point(path):
        raise SafetyError(f"Symlink or reparse-point input is not allowed: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise SafetyError(f"Source changed while opening: {path.name}")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise SafetyError(f"Source changed while hashing: {path.name}")
    return digest.hexdigest()


def read_approved_bytes(path: Path, expected_sha256: str) -> bytes:
    """Read one approved source snapshot without following a late path swap."""
    before = path.lstat()
    if _is_reparse_point(path):
        raise SafetyError(f"Symlink or reparse-point input is not allowed: {path.name}")
    if before.st_size > config.MAX_FILE_BYTES:
        raise SafetyError(f"{path.name} exceeds the approved file-size limit.")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
            raise SafetyError(f"Source changed while opening: {path.name}")
        total = 0
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if total > config.MAX_FILE_BYTES:
                raise SafetyError(f"{path.name} exceeded the approved file-size limit while reading.")
            digest.update(chunk)
            chunks.append(chunk)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SafetyError(f"Source changed while reading: {path.name}")
    if digest.hexdigest() != expected_sha256:
        raise SafetyError(f"Source no longer matches the approved manifest: {path.name}")
    return b"".join(chunks)


def validate_input_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise SafetyError("The chapter folder must be an absolute path.")
    if not raw.exists() or not raw.is_dir():
        raise SafetyError("The chapter folder does not exist or is not a directory.")
    if _is_reparse_point(raw):
        raise SafetyError("A symlinked or reparse-point chapter folder is not allowed.")
    return raw.resolve(strict=True)


def validate_output_parent(value: str | Path, input_root: Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise SafetyError("The output folder must be an absolute path.")
    if not raw.exists() or not raw.is_dir():
        raise SafetyError("The output parent folder does not exist.")
    if _is_reparse_point(raw):
        raise SafetyError("A symlinked or reparse-point output folder is not allowed.")
    resolved = raw.resolve(strict=True)
    try:
        resolved.relative_to(input_root)
    except ValueError:
        pass
    else:
        raise SafetyError("The output folder cannot be inside the source folder.")
    return resolved


def scan_sources(root: Path) -> list[SourceFile]:
    root = validate_input_root(root)
    sources: list[SourceFile] = []
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        if _is_reparse_point(directory):
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not _is_reparse_point(path):
                        pending.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if path.name.startswith("~$") or path.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                if size <= 0:
                    continue
                if size > config.MAX_FILE_BYTES:
                    raise SafetyError(f"{path.name} exceeds the {config.MAX_FILE_BYTES // 1024 // 1024} MB limit.")
                total += size
                if total > config.MAX_TOTAL_BYTES:
                    raise SafetyError("The selected chapter set exceeds the total size limit.")
                # Keep the lexical path produced by the already-validated, non-reparse
                # traversal. Resolving only the child expands 8.3 names on Windows and
                # can make a safe child appear to be outside the same root.
                relative = path.relative_to(root).as_posix()
                sources.append(SourceFile(path, relative, size, sha256_file(path)))
                if len(sources) > config.MAX_CHAPTERS:
                    raise SafetyError(f"A run may contain at most {config.MAX_CHAPTERS} chapters.")
    return sorted(sources, key=lambda item: item.relative_path.casefold())


_OPC_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OPC_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
# Only fields that operate on the document's own content/metadata are admitted.
# Unknown fields must be flattened before review, rather than interpreted by Word.
_LOCAL_WORD_FIELDS = set(
    "PAGE NUMPAGES SECTION SECTIONPAGES REF PAGEREF NOTEREF SEQ TOC TOA TOF INDEX XE TC TA CITATION BIBLIOGRAPHY DATE TIME CREATEDATE SAVEDATE PRINTDATE EDITTIME AUTHOR TITLE SUBJECT KEYWORDS COMMENTS LASTSAVEDBY REVNUM NUMCHARS NUMWORDS NUMPAGES DOCPROPERTY DOCVARIABLE FILENAME FILESIZE QUOTE SYMBOL EQ IF COMPARE SET ASK FILLIN AUTONUM AUTONUMLGL AUTONUMOUT LISTNUM ADVANCE FORM CHECKBOX FORMCHECKBOX FORMDROPDOWN FORMTEXT =".split()
)
_ACTIVE_RELATIONSHIPS = {
    "oleobject",
    "package",
    "control",
    "activexcontrol",
    "activexcontrolbinary",
    "vbaproject",
    "afchunk",
    "subdocument",
    "attachedtemplate",
    "mailmergesource",
    "mailmergeheadersource",
}


def _docx_xml(payload: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False, huge_tree=False)
    try:
        root = etree.fromstring(payload, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise SafetyError("DOCX contains malformed XML.") from exc
    # libxml detects declarations after decoding UTF-16/32 as well as UTF-8.
    if root.getroottree().docinfo.doctype:
        raise SafetyError("DOCX contains disallowed XML declarations.")
    return root


def _check_field(instruction: str) -> None:
    command = instruction.strip().split(maxsplit=1)
    if not command or command[0].upper() not in _LOCAL_WORD_FIELDS:
        raise SafetyError("DOCX contains an external or unsupported Word field; flatten it before review.")


def _check_word_xml(root) -> None:
    fields: list[list[str]] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element)
        if name.namespace not in _WORD_NAMESPACES:
            continue
        if name.localname in {"altChunk", "object", "control", "subDoc", "mailMerge"}:
            raise SafetyError("DOCX contains linked or active Word content; flatten it before review.")
        if name.localname == "fldSimple":
            _check_field(element.get(f"{{{name.namespace}}}instr") or "")
        elif name.localname == "fldChar":
            kind = element.get(f"{{{name.namespace}}}fldCharType")
            if kind == "begin":
                fields.append([])
            elif kind in {"separate", "end"}:
                if not fields:
                    raise SafetyError("DOCX contains an unbalanced Word field.")
                if kind == "separate":
                    _check_field("".join(fields[-1]))
                else:
                    _check_field("".join(fields.pop()))
            else:
                raise SafetyError("DOCX contains an invalid Word field marker.")
        elif name.localname in {"instrText", "delInstrText"}:
            if not fields:
                raise SafetyError("DOCX contains an instruction outside a Word field.")
            fields[-1].append(element.text or "")
    if fields:
        raise SafetyError("DOCX contains an unbalanced Word field.")


def _check_relationships(root, member: str) -> None:
    if root.tag != f"{{{_OPC_REL_NS}}}Relationships":
        raise SafetyError("DOCX contains an invalid relationships root.")
    seen = set()
    for relationship in root:
        if not isinstance(relationship.tag, str):
            continue
        if relationship.tag != f"{{{_OPC_REL_NS}}}Relationship":
            raise SafetyError("DOCX contains an invalid relationship element.")
        rid = relationship.get("Id", "")
        kind = relationship.get("Type", "")
        target = relationship.get("Target", "")
        mode = relationship.get("TargetMode", "Internal")
        if not rid or rid in seen or not kind or not target or mode not in {"Internal", "External"}:
            raise SafetyError("DOCX contains malformed or duplicate relationships.")
        seen.add(rid)
        if kind.rsplit("/", 1)[-1].casefold() in _ACTIVE_RELATIONSHIPS:
            raise SafetyError("DOCX contains an active or linked-content relationship.")
        parsed = urllib.parse.urlsplit(target)
        if mode == "External":
            safe_hyperlink = (
                kind.endswith("/hyperlink")
                and parsed.scheme.casefold() in {"http", "https", "mailto"}
                and len(target) <= 2048
                and not parsed.username
                and not parsed.password
                and not any(ord(c) < 32 for c in target)
                and (parsed.scheme.casefold() == "mailto" or bool(parsed.hostname))
            )
            if not safe_hyperlink:
                raise SafetyError("DOCX contains a disallowed external relationship.")
        else:
            decoded = urllib.parse.unquote(target)
            decoded_url = urllib.parse.urlsplit(decoded)
            base = posixpath.dirname(posixpath.dirname(member)) if member != "_rels/.rels" else ""
            resolved = posixpath.normpath(posixpath.join(base, decoded.lstrip("/")))
            if (
                parsed.scheme
                or parsed.netloc
                or decoded_url.scheme
                or decoded_url.netloc
                or "\\" in decoded
                or any(ord(c) < 32 for c in decoded)
                or resolved == ".."
                or resolved.startswith("../")
            ):
                raise SafetyError("DOCX contains an unsafe internal relationship target.")


def _check_content_types(root) -> tuple[set[str], set[str]]:
    if root.tag != f"{{{_OPC_CT_NS}}}Types":
        raise SafetyError("DOCX contains invalid content-type metadata.")
    seen = set()
    xml_parts: set[str] = set()
    xml_extensions: set[str] = set()
    for entry in root:
        if not isinstance(entry.tag, str):
            continue
        name = etree.QName(entry)
        if name.namespace != _OPC_CT_NS or name.localname not in {"Default", "Override"}:
            raise SafetyError("DOCX contains invalid content-type metadata.")
        key = entry.get("PartName" if name.localname == "Override" else "Extension", "")
        content = entry.get("ContentType", "").casefold()
        if not key or not content or (name.localname, key.casefold()) in seen:
            raise SafetyError("DOCX contains malformed content-type metadata.")
        seen.add((name.localname, key.casefold()))
        if content.endswith(("+xml", "/xml")):
            if name.localname == "Override":
                xml_parts.add(urllib.parse.unquote(key).lstrip("/"))
            else:
                xml_extensions.add(key.casefold())
        if any(token in content for token in ("macroenabled", "vbaproject", "activex", "oleobject", "ms-office.package", "text/html", "application/xhtml")):
            raise SafetyError("DOCX contains active or imported content types.")

    return xml_parts, xml_extensions


def validate_docx_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > config.MAX_DOCX_MEMBERS:
                raise SafetyError("DOCX contains too many archive members.")
            names: set[str] = set()
            actual_names: set[str] = set()
            expanded = 0
            for item in members:
                normalized = item.filename.replace("\\", "/")
                if "\\" in item.filename or any(ord(c) < 32 for c in item.filename):
                    raise SafetyError("DOCX contains an ambiguous archive path.")
                parts = [part for part in normalized.split("/") if part]
                if normalized.startswith("/") or ".." in parts or "." in parts or "//" in normalized:
                    raise SafetyError("DOCX contains an unsafe archive path.")
                folded = normalized.casefold()
                if folded in names:
                    raise SafetyError("DOCX contains duplicate archive member names.")
                names.add(folded)
                actual_names.add(normalized)
                if item.flag_bits & 0x1:
                    raise SafetyError("Encrypted DOCX archive members are not allowed.")
                if item.file_size > config.MAX_DOCX_MEMBER_BYTES:
                    raise SafetyError("DOCX contains an oversized archive member.")
                expanded += item.file_size
                if expanded > config.MAX_DOCX_EXPANDED_BYTES:
                    raise SafetyError("DOCX expanded size exceeds the safety limit.")
                if item.file_size and not item.compress_size:
                    raise SafetyError("DOCX contains an invalid zero-size compressed member.")
                if item.compress_size and item.file_size / item.compress_size > 250:
                    raise SafetyError("DOCX contains a suspicious compression ratio.")
                if folded.startswith(("word/embeddings/", "word/activex/", "customui/")) or folded == "word/vbaproject.bin":
                    raise SafetyError("DOCX contains active or embedded content that must be flattened before review.")
            if "[Content_Types].xml" not in actual_names or "word/document.xml" not in actual_names:
                raise SafetyError("File is not a valid Word document package.")
            content_types = _docx_xml(archive.read("[Content_Types].xml"))
            xml_parts, xml_extensions = _check_content_types(content_types)
            for item in members:
                xml_name = item.filename.replace("\\", "/")
                if xml_name.casefold().endswith((".xml", ".rels")) or xml_name in xml_parts or xml_name.rsplit(".", 1)[-1].casefold() in xml_extensions:
                    root = _docx_xml(archive.read(item))
                    if xml_name.casefold().endswith(".rels"):
                        _check_relationships(root, xml_name)
                    elif xml_name == "[Content_Types].xml":
                        _check_content_types(root)
                    _check_word_xml(root)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, ValueError, zlib.error, EOFError) as exc:
        if isinstance(exc, SafetyError):
            raise
        raise SafetyError("File is not a readable DOCX package.") from exc


def assert_public_dns(host: str) -> None:
    if host not in config.ALLOWED_EGRESS_HOSTS:
        raise SafetyError("Network host is not allowlisted.")
    addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses:
        raise SafetyError("Network host did not resolve.")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise SafetyError("Network host resolved to a non-public address.")


def safe_slug(value: str, fallback: str = "chapter") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (value[:80] or fallback).lower()


def scrub_message(message: object, secret: str | None = None) -> str:
    text = str(message)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"\blr_[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)
    return text[:800]
