# Prism Technical Documentation

Comprehensive reference for Prism's architecture, data formats, pipelines, and configuration.

> **Supported IDEs**: Prism works with both **Claude Code** and **Cursor**. `prism init` configures both automatically (hooks, MCP server, skills/rules, and context files). To keep this document readable, examples below mostly say "Claude Code" — unless a section calls out a difference, everything applies equally to Cursor. The integration-point differences (hook scripts, MCP registration paths, context file locations) are summarized in [Project Initialization](#project-initialization) and the [File System Layout](#file-system-layout).

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Installation](#installation)
- [Project Initialization](#project-initialization)
- [Observation Pipeline](#observation-pipeline)
- [Extraction Pipeline](#extraction-pipeline)
- [Engram Lifecycle](#engram-lifecycle)
- [Context Injection](#context-injection)
- [MCP Server](#mcp-server)
- [CLI Reference](#cli-reference)
- [Slash Commands](#slash-commands)
- [Engram-to-Skill Promotion](#engram-to-skill-promotion)
- [Team Registry](#team-registry)
- [Data Formats](#data-formats)
- [Configuration Reference](#configuration-reference)
- [Security](#security)
- [File System Layout](#file-system-layout)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

Prism sits between your IDE (Claude Code or Cursor) and a knowledge store. It has three layers:

```
Claude Code / Cursor session
  |                    |
  | hooks (observe)    | MCP (query)
  v                    v
+----------------------------------+
|            Prism                  |
|                                  |
|  Observations -> Extraction ->   |
|  Engrams -> Context Injection    |
|                                  |
|  Engrams -> Promotion -> Skills  |
|  Skills -> Registry (team)       |
+----------------------------------+
  |
  v
~/.prism/  (file-based storage)
```

**Personal layer**: Hooks capture tool usage as observations. A two-phase extraction pipeline (fast model proposes, strong model validates — via `claude` or Cursor `agent` depending on backend) converts patterns into engrams. Engrams flow back into the IDE through context files (push) and MCP tools (pull).

**Team layer**: High-confidence engrams can be promoted to skill format. Slash commands mine codebases and git history for additional skills. Skills are published to a Cloudflare Worker-backed registry that teams query.

**Key constraints**:
- Zero runtime Python dependencies (stdlib only)
- Hooks never block the IDE (exit 0 always); extraction, review, and daily sync spawn in the background after a fast synchronous observation insert
- AI calls go through IDE-native CLIs (`claude` or Cursor `agent`), not the Anthropic SDK or cursor-sdk
- Observations stored in SQLite (`prism.db`, WAL mode, FTS5); engrams, index, and config remain flat files (Markdown, JSON)

---

## Installation

### Prerequisites

| Requirement | Version | Required |
|-------------|---------|----------|
| Python | 3.12+ | Yes |
| git | any | Yes |
| Claude Code **or** Cursor | current | Yes (one IDE) |
| `claude` CLI | latest | For Claude Code extraction (`claude login`) |
| `agent` CLI | latest | For Cursor extraction (`agent login`) |

You need **one** extraction CLI for your IDE — not both. Observation capture works without either; engram generation does not. No Anthropic SDK, cursor-sdk, or API keys for the personal layer — only `claude login` or `agent login`.

`prism init` configures hooks and MCP for **both** IDEs. If you use only one, the other integration files sit unused until you open that IDE.

### Install

```bash
git clone <repo-url> && cd prism
./install.sh
```

### What `install.sh` does

1. **Checks prerequisites**: python3 (hard fail), git (hard fail), claude or agent CLI (soft warning), Python 3.12+ (recommended warning)
2. **Creates directory tree**: `~/.prism/{global/engrams, archive, hooks, agents, lib, skills, projects, cache, schemas, templates}` (`prism.db` is created on first observation)
3. **Copies source files**: hooks/, agents/, lib/*.py, skills/*/, schemas/*.json, templates/
4. **Writes defaults** (only if missing):
   - `config.json` with a minimal threshold subset (runtime merges full defaults from `lib/config.py`)
   - `index.json` with empty engram list
   - `registries.json` with the public read-only registry
   - `constitution.md` from template (never overwritten on upgrades)
5. **Creates CLI symlink**: `~/.local/bin/prism` -> `~/.prism/prism`
6. **Verifies PATH**: warns if `~/.local/bin` is not in `$PATH`

The installer is idempotent. Re-running updates code but preserves config, index, constitution, and project data.

### Verify

```bash
prism --help
```

---

## Project Initialization

```bash
cd your-project
prism init
```

`prism init` configures four integration points, for **both Claude Code and Cursor**:

| Integration | Claude Code | Cursor |
|-------------|-------------|--------|
| Hook (observe) | `.claude/settings.local.json` → `PreToolUse` → `capture.sh pre` (sets `PRISM_SOURCE=claude_code`) | `.cursor/hooks.json` → `preToolUse` → `capture_cursor.sh pre` (sets `PRISM_SOURCE=cursor`) |
| MCP (query) | `~/.claude.json` → `projects[cwd].mcpServers.prism` | `~/.cursor/mcp.json` → `mcpServers.prism` |
| Skills / rules | `.claude/skills/` symlinks | `.cursor/rules/` |
| Context push | `.claude/prism.md` | `.cursor/rules/prism.mdc` |

The sections below show the Claude Code form; the Cursor equivalent is configured at the same time.

### 1. Hooks

Adds a PreToolUse hook to `.claude/settings.local.json` (project-level). The hook command sets `PRISM_PROJECT_ID` and `PRISM_SOURCE` so observations are tagged and scoped correctly:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "env PRISM_PROJECT_ID=<project-id> PRISM_SOURCE=claude_code ~/.prism/hooks/capture.sh pre"
      }]
    }],
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "~/.prism/prism maintain --quiet"
      }]
    }]
  }
}
```

Cursor uses the same capture pipeline via `.cursor/hooks.json` → `preToolUse` → `capture_cursor.sh pre` (with `PRISM_SOURCE=cursor`). It also adds a Claude Code `SessionStart` hook that runs `prism maintain --quiet` once per session to apply confidence decay (see [Engram Lifecycle](#engram-lifecycle)); Cursor has no automatic maintain hook.

### 2. MCP Server

Registers the Prism MCP server so the IDE can query knowledge mid-session:
- **Claude Code**: `~/.claude.json` → `projects[<repo-path>].mcpServers.prism`
- **Cursor**: `~/.cursor/mcp.json` → `mcpServers.prism`

Example entry (Cursor / simplified):

```json
{
  "mcpServers": {
    "prism": {
      "command": "python3",
      "args": ["~/.prism/lib/mcp_server.py"],
      "env": {"PRISM_PROJECT_ID": "<project-id>"}
    }
  }
}
```

(`prism init` uses the current Python interpreter path, not necessarily `python3`.)

### 3. Slash Commands / Cursor Rules

Symlinks `~/.prism/skills/` into the project:
- **Claude Code**: `.claude/skills/<name>/` → skill directory
- **Cursor**: `.cursor/rules/<name>.mdc` → `SKILL.md` in each skill directory

### 4. Context Files

Runs `prism sync` to generate `.claude/prism.md` and `.cursor/rules/prism.mdc` from active engrams.

### Managing the capture hook

The PreToolUse hook fires on every tool call. It costs no tokens itself (pure file I/O), but it triggers background AI processes that do:

| Background process | Trigger | Approximate cost |
|---|---|---|
| `prism review` (session insights) | Every 5 observations | ~2k–8k tokens (fast tier per backend) |
| `prism extract` (engram extraction) | Every 15 observations | ~5k–15k tokens (fast + strong tier per backend) |

Users who want to control when AI calls happen can disable the hook and run extraction manually:

```bash
prism disable hook          # Remove capture hooks from Claude Code and Cursor (this project)
prism analyze-sessions --extract  # Manually extract after a session
prism enable hook           # Re-add capture hooks for both IDEs
```

`prism disable hook` removes `PreToolUse` from `.claude/settings.local.json` and `preToolUse` from `.cursor/hooks.json`. MCP, skills, and CLI commands are unchanged. `prism enable hook` re-adds hooks for both IDEs and refreshes MCP entries.

MCP tools, skills, and all CLI commands remain fully functional with the hook disabled.

### Project Detection

Prism identifies projects by a stable hash derived from git metadata. Detection order:

1. `PRISM_PROJECT_ID` environment variable
2. `.claude/.prism_project_id` file in project's `.claude/` directory (written by `prism init`)
3. SHA256 of git remote URL (first 12 hex chars)
4. SHA256 of git repo root path
5. `"global"` (fallback)

---

## Observation Pipeline

### Flow

Both IDEs feed the same pipeline through their own hook script. Claude Code uses `capture.sh` (sets `PRISM_SOURCE=claude_code`); Cursor uses `capture_cursor.sh` (sets `PRISM_SOURCE=cursor`). Both pipe straight into the same `capture.py`:

```
Claude Code / Cursor tool call
  -> Hook fires (capture.sh / capture_cursor.sh)
    -> Reads JSON from stdin
    -> Pipes to python3 capture.py
      -> Scrubs secrets + adversarial block check
      -> Compresses prose (preserves code/paths/URLs/identifiers)
      -> Truncates to safe length
      -> Inserts into prism.db (SQLite, WAL mode)
      -> Checks extraction trigger threshold
      -> (once per project per UTC day) spawns `prism sync` in background
```

The only difference between the two is the `source` recorded on each observation (`claude_code` vs `cursor`).

### What gets captured

Live hooks (`PreToolUse` / `preToolUse`) always record **`tool_start`** — one row per tool call. They do not emit `tool_end` or other event types.

Other event types (`tool_end`, `tool_rejected`, `user_query`, `user_guidance`, `session_insight`) are written by **`prism analyze-sessions`** (transcript import) or **session review**, not by live capture.

Every captured row is stored in `~/.prism/prism.db`. Key columns for hook observations:

| Column | Example value |
|--------|---------------|
| `session_id` | `"abc123"` |
| `project_id` | `"f4a3b2c1d0e9"` |
| `event` | `"tool_start"` |
| `tool` | `"Write"` |
| `source` | `"claude_code"` |
| `input_summary` | `"Write src/config.ts: export const ..."` (compressed) |
| `compressed` | `1` |
| `intensity` | `"lite"` |
| `extracted_at` | `NULL` (set when extracted) |
| `ts` | `1713096000` (Unix) |

### Observation compression

Before any observation is written, the input summary passes through a three-step pipeline implemented in `lib/observation_summary.py`:

1. **Scrub** — secrets and adversarial prompt patterns are removed (`lib/scrub.py`)
2. **Compress** — prose segments are compressed via a modified version of [Cavemem](https://github.com/JuliusBrussee/cavemem)'s approach (`lib/compress.py`): a tokenizer (`lib/text_tokenize.py`) splits the text into *preserved* segments (code fences, inline code, URLs, file paths, shell commands, identifiers, version numbers, dates, numbers, headings) and *prose* segments; only prose is touched — fillers, hedges, pleasantries, and articles are stripped and common words are abbreviated using a built-in lexicon (`lib/lexicon.json`). Default intensity is `lite`.
3. **Truncate** — the result is capped at `MAX_PAYLOAD_LENGTH` bytes.

Compression failures fall back to scrubbed-only text so the hook never crashes.

### Hook safety

- `capture.sh` (Claude Code) and `capture_cursor.sh` (Cursor) always exit 0, even on errors
- The hook subprocess performs a fast SQLite insert synchronously; extraction, review, and daily sync spawn in the background (no user-perceived delay on the hot path)
- Stdin piped directly to a single Python process (avoids spawning multiple interpreters)
- Secret scrubbing and adversarial block check run before any data is written to disk
- SQLite WAL mode allows concurrent readers without blocking the writer

---

## Extraction Pipeline

Extraction converts raw observations into structured engrams using a two-phase AI pipeline.

### Trigger

Extraction runs when:
- Observation count crosses `extract_threshold` (default: 15) -- triggered automatically
- User runs `prism extract` manually

### Phase 1: Proposal (fast model)

The extractor agent (`agents/extractor.md`) reads recent observations and proposes candidate engrams:

```
Observations (SQLite) -> agent CLI (fast tier) -> Candidate engrams (markdown)
```

Backend selection (`lib/agent_runner.py`):

| IDE | CLI | Phase 1 model (default) |
|-----|-----|-------------------------|
| Claude Code | `claude --print` | `haiku` |
| Cursor | `agent -p` | `composer-2.5[fast=false]` |

Resolution order: `--backend` flag → `PRISM_AGENT_BACKEND` → `config.agent_backend` → `PRISM_SOURCE` → unanimous pending source → mixed fallback (`mixed_backend_preference`) → `claude`.

**Single-IDE and mixed projects**

| Scenario | Auto-extract (from hook) | Manual `prism extract` |
|----------|--------------------------|------------------------|
| Claude Code only | `claude` CLI (`PRISM_SOURCE=claude_code`) | `claude` |
| Cursor only | `agent` CLI (`PRISM_SOURCE=cursor`) | `agent` |
| Mixed pending (both sources) | Calling IDE's CLI (hook passes `--backend` from `PRISM_SOURCE`) | `mixed_backend_preference` if that CLI is installed, else the other |
| Unanimous pending (all one source) | That IDE's CLI | That IDE's CLI |

`prism status` prints pending source counts and mixed-project hints when applicable. Force a backend: `prism extract --backend claude` or `prism extract --backend cursor`.

Each candidate has: kind, trigger, tags, domain, confidence (initial), content.

**Engram kinds**:
| Kind | Description |
|------|-------------|
| `preference` | User consistently chooses a specific approach |
| `correction` | User explicitly corrected a behavior |
| `solution` | Hard-won fix: multiple failed attempts before a non-obvious resolution |
| `procedure` | Multi-step workflow the user follows |
| `domain_fact` | Domain-specific knowledge relevant to the project |
| `error_recipe` | Known solution to a recurring error |

### Phase 2: Validation (strong model)

The validator agent (`agents/validator.md`) reviews each candidate through 4 safety gates:

| IDE | CLI | Phase 2 model (default) |
|-----|-----|-------------------------|
| Claude Code | `claude --print` | `sonnet` |
| Cursor | `agent -p --force` | `claude-4.6-sonnet-medium` |

1. **Constitution check** -- Does this violate any safety principle in `constitution.md`?
2. **Evidence check** -- Is there enough observation evidence to support this?
3. **Contradiction check** -- Does this conflict with existing engrams?
4. **Safety check** -- Could this cause harm if applied broadly?

Only candidates passing all 4 gates are written as engrams.

**Observations vs confidence** — Observations accumulate for extraction, but **re-observing the same pattern in hook capture does not raise engram confidence**. Confidence moves on use events (MCP retrieval, session-review overlap), validator output, or extraction merge (`max` of file vs index) — see [Engram Lifecycle](#engram-lifecycle).

### Session Review

A separate reviewer agent (`agents/reviewer.md`) scans session observations for conversational insights (corrections, preferences, decisions) that hooks might miss. Uses the **fast** model tier for the active backend (`haiku` or `composer-2.5[fast=false]`).

```bash
prism review --session <session-id>
```

The review interval is configurable (`review_interval`, default: every 5 observations). Session review may also credit pushed engrams (corrections, preferences, pinned) when their domain overlapped the session — see [Engram Lifecycle](#engram-lifecycle).

---

## Engram Lifecycle

Engrams are living knowledge units with confidence scores that change over time. Confidence moves on **use events** (MCP retrieval or detected application of pushed knowledge) — not on mere placement in the context file.

### Confidence model

- **Initial confidence**: Set by the extraction validator (typically 0.4–0.7 for new patterns). `prism learn` and `prism correct` create engrams at 0.8.
- **Reinforcement (up)**: One diminishing-returns impulse per real use-event, at most once per calendar day per engram:
  - `new = current + reinforce_alpha × (confidence_ceiling − current)` (defaults: `reinforce_alpha` 0.15, `confidence_ceiling` 1.0)
  - **Triggers**: MCP retrieval (`prism_search`, `prism_get`, `prism_relevant` when an engram is returned), or session review crediting a pushed engram whose trigger/domain overlapped the session (`overlap_min_terms`, default 2 shared terms)
  - **Not a trigger**: being selected for `.claude/prism.md` / `.cursor/rules/prism.mdc` — sync is read-only on confidence
  - **Not a trigger**: repeated hook observations of the same pattern — observations feed extraction separately; they do not boost confidence on their own
  - Extraction can also raise confidence when the validator writes a higher value to the engram file (merged with the index via `max`)
- **Decay (down)**: Exponential pull toward `decay_floor` (default 0.1), recomputed from `confidence_base` (the value at last use) over idle days since `last_used`:
  - No decay while idle ≤ `decay_grace_days` (default 3)
  - After grace: `confidence = decay_floor + (confidence_base − decay_floor) × exp(−ln2 / half_life × (idle − grace))` with `decay_half_life_weeks` default 4 (28-day half-life)
  - Pure function of timestamps — idempotent across maintenance runs, never compounds
- **Archive**: Engrams that decay below `archive_threshold` (default 0.2) are moved to `~/.prism/archive/` (recoverable). **Corrections and preferences** decay for bookkeeping but are **never** auto-archived — they stay in the push lane by kind.
- **Delete**: Archived engrams at or below `delete_threshold` (default 0.0) are permanently deleted on the next maintenance run.
- **Pinning**: Pinned engrams skip the decay loop entirely.

### Index fields

Confidence state lives primarily in `index.json` (not all fields are mirrored in engram frontmatter):

- `confidence` — effective score (updated by maintain and reinforce)
- `confidence_base` — baseline at the last use-event; decay recomputes from this
- `last_used` — date of the last use-event; drives decay and reinforce idempotency
- `last_observed` — last time the pattern appeared in observations; fallback for `last_used` on legacy entries

Top-level `last_maintained` (date) gates the daily decay pass.

### When decay runs

Decay and archiving happen during `prism maintain` in two passes:

1. **Delete pass** — remove archived engrams at or below `delete_threshold` (always runs)
2. **Decay pass** — decay active engrams and archive those below `archive_threshold` (at most once per calendar day, tracked by `last_maintained` in `index.json`)

`prism init` installs a Claude Code `SessionStart` hook that calls `prism maintain --quiet` once per session (Cursor has no automatic maintain hook — run `prism maintain` manually). You can also run `prism maintain` at any time. A maintenance run that changes anything re-syncs the context file.

### Manual management

```bash
prism learn "Always use pnpm in this project"          # Create engram (project scope)
prism learn "Prefer functional components" --scope global  # Create engram (global scope)
prism correct <id> "Use vitest, not jest"               # Supersede with correction
prism forget <id>                                        # Archive immediately
prism maintain                                           # Run decay cycle
```

All manual operations auto-sync `.claude/prism.md` and `.cursor/rules/prism.mdc`.

### Bootstrapping from history

```bash
prism analyze-sessions --last 10        # Analyze last 10 sessions
prism analyze-sessions --since 2026-04-01  # Analyze sessions since date
prism analyze-sessions --all --extract  # Analyze sessions across all projects, then extract
prism analyze-sessions --list           # List available sessions
prism analyze-sessions "query"          # Search session content (SQLite FTS5, 0 tokens). Combine with --last, --since, --all. NOT compatible with --extract
prism analyze-sessions --force --last 10  # Re-analyze sessions even if already processed
prism analyze-sessions --source cursor  # Cursor transcripts only (default: all sources)
```

This scans existing Claude Code and Cursor session transcripts and creates observations from them, which can then be extracted into engrams.

---

## Context Injection

Prism uses two channels to get knowledge into a session.

### Push: context file

A single `sync` step writes the context file for both IDEs at once:
- **Claude Code**: `.claude/prism.md`
- **Cursor**: `.cursor/rules/prism.mdc` (same content, prefixed with an `alwaysApply: true` rule frontmatter block)

The IDE reads this file as project instructions/rules at session start. It is regenerated automatically by `prism init`, `prism learn`, `prism correct`, `prism forget`, `prism maintain` (when something changed), and the `prism_record` MCP tool. If no engrams qualify, the stale context files are removed.

**Selection** — at most 10 entries are pushed (the rest stay searchable via MCP). Routing is by **kind**, not confidence score:
1. Pinned entries
2. Corrections (never dropped before preferences — past mistakes must be present before the model acts)
3. Preferences

Within each tier, higher confidence is a tiebreak only. Sync does **not** reinforce pushed entries — confidence moves only on use events (see [Engram Lifecycle](#engram-lifecycle)). Session review may credit a pushed engram when its domain overlapped the session.

**Rendered sections** (in order, each omitted if empty): Corrections, Pinned, Key Preferences, Publish-Ready (engrams that have crossed the promotion gates), followed by an MCP footer with `prism_search` / `prism_record` usage guidance.

**Format**:

```markdown
# Learned Knowledge (Prism)
<!-- Updated: 2026-04-14T12:00:00Z | 8 pushed, 24 via MCP -->

## Corrections -- do NOT repeat these

- When asked about testing: Use vitest, not jest

## Pinned

- Deploy procedure: always run migrations first (0.90)

## Key Preferences

- Use pnpm for package management (0.75)
- Prefer TypeScript strict mode (0.72)

---
Full knowledge base (24 more entries) available via prism MCP tools.

**Search** (`prism_search`): when encountering errors, starting tasks, or making design decisions.

**Record** (`prism_record`): proactively record knowledge when you discover it:
...
```

Size is controlled by `max_context_lines` (default: 100); the file is truncated past that limit.

### Pull: MCP Server

For mid-session queries. The model calls MCP tools when it needs specific knowledge beyond what's in the context file. See [MCP Server](#mcp-server).

---

## MCP Server

The MCP server runs as a stdio subprocess of the IDE, speaking JSON-RPC 2.0 (protocol version `2025-03-26`).

### Tools

#### `prism_search`

Token-based Jaccard similarity search across engrams (not FTS — FTS is used for observation/session search only).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Search query |
| `limit` | number | 5 | Max results |

Returns scored results with trigger, tags, confidence, and relevance score. Boosts error-related queries toward `error_recipe` entries.

#### `prism_get`

Retrieve a specific engram by ID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | string | required | Engram ID (kebab-case slug) |

Returns full entry with all metadata and content.

#### `prism_relevant`

Find entries relevant to the current context (file being edited, tool being used).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_path` | string | optional | Current file path (used to infer domain) |
| `domain` | string | optional | Explicit domain (python, react, testing, etc.) |
| `limit` | number | 5 | Max results |

#### `prism_record`

Record an observation directly from the IDE conversation.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | string | required | The knowledge to record |
| `kind` | string | `preference` | Type: `preference`, `correction`, `procedure`, `error_recipe`, `domain_fact`, `solution` |

Writes a new engram immediately and auto-syncs context files (`.claude/prism.md` and `.cursor/rules/prism.mdc`).

> **Project scoping**: All MCP tools scope to the current project automatically via the `PRISM_PROJECT_ID` environment variable set by `prism init`. No `project_id` argument is needed.

### Implementation notes

- stdout is exclusively for JSON-RPC messages (no stray prints)
- All logging goes to stderr
- stdout buffering is explicitly handled (flush after every write)
- MCP tools that return engrams fire one daily-idempotent reinforce impulse per matched entry (see [Engram Lifecycle](#engram-lifecycle))

---

## CLI Reference

### Commands

| Command | Description |
|---------|-------------|
| `prism init` | Initialize Prism for current project |
| `prism status [--project ID]` | Show active knowledge and project info |
| `prism learn <text> [--scope project\|global]` | Manually create an engram |
| `prism correct <id> <text>` | Supersede engram with correction |
| `prism forget <id>` | Archive an engram |
| `prism extract [--project ID] [--backend claude\|cursor]` | Run extraction pipeline on observations |
| `prism review --session ID [--project ID] [--backend claude\|cursor]` | Analyze a session transcript |
| `prism analyze-sessions [flags]` | Bootstrap from existing Claude Code or Cursor session transcripts |
| `prism unlock` | Force-clear a stuck extraction lock |
| `prism disable hook` | Remove capture hooks from Claude Code and Cursor (MCP and skills unchanged) |
| `prism enable hook` | Re-add capture hooks for Claude Code and Cursor (and refresh MCP config) |
| `prism reset [--yes] [--project ID]` | Delete all project data (engrams, observations, candidates) and start fresh |
| `prism uninstall [--yes] [--project ID]` | Remove all Prism integration from this project (undoes `prism init`) |
| `prism maintain [--quiet]` | Run confidence decay and archive expired engrams |
| `prism sync [--project ID] [--quiet]` | Regenerate context files from active engrams |
| `prism promote <id> [--name NAME]` | Convert engram to publishable skill format |
| `prism stats [--days N] [--limit N] [--project ID] [--json]` | Show MCP retrieval analytics |
| `prism dashboard [--port N] [--no-open]` | Launch local web dashboard (read-only) |
| `prism log [--last N] [--extractions] [--insights] [--rejected] [--json]` | Show recent observations |
| `prism config [key [value]]` | Get or set configuration |
| `prism registry ...` | Manage skill registries (see [Team Registry](#team-registry)) |

### `prism analyze-sessions` flags

| Flag | Description |
|------|-------------|
| `--all` | Analyze sessions across all projects (not just the current one) |
| `--extract` | Run extraction after analysis |
| `--dry-run` | Show what would be analyzed without doing it |
| `--list` | List available sessions |
| `--since DATE` | Only sessions after this date |
| `--last N` | Only the N most recent sessions |
| `--source claude\|cursor\|all` | Session transcript source (default: `all`) |
| `--force` | Re-analyze sessions even if already processed (resets tracker for matched sessions) |

### `prism log` flags

| Flag | Description |
|------|-------------|
| `--last N` | Show last N entries (default: 20) |
| `--extractions` | Show extraction events only |
| `--insights` | Show session review insights only |
| `--rejected` | Show rejected extraction candidates with failing gate reasons |
| `--json` | Output as JSON |

---

## Slash Commands

Prism includes 12 slash commands available in Claude Code (as skills) and Cursor (as rules) after `prism init`. These are SKILL.md files that the model follows as step-by-step instructions.

### Analysis & Mining

| Command | Description |
|---------|-------------|
| `/analyze-agent-codebase` | Deep 6-cluster analysis of an agentic codebase (architecture, state, tools, error handling, coordination, evaluation) |
| `/mine-history` | Extract incident patterns from git history |
| `/mine-design` | Extract architectural design decisions from code |

### Extraction & Synthesis

| Command | Description |
|---------|-------------|
| `/extract-skills` | Transform codebase analysis reports into framework-agnostic skills |
| `/synthesize` | Promote incident clusters into publishable skills |
| `/synthesize-decisions` | Convert design decision reports into skills |

### Quality & Curation

| Command | Description |
|---------|-------------|
| `/curate-skills` | Quality review pass on extracted skills (dedup, accuracy, formatting) |

### Publishing & Querying

| Command | Description |
|---------|-------------|
| `/publish-skills` | Publish skills to team registry with delta tracking |
| `/advise-skills` | Query registries for skills relevant to a question |
| `/audit-code` | Audit current codebase against registry skill patterns |

### Pipelines (orchestrate multiple steps)

| Command | Description |
|---------|-------------|
| `/run-analysis-pipeline` | Codebase pipeline: `analyze-agent-codebase` → `extract-skills` (agentic path) or `mine-design` → `synthesize-decisions` (general path); then `curate-skills` → `publish-skills` |
| `/run-history-pipeline` | History pipeline: `mine-history` → `synthesize` → `curate-skills` → `publish-skills` |

### Output

All extraction and analysis commands write to `_analysis/` in the project root:
- `_analysis/extracted_skills_codebase/` and `_analysis/extracted_skills_history/` -- Skill directories with `plugin.json` + `SKILL.md`
- `_analysis/.published.json` -- Delta tracking for published skills

---

## Engram-to-Skill Promotion

`prism promote` bridges personal knowledge (engrams) to team knowledge (skills).

### Gate checks

Promotion requires:
- Confidence >= `publish_min_confidence` (default: 0.7)
- Evidence count >= `publish_min_evidence` (default: 3)
- Source is not `"registry"` (can't re-promote imported skills)

### What it produces

For an engram about TypeScript strict mode:

```
_analysis/extracted_skills_codebase/typescript-strict-mode/
  plugin.json    # Metadata (name, description, author, category, source: "engram")
  SKILL.md       # Instructions with frontmatter
```

**plugin.json** fields:
- `name`: Auto-generated kebab-case (or `--name` override)
- `description`: Includes `TRIGGER when:` clause (required by schema)
- `author`: From `git config user.name`
- `repository`: From `git remote get-url origin`
- `category`: Mapped from engram kind (preference -> architecture, procedure -> execution-control, etc.)
- `source`: `"engram"` (distinguishes promoted personal knowledge from other sources)
- `commit_date`: DD-MM-YYYY format
- `source_hash`: Current git short hash

### After promotion

```bash
/curate-skills     # Quality review
/publish-skills    # Publish to registry
```

---

## Team Registry

Teams share skills through registries backed by GitHub repos and Cloudflare Workers.

### Architecture

```
prism CLI  ->  Cloudflare Worker  ->  GitHub Repo
  (publish)     (API proxy)          (storage, PRs, CI)
  (query)       (auth, cache)        (skill-registry.json)
```

### Commands

```bash
prism registry create                        # Set up new registry (guided wizard)
prism registry add <name> --url <url>        # Add a registry (--token, --read-only optional)
prism registry remove <name>                 # Remove a registry
prism registry list                          # List configured registries
prism registry default <name>                # Set default write target
prism registry token create <name>           # Generate API token
prism registry token revoke <name> <token>   # Revoke an API token
```

Registry configuration is stored in `~/.prism/registries.json` with per-registry tokens (file permissions `0o600`). Seeded at install with a read-only public registry (`prism-open-source`); `prism init` does not modify registry config.

### Multi-registry support

- Read from all configured registries (merged results, tagged by source)
- Write to a specific registry (delta tracked per-registry)
- 24h TTL cache for fetched `skill-registry.json`

---

## Data Formats

### Observations (SQLite)

Location: `~/.prism/prism.db` — one database shared across all projects.

WAL mode is enabled for concurrent access. An FTS5 virtual table (`observations_fts`) mirrors `input_summary` for full-text search using Porter stemming, kept in sync via INSERT/DELETE/UPDATE triggers.

**Schema**:

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `session_id` | TEXT | FK → `sessions.id` |
| `project_id` | TEXT | 12-char project hash |
| `event` | TEXT | `tool_start` (live hooks); `tool_end`, `tool_rejected`, `user_query`, `user_guidance` (transcript import); `session_insight` (session review) |
| `tool` | TEXT | Tool name |
| `source` | TEXT | `claude_code`, `cursor` (hooks), `session_import` (Claude JSONL), `cursor_transcript` (Cursor JSONL) |
| `input_summary` | TEXT | Compressed + scrubbed tool input |
| `compressed` | INTEGER | Always `1` |
| `intensity` | TEXT | Always `lite` |
| `extracted_at` | INTEGER | Unix ts when extracted; `NULL` = pending |
| `insight_type` | TEXT | Set on `session_insight` rows |
| `evidence` | TEXT | Supporting text for insight rows |
| `ts` | INTEGER | Unix timestamp |

Extracted observations older than 30 days are purged at the end of `prism extract`. Retention is controlled by `observation_retention_seconds` (default 30 days if omitted — optional key, not written by `install.sh`).

### Engram files (Markdown + YAML frontmatter)

Location: `~/.prism/global/engrams/<id>.md` or `~/.prism/projects/<project_id>/engrams/<id>.md`

```markdown
---
id: prism-1713100800-a1b2c3
kind: preference
trigger: "Always use pnpm for package management"
tags:
  - nodejs
  - package-manager
domain: javascript
confidence: 0.75
evidence_count: 5
scope: project
project_id: f4a3b2c1d0e9
---

Use pnpm instead of npm for all Node.js projects. It's faster,
uses less disk space through hard linking, and has stricter
dependency resolution that prevents phantom dependencies.
```

### Index (JSON)

Location: `~/.prism/index.json`

```json
{
  "last_maintained": "2026-04-14",
  "engrams": [
    {
      "id": "prism-1713100800-a1b2c3",
      "kind": "preference",
      "trigger": "Always use pnpm for package management",
      "tags": ["nodejs", "package-manager"],
      "domain": "javascript",
      "confidence": 0.75,
      "confidence_base": 0.75,
      "evidence_count": 5,
      "source": "hook",
      "scope": "project",
      "project_id": "f4a3b2c1d0e9",
      "path": "projects/f4a3b2c1d0e9/engrams/prism-1713100800-a1b2c3.md",
      "last_observed": "2026-04-14",
      "last_used": "2026-04-14",
      "pinned": false
    }
  ]
}
```

The index is protected by file locking (`fcntl.flock`) with atomic writes (write to temp file, then `os.rename`). A `.bak` backup is created on every write. Stale locks older than 10 minutes are automatically broken.

### Skill plugin.json

```json
{
  "name": "typescript-strict-mode",
  "description": "Always enable TypeScript strict mode for full type safety. TRIGGER when: setting up a new TypeScript project, configuring tsconfig.json, reviewing type safety settings.",
  "author": "Your Name",
  "repository": "org/repo",
  "category": ["architecture"],
  "source": "engram",
  "commit_date": "14-04-2026",
  "source_hash": "a1b2c3d"
}
```

### Published delta tracking

Location: `_analysis/.published.json`

```json
{
  "typescript-strict-mode": {
    "default": {
      "published_at": "2026-04-14T12:00:00Z",
      "content_hash": "a1b2c3d4e5f6"
    }
  }
}
```

Content hash is SHA256 of plugin.json + SKILL.md concatenated, first 12 hex chars.

---

## Configuration Reference

Location: `~/.prism/config.json`

| Key | Default | Description |
|-----|---------|-------------|
| `extract_threshold` | 15 | Number of observations before auto-extraction triggers |
| `agent_backend` | `auto` | `auto`, `claude`, or `cursor` — which CLI runs extraction/review |
| `mixed_backend_preference` | `cursor` | When pending observations are mixed, prefer this CLI if installed |
| `cursor_models.fast` | `composer-2.5[fast=false]` | Cursor `agent --model` for phase 1 extract and session review |
| `cursor_models.strong` | `claude-4.6-sonnet-medium` | Cursor `agent --model` for phase 2 validation |
| `reinforce_alpha` | 0.15 | Use-event impulse fraction of remaining headroom toward `confidence_ceiling` |
| `confidence_ceiling` | 1.0 | Upper asymptote for reinforce impulses (replaces the old 0.95 hard cap) |
| `decay_half_life_weeks` | 4 | Idle half-life for exponential decay (after `decay_grace_days`) |
| `decay_grace_days` | 3 | No decay until idle days exceed this |
| `decay_floor` | 0.1 | Decay asymptote (below `archive_threshold` so stale engrams rotate out) |
| `overlap_min_terms` | 2 | Shared significant terms before a pushed engram counts as used in session review |
| `archive_threshold` | 0.2 | Archive engrams below this confidence (corrections/preferences exempt) |
| `delete_threshold` | 0.0 | Permanently delete archived engrams at or below this confidence |
| `observation_retention_seconds` | 2592000 (30 days) if omitted | Optional. Purge extracted observations older than this at end of `prism extract` |
| `publish_min_confidence` | 0.7 | Minimum confidence for skill promotion |
| `publish_min_evidence` | 3 | Minimum evidence count for skill promotion |
| `max_context_lines` | 100 | Maximum lines in generated context file (`.claude/prism.md` / `.cursor/rules/prism.mdc`) |
| `review_interval` | 5 | Observations between automatic session reviews (0 = disabled) |
| `review_cooldown_seconds` | 1800 | Minimum seconds between automatic reviews per session |
| `review_timeout` | 60 | Seconds before review subprocess is killed |
| `cache_max_age_hours` | 24 | Max age before a fetched registry cache is considered stale |
| `scrub_patterns` | (see below) | Additional secret detection regex patterns |
| `block_patterns` | (see below) | Adversarial prompt detection patterns |

Team registries are configured in `~/.prism/registries.json` (see [Team Registry](#team-registry)), not in `config.json`. The legacy `registry_url` key in `config.json` is auto-migrated to `registries.json` on first registry access if present.

### Secret scrub patterns (built-in)

These are hardcoded as a security baseline and cannot be disabled:

- API keys, secrets, tokens, passwords, credentials (`key=value` patterns)
- Bearer tokens
- OpenAI keys (`sk-*`)
- GitHub PATs (`ghp_*`, `gho_*`, `ghs_*`, `github_pat_*`)
- Slack tokens (`xoxb-*`)
- AWS access keys (`AKIA*`)
- URLs with embedded credentials
- Private keys (PEM format)
- JWTs (`eyJ*`)

Additional patterns can be added via `scrub_patterns` in config.

### Environment variables

| Variable | Description |
|----------|-------------|
| `PRISM_HOME` | Override default `~/.prism` location |
| `PRISM_PROJECT_ID` | Override auto-detected project ID |
| `PRISM_SOURCE` | Set by hooks (`claude_code` or `cursor`); influences `agent_backend: auto` |
| `PRISM_AGENT_BACKEND` | Force extraction/review CLI: `claude` or `cursor` |
| `REGISTRY_TOKEN` | Bearer token for registry API authentication |

---

## Security

### Observation scrubbing

All captured observations are scrubbed before writing to disk. The scrubber runs a set of hardcoded baseline patterns (cannot be disabled) plus any user-configured patterns. Matched content is replaced with `[REDACTED]`.

### Adversarial prompt detection

Block patterns detect attempts to manipulate the extraction pipeline (e.g., "expand access", "grant permissions"). Observations matching these patterns are discarded.

### Constitution

`~/.prism/constitution.md` defines safety principles that the validation pipeline checks against. It is created from a template on first install and never overwritten by upgrades.

### File safety

- Index writes use file locking + atomic rename (no partial writes)
- Hooks never block the IDE (exit 0 always)
- Subprocess calls use timeouts (default: 5s for git, 60s for reviews)
- No network calls in the personal layer (extraction uses local `claude` or `agent` CLI subprocesses)

---

## File System Layout

```
~/.prism/
  prism                          # CLI entry point
  config.json                    # User configuration
  constitution.md                # Safety principles (never overwritten)
  prism.db                       # SQLite database — all observations + FTS5 index (shared across projects)
  lib/                           # Python library
    cli.py                       # Command router
    commands.py                  # Command implementations
    config.py                    # Config management
    capture.py                   # Observation processor (hot path)
    storage.py                   # SQLite read/write layer
    schema.py                    # SQLite schema DDL
    observation_summary.py       # scrub → compress → truncate pipeline
    compress.py                  # Prose compression (Cavemem-inspired)
    text_tokenize.py             # Segment tokenizer (preserved vs. prose)
    lexicon.py / lexicon.json    # Abbreviations, fillers, hedges, articles
    expand.py                    # Inverse of compress (decompression)
    extract.py                   # Extraction pipeline
    agent_runner.py              # IDE agent CLI subprocess (claude / agent)
    confidence.py                # Pure reinforce/decay math
    frontmatter.py               # Custom YAML frontmatter parser (no PyYAML)
    index.py                     # Index management (load/save/lock)
    mcp_server.py                # MCP server (stdio, JSON-RPC)
    sync.py                      # Context sync (.claude/prism.md + .cursor/rules/prism.mdc)
    search.py                    # FTS5 search over observations
    review.py                    # Session review
    sessions.py                  # Session transcript import (Claude Code + Cursor)
    project.py                   # Project detection
    trigger.py                   # Auto-extraction trigger
    bridge.py                    # Engram-to-skill promotion
    scrub.py                     # Secret scrubbing + adversarial detection
    dashboard.py / dashboard.html  # Local web dashboard (`prism dashboard`)
  hooks/
    capture.sh                   # Claude Code hook (PreToolUse; PRISM_SOURCE=claude_code)
    capture_cursor.sh            # Cursor hook (preToolUse; PRISM_SOURCE=cursor)
  agents/
    extractor.md                 # Phase 1 extraction prompt (fast tier)
    validator.md                 # Phase 2 validation prompt (strong tier)
    reviewer.md                  # Session review prompt
  skills/                        # 12 slash commands
    analyze-agent-codebase/      # (SKILL.md + question cluster files)
    mine-history/
    mine-design/
    extract-skills/
    synthesize/
    synthesize-decisions/
    curate-skills/
    publish-skills/
    advise-skills/
    audit-code/
    run-analysis-pipeline/
    run-history-pipeline/
  registries.json                # Team registry configuration
  templates/                     # constitution.md and registry wizard templates
  cache/                         # Registry fetch cache
  index.json                     # Master engram index
  schemas/
    plugin.schema.json           # Skill validation schema
  global/
    engrams/
      *.md                       # Global engram files
  projects/
    <project-hash>/
      engrams/
        *.md                     # Project-scoped engrams
  archive/                       # Archived (decayed) engrams
```

---

## Troubleshooting

### `prism: command not found`

Ensure `~/.local/bin` is in your PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Hooks not firing

**Claude Code** — check `.claude/settings.local.json`:

```bash
cat .claude/settings.local.json | python3 -m json.tool
```

Look for a `PreToolUse` entry pointing to `capture.sh`. If missing, run `prism init` again or `prism enable hook`.

**Cursor** — check `.cursor/hooks.json`:

```bash
cat .cursor/hooks.json | python3 -m json.tool
```

Look for a `preToolUse` entry pointing to `capture_cursor.sh`. If missing, run `prism init` again.

### MCP server not connecting

Check stderr output:

```bash
python3 ~/.prism/lib/mcp_server.py 2>/tmp/prism-mcp.log
# Then check /tmp/prism-mcp.log
```

Common issue: stray `print()` statements in lib code corrupt the JSON-RPC stream. All output must go to stderr.

### Extraction not triggering

Observations live in SQLite (`~/.prism/prism.db`), not in per-project JSONL files anymore. Check the recent log and the count of pending (un-extracted) observations:

```bash
prism log --last 5

# Pending observations for a project (this is what the trigger counts):
sqlite3 ~/.prism/prism.db \
  "SELECT COUNT(*) FROM observations
   WHERE project_id = '<your-project-id>'
     AND extracted_at IS NULL
     AND event != 'session_insight';"
```

Find `<your-project-id>` with `prism status`. Extraction triggers automatically once that count crosses `extract_threshold` (default: 15); `session_insight` rows are excluded from the trigger count. If a previous extract crashed mid-run, a stale lock can block new runs — clear it with `prism unlock`. Run extraction manually any time with `prism extract`.

### Engrams not appearing in the IDE

Check what engrams exist and inspect the context files:

```bash
prism status
cat .claude/prism.md
cat .cursor/rules/prism.mdc   # Cursor
```

If context files are empty, check `prism status` to verify engrams exist for the current project. They are regenerated automatically whenever you run `prism learn`, `prism correct`, `prism forget`, `prism maintain` (when something changed), or `prism sync`.

### Stale lock on index.json

If a process crashed while writing, you may see lock errors. Prism auto-breaks locks older than 10 minutes. To force:

```bash
rm ~/.prism/index.lock 2>/dev/null
```
