from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import config
from .offline import build_private_review, select_source
from .pipeline import RunManager
from .review import load_skill_policy
from .security import SafetyError, scrub_message
from .state import StateStore


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, manager: RunManager, store: StateStore, session_token: str):
        super().__init__(address, handler)
        self.manager = manager
        self.store = store
        self.session_token = session_token


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.command} {self.path.split('?', 1)[0]}\n")

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def _host_allowed(self) -> bool:
        host_value = self.headers.get("Host", "")
        try:
            parsed = urllib.parse.urlsplit("//" + host_value)
        except ValueError:
            return False
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == self.server.server_port

    def _mutation_allowed(self) -> bool:
        if not self._host_allowed():
            return False
        if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.session_token):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return False
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        return origin in allowed

    def _json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, message: object) -> None:
        self._json({"error": scrub_message(message, os.environ.get("LUNAROUTE_API_KEY"))}, status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SafetyError("Invalid request length.") from exc
        if length <= 0 or length > config.MAX_REQUEST_BYTES:
            raise SafetyError("Request body size is invalid.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SafetyError("Request body must be valid UTF-8 JSON.") from exc
        if not isinstance(value, dict):
            raise SafetyError("Request body must be a JSON object.")
        return value

    def _serve_static(self, name: str) -> None:
        allowed = {"index.html", "app.js", "styles.css"}
        if name not in allowed:
            self._error(404, "Not found")
            return
        path = config.STATIC_ROOT / name
        if not path.is_file():
            self._error(404, "Static asset is missing")
            return
        payload = path.read_bytes()
        if name == "index.html":
            payload = payload.replace(b"__WORKBENCH_TOKEN__", self.server.session_token.encode("ascii"))
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or name.endswith(".js"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self._security_headers(content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._error(403, "Host is not allowed")
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._serve_static("index.html")
            return
        if path == "/app.js":
            self._serve_static("app.js")
            return
        if path == "/styles.css":
            self._serve_static("styles.css")
            return
        if path == "/api/health":
            skills = {}
            for name in config.SKILL_PINS:
                try:
                    policy = load_skill_policy(name)
                    skills[name] = {"ready": True, "commit": policy.commit, "version": policy.version}
                except Exception as exc:
                    skills[name] = {"ready": False, "error": scrub_message(exc)}
            self._json({"app": config.APP_NAME, "version": config.APP_VERSION, "provider_key_configured": bool(os.environ.get("LUNAROUTE_API_KEY", "").strip()), "model": config.DEFAULT_BATCH_MODEL, "skills": skills})
            return
        if path == "/api/runs":
            self._json({"runs": self.server.store.list_runs()})
            return
        match = _match_run(path)
        if match:
            run_id, action = match
            run = self.server.store.get_run(run_id)
            if not run:
                self._error(404, "Run not found")
                return
            if action == "status":
                self._json({"run": run, "chapters": self.server.store.get_chapters(run_id)})
                return
        self._error(404, "Not found")

    def do_POST(self) -> None:
        if not self._mutation_allowed():
            self._error(403, "Request origin or workbench token is invalid")
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
            if path == "/api/private/inspect":
                source = select_source(str(body.get("source") or ""))
                self._json({"sha256": source.sha256, "size_bytes": source.size})
                return
            if path == "/api/private/review":
                fields = ("source", "output", "sha256", "edition", "owner")
                if any(not isinstance(body.get(field), str) for field in fields):
                    raise SafetyError("Select a source and supply an output folder, hash, edition and owner.")
                folder = build_private_review(*(body[field] for field in fields))
                self._json({"output_folder": str(folder), "status": "pending_human_review"}, HTTPStatus.CREATED)
                return
            if path == "/api/scan":
                self._json(self.server.manager.scan(str(body.get("input_root") or "")))
                return
            if path == "/api/runs":
                run_id = self.server.manager.create_and_start(body)
                self._json({"run_id": run_id}, HTTPStatus.CREATED)
                return
            match = _match_run(path)
            if match:
                run_id, action = match
                if action == "cancel":
                    self.server.manager.cancel(run_id)
                    self._json({"run_id": run_id, "status": "cancelling"})
                    return
                if action == "resume":
                    self.server.manager.resume(run_id)
                    self._json({"run_id": run_id, "status": "queued"})
                    return
                if action == "open":
                    run = self.server.store.get_run(run_id)
                    if not run:
                        raise SafetyError("Run not found.")
                    output = Path(run["output_root"])
                    if os.name != "nt":
                        raise SafetyError("Open Folder is available on Windows only.")
                    # The target is a validated run-owned directory and no shell is involved.
                    os.startfile(str(output))
                    self._json({"run_id": run_id, "opened": True, "output_root": str(output)})
                    return
            self._error(404, "Not found")
        except SafetyError as exc:
            self._error(400, exc)
        except Exception as exc:
            self._error(500, exc)

    def do_OPTIONS(self) -> None:
        self._error(405, "Cross-origin preflight is not supported")


def _match_run(path: str) -> tuple[str, str] | None:
    parts = [urllib.parse.unquote(part) for part in path.strip("/").split("/")]
    if len(parts) == 3 and parts[:2] == ["api", "runs"]:
        return parts[2], "status"
    if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in {"cancel", "resume", "open"}:
        return parts[2], parts[3]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=config.APP_NAME)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if args.port < 1024 or args.port > 65535:
        parser.error("port must be between 1024 and 65535")
    config.ensure_runtime_dirs()
    store = StateStore(config.DB_PATH)
    recovered = store.recover_orphaned_runs()
    manager = RunManager(store)
    token = secrets.token_urlsafe(32)
    server = WorkbenchServer((config.HOST, args.port), Handler, manager, store, token)
    url = f"http://{config.HOST}:{server.server_port}/"
    print(f"{config.APP_NAME} is running at {url}")
    if recovered:
        print(f"Recovered {recovered} interrupted run{'s' if recovered != 1 else ''}; use Resume from Recent runs.")
    print("Keep this terminal open while reviews are running. Press Ctrl+C to stop the workbench.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping workbench.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
