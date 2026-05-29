#!/usr/bin/env bash
# Prism retrieval hook for Claude Code (UserPromptSubmit).
# Reads the prompt on stdin, prints relevant engrams as context on stdout.
# NEVER blocks Claude Code — always exit 0.

PRISM_HOME="${PRISM_HOME:-$HOME/.prism}"
python3 "$PRISM_HOME/lib/retrieve.py" 2>/dev/null || true
exit 0
