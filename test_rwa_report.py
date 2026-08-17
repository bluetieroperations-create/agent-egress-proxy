#!/usr/bin/env python3
"""
test_rwa_report.py -- TDD for the corpus intelligence report. Each test names the
MUTATION it kills.
"""
import unittest

from rwa_ledger import build_outcome_event
from rwa_report import build_report

EVM_A = "0x" + "aa" * 20
EVM_B = "0x" + "bb" * 20


def _buy(rid, token=EVM_A, chain="base", issuer="backed", verdict="GO",
         peg=1.0, grade=None, symbol="AAPLx"):
    return {"event": "buy", "receipt_id": rid, "ts": 1, "token": token, "chain": chain,
            "issuer": issuer, "symbol": symbol, "underlying_symbol": "AAPL",
            "underlying_price": 100.0, "paid_unit_price": 100.0 * peg, "peg_ratio": peg,
            "verdict": verdict, "restriction_grade": grade, "trading_halted": False}


def _out(rid, token=EVM_A, chain="base", issuer="backed", underwater=False,
         settled=True, mark=1.0):
    be = _buy(rid, token=token, chain=chain, issuer=issuer)
    return build_outcome_event(be, {"mark_ratio": mark, "underwater": underwater,
                                    "holds_balance": settled}, now=2)


class TestBuildReport(unittest.TestCase):
    def test_totals_and_verdict_mix(self):
        events = [_buy("r1"), _buy("r2", verdict="HOLD"), _out("r1")]
        r = build_report(events)
        self.assertEqual(r["totals"], {"buys": 2, "outcomes": 1,
                                       "distinct_assets": 1, "distinct_issuers": 1})
        self.assertEqual(r["verdict_mix"], {"GO": 1, "HOLD": 1})

    def test_restriction_posture(self):
        r = build_report([_buy("r1", grade="blocked"), _buy("r2", grade="blocked"),
                          _buy("r3", grade="ready")])
        self.assertEqual(r["restriction_posture"], {"blocked": 2, "ready": 1})

    def test_overpriced_leaderboard_sorted_desc(self):
        # MUTATION: ascending sort (or no sort) would bury the worst depeg.
        events = [_buy("r1", token=EVM_A, peg=1.05, symbol="AAPLx"),
                  _buy("r2", token=EVM_B, peg=1.80, symbol="TSLAx")]
        top = build_report(events)["most_overpriced"]
        self.assertEqual(top[0]["symbol"], "TSLAx")
        self.assertAlmostEqual(top[0]["max_peg_ratio"], 1.80)

    def test_underwater_leaderboard(self):
        events = [_buy("r1", token=EVM_A), _out("r1", token=EVM_A, underwater=True),
                  _buy("r2", token=EVM_B), _out("r2", token=EVM_B, underwater=False)]
        top = build_report(events)["most_underwater"]
        self.assertEqual(top[0]["token"], EVM_A)
        self.assertEqual(top[0]["underwater_rate"], 1.0)

    def test_issuer_directory_ranks_best_first(self):
        # 'good' issuer: 6 clean labeled outcomes -> high; 'bad': 6 underwater/unsettled -> low.
        events = []
        for i in range(6):
            events += [_buy("g%d" % i, token=EVM_A, issuer="good"),
                       _out("g%d" % i, token=EVM_A, issuer="good",
                            underwater=False, settled=True)]
            events += [_buy("b%d" % i, token=EVM_B, issuer="bad", verdict="STOP"),
                       _out("b%d" % i, token=EVM_B, issuer="bad",
                            underwater=True, settled=False)]
        directory = build_report(events)["issuer_directory"]
        self.assertEqual(directory[0]["issuer"], "good")
        self.assertEqual(directory[0]["trust"], "high")
        self.assertEqual(directory[-1]["issuer"], "bad")
        self.assertEqual(directory[-1]["trust"], "low")

    def test_empty_corpus_no_crash(self):
        r = build_report([])
        self.assertEqual(r["totals"]["buys"], 0)
        self.assertEqual(r["issuer_directory"], [])

    def test_ignores_non_dict_events(self):
        r = build_report([_buy("r1"), "junk", None, 42])
        self.assertEqual(r["totals"]["buys"], 1)


if __name__ == "__main__":
    unittest.main()
