from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import config
from .artifacts import (
    append_jsonl,
    atomic_write_bytes,
    atomic_write_json,
    object_sha256,
    utc_now,
    write_once_json,
)
from .citations import build_inventory
from .crossref import lookup_many
from .ingest import extract_chapter
from .packets import build_chapter_packet, run_summary_html
from .private_paths import private_path
from .provider import CallBudget, LunaRouteClient
from .review import author_items, load_skill_policy, run_rci, run_sei
from .security import (
    SafetyError,
    SourceFile,
    scan_sources,
    scrub_message,
    sha256_file,
    validate_input_root,
    validate_output_parent,
)
from .state import StateStore


class RunManager:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def scan(self, input_root: str) -> dict[str, Any]:
        root = validate_input_root(private_path(input_root))
        sources = scan_sources(root)
        return {
            "input_root": str(root),
            "chapters": [{"relative_path": source.relative_path, "size": source.size, "sha256": source.sha256} for source in sources],
            "count": len(sources),
            "total_bytes": sum(source.size for source in sources),
            "supported_extensions": sorted(config.SUPPORTED_EXTENSIONS),
        }

    def create_and_start(self, request: dict[str, Any]) -> str:
        input_root = validate_input_root(private_path(str(request.get("input_root") or "")))
        output_parent = validate_output_parent(private_path(str(request.get("output_parent") or "")), input_root)
        project_label = str(request.get("project_label") or "University Press chapter review").strip()[:160]
        case_owner = str(request.get("case_owner") or "").strip()[:160]
        classification = str(request.get("classification") or "")
        provider_approved = request.get("provider_approved") is True
        model = str(request.get("model") or config.DEFAULT_BATCH_MODEL)
        crossref_enabled = request.get("crossref_enabled") is True
        concurrency = int(request.get("concurrency") or 3)
        run_mode = str(request.get("run_mode") or "baseline")
        if classification not in {"public", "unpublished_approved", "restricted"}:
            raise SafetyError("Choose a valid manuscript classification.")
        if classification == "restricted":
            raise SafetyError("Restricted material is blocked from external processing in this MVP.")
        if not provider_approved:
            raise SafetyError("Confirm that LunaRoute is an approved data path for this chapter set.")
        if not case_owner:
            raise SafetyError("Name the editor or case owner for this run.")
        if model not in config.ALLOWED_MODELS:
            raise SafetyError("Choose an allowlisted GLM 5.3 model.")
        if concurrency < 1 or concurrency > config.MAX_WORKERS:
            raise SafetyError(f"Concurrency must be between 1 and {config.MAX_WORKERS}.")
        if run_mode not in {"baseline", "incremental", "full", "release", "proof"}:
            raise SafetyError("Choose a valid RCI run mode.")
        if not os.environ.get("LUNAROUTE_API_KEY", "").strip():
            raise SafetyError("LUNAROUTE_API_KEY is not available in the process environment.")

        rci_policy = load_skill_policy("reference-citation-integrity")
        sei_policy = load_skill_policy("scholarly-editorial-integrity")
        sources = scan_sources(input_root)
        if not sources:
            raise SafetyError("No supported chapter files were found.")
        timestamp = utc_now().replace("-", "").replace(":", "").replace("Z", "Z")
        run_id = f"UPW-{timestamp}-{uuid.uuid4().hex[:8]}"
        output_root = output_parent / f"UniversityPress-Review-{timestamp}-{run_id[-8:]}"
        output_root.mkdir(parents=False, exist_ok=False)
        approval_at = utc_now()
        manifest_records = [
            {"chapter_id": f"CH{index:03d}", "relative_path": source.relative_path, "source_path": str(source.path), "size": source.size, "sha256": source.sha256}
            for index, source in enumerate(sources, 1)
        ]
        manifest = {"schema_version": 1, "run_id": run_id, "created_at": approval_at, "input_root": str(input_root), "records": manifest_records}
        manifest_hash = object_sha256(manifest)
        manifest["manifest_sha256"] = manifest_hash
        plan = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": approval_at,
            "project_label": project_label,
            "company_owner": "University Press",
            "navy_yard_design_authority": "Navy Yard Workbench Foundry",
            "policy_bound_plan": True,
            "stages": ["source_manifest", "RCI-01..07", "SEI-01..10", "author_packet", "independent_human_acceptance"],
            "skill_commits": {rci_policy.name: rci_policy.commit, sei_policy.name: sei_policy.commit},
            "model": model,
            "provider": {"name": "LunaRoute", "endpoint_host": "gw.lunaroute.com", "approved_at": approval_at, "classification": classification, "manifest_sha256": manifest_hash},
            "crossref_enabled": crossref_enabled,
            "concurrency": concurrency,
            "run_mode": run_mode,
            "prohibited_effects": ["source overwrite", "automatic identity-bearing edit", "publication", "formal allegation", "unapproved external processing"],
            "reserved_human_decisions": ["accept or reject edits", "resolve exceptions", "adjudicate serious concerns", "authorize publication"],
            "manifest_sha256": manifest_hash,
        }
        constitution = json.loads((config.APP_ROOT / "config" / "company-constitution.json").read_text(encoding="utf-8"))
        workbench_configuration = json.loads((config.APP_ROOT / "config" / "workbench-configuration.json").read_text(encoding="utf-8"))
        plan["company_constitution_sha256"] = object_sha256(constitution)
        plan["workbench_configuration_sha256"] = object_sha256(workbench_configuration)
        plan_hash = object_sha256(plan)
        plan["plan_sha256"] = plan_hash
        write_once_json(output_root / "00_SOURCE_MANIFEST.json", manifest)
        write_once_json(output_root / "00_IMMUTABLE_PLAN.json", plan)
        write_once_json(output_root / "00_COMPANY_CONSTITUTION.json", constitution)
        write_once_json(output_root / "00_WORKBENCH_CONFIGURATION.json", workbench_configuration)
        record = {
            "run_id": run_id,
            "created_at": approval_at,
            "updated_at": approval_at,
            "status": "queued",
            "input_root": str(input_root),
            "output_root": str(output_root),
            "project_label": project_label,
            "case_owner": case_owner,
            "classification": classification,
            "provider_approved": 1,
            "approval_at": approval_at,
            "model": model,
            "crossref_enabled": int(crossref_enabled),
            "concurrency": concurrency,
            "run_mode": run_mode,
            "worker_pid": os.getpid(),
            "heartbeat_at": approval_at,
            "manifest_hash": manifest_hash,
            "plan_hash": plan_hash,
            "error": None,
            "budget_json": json.dumps({}),
        }
        self.store.create_run(record, manifest_records)
        self._emit(run_id, "run_created", {"chapter_count": len(sources), "manifest_sha256": manifest_hash, "plan_sha256": plan_hash})
        self._start_thread(run_id)
        return run_id

    def _start_thread(self, run_id: str) -> None:
        with self._lock:
            existing = self._threads.get(run_id)
            if existing and existing.is_alive():
                raise SafetyError("Run is already active.")
            cancel = threading.Event()
            thread = threading.Thread(target=self._execute, args=(run_id, cancel), name=f"upress-{run_id[-8:]}", daemon=True)
            self._cancel[run_id] = cancel
            self._threads[run_id] = thread
            self.store.update_run(run_id, worker_pid=os.getpid(), heartbeat_at=utc_now())
            thread.start()

    def resume(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if not run:
            raise SafetyError("Run not found.")
        if run["status"] == "complete":
            raise SafetyError("Completed runs cannot be resumed.")
        if not run["provider_approved"] or run["classification"] == "restricted":
            raise SafetyError("The provider approval gate is not satisfied.")
        self.store.reset_incomplete(run_id)
        self.store.update_run(run_id, status="queued", error=None)
        self._start_thread(run_id)

    def cancel(self, run_id: str) -> None:
        with self._lock:
            event = self._cancel.get(run_id)
        if not event:
            raise SafetyError("Run is not active.")
        event.set()
        self.store.update_run(run_id, status="cancelling")
        self._emit(run_id, "cancel_requested", {})

    def _emit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        safe_payload = json.loads(json.dumps(payload, ensure_ascii=False))
        self.store.update_run(run_id, heartbeat_at=utc_now())
        self.store.append_event(run_id, event_type, safe_payload)
        run = self.store.get_run(run_id)
        if run:
            append_jsonl(Path(run["output_root"]) / "events.jsonl", {"created_at": utc_now(), "event": event_type, **safe_payload})

    def _execute(self, run_id: str, cancel: threading.Event) -> None:
        run = self.store.get_run(run_id)
        if run is None:
            return
        chapters = [item for item in self.store.get_chapters(run_id) if item["status"] != "packet_built"]
        budget = CallBudget(max_calls=min(config.MAX_MODEL_CALLS_PER_RUN, max(2, len(chapters) * 2)), max_input_chars=max(2_000_000, len(chapters) * config.MAX_MODEL_INPUT_CHARS * 2))
        try:
            rci_policy = load_skill_policy("reference-citation-integrity")
            sei_policy = load_skill_policy("scholarly-editorial-integrity")
            self.store.update_run(run_id, status="running", error=None, worker_pid=os.getpid(), heartbeat_at=utc_now())
            self._emit(run_id, "run_started", {"pending_chapters": len(chapters), "model": run["model"]})
            with ThreadPoolExecutor(max_workers=run["concurrency"], thread_name_prefix="chapter-review") as executor:
                futures = {executor.submit(self._process_chapter, run, item, budget, rci_policy, sei_policy, cancel): item for item in chapters}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        message = scrub_message(exc, os.environ.get("LUNAROUTE_API_KEY"))
                        self.store.update_chapter(run_id, item["chapter_id"], status="failed", stage="failed", error=message)
                        self._emit(run_id, "chapter_failed", {"chapter_id": item["chapter_id"], "error": message})
                    if cancel.is_set():
                        for other in futures:
                            other.cancel()
            statuses = self.store.get_chapters(run_id)
            completed = sum(1 for item in statuses if item["status"] == "packet_built")
            failed = sum(1 for item in statuses if item["status"] in {"failed", "source_drifted", "blocked"})
            if cancel.is_set():
                final_status = "cancelled"
            elif completed == len(statuses):
                final_status = "complete"
            elif completed:
                final_status = "partial"
            else:
                final_status = "failed"
            self.store.update_run(run_id, status=final_status, budget_json=json.dumps(budget.snapshot()), worker_pid=None, heartbeat_at=utc_now())
            self._finalize(run_id, failed)
        except Exception as exc:
            message = scrub_message(exc, os.environ.get("LUNAROUTE_API_KEY"))
            self.store.update_run(run_id, status="failed", error=message, budget_json=json.dumps(budget.snapshot()), worker_pid=None, heartbeat_at=utc_now())
            self._emit(run_id, "run_failed", {"error": message})
            self._finalize(run_id, 1)

    def _process_chapter(self, run: dict[str, Any], item: dict[str, Any], budget: CallBudget, rci_policy, sei_policy, cancel: threading.Event) -> None:
        run_id = run["run_id"]
        chapter_id = item["chapter_id"]
        source_path = Path(item["source_path"])
        source = SourceFile(source_path, item["relative_path"], item["size"], item["sha256"])
        if cancel.is_set():
            return
        if sha256_file(source_path) != item["sha256"]:
            self.store.update_chapter(run_id, chapter_id, status="source_drifted", stage="blocked", error="Source hash changed after approval.")
            self._emit(run_id, "source_drifted", {"chapter_id": chapter_id})
            return
        chapter = extract_chapter(source, chapter_id)
        inventory = build_inventory(chapter)
        crossref_records = lookup_many(inventory["dois"]) if run["crossref_enabled"] else []
        if cancel.is_set():
            return
        if sha256_file(source_path) != item["sha256"]:
            self.store.update_chapter(run_id, chapter_id, status="source_drifted", stage="blocked", error="Source hash changed before external processing.")
            self._emit(run_id, "source_drifted", {"chapter_id": chapter_id})
            return
        client = LunaRouteClient(run["model"], budget)
        self.store.update_chapter(run_id, chapter_id, status="rci_running", stage="rci")
        self._emit(run_id, "rci_started", {"chapter_id": chapter_id})
        rci_result, rci_workbench = run_rci(client, rci_policy, chapter, inventory, crossref_records, "UniversityPress-APA7-pilot-v0.1", run["run_mode"])
        self.store.update_chapter(run_id, chapter_id, status="rci_done", stage="rci_complete", rci_findings=len(rci_result["findings"]))
        self._emit(run_id, "rci_finished", {"chapter_id": chapter_id, "release_status": rci_result["release_status"], "findings": len(rci_result["findings"])})
        if cancel.is_set():
            return
        if sha256_file(source_path) != item["sha256"]:
            self.store.update_chapter(run_id, chapter_id, status="source_drifted", stage="blocked", error="Source hash changed before scholarly review.")
            self._emit(run_id, "source_drifted", {"chapter_id": chapter_id})
            return
        self.store.update_chapter(run_id, chapter_id, status="sei_running", stage="sei")
        sei_result, sei_workbench = run_sei(client, sei_policy, chapter, inventory, rci_result, crossref_records, run["case_owner"], run["classification"])
        self.store.update_chapter(run_id, chapter_id, status="packet_building", stage="packet", sei_findings=len(sei_result["findings"]))
        self._emit(run_id, "sei_finished", {"chapter_id": chapter_id, "release_status": sei_result["release_status"], "findings": len(sei_result["findings"])})
        if sha256_file(source_path) != item["sha256"]:
            self.store.update_chapter(run_id, chapter_id, status="source_drifted", stage="blocked", error="Source hash changed before packet assembly.")
            self._emit(run_id, "source_drifted", {"chapter_id": chapter_id})
            return
        items = author_items(rci_result, rci_workbench, sei_result, sei_workbench)
        receipt = build_chapter_packet(chapter, inventory, items, rci_result, rci_workbench, sei_result, sei_workbench, Path(run["output_root"]))
        review_html = Path(receipt["review_html"]["path"]).relative_to(Path(run["output_root"])).as_posix()
        packet = {"review_html_relative": review_html, "author_packet": receipt["author_packet"]["path"], "chapter_receipt": str(Path(receipt["review_html"]["path"]).parent.parent / "editorial-record" / "chapter-receipt.json")}
        self.store.update_chapter(run_id, chapter_id, status="packet_built", stage="complete", packet_json=json.dumps(packet), error=None)
        for finding in items:
            append_jsonl(Path(run["output_root"]) / "learning-observations.jsonl", {"schema_version": 1, "recorded_at": utc_now(), "run_id": run_id, "chapter_id": chapter_id, "finding_id": finding["id"], "family": finding["family"], "observation": finding["reason"], "proposal": finding.get("proposed_change") or finding.get("author_query"), "human_decision": None, "accepted": None})
        self._emit(run_id, "chapter_complete", {"chapter_id": chapter_id, "author_facing_items": len(items)})

    def _finalize(self, run_id: str, failed_count: int) -> None:
        run = self.store.get_run(run_id)
        if not run:
            return
        chapters = self.store.get_chapters(run_id)
        output_root = Path(run["output_root"])
        receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": run["created_at"],
            "completed_at": utc_now(),
            "status": run["status"],
            "project_label": run["project_label"],
            "case_owner": run["case_owner"],
            "source_manifest_sha256": run["manifest_hash"],
            "immutable_plan_sha256": run["plan_hash"],
            "skill_commits": config.SKILL_PINS,
            "model": run["model"],
            "provider_approval": {"approved": run["provider_approved"], "approved_at": run["approval_at"], "classification": run["classification"], "manifest_sha256": run["manifest_hash"]},
            "chapters": [{"chapter_id": item["chapter_id"], "relative_path": item["relative_path"], "source_sha256": item["sha256"], "status": item["status"], "rci_findings": item["rci_findings"], "sei_findings": item["sei_findings"], "error": item["error"], "packet": item["packet"]} for item in chapters],
            "budget": run["budget"],
            "exceptions": failed_count,
            "source_overwritten": False,
            "publication_authorized": False,
            "acceptance_required_from": run["case_owner"],
        }
        receipt["receipt_sha256"] = object_sha256(receipt)
        atomic_write_json(output_root / "00_RUN_RECEIPT.json", receipt)
        atomic_write_bytes(output_root / "00_OPEN_RUN_SUMMARY.html", run_summary_html(run, chapters).encode("utf-8"))
        self._emit(run_id, "run_finished", {"status": run["status"], "completed": sum(1 for item in chapters if item["status"] == "packet_built"), "failed": failed_count})
