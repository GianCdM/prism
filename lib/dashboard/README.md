# Prism Dashboard

Local metrics dashboard for Prism: track MCP tool usage, session activity, engram reinforcement, and search coverage.

## What it tracks

- **Sessions** — Claude Code transcripts (start/end time, duration, project, cwd)
- **MCP calls** — every `prism_search`, `prism_relevant`, `prism_get`, `prism_record` call (tool, params, result count, duration, engrams returned)
- **Engrams** — current state snapshot from `index.json` (confidence, evidence count, pinned, scope, kind)
- **Engram events** — push-layer reinforcement, search hits, observation matches
- **Observations** — tool calls captured by Prism hooks (event type, summary)

## Storage

- `~/.prism-dashboard/metrics.db` — SQLite, embedded
- `~/.prism/metrics.jsonl` — append-only log written by the instrumented MCP server

Both are local. No network, no third-party tracking.

## Run it

```bash
# Start the web server (default: http://127.0.0.1:7878)
prism dashboard serve

# Run collector once without serving (manual refresh)
prism dashboard collect

# Wipe local metrics DB and restart
prism dashboard reset
```

Dependency: Flask (`pip install flask`). Chart.js is loaded from CDN.

## Views

| Route | What it shows |
|---|---|
| `/` | Cards (totals, coverage %), activity timeline, top engrams by hits (30d), recent sessions |
| `/sessions` | Sessions table with filters (date range, project, has MCP calls) |
| `/sessions/<id>` | Drill-down: MCP calls timeline, engrams referenced, observations breakdown |
| `/engrams` | Engrams ranking with filters (scope, kind, min confidence, with hits) |
| `/engrams/<id>` | Drill-down: events timeline, sessions where used, source content |
| `/coverage` | % of sessions with `prism_search`/`prism_relevant` calls, daily trend, histogram of calls/session, list of sessions without search |

## What is "coverage"?

The fraction of sessions where Claude called `prism_search` or `prism_relevant` at least once. A low coverage means Claude is relying only on the **push layer** (top 10 engrams loaded at boot) and is not searching the broader knowledge base. Pinning critical engrams or expanding `max_items` in `sync.py` can mitigate this.

## Instrumentation

The MCP server (`lib/mcp_server.py`) writes one JSONL record per tool call to `~/.prism/metrics.jsonl` with:

```json
{
  "timestamp": "2026-05-19T22:00:00Z",
  "session_id": "abc123...",
  "tool": "prism_search",
  "params": {"query": "...", "limit": 5},
  "result_count": 3,
  "engrams_returned": ["engram-a", "engram-b", "engram-c"],
  "duration_ms": 42
}
```

The session_id is read from `CLAUDE_SESSION_ID` env var (set by Claude Code). If absent, `session_id` is null but the metric still records.

## Limitations

- Only metrics generated **after** instrumentation are captured. Historical MCP calls are not recoverable.
- `~/.prism/metrics.jsonl` grows unbounded. Run `prism dashboard collect` periodically to drain it (the collector tracks offsets, so re-reading is fast).
- Session boundaries are inferred from transcript files in `~/.claude/projects/`. Hooks-only sessions without a transcript won't appear.
- SQLite is single-machine. No sync.

## Tests

```bash
cd ~/src/prism
python3 -m unittest lib.dashboard.test_collector
```
