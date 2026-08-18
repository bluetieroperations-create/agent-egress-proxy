"""Guard the DEPLOY MANIFEST: files the running container must actually contain.

WHY THIS EXISTS. A missing data file does not crash Blackwall -- every artifact
loader fails OPEN by design, so the container boots, answers /healthz with 200,
and silently serves DEGRADED verdicts. That failure mode has now happened twice:
the RWA gate shipped unwired, and `data/directory.json` was left out of the image
while `advertised_prices.py` defaulted to reading it.

Verified by simulating the image layout: with directory.json the netintel.dev
request returns HOLD; without it, healthz is still 200 and the same request
returns STOP. Nothing in the test suite or the health check could see it.

These tests read the Dockerfile as text on purpose -- they must fail when someone
edits the COPY lines, not merely when a file is absent from the repo.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def dockerfile():
    with open(os.path.join(ROOT, "Dockerfile")) as handle:
        return handle.read()


def copied_paths():
    """Every source path named on a COPY line in the Dockerfile."""
    paths = []
    for line in dockerfile().splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = line.split()[1:]
        if len(parts) >= 2:
            paths.extend(parts[:-1])       # last token is the destination
    return paths


class DeployManifest(unittest.TestCase):
    #: Runtime data artifacts the image must carry, and what breaks without each.
    REQUIRED = {
        "data/directory.json":
            "advertised_prices.py -> the price-STOP corroboration arm goes inert "
            "and legitimate premium-tier endpoints are STOP'd",
        "data/reputation_seed.db.gz":
            "the container boots with an EMPTY store -> every payee cold-starts",
        "data/category_index.json":
            "the per-category price baseline goes inert",
        "data/divergence_index.json":
            "the advertised-vs-settled divergence gate goes inert",
        "sanctions.txt":
            "OFAC screening falls back to empty -> the compliance floor is gone",
    }

    def test_every_required_artifact_is_copied_into_the_image(self):
        # MUTATION: deleting any COPY line above -> this names the exact file and
        # the exact consequence, instead of a 200 /healthz and a wrong verdict.
        copied = copied_paths()
        for path, consequence in self.REQUIRED.items():
            self.assertTrue(
                any(path in entry for entry in copied),
                "Dockerfile does not COPY %s -- %s" % (path, consequence))

    def test_required_artifacts_exist_in_the_repo(self):
        # A COPY of a missing file fails the BUILD, which is loud and fine; this
        # catches the artifact being deleted while the COPY line stays.
        for path in self.REQUIRED:
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)),
                            "missing repo artifact: %s" % path)

    def test_all_modules_are_copied_not_a_hand_maintained_subset(self):
        # forecast() lazily imports modules at REQUEST time, so a subset silently
        # 502s the verdict path. The Dockerfile comment says this; assert it.
        # MUTATION: replacing `COPY *.py` with a list -> fails here.
        self.assertIn("COPY *.py", dockerfile())

    def test_advertised_index_default_matches_what_is_copied(self):
        # The wiring reads BLACKWALL_ADVERTISED_INDEX with a default path. If that
        # default drifts from the COPY destination, the arm goes inert again.
        with open(os.path.join(ROOT, "blackwall.py")) as handle:
            source = handle.read()
        match = re.search(r'BLACKWALL_ADVERTISED_INDEX"\s*,\s*"([^"]+)"', source)
        self.assertIsNotNone(match, "advertised-index default not found in blackwall.py")
        self.assertIn(match.group(1), " ".join(copied_paths()),
                      "the default advertised-index path is not COPYed into the image")


class PublicEndpointDefaults(unittest.TestCase):
    """A PUBLIC deploy must not ship with throttling off.

    BLACKWALL_RATE_LIMIT defaults to "0", which DISABLES limiting. render-free.yaml
    set it; render.yaml and fly.toml did not, so the paid and Fly deploys would have
    exposed an unthrottled public endpoint -- worst with billing OFF, where every
    call is free to the caller.
    """

    CONFIGS = ("render.yaml", "render-free.yaml", "fly.toml")

    def test_every_public_deploy_config_sets_a_rate_limit(self):
        # MUTATION: dropping the setting from any config -> that target ships
        # unthrottled, and nothing else in the suite would notice.
        for name in self.CONFIGS:
            with open(os.path.join(ROOT, name)) as handle:
                text = handle.read()
            self.assertIn("BLACKWALL_RATE_LIMIT", text,
                          "%s exposes a public endpoint with rate limiting at its "
                          "default of 0 (disabled)" % name)
            # Two shapes: TOML `KEY = "120"` and YAML `- key: KEY` / `value: "120"`
            # on the NEXT line (the colon precedes the name there, not follows it).
            value = re.search(
                r'BLACKWALL_RATE_LIMIT(?:"?\s*=\s*|\s*\n\s*value:\s*)"?(\d+)', text)
            self.assertIsNotNone(value, "%s: unparseable rate limit" % name)
            self.assertGreater(int(value.group(1)), 0,
                               "%s sets a rate limit of 0, which disables it" % name)

    def test_every_public_deploy_config_binds_all_interfaces(self):
        # A container that binds 127.0.0.1 is unreachable from outside; the default
        # is localhost-only by design, so each config must opt in explicitly.
        for name in self.CONFIGS:
            with open(os.path.join(ROOT, name)) as handle:
                self.assertIn("0.0.0.0", handle.read(), "%s does not bind 0.0.0.0" % name)


if __name__ == "__main__":
    unittest.main()
