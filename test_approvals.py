"""
Tests for approvals.py -- the human-in-the-loop half of a HOLD.

Each test states the mutation it kills. Pure and network-free.
"""
import unittest

import approvals as A

HOLD = {"verdict": "HOLD", "receipt_id": "r1", "reasons": ["thin history"]}
CLAIM = {"counterparty": "0xA", "amount": "0.05", "asset": "USDC", "chain": "base"}


def opened(verdict=None, claim=None, now=1000, **kw):
    return A.open_approval(verdict or HOLD, claim or CLAIM, now=now, **kw)


class TestOnlyAHoldIsApprovable(unittest.TestCase):
    """Property 1. A human clicking approve must not be able to route around a
    sanctions hit, a payload mismatch or a leaked credential."""

    def test_a_stop_can_never_be_opened_for_approval(self):
        # Kills: widening APPROVABLE, which turns the whole mechanism into a
        # one-click bypass of the only verdicts that are non-negotiable.
        self.assertIsNone(A.open_approval({"verdict": "STOP"}, CLAIM))

    def test_a_hard_stop_labelled_HOLD_is_still_refused(self):
        # Kills: trusting the verdict STRING alone. A HOLD carrying hard_stop is
        # incoherent, and the safe reading of an incoherent verdict is the more
        # restrictive one.
        self.assertIsNone(
            A.open_approval({"verdict": "HOLD", "hard_stop": True}, CLAIM))

    def test_a_go_is_not_opened_because_there_is_nothing_to_ask(self):
        # Kills: opening approvals for everything, which trains the operator to
        # click through and destroys the signal.
        self.assertIsNone(A.open_approval({"verdict": "GO"}, CLAIM))

    def test_junk_verdicts_are_refused_not_defaulted(self):
        # Kills: treating an unrecognized verdict as approvable.
        for bad in (None, {}, [], "HOLD", {"verdict": "MAYBE"}, {"verdict": None}):
            self.assertIsNone(A.open_approval(bad, CLAIM), bad)


class TestBoundToTheExactPayment(unittest.TestCase):
    """Property 2, the load-bearing one. Without it an approval is a laundering
    step: get five cents approved, spend it on five hundred dollars."""

    def _approved(self):
        return A.decide(opened(), True, now=1010)

    def test_a_different_amount_cannot_redeem_it(self):
        # Kills: dropping the digest check.
        ok, why, _ = A.redeem(self._approved(), dict(CLAIM, amount="500"), now=1020)
        self.assertFalse(ok)
        self.assertIn("different payment", why)

    def test_a_different_payee_cannot_redeem_it(self):
        # Kills: binding on amount alone -- the payee is the half that matters.
        ok, _, _ = A.redeem(self._approved(), dict(CLAIM, counterparty="0xB"), now=1020)
        self.assertFalse(ok)

    def test_a_different_chain_cannot_redeem_it(self):
        # Kills: ignoring the network. The same address on another chain is a
        # different contract and a different recipient.
        ok, _, _ = A.redeem(self._approved(), dict(CLAIM, chain="eip155:137"), now=1020)
        self.assertFalse(ok)

    def test_extra_context_does_NOT_invalidate_it(self):
        # Kills: digesting the whole claim. A caller may legitimately re-send
        # with a resource URL or price history attached, and a human's answer
        # must survive that -- otherwise the feature is unusable in practice.
        richer = dict(CLAIM, resource="https://x.test/a", price_history=["0.05"])
        ok, _, _ = A.redeem(self._approved(), richer, now=1020)
        self.assertTrue(ok)

    def test_the_digest_is_case_and_whitespace_stable(self):
        # Kills: hashing raw strings. A live 402 returns an EIP-55 checksummed
        # payTo while the crawl stores lowercase -- the exact join that silently
        # missed 64 of 69 endpoints in advertised_prices.
        self.assertEqual(A.claim_digest(CLAIM),
                         A.claim_digest(dict(CLAIM, counterparty="  0xa  ")))

    def test_distinct_payments_do_not_collide(self):
        # Kills: a digest built by concatenation without a separator, where
        # ("ab","c") and ("a","bc") hash alike.
        self.assertNotEqual(A.claim_digest({"counterparty": "ab", "amount": "c"}),
                            A.claim_digest({"counterparty": "a", "amount": "bc"}))


class TestSingleUseAndExpiry(unittest.TestCase):
    """Properties 3 and 4."""

    def test_an_approval_is_consumed_by_use(self):
        # Kills: leaving it APPROVED, which turns one answer into a standing
        # allowance for every future payment with those fields.
        rec = A.decide(opened(), True, now=1010)
        ok, _, consumed = A.redeem(rec, CLAIM, now=1020)
        self.assertTrue(ok)
        self.assertEqual(consumed["state"], A.CONSUMED)
        self.assertFalse(A.redeem(consumed, CLAIM, now=1021)[0])

    def test_a_failed_redemption_does_NOT_burn_the_approval(self):
        # Kills: consuming on the wrong-claim path, which would let anyone who
        # can guess an id destroy a human's answer.
        rec = A.decide(opened(), True, now=1010)
        A.redeem(rec, dict(CLAIM, amount="9"), now=1020)
        self.assertTrue(A.redeem(rec, CLAIM, now=1020)[0])

    def test_it_dies_ON_its_expiry_second_not_after(self):
        # Kills: a > comparison. Off-by-one on a security window is a window.
        rec = A.decide(opened(now=1000, ttl=10), True, now=1005)
        self.assertFalse(A.redeem(rec, CLAIM, now=1010)[0])
        self.assertTrue(A.redeem(rec, CLAIM, now=1009)[0])

    def test_deciding_an_expired_request_expires_it_rather_than_approving(self):
        # Kills: letting a human approve something the engine judged long ago,
        # after a payee could have been sanctioned in between.
        rec = A.decide(opened(now=1000, ttl=10), True, now=5000)
        self.assertEqual(rec["state"], A.EXPIRED)

    def test_a_junk_ttl_falls_back_rather_than_becoming_infinite(self):
        # Kills: int(ttl) raising or a None ttl producing an approval that
        # never expires.
        for bad in (None, "abc", -5, 0, [1]):
            rec = A.open_approval(HOLD, CLAIM, now=1000, ttl=bad)
            self.assertGreater(rec["expires_at"], 1000, bad)


class TestTerminalStatesAreFinal(unittest.TestCase):
    def test_a_decline_cannot_be_flipped_to_approved(self):
        # Kills: re-deciding, which lets a retry loop grind an approval out of
        # a refusal.
        declined = A.decide(opened(), False, now=1010)
        again = A.decide(declined, True, now=1011)
        self.assertEqual(again["state"], A.DECLINED)

    def test_a_declined_approval_says_so_rather_than_saying_pending(self):
        # Kills: collapsing declined into the generic not-approved branch. The
        # caller must be able to tell "wait" from "no".
        ok, why, _ = A.redeem(A.decide(opened(), False, now=1010), CLAIM, now=1020)
        self.assertFalse(ok)
        self.assertIn("declined", why)

    def test_pending_cannot_be_redeemed(self):
        # Kills: treating "opened" as "allowed".
        ok, why, _ = A.redeem(opened(), CLAIM, now=1010)
        self.assertFalse(ok)
        self.assertIn("pending", why)

    def test_decide_never_mutates_its_input(self):
        # Kills: in-place edits, which every other fold in this codebase avoids.
        rec = opened()
        A.decide(rec, True, now=1010)
        self.assertEqual(rec["state"], A.PENDING)


class TestTheTokenIsOwnerOnly(unittest.TestCase):
    """Property 5. Seeing an id in a log must not authorize deciding it."""

    def test_a_token_authorizes_only_its_own_approval(self):
        # Kills: a constant token, or one not bound to the id.
        self.assertTrue(A.verify_approval_token("a1", A.sign_approval_token("a1")))
        self.assertFalse(A.verify_approval_token("a1", A.sign_approval_token("a2")))

    def test_an_approval_token_is_not_a_report_token(self):
        # Kills: dropping the domain separator, which would silently make the
        # right to report an outcome into the right to authorize a payment.
        import blackwall
        self.assertNotEqual(A.sign_approval_token("x"),
                            blackwall.sign_report_token("x"))

    def test_missing_and_malformed_tokens_are_refused(self):
        # Kills: a truthiness check that lets None or a non-string through.
        for bad in (None, "", 0, [], {}, b"x"):
            self.assertFalse(A.verify_approval_token("a1", bad), bad)


class TestWhatAPollerCanSee(unittest.TestCase):
    def test_the_public_view_never_leaks_the_digest(self):
        # Kills: returning the record verbatim. BOUND_FIELDS is short and
        # low-entropy, so publishing the digest lets anyone with the id confirm
        # amounts and payees by brute force.
        self.assertNotIn("digest", A.public_view(A.decide(opened(), True, now=1010)))

    def test_the_public_view_carries_what_a_human_needs_to_decide(self):
        # Kills: redacting so hard the approver cannot see what they are approving.
        view = A.public_view(opened())
        for field in ("approval_id", "state", "verdict", "reasons", "expires_at"):
            self.assertIn(field, view)


class TestTheStore(unittest.TestCase):
    def test_it_is_bounded_so_an_opener_cannot_exhaust_memory(self):
        # Kills: an unbounded dict. Opening approvals is the cheapest call in
        # the API and it writes.
        store = A.MemoryApprovalStore(limit=5)
        for i in range(50):
            store.put(A.open_approval(HOLD, CLAIM, now=1000 + i))
        self.assertLessEqual(len(store), 5)

    def test_eviction_drops_the_oldest_which_fails_safe(self):
        # Kills: evicting the newest. A dropped approval cannot be redeemed,
        # so losing the oldest is the safe direction.
        store = A.MemoryApprovalStore(limit=2)
        first = store.put(A.open_approval(HOLD, CLAIM, now=1000))
        store.put(A.open_approval(HOLD, CLAIM, now=2000))
        store.put(A.open_approval(HOLD, CLAIM, now=3000))
        self.assertIsNone(store.get(first["approval_id"]))

    def test_sweep_expires_pending_rows(self):
        # Kills: a sweep that silently APPROVES or deletes rather than expiring.
        store = A.MemoryApprovalStore()
        rec = store.put(A.open_approval(HOLD, CLAIM, now=1000, ttl=10))
        self.assertEqual(A.sweep(store, now=5000), 1)
        self.assertEqual(store.get(rec["approval_id"])["state"], A.EXPIRED)

    def test_the_store_hands_back_copies_not_references(self):
        # Kills: returning the live dict, letting a caller edit stored state.
        store = A.MemoryApprovalStore()
        rec = store.put(A.open_approval(HOLD, CLAIM, now=1000))
        got = store.get(rec["approval_id"])
        got["state"] = A.APPROVED
        self.assertEqual(store.get(rec["approval_id"])["state"], A.PENDING)


class TestNeverRaises(unittest.TestCase):
    def test_hostile_input_across_the_surface(self):
        # Kills: assuming shape. Every one of these is reachable from a request.
        for bad in (None, {}, [], "x", 7, {"state": "weird"}):
            A.redeem(bad, CLAIM)
            A.decide(bad, True)
            A.public_view(bad)
            A.is_expired(bad)
        for bad in (None, [], "x", 7):
            A.claim_digest(bad)


class TestTheLiveHttpPath(unittest.TestCase):
    """Runs a REAL server. Unit tests cannot see the binding hazard honeypot.py
    documents: an attribute declared on _Handler but never bound in
    serve_forever keeps its None default, every call 500s, and the route is
    still advertised in the descriptor."""

    @classmethod
    def setUpClass(cls):
        import json as _json
        import threading
        import urllib.request
        import blackwall
        cls._json, cls._req = _json, urllib.request
        src = blackwall.MockReputationSource()
        cls.server = blackwall.BlackwallServer(host="127.0.0.1", port=0,
                                               reputation_source=src)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(200):
            if getattr(cls.server, "_httpd", None):
                break
            import time as _t
            _t.sleep(0.02)
        cls.base = "http://127.0.0.1:%d" % cls.server._httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server._httpd.shutdown()
        except Exception:
            pass

    def _post(self, path, body):
        data = self._json.dumps(body).encode()
        req = self._req.Request(self.base + path, data,
                                {"Content-Type": "application/json"})
        try:
            with self._req.urlopen(req, timeout=10) as r:
                return r.status, self._json.load(r)
        except Exception as e:
            return getattr(e, "code", 0), self._json.loads(e.read()) if hasattr(e, "read") else {}

    def _get(self, path):
        try:
            with self._req.urlopen(self.base + path, timeout=10) as r:
                return r.status, self._json.load(r)
        except Exception as e:
            return getattr(e, "code", 0), {}

    def test_the_whole_loop_works_over_http(self):
        # Kills: the seventh-edit binding omission. Every assertion here passes
        # in-process with the store unbound and 500s over HTTP.
        status, opened_ = self._post("/v1/approvals", {
            "verdict": {"verdict": "HOLD", "receipt_id": "r9",
                        "reasons": ["thin history"]},
            "claim": CLAIM})
        self.assertEqual(status, 200, opened_)
        self.assertTrue(opened_["approvable"])
        approval_id, token = opened_["approval_id"], opened_["approval_token"]

        status, polled = self._get("/v1/approvals/" + approval_id)
        self.assertEqual((status, polled["state"]), (200, A.PENDING))

        status, decided = self._post("/v1/approvals/decide", {
            "approval_id": approval_id, "approval_token": token,
            "approve": True, "actor": "ops@example.test"})
        self.assertEqual((status, decided["state"]), (200, A.APPROVED))

        status, polled = self._get("/v1/approvals/" + approval_id)
        self.assertEqual(polled["state"], A.APPROVED)

    def test_a_stop_is_refused_over_http_too(self):
        # Kills: the API offering a button the core refuses. The route must not
        # be the place the STOP rule is forgotten.
        status, body = self._post("/v1/approvals", {
            "verdict": {"verdict": "STOP", "hard_stop": True}, "claim": CLAIM})
        self.assertEqual(status, 200)
        self.assertFalse(body["approvable"])

    def test_deciding_without_the_token_is_refused(self):
        # Kills: dropping the token check, which would let anyone who sees an
        # id in a log approve someone else's payment.
        _, opened_ = self._post("/v1/approvals", {
            "verdict": {"verdict": "HOLD"}, "claim": CLAIM})
        status, _ = self._post("/v1/approvals/decide", {
            "approval_id": opened_["approval_id"], "approve": True})
        self.assertEqual(status, 403)

    def test_a_wrong_token_gets_403_for_a_REAL_id_too(self):
        # Kills: answering 404 for an unknown id and 403 for a known one, which
        # turns the endpoint into an oracle for enumerating pending approvals.
        _, opened_ = self._post("/v1/approvals", {
            "verdict": {"verdict": "HOLD"}, "claim": CLAIM})
        real, _ = self._post("/v1/approvals/decide", {
            "approval_id": opened_["approval_id"],
            "approval_token": "0" * 32, "approve": True})
        fake, _ = self._post("/v1/approvals/decide", {
            "approval_id": "does-not-exist",
            "approval_token": "0" * 32, "approve": True})
        self.assertEqual(real, 403)
        self.assertEqual(real, fake)

    def test_the_poll_never_returns_the_token_or_digest(self):
        # Kills: echoing the record verbatim, which would publish the
        # capability token to any unauthenticated poller.
        _, opened_ = self._post("/v1/approvals", {
            "verdict": {"verdict": "HOLD"}, "claim": CLAIM})
        _, polled = self._get("/v1/approvals/" + opened_["approval_id"])
        self.assertNotIn("approval_token", polled)
        self.assertNotIn("digest", polled)

    def test_the_routes_are_advertised_in_openapi(self):
        # Kills: shipping a feature no client can discover.
        #
        # NB: checked against /openapi.json, NOT the x402 descriptor. The first
        # version of this test asserted the descriptor and failed -- correctly.
        # `/.well-known/x402` deliberately advertises ONE resource (the verdict
        # itself); it is a summary for x402 clients, not the route table. The
        # test was wrong about where routes live, not the code.
        status, spec = self._get("/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("/v1/approvals", spec.get("paths", {}))
        self.assertIn("/v1/approvals/decide", spec.get("paths", {}))


class TestTheBindingHazardIsGuardedForStoresToo(unittest.TestCase):
    """`test_honeypot.TestHandlerBinding` asserts every `*_source` on `_Handler`
    is bound in `serve_forever`. The approvals STORE is bound the same way but
    is not named `*_source`, so that guard does not cover it -- and the hazard
    is identical: omit the dict entry, the attribute keeps its None default,
    every approval call 500s, and the route is still advertised.

    So the property is asserted over the attributes a handler METHOD actually
    reads off self, which needs no naming convention at all.
    """

    def test_every_handler_attribute_used_by_a_method_is_bound(self):
        # Kills: adding any future store or source and forgetting the seventh
        # edit, whatever it is called.
        import ast
        import inspect

        import blackwall
        tree = ast.parse(inspect.getsource(blackwall))
        declared, used, bound = set(), set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_Handler":
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for t in stmt.targets:
                            # Public names only. `_CORS` and friends are class
                            # CONSTANTS, not injected dependencies -- they are
                            # meant to keep their class-level value, and the
                            # first version of this test flagged `_CORS`
                            # correctly by its own rule and wrongly by intent.
                            if isinstance(t, ast.Name) and not t.id.startswith("_"):
                                declared.add(t.id)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and \
                            isinstance(sub.value, ast.Name) and \
                            sub.value.id == "self":
                        used.add(sub.attr)
            if isinstance(node, ast.FunctionDef) and node.name == "serve_forever":
                for d in ast.walk(node):
                    if isinstance(d, ast.Dict):
                        for k in d.keys:
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                bound.add(k.value)
        self.assertIn("approvals", declared, "the store default vanished")
        missing = (declared & used) - bound
        self.assertEqual(missing, set(),
                         "declared on _Handler and READ by a handler method but "
                         "never bound in serve_forever -- they stay None and the "
                         "feature is silently inert: %s" % sorted(missing))
