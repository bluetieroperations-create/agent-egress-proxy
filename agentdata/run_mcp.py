#!/usr/bin/env python3
"""Entrypoint: run the MCP (stdio) server. `python -m agentdata.run_mcp`.

Wire this into an MCP client (Claude Desktop, Cursor) as a stdio server command.
"""
from __future__ import annotations

from .core.config import Config
from .core.mcp import run
from .core.platform import build_default_platform


def main():
    platform = build_default_platform(Config.load())
    run(platform)


if __name__ == "__main__":
    main()
