from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from upress_workbench.pipeline import RunManager
from upress_workbench.server import Handler, WorkbenchServer
from upress_workbench.state import StateStore


class BrowserPrivateWorkflow(unittest.TestCase):
    def test_offline_http_selection_and_review_are_bound_to_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source" / "test.txt"
            source.parent.mkdir()
            source.write_text("A mechanical observation (Test, 2000).", encoding="utf-8")
            output = root / "private"
            output.mkdir()
            store = StateStore(root / "ui.sqlite3")
            token = "local-test-token"
            with WorkbenchServer(("127.0.0.1", 0), Handler, RunManager(store), store, token) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                endpoint = f"http://127.0.0.1:{server.server_port}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def post(route, data, origin=endpoint, header_token=token):
                    req = urllib.request.Request(endpoint + route, data=json.dumps(data).encode(), headers={
                        "Content-Type": "application/json", "Origin": origin, "X-Workbench-Token": header_token})
                    with opener.open(req, timeout=10) as response:  # loopback test server
                        return json.load(response)

                try:
                    with opener.open(endpoint + "/", timeout=5) as response:
                        page = response.read()
                        self.assertIn(b'id="private-form"', page)
                        self.assertIn(b"offline", page)
                        self.assertIn("frame-ancestors 'none'", response.headers.get("Content-Security-Policy"))
                    route = "/api/private/inspect"
                    for origin, credential in (("https://untrusted.invalid", token), (endpoint, "incorrect")):
                        with self.assertRaises(urllib.error.HTTPError) as blocked:
                            post(route, {"source": str(source)}, origin, credential)
                        self.assertEqual(blocked.exception.code, 403)
                    selected = post(route, {"source": str(source)})
                    args = {"source": str(source), "output": str(output), "owner": "Editor", "edition": "Fixture", "sha256": selected["sha256"]}
                    source.write_text("Second draft", encoding="utf-8")
                    with self.assertRaises(urllib.error.HTTPError) as stale:
                        post("/api/private/review", args)
                    self.assertEqual(stale.exception.code, 400)
                    self.assertEqual(list(output.iterdir()), [])
                    args["sha256"] = post(route, {"source": str(source)})["sha256"]
                    result = post("/api/private/review", args)
                    self.assertEqual(result["status"], "pending_human_review")
                    self.assertTrue((Path(result["output_folder"]) / "author-review.docx").is_file())
                    self.assertEqual(store.list_runs(), [])
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
