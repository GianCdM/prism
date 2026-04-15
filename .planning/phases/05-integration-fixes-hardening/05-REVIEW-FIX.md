---
phase: 05-integration-fixes-hardening
fixed_at: 2026-04-15T08:19:25Z
review_path: .planning/phases/05-integration-fixes-hardening/05-REVIEW.md
iteration: 1
findings_in_scope: 6
fixed: 6
skipped: 0
status: all_fixed
---

# Phase 05: Code Review Fix Report

**Fixed at:** 2026-04-15T08:19:25Z
**Source review:** .planning/phases/05-integration-fixes-hardening/05-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 6
- Fixed: 6
- Skipped: 0

## Fixed Issues

### CR-01: Path traversal via MCP `prism_get` -- entry path not validated

**Files modified:** `lib/mcp_server.py`
**Commit:** 2ecf9da
**Applied fix:** Added `.resolve()` to the filepath construction and a guard that checks `str(filepath).startswith(str(PRISM_HOME.resolve()))` before reading the file. Returns `None` if the resolved path escapes PRISM_HOME, preventing path traversal via `../` sequences in index entries.

### WR-01: Race condition in `_update_gitignore` -- reads file while appending

**Files modified:** `lib/commands.py`
**Commit:** f672cb0
**Applied fix:** Reads `.gitignore` content once into `existing_content` before opening for append, instead of calling `gitignore_path.read_text()` twice inside the `with open(..., "a")` block. The cached content is used for both the existing-lines set and the trailing-newline check.

### WR-02: Duplicate "# Prism" comment block appended on re-init with partial entries

**Files modified:** `lib/commands.py`
**Commit:** 0a7495c
**Applied fix:** Added a check `if "# Prism (auto-generated, machine-specific)" not in existing_lines` before writing the comment header, so re-running `prism init` with partial missing entries no longer appends duplicate comment blocks.

### WR-03: MCP `_record` does not validate `kind` parameter against allowed values

**Files modified:** `lib/mcp_server.py`
**Commit:** 5b65755
**Applied fix:** Added `VALID_KINDS` set at module level with the 6 allowed kind values. Added validation at the top of `_record` that returns an error response if `kind not in VALID_KINDS`, enforcing the schema constraint server-side rather than relying on advisory MCP schema validation.

### WR-04: `cmd_forget` uses `entry.get("path", "")` which can construct path to PRISM_HOME root

**Files modified:** `lib/commands.py`
**Commit:** d3e7d55
**Applied fix:** Changed the guard from `source_path.exists()` to `entry.get("path") and source_path.is_file()`. This ensures the path field is non-empty and points to an actual file (not a directory), preventing the edge case where an empty path would resolve to PRISM_HOME itself.

### WR-05: `cmd_maintain` iterates `list(index["engrams"])` but calls `remove_entry()`/`update_confidence()` which reload the full index each time

**Files modified:** `lib/commands.py`
**Commit:** bb3b593, 6ffb620
**Applied fix:** Refactored `cmd_maintain` to batch all index modifications into a single load/save cycle. Archived entry IDs are collected into a `to_archive_ids` set, confidence updates are applied in-place on the entry dicts, and a single `save_index(index)` call is made at the end. Also added the missing `save_index` import and applied the WR-04 safety pattern (`is_file()` check) to the archive path in maintain. This is a logic refactor: requires human verification.

## Skipped Issues

None -- all in-scope findings were fixed.

---

_Fixed: 2026-04-15T08:19:25Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
