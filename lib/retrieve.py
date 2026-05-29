#!/usr/bin/env python3
"""Prism retrieval hook (Claude Code UserPromptSubmit).

Reads the user's prompt on stdin, searches the FULL engram base by relevance to
that prompt, and prints the top matches as additional context. The push layer
(prism.md) only carries the top ~20 by confidence; this surfaces the REST — the
dormant base — automatically, by relevance to what the user is asking right now.

Activates the PULL layer without relying on the model deciding to search nor on
the user asking. Never blocks: exits 0, prints nothing on low signal or error.
"""
# TODO (ao promover pra MR no upstream): unificar com a busca do prism_search.
# Este hook DUPLICA de propósito o algoritmo de mcp_server._search (Jaccard de
# tokens sobre o index) — escolha consciente pra não mexer no servidor MCP em
# uso enquanto isto é protótipo local. Na MR:
#   1. extrair _tokenize + _search pra um módulo compartilhado (ex.:
#      lib/engram_search.py); mcp_server._search vira wrapper fino.
#   2. importar search_engrams() de lá, passando os gates como params
#      (min_overlap=2, token_min_len=4, stopwords=_STOP, include_id=True).
#   3. fechar os 2 gaps desta cópia vs o _search original (no-op p/ base 100%
#      global de hoje): scoping por project_id (mcp_server._search L57-59) e
#      boost de error_recipe pra queries de erro (L70).
import json
import os
import re
import sys
from pathlib import Path

MIN_PROMPT_CHARS = 15   # skip terse prompts ("ok", "vai", "continua")
MAX_RESULTS = 5         # token budget per turn
MIN_SCORE = 0.05        # relevance gate
MIN_OVERLAP = 2         # require >=2 shared terms (cuts single-common-word noise)

_STOP = {
    "quando", "para", "como", "onde", "fazer", "quero", "preciso", "sobre",
    "isso", "esse", "essa", "esses", "aquele", "tudo", "agora", "depois",
    "entao", "tambem", "ainda", "porque", "qual", "quais", "mais", "menos",
    "pode", "fica", "vamos", "isso", "esta", "este", "seria",
    "with", "when", "what", "that", "this", "into", "from", "your", "have",
    "should", "would", "could", "about", "which", "there", "their", "then",
    "prism", "engram", "claude",
}


def _tokenize(text):
    return {
        t for t in re.split(r"[\s\-_/.,;:!?()\"'\[\]{}@#]+", (text or "").lower())
        if len(t) >= 4 and t not in _STOP
    }


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
    try:
        index = json.loads((prism_home / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return

    qtok = _tokenize(prompt)
    if len(qtok) < 2:
        return

    scored = []
    for e in index.get("engrams", []):
        etok = _tokenize(
            (e.get("trigger") or "") + " "
            + (e.get("id") or "").replace("-", " ") + " "
            + " ".join(e.get("tags") or []) + " "
            + (e.get("domain") or "")
        )
        if not etok:
            continue
        overlap = qtok & etok
        if len(overlap) < MIN_OVERLAP:
            continue
        score = len(overlap) / len(qtok | etok) + (e.get("confidence", 0) * 0.03)
        if score >= MIN_SCORE:
            scored.append((score, e))

    if not scored:
        return
    scored.sort(key=lambda x: -x[0])
    top = scored[:MAX_RESULTS]

    out = [
        "<prism-knowledge>",
        "Engrams aprendidos possivelmente relevantes pra esta mensagem "
        "(use `prism_get <id>` pro conteúdo completo se for aplicar):",
    ]
    for _, e in top:
        trig = (e.get("trigger") or "").strip().strip('"')[:130]
        out.append(f"- [{e.get('id')}] (conf {e.get('confidence', '?')}) — {trig}")
    out.append("</prism-knowledge>")
    print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block the prompt (CC-safe)
