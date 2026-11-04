"""Synthetic recovery and local HTTP boundary regressions."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from upress_workbench.offline import recover_interrupted
from upress_workbench.pipeline import RunManager
from upress_workbench.security import SafetyError
from upress_workbench.server import Handler, WorkbenchServer
from upress_workbench.state import StateStore


class InterruptedRecoveryRegression(unittest.TestCase):
    def test_partial_marker_does_not_hide_valid_inactive_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stopped = subprocess.Popen([sys.executable, "-c", "pass"])
            self.assertEqual(stopped.wait(timeout=10), 0)
            valid_name = ".incomplete-" + "1" * 32 + "-abcdefgh"
            valid = root / valid_name
            valid.mkdir()
            (valid / "stage.json").write_text(json.dumps({"run_id": "PrivateReview-" + "1" * 32, "pid": stopped.pid}), encoding="utf-8")
            broken_name = ".incomplete-" + "2" * 32 + "-abcdefgh"
            broken = root / broken_name
            broken.mkdir()
            for payload in (b'{"run_id":', b'\xff'):
                with self.subTest(payload=payload):
                    (broken / "stage.json").write_bytes(payload)
                    self.assertEqual(recover_interrupted(root), [valid_name])
                    with self.assertRaisesRegex(SafetyError, "unrecognized"):
                        recover_interrupted(root, broken_name)
                    self.assertEqual((broken / "stage.json").read_bytes(), payload)
                    self.assertTrue(valid.is_dir())


class MalformedHttpRegression(unittest.TestCase):
    def test_malformed_auth_headers_return_403_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            with WorkbenchServer(("127.0.0.1", 0), Handler, RunManager(store), store, "test-token") as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for method, headers in (
                        ("POST", {"X-Workbench-Token": "é"}),
                        ("POST", {"Host": "localhost:invalid"}),
                        ("GET", {"Host": "localhost:invalid"}),
                    ):
                        with self.subTest(method=method, headers=headers):
                            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                            try:
                                connection.request(method, "/api/runs", body="{}", headers={
                                    "Origin": f"http://127.0.0.1:{server.server_port}",
                                    "X-Workbench-Token": "test-token", **headers,
                                })
                                try:
                                    response = connection.getresponse()
                                except http.client.RemoteDisconnected:
                                    self.fail("Malformed authentication headers closed the connection instead of returning 403")
                                self.assertEqual(response.status, 403)
                                self.assertIn("error", json.loads(response.read()))
                                self.assertEqual(store.list_runs(), [])
                            finally:
                                connection.close()
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        connection.request("GET", "/")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read()
                    finally:
                        connection.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
