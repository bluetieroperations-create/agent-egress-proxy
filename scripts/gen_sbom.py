#!/usr/bin/env python3
"""
Generate a CycloneDX 1.5 SBOM for BlackWall.

BlackWall has ZERO third-party dependencies (Python standard library only), so
its SBOM is small and honest: one application component, no dependency graph.
This generator is itself stdlib-only, matching the product's supply-chain
posture. It computes a SHA-256 over the shipped source so the SBOM is a real,
verifiable inventory rather than a hand-written claim.

Usage:
    python scripts/gen_sbom.py [--version VERSION] [--timestamp ISO8601] > sbom.json

--version    release/version string (default: "0.0.0-dev")
--timestamp  SBOM metadata timestamp (default: fixed placeholder; CI passes the
             build time). Kept a parameter so committed output is reproducible.
"""
import argparse
import hashlib
import json
import os
import sys

# The files that constitute the shipped product (the runtime is a single file;
# tests + audit are included as they are part of the verifiable artifact).
COMPONENT_FILES = ["egress_proxy.py"]
EVIDENCE_FILES = ["test_egress_proxy.py", "audit_claims.py"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_sbom(repo, version, timestamp):
    def hashes_for(rel):
        p = os.path.join(repo, rel)
        return [{"alg": "SHA-256", "content": sha256(p)}] if os.path.exists(p) else []

    component_hashes = []
    for rel in COMPONENT_FILES:
        component_hashes.extend(hashes_for(rel))

    properties = [
        {"name": "blackwall:dependencies", "value": "0 (Python standard library only)"},
        {"name": "blackwall:build", "value": "none (interpreted single-file source, no build step)"},
        {"name": "blackwall:python_requires", "value": ">=3.8"},
    ]
    for rel in EVIDENCE_FILES:
        for h in hashes_for(rel):
            properties.append({"name": f"blackwall:evidence:{rel}:sha256", "value": h["content"]})

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": [{"vendor": "BlueTier Operations", "name": "gen_sbom.py", "version": "1.0"}],
            "component": {
                "type": "application",
                "bom-ref": "pkg:generic/blackwall@" + version,
                "name": "blackwall",
                "version": version,
                "description": "Localhost-only egress-control forward proxy for AI agents.",
                "publisher": "BlueTier Operations",
                "licenses": [{"license": {"id": "MIT"}}],
                "hashes": component_hashes,
                "properties": properties,
            },
        },
        # Zero third-party dependencies: the component depends on nothing.
        "components": [],
        "dependencies": [
            {"ref": "pkg:generic/blackwall@" + version, "dependsOn": []}
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a CycloneDX SBOM for BlackWall.")
    ap.add_argument("--version", default="0.0.0-dev")
    ap.add_argument("--timestamp", default="1970-01-01T00:00:00Z",
                    help="SBOM metadata timestamp (CI passes build time)")
    args = ap.parse_args(argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sbom = build_sbom(repo, args.version, args.timestamp)
    json.dump(sbom, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
