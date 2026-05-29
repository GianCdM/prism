"""Flask dashboard server — http://localhost:7878 by default.

Routes:
  /             Overview cards + timeline + top engrams
  /sessions     Sessions table with filters + drill-down
  /sessions/<id>  Session detail
  /engrams      Engrams ranking + filters
  /engrams/<id>   Engram detail
  /coverage     % of sessions with prism_search calls
  /api/refresh  POST — runs collector + returns stats
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from flask import Flask, jsonify, render_template, request
except ImportError as e:
    raise SystemExit(
        "Flask is required for the dashboard. Install with: pip install flask\n"
        f"(import error: {e})"
    )

from .collector import DB_PATH, collect_all, ensure_db

try:
    from ..config import DEFAULT_CONFIG, get_config
except ImportError:
    # Fallback if running standalone (shouldn't happen in practice)
    DEFAULT_CONFIG = {"max_push_items": 10, "max_context_lines": 100}

    def get_config() -> dict:
        return dict(DEFAULT_CONFIG)


app = Flask(__name__, template_folder="templates", static_folder="static")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _config_view() -> list[dict]:
    """Surface key Prism config values with default-vs-custom annotation.

    Returns a list of {key, value, default, is_custom, description} dicts
    for the keys that meaningfully affect engram lifecycle/push layer.
    """
    cfg = get_config()
    keys = [
        ("max_push_items", "engrams pushed into prism.md (the active context layer)"),
        ("max_context_lines", "max lines in the generated prism.md file"),
        ("decay_rate_per_week", "confidence drop per week without observation"),
        ("archive_threshold", "engrams below this confidence are archived"),
        ("extract_threshold", "observations before auto-extraction triggers"),
    ]
    out = []
    for k, desc in keys:
        val = cfg.get(k, DEFAULT_CONFIG.get(k))
        default = DEFAULT_CONFIG.get(k)
        out.append({
            "key": k,
            "value": val,
            "default": default,
            "is_custom": val != default,
            "description": desc,
        })
    return out


def _select_prism_md_entries(conn: sqlite3.Connection, max_items: int = 10) -> list:
    """Replicate the prism.md push layer selection (lib/sync.py _select_prompt_entries).

    Priority:
      1. Pinned (always included)
      2. Corrections with conf >= 0.8 (forced — Claude does not search past errors)
      3. Top N by confidence (fills the rest)
    Final cap: max_items.

    Adds `hits` field (count of engram_events) for each row.
    """
    selected: list = []
    selected_ids: set = set()

    def _add(row):
        if row["engram_id"] not in selected_ids:
            d = dict(row)
            hits = conn.execute(
                "SELECT COUNT(*) c FROM engram_events WHERE engram_id=?",
                (row["engram_id"],)
            ).fetchone()["c"]
            d["hits"] = hits
            selected.append(d)
            selected_ids.add(row["engram_id"])

    # 1. Pinned
    for r in conn.execute("SELECT * FROM engrams WHERE pinned=1 ORDER BY confidence DESC"):
        _add(r)

    # 2. Corrections with conf >= 0.8
    for r in conn.execute(
        "SELECT * FROM engrams WHERE kind='correction' AND confidence >= 0.8 ORDER BY confidence DESC"
    ):
        if len(selected) >= max_items:
            break
        _add(r)

    # 3. Top remaining by confidence
    for r in conn.execute("SELECT * FROM engrams ORDER BY confidence DESC"):
        if len(selected) >= max_items:
            break
        _add(r)

    return selected[:max_items]


def _date_range_from_request() -> tuple[str | None, str | None]:
    start = request.args.get("start")
    end = request.args.get("end")
    return start, end


def _apply_date_filter(query: str, params: list, start: str | None, end: str | None, col: str = "timestamp") -> tuple[str, list]:
    if start:
        query += f" AND {col} >= ?"
        params.append(start)
    if end:
        query += f" AND {col} <= ?"
        params.append(end + "T23:59:59")
    return query, params


@app.route("/")
def overview():
    """Cards + timeline + top engrams."""
    start, end = _date_range_from_request()
    project_filter = request.args.get("project_id")

    conn = _conn()
    try:
        # Totals — sessions/obs/with_search come from the sessions table
        q = "SELECT COUNT(*) c, COALESCE(SUM(observations_count), 0) obs, COALESCE(SUM(has_search), 0) with_search FROM sessions WHERE 1=1"
        params: list = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        row = conn.execute(q, params).fetchone()
        total_sessions = row["c"] or 0
        total_obs = row["obs"] or 0
        sessions_with_search = row["with_search"] or 0
        coverage_pct = round(100 * sessions_with_search / total_sessions, 1) if total_sessions else 0

        # MCP totals + breakdown — authoritative source: mcp_calls table directly
        # (avoids inconsistency when calls exist without an attributed session_id).
        q = "SELECT COUNT(*) c FROM mcp_calls WHERE 1=1"
        params = []
        q, params = _apply_date_filter(q, params, start, end)
        total_mcp = conn.execute(q, params).fetchone()["c"] or 0

        q = "SELECT tool_name, COUNT(*) c FROM mcp_calls WHERE 1=1"
        params = []
        q, params = _apply_date_filter(q, params, start, end)
        q += " GROUP BY tool_name ORDER BY c DESC"
        mcp_breakdown = [dict(r) for r in conn.execute(q, params)]

        # Total engrams
        engrams_count = conn.execute("SELECT COUNT(*) c FROM engrams").fetchone()["c"] or 0
        pinned_count = conn.execute("SELECT COUNT(*) c FROM engrams WHERE pinned=1").fetchone()["c"] or 0

        # Timeline: sessions per day
        q = """
            SELECT date(started_at) d, COUNT(*) sessions,
                   COALESCE(SUM(mcp_calls_count), 0) mcp_calls,
                   COALESCE(SUM(observations_count), 0) observations
            FROM sessions WHERE 1=1
        """
        params = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        q += " GROUP BY date(started_at) ORDER BY d"
        timeline = [dict(r) for r in conn.execute(q, params)]

        # Hourly (last 48h) — group each metric by its own event timestamp
        # so MCP calls in a long-running session land on the call's hour,
        # not the session's started_at hour.
        cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        hours_per_metric: dict[str, dict[str, int]] = {}

        def _bucket(query: str, key: str, params_local: list) -> None:
            for row in conn.execute(query, params_local):
                h = row[0]
                if not h:
                    continue
                hours_per_metric.setdefault(h, {"sessions": 0, "mcp_calls": 0, "observations": 0})
                hours_per_metric[h][key] = row[1]

        # Sessions (by started_at)
        q = "SELECT strftime('%Y-%m-%d %H:00', started_at) h, COUNT(*) FROM sessions WHERE started_at >= ?"
        p: list = [cutoff_48h]
        if project_filter:
            q += " AND project_id=?"
            p.append(project_filter)
        q += " GROUP BY h"
        _bucket(q, "sessions", p)

        # MCP calls (by call timestamp)
        q = "SELECT strftime('%Y-%m-%d %H:00', timestamp) h, COUNT(*) FROM mcp_calls WHERE timestamp >= ? GROUP BY h"
        _bucket(q, "mcp_calls", [cutoff_48h])

        # Observations (by event timestamp)
        q = "SELECT strftime('%Y-%m-%d %H:00', timestamp) h, COUNT(*) FROM observations WHERE timestamp >= ?"
        p = [cutoff_48h]
        if project_filter:
            q += " AND project_id=?"
            p.append(project_filter)
        q += " GROUP BY h"
        _bucket(q, "observations", p)

        hourly_48h = [
            {"h": h, **vals}
            for h, vals in sorted(hours_per_metric.items())
        ]

        # Engrams in the prism.md push layer — replicates Prism's selection.
        # Reads max_push_items from user's config so overview reflects reality.
        max_push = get_config().get("max_push_items", 10)
        top_engrams = _select_prism_md_entries(conn, max_items=max_push)

        # Top by MCP hits in the last 30d (actual engagement)
        last_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        q = """
            SELECT e.engram_id, eg.scope, eg.kind, eg.confidence, eg.pinned,
                   COUNT(*) hits
            FROM engram_events e
            LEFT JOIN engrams eg ON eg.engram_id = e.engram_id
            WHERE e.timestamp >= ?
            GROUP BY e.engram_id
            ORDER BY hits DESC LIMIT 10
        """
        top_by_hits = [dict(r) for r in conn.execute(q, [last_30])]

        # Recent sessions
        q = """
            SELECT session_id, started_at, duration_sec, project_id,
                   observations_count, mcp_calls_count, engrams_reinforced_count, has_search
            FROM sessions WHERE 1=1
        """
        params = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        q += " ORDER BY started_at DESC LIMIT 10"
        recent_sessions = [dict(r) for r in conn.execute(q, params)]

        # Projects list for filter
        projects = [r["project_id"] for r in conn.execute("SELECT DISTINCT project_id FROM sessions WHERE project_id IS NOT NULL ORDER BY project_id")]
    finally:
        conn.close()

    return render_template(
        "overview.html",
        total_sessions=total_sessions,
        total_obs=total_obs,
        total_mcp=total_mcp,
        sessions_with_search=sessions_with_search,
        coverage_pct=coverage_pct,
        mcp_breakdown=mcp_breakdown,
        engrams_count=engrams_count,
        pinned_count=pinned_count,
        timeline=timeline,
        hourly_48h=hourly_48h,
        top_engrams=top_engrams,
        top_by_hits=top_by_hits,
        recent_sessions=recent_sessions,
        projects=projects,
        active_project=project_filter,
        start=start,
        end=end,
        config_view=_config_view(),
    )


@app.route("/sessions")
def sessions():
    """Sessions table with filters."""
    start, end = _date_range_from_request()
    project_filter = request.args.get("project_id")
    has_mcp = request.args.get("has_mcp")

    conn = _conn()
    try:
        q = """
            SELECT session_id, started_at, duration_sec, project_id, cwd,
                   observations_count, mcp_calls_count, engrams_reinforced_count, has_search
            FROM sessions WHERE 1=1
        """
        params: list = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        if has_mcp == "1":
            q += " AND mcp_calls_count > 0"
        elif has_mcp == "0":
            q += " AND mcp_calls_count = 0"
        q += " ORDER BY started_at DESC LIMIT 200"
        rows = [dict(r) for r in conn.execute(q, params)]

        projects = [r["project_id"] for r in conn.execute("SELECT DISTINCT project_id FROM sessions WHERE project_id IS NOT NULL ORDER BY project_id")]
    finally:
        conn.close()

    return render_template(
        "sessions.html",
        sessions=rows,
        projects=projects,
        active_project=project_filter,
        has_mcp=has_mcp,
        start=start,
        end=end,
    )


@app.route("/sessions/<session_id>")
def session_detail(session_id: str):
    """Drill-down of a single session."""
    conn = _conn()
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not sess:
            return f"Session {session_id} not found", 404

        # MCP calls
        mcp = [dict(r) for r in conn.execute(
            "SELECT * FROM mcp_calls WHERE session_id=? ORDER BY timestamp", (session_id,)
        )]

        # Engram events
        engrams_used = [dict(r) for r in conn.execute(
            """
            SELECT e.engram_id, e.event_type, e.timestamp, eg.kind, eg.scope, eg.confidence, eg.pinned
            FROM engram_events e
            LEFT JOIN engrams eg ON eg.engram_id = e.engram_id
            WHERE e.session_id=?
            ORDER BY e.timestamp
            """, (session_id,)
        )]

        # Observations breakdown
        obs_summary = [dict(r) for r in conn.execute(
            """
            SELECT event_type, tool_name, COUNT(*) c
            FROM observations WHERE session_id=?
            GROUP BY event_type, tool_name ORDER BY c DESC
            """, (session_id,)
        )]
    finally:
        conn.close()

    return render_template(
        "session_detail.html",
        session=dict(sess),
        mcp_calls=mcp,
        engrams_used=engrams_used,
        observations_summary=obs_summary,
    )


@app.route("/engrams")
def engrams_list():
    """Engrams ranking with filters."""
    scope = request.args.get("scope")
    kind = request.args.get("kind")
    min_conf = request.args.get("min_conf", type=float)
    with_hits = request.args.get("with_hits")

    conn = _conn()
    try:
        # Aggregate counts for header cards (always ALL — ignores filters)
        totals = {"total": 0, "global": 0, "project": 0, "pinned": 0, "with_hits": 0}
        totals["total"] = conn.execute("SELECT COUNT(*) c FROM engrams").fetchone()["c"]
        totals["global"] = conn.execute("SELECT COUNT(*) c FROM engrams WHERE scope='global'").fetchone()["c"]
        totals["project"] = conn.execute("SELECT COUNT(*) c FROM engrams WHERE scope='project'").fetchone()["c"]
        totals["pinned"] = conn.execute("SELECT COUNT(*) c FROM engrams WHERE pinned=1").fetchone()["c"]
        totals["with_hits"] = conn.execute(
            "SELECT COUNT(DISTINCT engram_id) c FROM engram_events"
        ).fetchone()["c"]

        # Counts by kind
        kind_counts = [
            dict(r) for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM engrams GROUP BY kind ORDER BY c DESC"
            )
        ]

        # Counts by confidence range
        conf_buckets = {
            "≥0.90": conn.execute("SELECT COUNT(*) c FROM engrams WHERE confidence >= 0.9").fetchone()["c"],
            "0.80–0.89": conn.execute("SELECT COUNT(*) c FROM engrams WHERE confidence >= 0.8 AND confidence < 0.9").fetchone()["c"],
            "0.70–0.79": conn.execute("SELECT COUNT(*) c FROM engrams WHERE confidence >= 0.7 AND confidence < 0.8").fetchone()["c"],
            "0.60–0.69": conn.execute("SELECT COUNT(*) c FROM engrams WHERE confidence >= 0.6 AND confidence < 0.7").fetchone()["c"],
            "<0.60": conn.execute("SELECT COUNT(*) c FROM engrams WHERE confidence < 0.6").fetchone()["c"],
        }

        # Listing with filters
        q = """
            SELECT eg.*,
                   (SELECT COUNT(*) FROM engram_events e WHERE e.engram_id = eg.engram_id) hits
            FROM engrams eg WHERE 1=1
        """
        params: list = []
        if scope:
            q += " AND eg.scope=?"
            params.append(scope)
        if kind:
            q += " AND eg.kind=?"
            params.append(kind)
        if min_conf is not None:
            q += " AND eg.confidence >= ?"
            params.append(min_conf)
        if with_hits == "1":
            q += " AND (SELECT COUNT(*) FROM engram_events e WHERE e.engram_id = eg.engram_id) > 0"
        q += " ORDER BY eg.confidence DESC, hits DESC LIMIT 500"
        rows = [dict(r) for r in conn.execute(q, params)]
    finally:
        conn.close()

    return render_template(
        "engrams.html",
        engrams=rows,
        totals=totals,
        kind_counts=kind_counts,
        conf_buckets=conf_buckets,
        scope=scope, kind=kind, min_conf=min_conf, with_hits=with_hits,
    )


@app.route("/engrams/<engram_id>")
def engram_detail(engram_id: str):
    """Drill-down of a single engram."""
    conn = _conn()
    try:
        eg = conn.execute("SELECT * FROM engrams WHERE engram_id=?", (engram_id,)).fetchone()
        if not eg:
            return f"Engram {engram_id} not found", 404

        events = [dict(r) for r in conn.execute(
            "SELECT * FROM engram_events WHERE engram_id=? ORDER BY timestamp DESC LIMIT 100",
            (engram_id,)
        )]

        # Sessions where this engram was used
        sessions = [dict(r) for r in conn.execute(
            """
            SELECT DISTINCT s.session_id, s.started_at, s.project_id, s.cwd
            FROM engram_events e
            JOIN sessions s ON s.session_id = e.session_id
            WHERE e.engram_id=?
            ORDER BY s.started_at DESC LIMIT 30
            """, (engram_id,)
        )]

        # Try to read source content
        from .collector import PRISM_HOME
        body = ""
        if eg["path"]:
            source = PRISM_HOME / eg["path"]
            if source.exists():
                body = source.read_text()[:5000]
    finally:
        conn.close()

    return render_template(
        "engram_detail.html",
        engram=dict(eg),
        events=events,
        sessions=sessions,
        body=body,
    )


@app.route("/coverage")
def coverage():
    """% of sessions with at least one prism_search/relevant call."""
    start, end = _date_range_from_request()
    project_filter = request.args.get("project_id")

    conn = _conn()
    try:
        # Coverage by day
        q = """
            SELECT date(started_at) d,
                   COUNT(*) total,
                   COALESCE(SUM(has_search), 0) with_search
            FROM sessions WHERE 1=1
        """
        params: list = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        q += " GROUP BY date(started_at) ORDER BY d"
        daily = []
        for r in conn.execute(q, params):
            row = dict(r)
            row["coverage_pct"] = round(100 * row["with_search"] / row["total"], 1) if row["total"] else 0
            daily.append(row)

        # Sessions without search (opportunities)
        q = """
            SELECT session_id, started_at, project_id, cwd, observations_count
            FROM sessions WHERE has_search=0
        """
        params = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        q += " ORDER BY started_at DESC LIMIT 50"
        sessions_without_search = [dict(r) for r in conn.execute(q, params)]

        # Histogram: MCP calls per session
        q = """
            SELECT mcp_calls_count, COUNT(*) c FROM sessions WHERE 1=1
        """
        params = []
        q, params = _apply_date_filter(q, params, start, end, "started_at")
        if project_filter:
            q += " AND project_id=?"
            params.append(project_filter)
        q += " GROUP BY mcp_calls_count ORDER BY mcp_calls_count"
        histogram = [dict(r) for r in conn.execute(q, params)]

        # Overall
        total = sum(d["total"] for d in daily)
        with_search = sum(d["with_search"] for d in daily)
        overall_pct = round(100 * with_search / total, 1) if total else 0

        # --- Push waste + Pull discovery (engram-level effectiveness, 30d window) ---
        # Push waste:    % of push-layer engrams that received 0 MCP hits in 30d.
        #                High = passive context delivered but never actively re-queried.
        # Pull discovery: % of non-push engrams that received >=1 MCP hit in 30d.
        #                Low = MCP search is decorative, Claude only consumes the push layer.
        max_push = get_config().get("max_push_items", 10)
        push_layer = _select_prism_md_entries(conn, max_items=max_push)
        push_layer_ids = [e["engram_id"] for e in push_layer]
        push_total = len(push_layer)

        last_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        hit_ids_30d = {
            r["engram_id"]
            for r in conn.execute(
                "SELECT DISTINCT engram_id FROM engram_events WHERE timestamp >= ?",
                (last_30,),
            )
        }

        push_with_hits = sum(1 for eid in push_layer_ids if eid in hit_ids_30d)
        push_no_hits = push_total - push_with_hits
        push_waste_pct = round(100 * push_no_hits / push_total, 1) if push_total else 0

        all_engram_ids = {
            r["engram_id"] for r in conn.execute("SELECT engram_id FROM engrams")
        }
        non_push_ids = all_engram_ids - set(push_layer_ids)
        non_push_total = len(non_push_ids)
        non_push_with_hits = len(non_push_ids & hit_ids_30d)
        pull_discovery_pct = (
            round(100 * non_push_with_hits / non_push_total, 1)
            if non_push_total
            else 0
        )

        # Push-layer engrams currently unused — actionable list
        push_no_hits_list = [e for e in push_layer if e["engram_id"] not in hit_ids_30d]

        projects = [r["project_id"] for r in conn.execute("SELECT DISTINCT project_id FROM sessions WHERE project_id IS NOT NULL ORDER BY project_id")]
    finally:
        conn.close()

    return render_template(
        "coverage.html",
        daily=daily,
        sessions_without_search=sessions_without_search,
        histogram=histogram,
        total=total,
        with_search=with_search,
        overall_pct=overall_pct,
        push_waste_pct=push_waste_pct,
        push_no_hits=push_no_hits,
        push_total=push_total,
        push_no_hits_list=push_no_hits_list,
        pull_discovery_pct=pull_discovery_pct,
        non_push_with_hits=non_push_with_hits,
        non_push_total=non_push_total,
        max_push=max_push,
        projects=projects,
        active_project=project_filter,
        start=start,
        end=end,
    )


@app.route("/attribution")
def attribution():
    """Knowledge delivery reframed as 3 channels — Push / Pull / Dormant — plus the
    influence proxy (which pushed engrams actually surface in observed work)."""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM engrams").fetchone()["c"] or 0
        pushed = {r["engram_id"] for r in conn.execute(
            "SELECT DISTINCT engram_id FROM engram_events WHERE event_type='push'")}
        pulled = {r["engram_id"] for r in conn.execute(
            "SELECT DISTINCT engram_id FROM engram_events "
            "WHERE event_type IN ('search_hit','get_hit','relevant_hit')")}
        influenced = {r["engram_id"] for r in conn.execute(
            "SELECT DISTINCT engram_id FROM engram_events WHERE event_type='observation_match'")}
        reached = pushed | pulled
        dormant_n = max(0, total - len(reached))

        srow = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(has_search), 0) s FROM sessions").fetchone()
        total_sessions = srow["c"] or 0
        with_search = srow["s"] or 0

        def _pct(n):
            return round(100 * n / total, 1) if total else 0

        push_n = len(pushed)
        push_infl = len(pushed & influenced)
        c = {
            "total": total,
            "push_n": push_n, "push_pct": _pct(push_n),
            "push_influencing": push_infl,
            "influence_rate": round(100 * push_infl / push_n, 1) if push_n else 0,
            "pull_n": len(pulled), "pull_pct": _pct(len(pulled)),
            "pull_only_n": len(pulled - pushed),
            "coverage_pct": round(100 * with_search / total_sessions, 1) if total_sessions else 0,
            "with_search": with_search, "total_sessions": total_sessions,
            "dormant_n": dormant_n, "dormant_pct": _pct(dormant_n),
        }

        influence = [dict(r) for r in conn.execute("""
            SELECT e.engram_id, eg.kind, eg.confidence, eg.pinned,
                   COUNT(DISTINCT e.session_id) sessions
            FROM engram_events e
            LEFT JOIN engrams eg ON eg.engram_id = e.engram_id
            WHERE e.event_type = 'observation_match'
            GROUP BY e.engram_id
            ORDER BY sessions DESC LIMIT 40
        """)]

        push_silent = [dict(r) for r in conn.execute("""
            SELECT eg.engram_id, eg.kind, eg.confidence, eg.pinned
            FROM engrams eg
            WHERE eg.engram_id IN (SELECT engram_id FROM engram_events WHERE event_type='push')
              AND eg.engram_id NOT IN (SELECT engram_id FROM engram_events WHERE event_type='observation_match')
            ORDER BY eg.confidence DESC
        """)]
    finally:
        conn.close()
    return render_template("attribution.html", c=c, influence=influence, push_silent=push_silent)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Run collector and return stats."""
    stats = collect_all()
    return jsonify(stats)


def serve(host: str = "127.0.0.1", port: int = 7878, debug: bool = False) -> None:
    """Run the Flask server."""
    ensure_db().close()
    # Initial collect on startup
    try:
        stats = collect_all()
        print(f"Initial collect: {stats}")
    except Exception as e:
        print(f"Initial collect failed (continuing): {e}")
    print(f"Prism Dashboard: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    serve()
