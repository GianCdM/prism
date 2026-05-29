"""Prism dashboard — metrics collection + local web UI.

Exposes:
  - collector: reads observations, archives, transcripts, MCP call logs
    into a local SQLite DB
  - server: Flask app at http://localhost:7878 with 6 views
"""
