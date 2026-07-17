# Copyright © 2026 MIH AI B.V.
# Licensed under the Apache License, Version 2.0
# See LICENSE file in the project root

"""Shared engram search — Jaccard similarity with configurable gates.

Used by the MCP server (prism_search) and the UserPromptSubmit retrieval hook
(retrieve.py). Both paths must score identically so engram reinforcement is
consistent regardless of channel.

Design principle: every gate is a tweakable parameter — no thresholds or
boosts are hardcoded in the caller. The function is purely stateless (no I/O,
no index reads — the caller passes the entry list).
"""

from __future__ import annotations

import re
from typing import Optional

_ERROR_TERMS = frozenset({
    "error", "fail", "crash", "exception", "oom", "bug", "broken", "issue",
})


def tokenize(
    text: str,
    *,
    min_len: int = 1,
    stop: set[str] | None = None,
) -> set[str]:
    """Split text into lowercase tokens, filtering stopwords and short tokens."""
    if not text:
        return set()
    tokens = set(
        t for t in re.split(r"[\s\-_/.,;:!?()\"'\[\]{}@#]+", text.lower()) if t
    )
    if stop:
        tokens -= stop
    if min_len > 1:
        tokens = {t for t in tokens if len(t) >= min_len}
    return tokens


def search_engrams(
    query: str,
    entries: list[dict],
    *,
    project_id: Optional[str] = None,
    limit: int = 5,
    min_overlap: int = 1,
    min_score: float = 0.05,
    min_token_len: int = 1,
    stop_words: set[str] | None = None,
    confidence_boost_weight: float = 0.05,
    error_recipe_boost: float = 1.3,
) -> list[dict]:
    """Token Jaccard search across an engram list.

    Every gate is a parameter — no thresholds are hardcoded in the caller.
    Returns entries with an added ``score`` field, sorted desc.
    """
    query_tokens = tokenize(query, min_len=min_token_len, stop=stop_words)
    if not query_tokens:
        return []

    is_error_query = bool(query_tokens & _ERROR_TERMS)

    scored: list[tuple[float, dict]] = []
    for entry in entries:
        # Project-id scoping (same semantics as the original mcp_server._search)
        if project_id and entry.get("scope") == "project":
            if entry.get("project_id") not in (project_id, "global"):
                continue

        entry_tokens = tokenize(
            (entry.get("trigger") or ""),
            min_len=min_token_len, stop=stop_words,
        )
        entry_tokens |= tokenize(
            " ".join(entry.get("tags") or []),
            min_len=min_token_len, stop=stop_words,
        )
        entry_tokens |= tokenize(
            (entry.get("domain") or ""),
            min_len=min_token_len, stop=stop_words,
        )
        entry_tokens |= tokenize(
            (entry.get("id") or "").replace("-", " "),
            min_len=min_token_len, stop=stop_words,
        )

        if not entry_tokens:
            continue

        overlap = query_tokens & entry_tokens
        if len(overlap) < min_overlap:
            continue

        union = query_tokens | entry_tokens
        score = len(overlap) / len(union) if union else 0.0

        if is_error_query and entry.get("kind") == "error_recipe":
            score *= error_recipe_boost

        score += entry.get("confidence", 0.0) * confidence_boost_weight

        if score >= min_score:
            scored.append((score, entry))

    scored.sort(key=lambda x: (-x[0], -x[1].get("confidence", 0)))
    return [{"score": round(s, 3), **e} for s, e in scored[:limit]]
