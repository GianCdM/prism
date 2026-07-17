#!/usr/bin/env python3
# Copyright © 2026 MIH AI B.V.
# Licensed under the Apache License, Version 2.0
# See LICENSE file in the project root

"""Prism retrieval hook (Claude Code UserPromptSubmit).

Reads the user's prompt on stdin, searches the FULL engram base by relevance to
that prompt, and prints the top matches as additional context. The push layer
(prism.md) only carries the top ~10 by confidence; this surfaces the REST — the
dormant pull layer — automatically, by relevance.

Never blocks: exits 0, prints nothing on low signal or error.
"""

import json
import os
import sys
from pathlib import Path

# ── gates — tune these, not the search module ──────────────────────────
MIN_PROMPT_CHARS = 15   # skip terse prompts ("ok", "vai", "continua")
MAX_RESULTS = 5         # engrams to surface per prompt
MIN_OVERLAP = 2         # require >=2 shared terms (cuts single-common-word noise)
MIN_TOKEN_LEN = 4        # words with <4 chars are mostly noise
MIN_SCORE = 0.05         # relevance gate — match the shared default

_STOP = frozenset({
    "quando", "para", "como", "onde", "fazer", "quero", "preciso", "sobre",
    "isso", "esse", "essa", "esses", "aquele", "tudo", "agora", "depois",
    "entao", "tambem", "ainda", "porque", "qual", "quais", "mais", "menos",
    "pode", "fica", "vamos", "esta", "este", "seria",
    "with", "when", "what", "that", "this", "into", "from", "your", "have",
    "should", "would", "could", "about", "which", "there", "their", "then",
    "prism", "engram", "claude",
})

# ── index cache — avoid re-reading + re-parsing on every prompt ────────
_index_cache: tuple[str, float, list[dict]] | None = None
"""Cached (path, mtime, entries) — cleared on mtime change (prism learn / extract)."""


def _load_engrams(prism_home: Path) -> list[dict]:
    """Load the master engram list, caching by index.json mtime."""
    global _index_cache
    index_path = prism_home / "index.json"
    try:
        st = index_path.stat()
        mtime = st.st_mtime
    except OSError:
        return []

    if _index_cache is not None:
        cached_path, cached_mtime, cached_entries = _index_cache
        if cached_path == str(index_path) and cached_mtime == mtime:
            return cached_entries

    try:
        index = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    entries = index.get("engrams", [])
    _index_cache = (str(index_path), mtime, entries)
    return entries


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    prompt = (data.get("prompt") or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return

    prism_home = Path(os.environ.get("PRISM_HOME", os.path.expanduser("~/.prism")))
    project_id = os.environ.get("PRISM_PROJECT_ID") or None

    sys.path.insert(0, str(prism_home))
    try:
        from lib.search import search_engrams
    except ImportError:
        return  # prism not installed in this environment

    entries = _load_engrams(prism_home)
    if not entries:
        return

    results = search_engrams(
        prompt,
        entries,
        project_id=project_id,
        limit=MAX_RESULTS,
        min_overlap=MIN_OVERLAP,
        min_score=MIN_SCORE,
        min_token_len=MIN_TOKEN_LEN,
        stop_words=_STOP,
        confidence_boost_weight=0.03,  # slightly lower — hook is a nudge, not a query
    )

    if not results:
        return

    out = [
        "<prism-knowledge>",
        "Engrams aprendidos possivelmente relevantes pra esta mensagem "
        "(use `prism_get <id>` pro conteúdo completo se for aplicar):",
    ]
    for r in results:
        trig = (r.get("trigger") or "").strip().strip('"')[:130]
        out.append(
            f"- [{r.get('id')}] (conf {r.get('confidence', '?')}) — {trig}"
        )
    out.append("</prism-knowledge>")
    print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block the prompt
