"""Supervised, single-file offline review. This module has no provider client."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

from . import config
from .artifacts import atomic_write_bytes, tree_sha256, utc_now, write_once_json
from .citations import build_inventory
from .ingest import extract_chapter
from .offline_export import export_offline_author_packet
from .private_paths import private_path, require_private_directory
from .security import SafetyError, SourceFile, read_approved_bytes, sha256_file, validate_output_parent
from .state import _process_is_alive

_STAGE_PATTERN = re.compile(r"\.incomplete-([0-9a-f]{32})-[a-z0-9_]{8}")
_PACKET_FILES = frozenset({"author-review.docx", "author-queries.md", "citation-inventory.json", "source-manifest.json",
                           "evidence-ledger.json", "decision-ledger.json", "run-receipt.json",
                           "source-snapshot.docx", "source-snapshot.md", "source-snapshot.txt"})


def recover_interrupted(output_parent: str | Path, discard: str | None = None) -> list[str]:
    """List abandoned stages or explicitly discard one recognized, inactive stage."""
    parent = private_path(output_parent)
    if not parent.is_dir() or (discard is not None and not _STAGE_PATTERN.fullmatch(discard)):
        raise SafetyError("Select the private output folder and an exact incomplete-stage name.")
    abandoned = []
    for stage in ([parent / discard] if discard else parent.glob(".incomplete-*")):
        match = _STAGE_PATTERN.fullmatch(stage.name)
        if not match or private_path(stage) != stage or not stage.is_dir():
            continue
        marker = stage / "stage.json"
        if not marker.is_file() or private_path(marker) != marker:
            continue
        info = json.loads(marker.read_text(encoding="utf-8"))
        pid = info.get("pid") if isinstance(info, dict) else None
        if not isinstance(pid, int) or pid <= 0 or info.get("run_id") != "PrivateReview-" + match[1] or _process_is_alive(pid):
            continue
        abandoned.append(stage.name)
        if discard:
            packet = stage / "packet"
            entries = list(stage.iterdir())
            if any(e not in {packet, marker} for e in entries):
                raise SafetyError("Unrecognized staging content. Inspect this folder manually.")
            if packet.exists():
                private_path(packet)
                files = list(packet.iterdir())
                for f in files:
                    temporary = re.fullmatch(r"\.(.+)\.[a-z0-9_]{8}\.tmp", f.name)
                    if (temporary[1] if temporary else f.name) not in _PACKET_FILES or private_path(f) != f or not f.is_file():
                        raise SafetyError("Unrecognized staging content. Inspect this folder manually.")
                for f in files:
                    f.unlink()
                packet.rmdir()
            marker.unlink()
            stage.rmdir()
    if discard and not abandoned:
        raise SafetyError("Stage is active, missing or unrecognized. Nothing was discarded.")
    return abandoned


def select_source(value: str | Path) -> SourceFile:
    path = private_path(value)
    if not path.is_file() or path.suffix.lower() not in config.SUPPORTED_EXTENSIONS or path.name.startswith("~$"):
        raise SafetyError("Select one DOCX, UTF-8 Markdown or text manuscript file.")
    stat = path.stat()
    if stat.st_nlink > 1:
        raise SafetyError("Private review requires a file without hard-link aliases.")
    if not 0 < stat.st_size <= config.MAX_FILE_BYTES:
        raise SafetyError("Select a nonempty manuscript of at most 20 MiB.")
    return SourceFile(path, path.name, stat.st_size, sha256_file(path))


def _edition_and_owner(edition: str, owner: str) -> None:
    if not all(isinstance(label, str) and 0 < len(label.strip()) <= 160 for label in (edition, owner)):
        raise SafetyError("Supply an edition or draft identifier and a case owner (up to 160 characters each).")


def build_private_review(source_path: str | Path, output_parent: str | Path, expected_sha256: str, edition: str, owner: str) -> Path:
    """Publish a complete local packet atomically, leaving repeated runs independent."""
    _edition_and_owner(edition, owner)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise SafetyError("Enter the full SHA-256 from the selected manuscript.")
    source = select_source(source_path)
    digest = expected_sha256.lower()
    if source.sha256 != digest:
        raise SafetyError("Source no longer matches the selected SHA-256. Inspect the edition again.")
    parent = validate_output_parent(private_path(output_parent), source.path.parent)
    payload = read_approved_bytes(source.path, digest)
    if source.path.suffix.lower() != ".docx":
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SafetyError("Text and Markdown chapters must be UTF-8 encoded.") from exc
        line_breaks = text.count("\n") + text.count("\r") - text.count("\r\n")
        if len(text) > 350_000 or line_breaks >= 8000:
            raise SafetyError("Offline text limit exceeded (350,000 characters or 8,000 physical lines). Select a smaller slice.")
    token = uuid.uuid4().hex
    run_id = "PrivateReview-" + token
    final = parent / run_id
    with tempfile.TemporaryDirectory(prefix=f".incomplete-{token}-", dir=parent) as temporary:
        staging = Path(temporary)
        require_private_directory(staging)
        write_once_json(staging / "stage.json", {"run_id": run_id, "pid": os.getpid(), "created_utc": utc_now()})
        packet = staging / "packet"
        packet.mkdir(mode=0o700)
        require_private_directory(packet)
        # The editor can resolve evidence against this exact file even if a later draft replaces the original.
        copy = packet / ("source-snapshot" + source.path.suffix.lower())
        atomic_write_bytes(copy, payload)
        chapter = extract_chapter(SourceFile(copy, source.relative_path, len(payload), digest), "CH001")
        if len(chapter.blocks) > 4000 or sum(len(b.text) for b in chapter.blocks) > 350_000:
            raise SafetyError("Offline single-chapter limit exceeded (4,000 blocks or 350,000 characters). Select a smaller slice.")
        inventory = build_inventory(chapter)
        findings = inventory["deterministic_findings"]
        if len(findings) > 500:
            raise SafetyError("More than 500 review queries. Select a smaller slice for a usable author packet.")
        manifest = {
            "schema_version": 1, "chapter_id": chapter.chapter_id, "run_id": run_id,
            "selected_file": str(source.path), "source_snapshot": copy.name, "sha256": digest,
            "size_bytes": len(payload), "edition_as_identified_by_operator": edition.strip(),
            "scope": "one selected file", "owner": owner.strip(),
        }
        write_once_json(packet / "source-manifest.json", manifest)
        write_once_json(packet / "citation-inventory.json", inventory)
        evidence = {
            "source_sha256": digest, "source_snapshot": copy.name, "chapter_id": chapter.chapter_id,
            "external_source_access": "not_attempted", "claim_support": "not_assessed",
            "locations": [{"location": b.locator, "context": b.context_type, "text": b.text} for b in chapter.blocks],
            "findings": [{k: f[k] for k in ("finding_id", "category", "location", "exact_text", "citation_ids", "reference_ids", "evidence")} for f in findings],
        }
        write_once_json(packet / "evidence-ledger.json", evidence)
        write_once_json(packet / "decision-ledger.json", {
            "run_id": run_id, "source_sha256": digest, "owner": owner.strip(), "publication_authorized": False,
            "instructions": "Keep this issued ledger with the receipt. Record human decisions in a separate working copy, keyed by finding_id.",
            "decisions": [{
                "finding_id": f["finding_id"], "location": f["location"], "author_query": f["author_query"],
                "status": "pending_human_review", "decision": None, "decided_by": None, "date": None, "rationale": None,
            } for f in findings],
        })
        annotations = export_offline_author_packet(chapter, inventory, packet)
        annotations["path"] = "author-review.docx"
        annotations["queries_path"] = "author-queries.md"
        hashes = {p.name: sha256_file(p) for p in sorted(packet.iterdir())}
        if hashes[copy.name] != digest or private_path(source.path) != source.path or sha256_file(source.path) != digest:
            raise SafetyError("Source changed during review. No completed packet was issued.")
        write_once_json(packet / "run-receipt.json", {
            "schema_version": 1, "run_id": run_id, "issued_utc": utc_now(),
            "implementation_sha256": tree_sha256(config.APP_ROOT / "upress_workbench"),
            "status": "offline_packet_built_pending_human_review", "mode": "offline_mechanical",
            "source_sha256_before": digest, "source_sha256_after": digest, "source_unchanged": True,
            "artifacts_sha256": hashes, "author_packet": annotations, "parser_coverage": inventory["parser_coverage"],
            "query_count": len(findings), "external_model_calls": 0, "metadata_lookups": 0,
            "rci_model_stage": "not_run", "sei_model_stage": "not_run", "source_support": "not_assessed",
            "publication_authorized": False, "editorial_acceptance": "pending",
        })
        os.rename(packet, final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local mechanical review of one private manuscript slice.")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Display the SHA-256 of one selected file")
    inspect.add_argument("--source", required=True)
    review = commands.add_parser("review", help="Build an offline Word packet for the selected SHA-256")
    for flag in ("source", "output", "sha256", "edition", "owner"):
        review.add_argument("--" + flag, required=True)
    recovery = commands.add_parser("recover", help="List interrupted stages or discard one inactive stage")
    recovery.add_argument("--output", required=True)
    recovery.add_argument("--discard", metavar="STAGE_NAME")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            print(select_source(args.source).sha256)
        elif args.command == "recover":
            for stage in recover_interrupted(args.output, args.discard):
                print(("Discarded " if args.discard else "") + stage)
        else:
            print(build_private_review(args.source, args.output, args.sha256, args.edition, args.owner))
    except SafetyError as exc:
        print(f"Review blocked. {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"Review blocked. Check file format, permissions and free space ({type(exc).__name__}).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
