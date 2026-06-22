# Complete list of commands and functionalities 

## Prism User Commands 
| Command | Description |
|---------|-------------|
| `prism init` | Initialize Prism for current project |
| `prism status` | Show active engrams grouped by kind with confidence scores |
| `prism learn <text> [--scope project\|global]` | Manually create an engram (confidence 0.8) |
| `prism correct <id> <text>` | Supersede an engram with a correction (archives old, creates new at 0.8) |
| `prism forget <id>` | Archive an engram immediately |
| `prism extract [--project ID] [--backend claude\|cursor]` | Run extraction pipeline on observations (fast model proposes, strong model validates — via `claude` or `agent` per backend) |
| `prism analyze-sessions [flags]` | Bootstrap observations from existing Claude Code or Cursor session transcripts |
| `prism review --session ID [--project ID] [--backend claude\|cursor]` | Analyze a single session transcript for conversational insights |
| `prism unlock` | Force-clear a stuck extraction lock (use if `prism extract` says "already in progress" after a crash) |
| `prism disable hook` | Remove capture hooks from `.claude/settings.local.json` and `.cursor/hooks.json` — stops automatic observation capture and the AI extraction/review calls it triggers. MCP, skills, and all CLI commands remain fully functional. |
| `prism enable hook` | Re-add capture hooks for Claude Code and Cursor and refresh MCP config (reverses `prism disable hook`) |
| `prism reset [--yes] [--project ID]` | Delete all project data (engrams, observations, candidates) and start fresh — hook and MCP stay wired, Prism resumes capturing immediately |
| `prism uninstall [--yes] [--project ID]` | Remove all Prism integration from this project: hooks, MCP entries, context files (`.claude/prism.md`, `.cursor/rules/prism.mdc`), skill symlinks/rules, project data, and `.gitignore` entries. Undoes `prism init`. `~/.prism/` global install and other projects are untouched. Run `prism init` to re-initialize. |
| `prism maintain [--quiet]` | Run confidence decay and archive expired engrams. Claude Code: also runs automatically via `SessionStart` hook (at most once per calendar day). Cursor: run manually. |
| `prism sync [--project ID] [--quiet]` | Regenerate `.claude/prism.md` and `.cursor/rules/prism.mdc` from active engrams (read-only on confidence) |
| `prism promote <id> [--name NAME]` | Convert engram to publishable skill format (requires confidence ≥ 0.7, evidence ≥ 3) |
| `prism stats [--days N] [--limit N] [--json] [--project ID]` | Show how often engrams are retrieved via MCP (`prism_search` / `prism_get` / `prism_relevant`): retrieval count + trend, search hit rate, IDE source split, most-retrieved engrams, and engrams never pulled. Distinguishes engrams *pulled* on demand from engrams merely *surfaced* in context files by sync (sync does not reinforce). |
| `prism log [--last N] [--extractions] [--insights] [--rejected] [--json]` | Show recent observations or extraction history |
| `prism config [key [value]]` | Get or set configuration values |
| `prism dashboard [--port N] [--no-open]` | Launch a local web dashboard (default port 4318) — browse engrams per project, a Global section, an Overview (counts, by-kind, by-domain, thresholds), and connected Registries. Zero dependencies; reads `~/.prism/` read-only. Ctrl-C to stop. |
| `prism registry <subcommand>` | Manage skill registries (see below) |

### `prism analyze-sessions` flags

| Flag | Description |
|------|-------------|
| `"query"` | Search past session content via SQLite FTS5 — resurfaces sessions where you discussed something specific |
| `--last N` | Only the N most recent sessions |
| `--since DATE` | Only sessions after this date (YYYY-MM-DD) |
| `--all` | Analyze sessions across all projects, not just current |
| `--source claude\|cursor\|all` | Session transcript source (default: `all`) |
| `--extract` | Run extraction immediately after analysis |
| `--list` | List available sessions with counts |
| `--dry-run` | Show what would be analyzed without writing observations |
| `--force` | Re-analyze sessions even if already processed (resets tracker for matched sessions) |

### `prism registry` subcommands

| Subcommand | Description |
|------------|-------------|
| `prism registry create` | Guided wizard to set up a new registry |
| `prism registry add <name> --url <url>` | Add a registry (`--token`, `--read-only` optional) |
| `prism registry remove <name>` | Remove a registry |
| `prism registry list` | Show all configured registries |
| `prism registry default <name>` | Set default write target |
| `prism registry token create <name>` | Generate an API token |
| `prism registry token revoke <name> <token>` | Revoke an API token |

---

## MCP Tools (called by the IDE mid-session)

| Tool | Parameters | Description |
|------|------------|-------------|
| `prism_search` | `query` (required), `limit` (default 5) | Token Jaccard search across engrams. Boosts `error_recipe` entries for error-related queries. Fires a daily-idempotent reinforce impulse on returned engrams. |
| `prism_get` | `id` (required) | Fetch full engram content by ID. Fires reinforce on the returned engram. |
| `prism_relevant` | `file_path` (optional), `domain` (optional), `limit` (default 5) | Engrams relevant to the current file or domain. Fires reinforce on returned engrams. |
| `prism_record` | `text` (required), `kind` (default `preference`) | Write a new engram mid-session. Kinds: `preference`, `correction`, `procedure`, `error_recipe`, `domain_fact`, `solution`. Corrections/preferences start at 0.8; other kinds at 0.9. Auto-syncs context files. |

> Project scoping is automatic — all tools use the `PRISM_PROJECT_ID` environment variable set by `prism init`. No `project_id` argument needed.

---

## Slash Commands (Claude Code skills / Cursor rules)

### Pipeline orchestrators
| Skill | Type | Description |
|-------|------|-------------|
| `/run-analysis-pipeline` | Orchestrator | Full vertical pipeline: analyze → extract → curate → publish, with state tracking and resumption |
| `/run-history-pipeline` | Orchestrator | Full horizontal pipeline: mine-history → synthesize → publish, with state tracking |

### Codebase & history analysis
| Skill | Type | Description |
|-------|------|-------------|
| `/analyze-agent-codebase` | Vertical extraction | Deep architectural analysis of an agentic codebase across six topic clusters |
| `/mine-design` | Horizontal extraction | Extract design decisions and trade-offs from current source code |
| `/mine-history` | Horizontal extraction | Mine git history for failure modes, architectural decisions, and directives |

### Skill extraction & synthesis
| Skill | Type | Description |
|-------|------|-------------|
| `/extract-skills` | Vertical extraction | Extract reusable skills from a codebase analysis report |
| `/synthesize-decisions` | Horizontal extraction | Extract practice skills from design decisions |
| `/synthesize` | Horizontal extraction | Extract skills from git history analysis |

### Publishing & Retrieval 
| Skill | Type | Description |
|-------|------|-------------|
| `/curate-skills` | Post-extraction | Quality pass — keep, delete, merge, or rewrite extracted skills before publishing |
| `/publish-skills` | Publishing | Publish skills to the registry with delta tracking (only republishes changed skills) |
| `/advise-skills` | Registry query | Find skills matching a natural language query across all configured registries |
| `/audit-code` | Registry query | Scan your codebase and surface relevant registry skills proactively |
