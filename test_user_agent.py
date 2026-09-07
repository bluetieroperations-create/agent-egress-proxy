"""
Tests for user_agent.py -- the single source of truth for outbound User-Agent
strings, plus the REGRESSION LOCK that keeps it single.

The lock (TestNoHardcodedUserAgents) is the point of this file. Consolidating 21
scattered UA literals is worth nothing if the twenty-second gets pasted in next
month; two modules had already re-derived the same Cloudflare workaround
independently before this module existed. Each test states the mutation it kills.
"""
import os
import re
import unittest

import user_agent as U


class TestBrowserShape(unittest.TestCase):
    """`browser()` is what almost every caller wants: it survives a strict
    Cloudflare configuration, which challenges non-browser agents with a 403 --
    and 403 is PERMANENT in http_util, so the host fails hard, not slowly."""

    def test_is_browser_prefixed(self):
        # Mutation: drop the Mozilla prefix -> strict Cloudflare hosts 403 us.
        self.assertTrue(U.browser().startswith("Mozilla/5.0"), U.browser())
        self.assertTrue(U.browser("dex").startswith("Mozilla/5.0"), U.browser("dex"))

    def test_still_identifies_us(self):
        # Mutation: paste a real browser UA -> we impersonate rather than declare,
        # and a host operator can no longer tell who is calling or why.
        self.assertIn("Blackwall", U.browser())
        self.assertIn("Blackwall", U.browser("dex"))

    def test_component_is_namespaced_under_the_product(self):
        self.assertEqual(U.browser("dex"), "Mozilla/5.0 (compatible; Blackwall-dex/0.1)")

    def test_bare_default_has_no_trailing_separator(self):
        # Mutation: build the name by concatenation and leave "Blackwall-/0.1".
        self.assertEqual(U.browser(), "Mozilla/5.0 (compatible; Blackwall/0.1)")


class TestBotShape(unittest.TestCase):
    """`bot()` exists so the liveness sweep can be honestly identifiable as
    automated. That sweep is the evidence x402 hosts do not bot-block (0 of 195
    refused it); browser-shaping it would destroy the measurement."""

    def test_is_not_browser_prefixed(self):
        # Mutation: route bot() through browser() -> the survey silently starts
        # claiming to be a browser and its result stops meaning anything.
        self.assertFalse(U.bot("liveness").startswith("Mozilla"), U.bot("liveness"))

    def test_declares_itself_automated_and_lowercase(self):
        self.assertEqual(U.bot("liveness"), "blackwall-liveness/0.1")

    def test_contact_is_appended_in_the_conventional_form(self):
        self.assertEqual(U.bot("liveness", "x402 directory check"),
                         "blackwall-liveness/0.1 (+x402 directory check)")

    def test_the_two_shapes_are_actually_different(self):
        self.assertNotEqual(U.bot("x"), U.browser("x"))


class TestHeaderInjectionGuard(unittest.TestCase):
    """A UA is interpolated straight into an HTTP request header, so a component
    carrying CR/LF could split the request. This is a guard, not a style check."""

    def test_rejects_crlf(self):
        # Mutation: drop the guard -> header splitting via a caller-supplied name.
        for bad in ("a\r\nX-Evil: 1", "a\nb", "a\rb"):
            with self.assertRaises(ValueError):
                U.browser(bad)
            with self.assertRaises(ValueError):
                U.bot(bad)

    def test_rejects_ua_grammar_breakers(self):
        for bad in ("x(y)", "a;b", 'a"b', "a/b", "a,b"):
            with self.assertRaises(ValueError):
                U.browser(bad)

    def test_rejects_empty_and_non_string(self):
        for bad in ("", "   ", None, 7, [] ):
            with self.assertRaises(ValueError):
                U.bot(bad)

    def test_rejects_a_bad_contact_too(self):
        # Mutation: validate the component but forget the contact.
        with self.assertRaises(ValueError):
            U.bot("liveness", "ok\r\nX-Evil: 1")

    def test_rejects_a_bad_version_too(self):
        with self.assertRaises(ValueError):
            U.browser("dex", version="1.0\r\nX: y")


class TestNoHardcodedUserAgents(unittest.TestCase):
    """THE LOCK. Scans the repo for User-Agent literals defined anywhere other
    than user_agent.py. Without this, consolidation decays back to 21 variants."""

    #: A string literal that looks like a UA we would send.
    UA_LITERAL = re.compile(
        r"""["'][^"']*(?:Mozilla/\d|(?i:blackwall)[-\w]*/\d)[^"']*["']""")
    #: Only flag literals actually being used AS a user-agent. Both spellings
    #: matter: the header form (`"User-Agent": ...`, `user_agent=...`) AND the
    #: constant form (`DEFAULT_UA = ...`), which is how the original http_util
    #: bug was written -- a lock that missed it would have missed the bug.
    UA_CONTEXT = re.compile(
        r"""(?i)(?:user[-_]agent["']?\s*[=:]|\b[A-Z_]*UA["']?\s*=)""")

    def _sources(self):
        """Production modules only. Test files legitimately construct UA strings
        to exercise overrides, so scanning them would flag their own fixtures."""
        for root, dirs, files in os.walk(os.path.dirname(os.path.abspath(__file__))):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "node_modules", "data")]
            for f in files:
                if f.endswith(".py") and not f.startswith("test_") and f != "user_agent.py":
                    yield os.path.join(root, f)

    def test_no_module_defines_its_own_user_agent(self):
        offenders = []
        for path in self._sources():
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]
                    if self.UA_CONTEXT.search(code) and self.UA_LITERAL.search(code):
                        offenders.append("%s:%d: %s"
                                         % (os.path.basename(path), n, line.strip()))
        self.assertEqual(offenders, [], "\n\nUser-Agent literals outside user_agent.py.\n"
                         "Call user_agent.browser() or user_agent.bot() instead:\n  "
                         + "\n  ".join(offenders) + "\n")

    def test_the_lock_actually_catches_something(self):
        # A lock that cannot fail is not a lock: prove the matcher fires on the
        # exact shapes this repo used to contain, in their real contexts.
        for line in ('    DEFAULT_UA = "Blackwall/0.1"',
                     '    headers = {"User-Agent": "Mozilla/5.0 (Blackwall bazaar check)"}',
                     '''    req(url, headers={"user-agent": "Blackwall-dex/1"})''',
                     '''    get_json(url, user_agent="blackwall-liveness/1.0")'''):
            code = line.split("#", 1)[0]
            self.assertTrue(self.UA_CONTEXT.search(code) and self.UA_LITERAL.search(code),
                            "lock failed to flag: %s" % line)

    def test_the_lock_does_not_fire_on_innocent_lines(self):
        # Mutation: widen the matcher until it flags ordinary strings and someone
        # disables the whole test to get the build green.
        for line in ('    print("blackwall: verdict anchoring ON")',
                     '    HOST = "blackwall-free.onrender.com"',
                     '    LABEL = "blackwall-ledger-mirror-v1"',
                     '    ua = user_agent.browser("dex")'):
            code = line.split("#", 1)[0]
            self.assertFalse(self.UA_CONTEXT.search(code) and self.UA_LITERAL.search(code),
                             "lock false-positived on: %s" % line)


class TestMigratedCallSites(unittest.TestCase):
    """The modules that had their own UA constant now source it from the policy,
    and the two that must stay bot-shaped still are."""

    def test_browser_shaped_modules(self):
        import http_util
        import reputation_onchain
        import settlement_watch
        for mod, ua in (("http_util", http_util.DEFAULT_UA),
                        ("reputation_onchain", reputation_onchain.DEFAULT_UA),
                        ("settlement_watch", settlement_watch.DEFAULT_UA)):
            self.assertTrue(ua.startswith("Mozilla/5.0"), "%s: %s" % (mod, ua))
            self.assertIn("Blackwall", ua)

    def test_survey_modules_stay_bot_shaped(self):
        # Mutation: sweep directory_liveness into browser() with everything else
        # -> the 0-of-195 "x402 hosts do not bot-block" result becomes unfounded.
        import asset_coverage
        import directory_liveness
        for mod, ua in (("directory_liveness", directory_liveness.USER_AGENT),
                        ("asset_coverage", asset_coverage.USER_AGENT)):
            self.assertFalse(ua.startswith("Mozilla"), "%s: %s" % (mod, ua))
            self.assertTrue(ua.startswith("blackwall-"), "%s: %s" % (mod, ua))

    def test_liveness_still_carries_its_contact_note(self):
        import directory_liveness
        self.assertIn("(+", directory_liveness.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
