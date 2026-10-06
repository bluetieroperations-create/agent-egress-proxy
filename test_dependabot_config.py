"""
test_dependabot_config.py -- every npm manifest must be WATCHED, by name.

WHY THIS EXISTS. There was no .github/dependabot.yml, so Dependabot ran on
auto-discovery. On 2026-09-15 it found the @vitest/mocker advisory and opened exactly ONE
pull request, against /integrations/openclaw (merged as #53). integrations/lucid depends
on the same package and was never raised; three weeks later it still carried tinypool
(critical, prototype pollution -> RCE), vitest (critical, path traversal), source-map-js
(high) and @vitest/mocker (moderate).

An advisory fixed in one directory and missed in its sibling is a coverage failure, and a
config listing directories by hand is only as good as the hand. So the config is checked
against the filesystem: add a package.json without listing it and this fails.

PARSED AS TEXT, not with pyyaml. The unittest job installs nothing and no other test in
this repo imports yaml, so a test that did would pass here and fail in CI -- which is
exactly how test_index_guard broke the build on 2026-09-30, by depending on something the
runner did not have. test_ci_coverage reads tests.yml the same way, for the same reason.
"""
import os
import re
import unittest

CONFIG = ".github/dependabot.yml"


def _repo_npm_dirs():
    """Every directory holding a package.json, as a /-prefixed repo-relative path."""
    found = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
        if "package.json" in files:
            rel = os.path.relpath(root, ".").replace(os.sep, "/")
            found.append("/" if rel == "." else "/" + rel)
    return sorted(found)


def _watched(ecosystem):
    """The `directory:` values declared for one ecosystem. Text-parsed deliberately."""
    with open(CONFIG) as fh:
        body = fh.read()
    # Split on the list markers so a directory is attributed to the block it sits in,
    # rather than to whichever `package-ecosystem` appeared most recently in the file.
    out = []
    for block in re.split(r"\n  - package-ecosystem:", body)[1:]:
        eco = block.lstrip().split("\n", 1)[0].strip().strip('"')
        m = re.search(r'^\s*directory:\s*"([^"]+)"', block, re.M)
        if eco == ecosystem and m:
            out.append(m.group(1))
    return sorted(out)


class TestEveryManifestIsWatched(unittest.TestCase):

    def test_the_config_exists(self):
        # kills: deleting the file and falling back to auto-discovery, which is what
        # produced the one-directory fix in the first place.
        self.assertTrue(os.path.exists(CONFIG), "%s is missing" % CONFIG)

    def test_every_npm_manifest_is_listed(self):
        # THE point of this file. kills: adding a package.json anywhere in the repo
        # without adding a Dependabot entry for it -- the silence that let lucid carry
        # two criticals for three weeks after its sibling was fixed.
        on_disk = set(_repo_npm_dirs())
        watched = set(_watched("npm"))
        missing = sorted(on_disk - watched)
        self.assertEqual(missing, [],
                         "package.json present but NOT watched by Dependabot:\n  "
                         + "\n  ".join(missing)
                         + "\n\nAdd a block for each in %s.\n" % CONFIG)

    def test_no_entry_points_at_a_directory_that_does_not_exist(self):
        # kills: leaving a stale block after a directory is removed or renamed, which
        # reads as coverage while watching nothing at all.
        on_disk = set(_repo_npm_dirs())
        stale = sorted(set(_watched("npm")) - on_disk)
        self.assertEqual(stale, [],
                         "Dependabot watches directories with no package.json:\n  "
                         + "\n  ".join(stale))

    def test_both_known_manifests_are_actually_found(self):
        # kills: a walk that silently matches nothing (a bad prune, a wrong cwd), which
        # would make the two tests above pass vacuously -- 0 on disk, 0 missing.
        dirs = _repo_npm_dirs()
        self.assertIn("/integrations/openclaw", dirs)
        self.assertIn("/integrations/lucid", dirs)

    def test_github_actions_is_watched(self):
        # The Node 20 deprecation notice has printed on every run in this repo for weeks.
        # kills: dropping the actions block, after which the next one goes unnoticed too.
        self.assertIn("/", _watched("github-actions"))


class TestTheIntegrationsDoNotDRIFT(unittest.TestCase):
    """openclaw and lucid share a test runner, and must share its major.

    This is the defect itself, stated as an invariant. #53 moved openclaw to vitest 5 and
    left lucid on 3; the two then disagreed for three weeks while lucid carried two
    criticals. Nothing in the repo noticed, because nothing compared them.

    Asserts they AGREE, not that they sit on a particular version. Pinning the number
    here would fail on the next legitimate bump and teach whoever hits it to delete the
    test -- Dependabot now watches both directories, so it raises them together and this
    stays green through an upgrade and red through a split.
    """

    @staticmethod
    def _vitest(directory):
        import json
        with open("integrations/%s/package.json" % directory) as fh:
            return json.load(fh)["devDependencies"]["vitest"]

    @staticmethod
    def _major(spec):
        return re.sub(r"^[^0-9]*", "", spec).split(".")[0]

    def test_both_integrations_pin_the_same_vitest_major(self):
        # kills: bumping one directory and not the other -- exactly what #53 did.
        oc, lu = self._vitest("openclaw"), self._vitest("lucid")
        self.assertEqual(self._major(oc), self._major(lu),
                         "openclaw pins vitest %s and lucid pins %s. They share a runner; "
                         "an advisory fixed in one and missed in the other is how lucid "
                         "carried two criticals for three weeks." % (oc, lu))

    def test_the_major_is_actually_parsed(self):
        # kills: a _major that returns the same thing for everything (''), which would
        # make the comparison above pass for any pair at all.
        self.assertEqual(self._major("^5.0.0"), "5")
        self.assertEqual(self._major("~3.2.1"), "3")
        self.assertEqual(self._major(">=4.0.0"), "4")
        self.assertNotEqual(self._major("^5.0.0"), self._major("^3.0.0"))
