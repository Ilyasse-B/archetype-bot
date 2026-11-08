"""
hackathon demo viewer — separate from the trading daemon.

starts/stops ollama + daemon + agent loop, and proxies live feeds.
not part of the core product.
"""

from __future__ import annotations

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 8787
