"""Synthetic DOCX trust-boundary regressions; never open in Word or use network."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from upress_workbench.security import SafetyError, validate_docx_archive

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


class DocxIntakeTests(unittest.TestCase):
    def package(self, root, body="", extra=None, types=""):
        path = root / "synthetic.docx"
        members = {
            "[Content_Types].xml": f'<Types xmlns="{CT}">{types}</Types>',
            "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        }
        members.update(extra or {})
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return path

    def reject(self, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(SafetyError):
                validate_docx_archive(self.package(Path(temporary), **kwargs))

    def test_split_external_command_fields_rejected(self):
        for chunks in [
            ("DDE", "AUTO shell command"),
            ("INCLUDE", 'TEXT "https://example.invalid/private"'),
            ("INCLUDEPICTURE ", '"https://example.invalid/pixel"'),
            ("DATA", "BASE query"),
        ]:
            with self.subTest(chunks=chunks):
                body = '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                body += "".join(f"<w:r><w:instrText>{chunk}</w:instrText></w:r>" for chunk in chunks)
                body += '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
                self.reject(body=body)

    def test_simple_external_field_and_altchunk_rejected(self):
        self.reject(body='<w:p><w:fldSimple w:instr="LINK Excel.Sheet.12 unsafe"/></w:p>')
        self.reject(body="<w:altChunk/>")

    def test_active_content_disguised_in_opc_metadata_rejected(self):
        self.reject(extra={"word/innocent.bin": b"not executed"}, types='<Override PartName="/word/innocent.bin" ContentType="application/vnd.ms-office.vbaProject"/>')
        self.reject(
            extra={
                "word/_rels/document.xml.rels": f'<Relationships xmlns="{R}"><Relationship Id="rId1" Type="{REL}oleObject" Target="innocent.bin"/></Relationships>',
                "word/innocent.bin": b"not executed",
            }
        )

    def test_utf16_doctype_rejected(self):
        xml = f'<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE w:document [<!ENTITY sample "untrusted">]><w:document xmlns:w="{W}"><w:body/></w:document>'
        self.reject(extra={"word/document.xml": xml.encode("utf-16")})

    def test_malformed_xml_and_relationships_rejected(self):
        for extra in [
            {"word/document.xml": "<not-closed>"},
            {"word/_rels/document.xml.rels": "<Relationships/>"},
            {
                "word/_rels/document.xml.rels": f'<Relationships xmlns="{R}"><Relationship Id="r1" Type="{REL}hyperlink" Target="https://example.invalid" TargetMode="external"/></Relationships>'
            },
            {"word/_rels/document.xml.rels": f'<Relationships xmlns="{R}"><Relationship Id="r1" Type="{REL}image" Target="../../outside.png"/></Relationships>'},
        ]:
            with self.subTest(extra=extra):
                self.reject(extra=extra)

    def test_renamed_xml_part_is_still_inspected(self):
        self.reject(
            extra={"word/innocent.bin": f'<w:document xmlns:w="{W}"><w:body><w:altChunk/></w:body></w:document>'},
            types='<Override PartName="/word/innocent.bin" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>',
        )

    def test_duplicate_relationship_ids_and_malformed_archive_rejected(self):
        self.reject(
            extra={
                "word/_rels/document.xml.rels": f'<Relationships xmlns="{R}"><Relationship Id="r1" Type="{REL}hyperlink" Target="https://example.invalid" TargetMode="External"/><Relationship Id="r1" Type="{REL}hyperlink" Target="https://example.invalid" TargetMode="External"/></Relationships>'
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "not-a-zip.docx"
            path.write_bytes(b"not a zip archive")
            with self.assertRaises(SafetyError):
                validate_docx_archive(path)

    def test_unbalanced_and_unknown_fields_rejected(self):
        self.reject(body="<w:p><w:r><w:instrText>DDEAUTO</w:instrText></w:r></w:p>")
        self.reject(body='<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r></w:p>')
        self.reject(body='<w:p><w:fldSimple w:instr="UNKNOWNCODE value"/></w:p>')

    def test_benign_complex_field_split_across_runs_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            body = '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PA</w:instrText></w:r><w:r><w:instrText>GE</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
            validate_docx_archive(self.package(Path(temporary), body=body))

    def test_benign_fields_and_explicit_hyperlinks_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            body = '<w:p><w:fldSimple w:instr="PAGE"/><w:fldSimple w:instr="REF bookmark"/></w:p>'
            rels = f'<Relationships xmlns="{R}"><!-- harmless metadata comment --><Relationship Id="r1" Type="{REL}hyperlink" Target="https://example.invalid/source" TargetMode="External"/></Relationships>'
            validate_docx_archive(self.package(Path(temporary), body=body, extra={"word/_rels/document.xml.rels": rels}))


if __name__ == "__main__":
    unittest.main()
