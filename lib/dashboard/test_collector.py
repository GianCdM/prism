"""Tests for the dashboard collector."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CollectorTest(unittest.TestCase):
    def setUp(self):
        # Isolate PRISM_HOME + DASHBOARD_HOME per test
        self.tmp_prism = tempfile.mkdtemp(prefix="prism-test-")
        self.tmp_dash = tempfile.mkdtemp(prefix="prism-dash-test-")
        os.environ["PRISM_HOME"] = self.tmp_prism
        os.environ["PRISM_DASHBOARD_HOME"] = self.tmp_dash

        # Force-reload the collector module so env vars take effect
        import importlib
        from . import collector as collector_module
        importlib.reload(collector_module)
        self.collector = collector_module

        # Seed structures
        (Path(self.tmp_prism) / "projects" / "deadbeef" / "engrams").mkdir(parents=True)
        (Path(self.tmp_prism) / "projects" / "deadbeef" / "observations.jsonl").write_text(
            json.dumps({
                "timestamp": "2026-05-19T10:00:00Z",
                "event": "tool_start",
                "tool": "Bash",
                "session": "session-abc",
                "project_id": "deadbeef",
                "input_summary": "ls",
            }) + "\n"
        )
        (Path(self.tmp_prism) / "metrics.jsonl").write_text(
            json.dumps({
                "timestamp": "2026-05-19T10:05:00Z",
                "session_id": "session-abc",
                "tool": "prism_search",
                "params": {"query": "test"},
                "result_count": 2,
                "engrams_returned": ["engram-a", "engram-b"],
                "duration_ms": 45,
            }) + "\n"
        )
        (Path(self.tmp_prism) / "index.json").write_text(json.dumps({
            "engrams": [
                {"id": "engram-a", "scope": "global", "kind": "preference",
                 "confidence": 0.9, "evidence_count": 3, "last_observed": "2026-05-19"},
                {"id": "engram-b", "scope": "project", "kind": "correction",
                 "confidence": 0.85, "evidence_count": 2, "last_observed": "2026-05-19",
                 "project_id": "deadbeef"},
            ]
        }))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_prism, ignore_errors=True)
        shutil.rmtree(self.tmp_dash, ignore_errors=True)

    def test_collect_engrams(self):
        conn = self.collector.ensure_db()
        n = self.collector.collect_engrams(conn)
        self.assertEqual(n, 2)
        row = conn.execute("SELECT * FROM engrams WHERE engram_id='engram-a'").fetchone()
        self.assertEqual(row["scope"], "global")
        self.assertEqual(row["confidence"], 0.9)
        conn.close()

    def test_collect_observations(self):
        conn = self.collector.ensure_db()
        n = self.collector.collect_observations(conn)
        self.assertEqual(n, 1)
        row = conn.execute("SELECT * FROM observations").fetchone()
        self.assertEqual(row["session_id"], "session-abc")
        self.assertEqual(row["tool_name"], "Bash")
        conn.close()

    def test_collect_mcp_calls(self):
        conn = self.collector.ensure_db()
        n = self.collector.collect_mcp_calls(conn)
        self.assertEqual(n, 1)
        # MCP call inserted
        row = conn.execute("SELECT * FROM mcp_calls").fetchone()
        self.assertEqual(row["tool_name"], "prism_search")
        # Engram events created (one per engram returned)
        events = conn.execute("SELECT * FROM engram_events ORDER BY engram_id").fetchall()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "search_hit")
        conn.close()

    def test_idempotent(self):
        """Running collect twice should not duplicate rows."""
        n1 = sum(self.collector.collect_all().values())
        n2 = sum(self.collector.collect_all().values())
        # Second run only re-counts session aggregates; new data = 0
        conn = self.collector.ensure_db()
        mcp_count = conn.execute("SELECT COUNT(*) c FROM mcp_calls").fetchone()["c"]
        obs_count = conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"]
        self.assertEqual(mcp_count, 1)
        self.assertEqual(obs_count, 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
