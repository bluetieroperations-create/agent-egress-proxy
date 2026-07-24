#!/usr/bin/env python3
"""Entrypoint: run the HTTP API. `python -m agentdata.run_http` (from repo root)."""
from __future__ import annotations

from .core.config import Config
from .core.platform import build_default_platform
from .core.server import run


def main():
    platform = build_default_platform(Config.load())
    run(platform)


if __name__ == "__main__":
    main()
