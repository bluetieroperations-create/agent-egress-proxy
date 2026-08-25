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
        "requirements-signing.txt":
            "the native constant-time Ed25519 backend is not installed, so "
            "receipt_signer falls back to the variable-time pure-Python signer "
            "and blackwall REFUSES to boot with signing on a public bind",
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


class NativeSigningBackendIsInstalled(unittest.TestCase):
    """The pure-Python Ed25519 fallback is variable-time and ~163ms/signature.

    Measured: 3.6ms -> 172.6ms end-to-end, and runtime tracks the nonce's Hamming
    weight (55ms at weight 1, 109ms at weight 253), leaking r to anyone who can
    time responses. blackwall refuses to boot with signing enabled on a public
    bind unless the backend is constant-time -- so an image without this installed
    can serve verdicts but can never serve verifiable receipts publicly.
    """

    def test_dockerfile_installs_the_signing_requirements(self):
        # MUTATION: dropping the pip step -> the image silently loses the native
        # backend, and enabling receipts in production becomes a boot failure.
        text = dockerfile()
        self.assertIn("requirements-signing.txt", text)
        self.assertRegex(text, r"pip install[^\n]*requirements-signing\.txt")

    def test_install_refuses_a_source_build(self):
        # MUTATION: dropping --only-binary -> a missing wheel triggers a source
        # build needing a Rust toolchain this image does not have, turning a clear
        # failure into a long confusing one.
        #
        # Asserts on the RUN LINE, not the file: the first version of this test
        # checked `"--only-binary" in dockerfile()` and passed even with the flag
        # deleted, because the phrase also appears in the comment above the step.
        # Caught by mutating it.
        run_lines = [l for l in dockerfile().splitlines()
                     if l.strip().startswith("RUN ")
                     and "requirements-signing.txt" in l]
        self.assertTrue(run_lines, "no RUN line installs requirements-signing.txt")
        for line in run_lines:
            self.assertIn("--only-binary", line)

    def test_requirement_is_version_bounded(self):
        # An unbounded requirement makes builds non-reproducible for the one
        # dependency that signs receipts.
        with open(os.path.join(ROOT, "requirements-signing.txt")) as handle:
            body = [l.strip() for l in handle
                    if l.strip() and not l.strip().startswith("#")]
        self.assertTrue(body, "requirements-signing.txt has no requirement")
        self.assertTrue(any(">=" in l and "<" in l for l in body),
                        "pin cryptography to a bounded range: %r" % body)

    def test_core_still_imports_without_the_optional_dependency(self):
        # THE invariant this whole design rests on: the engine is stdlib-only and
        # must run with no third-party package installed.
        # MUTATION: importing cryptography at module scope in receipt_signer ->
        # fails here, and a plain python:slim image can no longer boot.
        import receipt_signer
        name, sign, _pub = receipt_signer.load_backend(prefer_native=False)
        self.assertEqual(name, receipt_signer.BACKEND_PURE)
        self.assertEqual(len(sign(bytes(range(32)), b"m")), 64)


class RestoreBlueprintKeepsTheOriginalHostname(unittest.TestCase):
    """On Render the service `name` IS the hostname: name X -> X.onrender.com.

    The engine's original address, agent-egress-proxy.onrender.com, is hard-wired
    into things this repo does not control: the remote MCP server at
    mcp.blackwalltier.com (which proxies to it), the public demo at
    check.blackwalltier.com/demo, and the awesome-x402 listing that sends traffic
    to both. With the backend down, every live forecast_payment call returns
    "Black_Wall oracle error: HTTP 404. No verdict -- do not treat as GO."

    Deploying render.yaml or render-free.yaml would come up as blackwall /
    blackwall-free .onrender.com -- a WORKING service at the WRONG address, which
    fixes nothing. So the restore blueprint's name is load-bearing, and renaming it
    silently re-breaks every one of those callers.
    """

    HOSTNAME = "agent-egress-proxy"

    def test_restore_blueprint_claims_the_original_service_name(self):
        # MUTATION: renaming the service -> the MCP server and demo keep 404ing
        # while the new deploy looks perfectly healthy at a different URL.
        with open(os.path.join(ROOT, "render-restore.yaml")) as handle:
            text = handle.read()
        self.assertRegex(text, r"name:\s*%s\b" % re.escape(self.HOSTNAME))

    def test_restore_blueprint_sets_the_advertised_index(self):
        # The free posture has no disk, so every artifact must come from the image.
        # MUTATION: dropping this -> the price-STOP corroboration arm is inert in
        # production and legitimate premium-tier endpoints get STOP'd.
        with open(os.path.join(ROOT, "render-restore.yaml")) as handle:
            text = handle.read()
        self.assertIn("BLACKWALL_ADVERTISED_INDEX", text)
        self.assertIn("/app/data/directory.json", text)

    def test_restore_blueprint_rate_limits_and_binds_publicly(self):
        with open(os.path.join(ROOT, "render-restore.yaml")) as handle:
            text = handle.read()
        self.assertIn("0.0.0.0", text)
        value = re.search(
            r'BLACKWALL_RATE_LIMIT(?:"?\s*=\s*|\s*\n\s*value:\s*)"?(\d+)', text)
        self.assertIsNotNone(value, "restore blueprint: unparseable rate limit")
        self.assertGreater(int(value.group(1)), 0)

    def test_docs_still_reference_the_hostname_we_are_restoring(self):
        # If the docs ever move to a different address, this blueprint is restoring
        # the wrong one. Keeps the two from drifting apart silently.
        with open(os.path.join(ROOT, "docs", "REGISTRIES.md")) as handle:
            self.assertIn(self.HOSTNAME + ".onrender.com", handle.read())


if __name__ == "__main__":
    unittest.main()
