#!/usr/bin/env python3
"""
creds_local.py -- load Blackwall/CDP credentials from a local file so the cdp_*
probe scripts don't need env vars set by hand on every run.

Reads ~/.blackwall-creds (KEY=VALUE lines) and populates os.environ for any key
that isn't ALREADY set (so an explicit env var still overrides the file, and
nothing breaks if the file is absent). The file lives in your HOME directory,
OUTSIDE any git repo, so it can't be committed. This module contains NO secrets
itself -- it only reads them -- so it is safe to commit.

Create ~/.blackwall-creds once (see setup note printed by the probes), e.g.:

    CDP_API_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    CDP_API_KEY_SECRET=base64secret==
    BAZAAR_WALLET_KEY=0x...        # optional: verify-probe / paid call only
"""
import os

CREDS_PATH = os.path.expanduser("~/.blackwall-creds")


def load_creds(path=CREDS_PATH):
    """
    Populate os.environ from the creds file for keys not already set. No-op if the
    file is absent. Empty values are skipped (so an unfilled template behaves like
    'not set'). Returns the list of key NAMES loaded (never values).
    """
    loaded = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded
