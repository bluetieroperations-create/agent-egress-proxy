"""
Every test file must actually run somewhere in CI.

WHY THIS EXISTS
---------------
The suite is this repo's primary quality guarantee, but the list of files to run
is hand-maintained in two places (.github/workflows/tests.yml and the Makefile).
Twelve test files had silently drifted out of both -- among them
test_bounded_server.py and test_remote_ledger.py, which cover the admission
control and the encrypted ledger mirror, i.e. the two most recently shipped
subsystems and the ones carrying HIGH-severity fixes. 422 tests were passing
locally and gating nothing. Nobody noticed until they were run by hand.

A hand-maintained list will drift again. This test fails the build the moment a
test file exists that no CI step runs, so the thirteenth cannot go quiet.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, ".github", "workflows", "tests.yml")
MAKEFILE = os.path.join(HERE, "Makefile")

#: Test files deliberately NOT run by the root jobs, each with its reason.
#: Adding an entry here is a decision that needs justifying in review; it is not
#: a place to silence a failing test.
EXEMPT = {
    # Live in their own directories with their own dependencies and their own
    # CI job (`integrations`), which invokes them by path from that directory.
    "integrations/langchain/test_blackwall_guard.py": "integrations job",
    "integrations/langchain/test_langchain_blackwall.py": "integrations job",
    "integrations/wallets/test_wallet_guard.py": "integrations job",
    "integrations/wallets/test_turnkey_signer.py": "integrations job",
    "integrations/wallets/test_privy_signer.py": "integrations job",
    "integrations/agentcore/test_agentcore_guard.py": "integrations job",
}


def _discover():
    """Every test_*.py in the repo, as a repo-relative POSIX path."""
    found = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), HERE)
                found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def _named_in(path):
    """Test filenames mentioned anywhere in a config file."""
    with open(path, "r", encoding="utf-8") as fh:
        return set(re.findall(r"\btest_[A-Za-z0-9_]+\.py\b", fh.read()))


class TestEveryTestFileRunsInCI(unittest.TestCase):
    def test_workflow_runs_every_test_file(self):
        # Mutation: add a test file and forget the workflow -> it gates nothing.
        named = _named_in(WORKFLOW)
        missing = [p for p in _discover()
                   if p not in EXEMPT and os.path.basename(p) not in named]
        self.assertEqual(missing, [], "\n\nTest files not run by any CI step in "
                         ".github/workflows/tests.yml:\n  " + "\n  ".join(missing) +
                         "\n\nAdd them to a step, or to EXEMPT here with a reason.\n")

    def test_makefile_runs_every_stdlib_test_file(self):
        # `make test` is what a contributor runs before pushing. If it is narrower
        # than CI, the build breaks after the push instead of before it.
        named = _named_in(MAKEFILE)
        missing = [p for p in _discover()
                   if p not in EXEMPT and os.path.basename(p) not in named]
        self.assertEqual(missing, [], "\n\nTest files missing from the Makefile "
                         "`test` target:\n  " + "\n  ".join(missing) +
                         "\n\nKeep it in step with .github/workflows/tests.yml.\n")

    def test_exempt_entries_still_exist(self):
        # Mutation: EXEMPT rots into a list of deleted files and quietly excuses
        # a real gap the next time a path matches one of its stale entries.
        for rel in EXEMPT:
            self.assertTrue(os.path.exists(os.path.join(HERE, rel)),
                            "EXEMPT names a file that no longer exists: %s" % rel)

    def test_discovery_finds_this_file(self):
        # A discovery bug that returned nothing would make both checks vacuous.
        self.assertIn("test_ci_coverage.py", _discover())
        self.assertGreater(len(_discover()), 50)


if __name__ == "__main__":
    unittest.main()
