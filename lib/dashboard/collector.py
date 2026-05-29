"""Collect Prism metrics from filesystem sources into local SQLite.

Sources:
  - ~/.prism/metrics.jsonl              (MCP calls — written by instrumented mcp_server)
  - ~/.prism/projects/*/observations.jsonl (+ archive)
  - ~/.prism/global/observations.jsonl     (+ archive)
  - ~/.prism/index.json                 (engram state snapshot)
  - ~/.claude/projects/*/<session>.jsonl (Claude Code transcripts)

Idempotent: re-running re-reads everything but skips already-imported rows
(uses last_seen offsets in collector_state).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRISM_HOME = Path(os.environ.get("PRISM_HOME", str(Path.home() / ".prism")))
DASHBOARD_HOME = Path(os.environ.get("PRISM_DASHBOARD_HOME", str(Path.home() / ".prism-dashboard")))
DB_PATH = DASHBOARD_HOME / "metrics.db"
METRICS_JSONL = PRISM_HOME / "metrics.jsonl"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def ensure_db() -> sqlite3.Connection:
    """Open/create DB with schema applied."""
    DASHBOARD_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def _get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM collector_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO collector_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def collect_engrams(conn: sqlite3.Connection) -> int:
    """Snapshot engrams from index.json into engrams table.

    Two-way sync: inserts/updates from index AND deletes rows for engrams that
    no longer exist in the index (forgotten/archived). engram_events are
    preserved so historical MCP hits keep showing in queries.
    """
    index_path = PRISM_HOME / "index.json"
    if not index_path.exists():
        return 0
    try:
        idx = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    current_ids = {e["id"] for e in idx.get("engrams", [])}
    db_ids = {r["engram_id"] for r in conn.execute("SELECT engram_id FROM engrams")}
    stale = db_ids - current_ids
    if stale:
        conn.executemany(
            "DELETE FROM engrams WHERE engram_id=?",
            [(eid,) for eid in stale],
        )

    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for e in idx.get("engrams", []):
        conn.execute(
            """
            INSERT INTO engrams(engram_id, scope, kind, domain, confidence,
                                evidence_count, last_observed, pinned,
                                project_id, path, trigger, snapshot_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(engram_id) DO UPDATE SET
                scope=excluded.scope,
                kind=excluded.kind,
                domain=excluded.domain,
                confidence=excluded.confidence,
                evidence_count=excluded.evidence_count,
                last_observed=excluded.last_observed,
                pinned=excluded.pinned,
                project_id=excluded.project_id,
                path=excluded.path,
                trigger=excluded.trigger,
                snapshot_at=excluded.snapshot_at
            """,
            (
                e["id"],
                e.get("scope"),
                e.get("kind"),
                e.get("domain"),
                e.get("confidence", 0),
                e.get("evidence_count", 0),
                e.get("last_observed"),
                1 if e.get("pinned") else 0,
                e.get("project_id"),
                e.get("path"),
                e.get("trigger"),
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def collect_observations(conn: sqlite3.Connection) -> int:
    """Import observations: prefer prism.db (SQLite), fall back to legacy JSONL."""
    prism_db = PRISM_HOME / "prism.db"
    if prism_db.exists():
        return _collect_observations_sqlite(conn, prism_db)
    return _collect_observations_jsonl(conn)


def _collect_observations_sqlite(conn: sqlite3.Connection, prism_db: Path) -> int:
    """Read new observations from prism.db incrementally (by row id)."""
    try:
        last_id = int(_get_state(conn, "prism_db_last_obs_id", "0"))
    except ValueError:
        last_id = 0

    try:
        src = sqlite3.connect(f"file:{prism_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        rows = src.execute(
            "SELECT id, session_id, project_id, event, tool, input_summary, ts "
            "FROM observations WHERE id > ? ORDER BY id",
            (last_id,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        src.close()

    inserted = 0
    max_id = last_id
    for oid, session_id, project_id, event, tool, summary, ts in rows:
        try:
            timestamp = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if ts else ""
            )
        except (ValueError, OSError, OverflowError):
            timestamp = ""
        conn.execute(
            """
            INSERT INTO observations(session_id, timestamp, event_type,
                                      tool_name, project_id, summary)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (session_id, timestamp, event, tool, project_id, (summary or "")[:300]),
        )
        inserted += 1
        if oid > max_id:
            max_id = oid

    _set_state(conn, "prism_db_last_obs_id", str(max_id))
    conn.commit()
    return inserted


def _collect_observations_jsonl(conn: sqlite3.Connection) -> int:
    """Legacy: read observations.jsonl from all project buckets (live + archive)."""
    last_inode_offset_json = _get_state(conn, "observations_offsets", "{}")
    try:
        offsets: dict[str, int] = json.loads(last_inode_offset_json)
    except json.JSONDecodeError:
        offsets = {}

    files: list[Path] = []
    # Live observations
    for path in PRISM_HOME.glob("projects/*/observations.jsonl"):
        files.append(path)
    if (PRISM_HOME / "global" / "observations.jsonl").exists():
        files.append(PRISM_HOME / "global" / "observations.jsonl")
    # Archive
    for path in PRISM_HOME.glob("global/observations.archive/*.jsonl"):
        files.append(path)
    for path in PRISM_HOME.glob("projects/*/observations.archive/*.jsonl"):
        files.append(path)

    inserted = 0
    for path in files:
        try:
            stat = path.stat()
            key = f"{stat.st_ino}:{path.name}"
            last_offset = offsets.get(key, 0)
            if stat.st_size <= last_offset:
                continue

            with path.open() as f:
                f.seek(last_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obs = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    conn.execute(
                        """
                        INSERT INTO observations(session_id, timestamp, event_type,
                                                  tool_name, project_id, summary)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            obs.get("session"),
                            obs.get("timestamp"),
                            obs.get("event"),
                            obs.get("tool"),
                            obs.get("project_id"),
                            (obs.get("input_summary") or "")[:300],
                        ),
                    )
                    inserted += 1
                offsets[key] = f.tell()
        except OSError:
            continue

    _set_state(conn, "observations_offsets", json.dumps(offsets))
    conn.commit()
    return inserted


def collect_mcp_calls(conn: sqlite3.Connection) -> int:
    """Read ~/.prism/metrics.jsonl (instrumented MCP server log)."""
    if not METRICS_JSONL.exists():
        return 0

    last_offset = int(_get_state(conn, "metrics_offset", "0"))
    stat = METRICS_JSONL.stat()
    if stat.st_size <= last_offset:
        return 0

    inserted = 0
    with METRICS_JSONL.open() as f:
        f.seek(last_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = rec.get("session_id")
            timestamp = rec.get("timestamp")
            tool_name = rec.get("tool")
            params = rec.get("params", {})
            result_count = rec.get("result_count")
            duration_ms = rec.get("duration_ms")
            engrams_returned = rec.get("engrams_returned") or []

            conn.execute(
                """
                INSERT INTO mcp_calls(session_id, timestamp, tool_name,
                                      params_json, result_count, duration_ms)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, timestamp, tool_name,
                    json.dumps(params, ensure_ascii=False),
                    result_count, duration_ms,
                ),
            )

            # Map engram events by tool type
            event_type_map = {
                "prism_search": "search_hit",
                "prism_relevant": "relevant_hit",
                "prism_get": "get_hit",
                "prism_record": "record_create",
                "hook_retrieve": "retrieve_hit",
            }
            event_type = event_type_map.get(tool_name, "unknown")
            for engram_id in engrams_returned:
                conn.execute(
                    """
                    INSERT INTO engram_events(session_id, engram_id, timestamp, event_type)
                    VALUES(?, ?, ?, ?)
                    """,
                    (session_id, engram_id, timestamp, event_type),
                )

            inserted += 1
        _set_state(conn, "metrics_offset", str(f.tell()))

    conn.commit()
    return inserted


def collect_sessions(conn: sqlite3.Connection) -> int:
    """Scan Claude Code transcripts to enumerate sessions + start/end times.

    cwd is read directly from the first message in the transcript that has it
    populated (Claude Code messages carry the authoritative cwd). Falls back
    to decoding the project folder name only if the transcript has no cwd.
    """
    if not CLAUDE_PROJECTS.exists():
        return 0

    inserted = 0
    for project_dir in CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        # Naive decode from folder name as fallback (loses `.` vs `/` info)
        fallback_cwd = "/" + project_dir.name.replace("-", "/").lstrip("/")

        for transcript in project_dir.glob("*.jsonl"):
            session_id = transcript.stem
            started_at = None
            ended_at = None
            cwd = None

            try:
                with transcript.open() as f:
                    for line in f:
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = msg.get("timestamp")
                        if ts:
                            if started_at is None:
                                started_at = ts
                            ended_at = ts
                        if cwd is None and msg.get("cwd"):
                            cwd = msg["cwd"]
            except OSError:
                continue

            if not started_at:
                continue
            if not cwd:
                cwd = fallback_cwd

            try:
                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                duration_sec = int((end_dt - start_dt).total_seconds())
            except (ValueError, AttributeError):
                duration_sec = None

            conn.execute(
                """
                INSERT INTO sessions(session_id, cwd, started_at, ended_at,
                                     duration_sec, transcript_path)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    cwd=excluded.cwd,
                    ended_at=excluded.ended_at,
                    duration_sec=excluded.duration_sec
                """,
                (session_id, cwd, started_at, ended_at, duration_sec, str(transcript)),
            )
            inserted += 1
    conn.commit()
    return inserted


_CWD_TO_PROJECT_ID_CACHE: dict[str, str | None] = {}


def _infer_project_id_from_cwd(cwd: str | None) -> str | None:
    """Same logic as Prism's detect_project_id: SHA256[:12] of git remote URL,
    or repo root path as fallback. Cached per cwd within a single collect run.
    """
    if not cwd:
        return None
    if cwd in _CWD_TO_PROJECT_ID_CACHE:
        return _CWD_TO_PROJECT_ID_CACHE[cwd]

    import hashlib
    import subprocess

    project_id: str | None = None
    cwd_path = Path(cwd)
    if cwd_path.exists() and cwd_path.is_dir():
        # 1. Try git remote URL
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0 and r.stdout.strip():
                project_id = hashlib.sha256(r.stdout.strip().encode()).hexdigest()[:12]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # 2. Fallback: repo root path
        if not project_id:
            try:
                r = subprocess.run(
                    ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0 and r.stdout.strip():
                    project_id = hashlib.sha256(r.stdout.strip().encode()).hexdigest()[:12]
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

    _CWD_TO_PROJECT_ID_CACHE[cwd] = project_id
    return project_id


def update_session_aggregates(conn: sqlite3.Connection) -> int:
    """Compute observations_count, mcp_calls_count, etc. per session."""
    _CWD_TO_PROJECT_ID_CACHE.clear()  # fresh cache per run

    updated = 0
    for row in conn.execute("SELECT session_id, cwd, project_id FROM sessions"):
        sid = row["session_id"]
        cwd = row["cwd"]
        existing_project_id = row["project_id"]

        obs = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id=?", (sid,)
        ).fetchone()[0]
        # mcp_calls_count = MCP READS only (search/relevant/get). Records (writes) and
        # hook_retrieve (separate channel) are queried independently where shown.
        mcp = conn.execute(
            "SELECT COUNT(*) FROM mcp_calls WHERE session_id=? "
            "AND tool_name IN ('prism_search','prism_relevant','prism_get')", (sid,)
        ).fetchone()[0]
        has_search = conn.execute(
            "SELECT COUNT(*) FROM mcp_calls WHERE session_id=? AND tool_name IN ('prism_search', 'prism_relevant')",
            (sid,),
        ).fetchone()[0] > 0
        reinforced = conn.execute(
            "SELECT COUNT(DISTINCT engram_id) FROM engram_events WHERE session_id=?",
            (sid,),
        ).fetchone()[0]

        # Project_id: priority
        #   1. already populated by an observation (most reliable)
        #   2. inferred via git on the cwd (same logic as Prism)
        project_id = existing_project_id
        if not project_id:
            obs_row = conn.execute(
                "SELECT project_id FROM observations WHERE session_id=? AND project_id IS NOT NULL LIMIT 1",
                (sid,),
            ).fetchone()
            if obs_row:
                project_id = obs_row["project_id"]
            else:
                project_id = _infer_project_id_from_cwd(cwd)

        conn.execute(
            """
            UPDATE sessions SET
                observations_count=?,
                mcp_calls_count=?,
                has_search=?,
                engrams_reinforced_count=?,
                project_id=COALESCE(?, project_id)
            WHERE session_id=?
            """,
            (obs, mcp, 1 if has_search else 0, reinforced, project_id, sid),
        )
        updated += 1
    conn.commit()
    return updated


def collect_push_events(conn: sqlite3.Connection) -> int:
    """Record a 'push' event for each engram currently in the prism.md push layer.

    Mirrors prism's push selection (sync._select_prompt_entries): pinned, then
    corrections with confidence >= 0.8, then top-N by confidence, capped at
    max_push_items. Deduped per (engram, UTC-day) so repeated collects don't pile up.

    This makes the PUSH channel visible: previously only MCP pull generated events,
    so the dashboard couldn't tell engrams DELIVERED via push from truly DORMANT
    ones (never pushed nor pulled).
    """
    max_push = 20
    try:
        cfg = json.loads((PRISM_HOME / "config.json").read_text())
        max_push = int(cfg.get("max_push_items", 20))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        pass

    selected: list[str] = []
    seen: set[str] = set()

    def _add(rows) -> None:
        for r in rows:
            eid = r[0]
            if eid not in seen and len(selected) < max_push:
                selected.append(eid)
                seen.add(eid)

    _add(conn.execute("SELECT engram_id FROM engrams WHERE pinned=1 ORDER BY confidence DESC"))
    _add(conn.execute("SELECT engram_id FROM engrams WHERE kind='correction' AND confidence >= 0.8 ORDER BY confidence DESC"))
    _add(conn.execute("SELECT engram_id FROM engrams ORDER BY confidence DESC"))
    push_ids = selected[:max_push]
    if not push_ids:
        return 0

    row = conn.execute("SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
    session_id = row[0] if row else None

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    for eid in push_ids:
        already = conn.execute(
            "SELECT 1 FROM engram_events WHERE engram_id=? AND event_type='push' "
            "AND substr(timestamp,1,10)=? LIMIT 1",
            (eid, today),
        ).fetchone()
        if already:
            continue
        conn.execute(
            "INSERT INTO engram_events(session_id, engram_id, timestamp, event_type) "
            "VALUES(?, ?, ?, ?)",
            (session_id, eid, ts, "push"),
        )
        inserted += 1
    conn.commit()
    return inserted


def collect_influence_events(conn: sqlite3.Connection) -> int:
    """Heuristic influence proxy: record 'observation_match' when a PUSHED engram's
    distinctive terms show up in a session's observations.

    Overlap-based (no LLM): tokens from the engram id + trigger, matched against
    observation summaries. A FLOOR signal — strong for technical engrams (distinctive
    tokens like 'mermaid', 'pptx'), weak for style preferences (behavioral, no
    keywords). Requires >=2 distinct term hits per (engram, session) to cut false
    positives. Deduped per (engram, session).
    """
    import re
    from collections import defaultdict

    STOP = {
        "quando", "usar", "para", "sempre", "escrever", "codigo", "arquivo", "arquivos",
        "projeto", "sessao", "sessoes", "claude", "prism", "engram", "engrams", "sobre",
        "entender", "decidir", "fazer", "quero", "preciso", "tambem", "ainda", "entao",
        "onde", "como", "mais", "deve", "cada", "skill", "skills", "tool", "tools",
        "with", "when", "that", "this", "into", "from", "your", "should", "always",
        "about", "which", "their", "there", "using", "while", "where", "before", "after",
        "value", "field", "files",
    }

    pushed = [r[0] for r in conn.execute(
        "SELECT DISTINCT engram_id FROM engram_events WHERE event_type='push'"
    ).fetchall()]
    if not pushed:
        return 0

    term_to_engrams: dict = defaultdict(set)
    for eid in pushed:
        row = conn.execute("SELECT trigger FROM engrams WHERE engram_id=?", (eid,)).fetchone()
        trigger = row[0] if row and row[0] else ""
        blob = (eid.replace("-", " ") + " " + trigger).lower()
        for t in set(re.split(r"[^a-z0-9]+", blob)):
            if len(t) >= 5 and t not in STOP:
                term_to_engrams[t].add(eid)
    if not term_to_engrams:
        return 0

    sess_eng_terms: dict = defaultdict(lambda: defaultdict(set))
    for sid, summary in conn.execute(
        "SELECT session_id, summary FROM observations "
        "WHERE session_id IS NOT NULL AND summary IS NOT NULL"
    ):
        for tok in set(re.split(r"[^a-z0-9]+", summary.lower())):
            for eid in term_to_engrams.get(tok, ()):
                sess_eng_terms[sid][eid].add(tok)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    for sid, eng_terms in sess_eng_terms.items():
        for eid, matched in eng_terms.items():
            if len(matched) < 2:
                continue
            if conn.execute(
                "SELECT 1 FROM engram_events WHERE engram_id=? AND session_id=? "
                "AND event_type='observation_match' LIMIT 1",
                (eid, sid),
            ).fetchone():
                continue
            conn.execute(
                "INSERT INTO engram_events(session_id, engram_id, timestamp, event_type) "
                "VALUES(?, ?, ?, ?)",
                (sid, eid, ts, "observation_match"),
            )
            inserted += 1
    conn.commit()
    return inserted


def collect_all() -> dict[str, int]:
    """Run all collectors. Returns stats per source."""
    conn = ensure_db()
    try:
        stats = {
            "engrams": collect_engrams(conn),
            "observations": collect_observations(conn),
            "mcp_calls": collect_mcp_calls(conn),
            "sessions": collect_sessions(conn),
        }
        stats["push_events"] = collect_push_events(conn)
        stats["influence_events"] = collect_influence_events(conn)
        stats["sessions_aggregated"] = update_session_aggregates(conn)
        return stats
    finally:
        conn.close()


if __name__ == "__main__":
    stats = collect_all()
    print(f"Collected: {stats}")
