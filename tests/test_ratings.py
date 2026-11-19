# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT
#  >>> FILE PATH:   tests/test_ratings.py
#  z194 - the rating model's rules (python -m unittest discover -s tests)
# ============================================================
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import rating_model as rm   # noqa: E402


class Scale(unittest.TestCase):
    def test_letters_and_notches(self):
        for k, r in enumerate(rm.SCALE, 1):
            self.assertEqual(rm.notch(r), k)
            self.assertEqual(rm.letter(k), r)
        self.assertEqual(rm.notch("Aa3"), 4)
        self.assertEqual(rm.notch("Baa3"), 10)
        self.assertEqual(rm.notch("BBB (high)"), 9)
        self.assertEqual(rm.notch("SD"), 22)
        self.assertEqual(rm.notch("A+ positive outlook"), 5)
        self.assertIsNone(rm.notch("N/A"))
        self.assertEqual(rm.letter(None), "NR")
        self.assertEqual(rm.letter(6.49), "A")
        self.assertEqual(rm.letter(6.5), "A-")

    def test_buckets(self):
        self.assertEqual(rm.bucket(1), "AAA")
        self.assertEqual(rm.bucket(10), "BBB")
        self.assertEqual(rm.bucket(11), "BB")
        self.assertEqual(rm.bucket(17), "CCC")
        self.assertEqual(rm.bucket(22), "D")

    def test_warf_round_trip_and_convexity(self):
        for n in range(1, 20):
            self.assertAlmostEqual(rm.notch_of_warf(rm.warf_of(n)), n, places=6)
        # half AAA, half B: the default-weighted rating is far worse than the linear midpoint (BBB-)
        lt = rm.look_through([("X", 1.0), ("Y", 1.0)], {"X": {"n": 1}, "Y": {"n": 15}})
        self.assertGreater(lt["n"], lt["avg"] + 2)
        self.assertEqual(lt["avg"], 8.0)


class Market(unittest.TestCase):
    def test_anchors_monotone_and_lookup(self):
        a = rm.spread_anchors({"AAA": 47, "AA": 67, "A": 76, "BBB": 115, "BB": 204, "B": 368, "CCC": 1329})
        self.assertEqual(len(a), 21)
        self.assertTrue(all(y > x for x, y in zip(a, a[1:])))
        prev = 0
        for bp in (5, 20, 40, 60, 90, 150, 300, 800, 2000, 9000):
            n = rm.cds_notch(bp, a)
            self.assertGreaterEqual(n, prev)
            prev = n
        self.assertEqual(rm.cds_notch(1, a), 1.0)
        self.assertIsNone(rm.cds_notch(None, a))
        self.assertEqual(len(rm.spread_anchors(None)), 21)          # FRED missing: fallback buckets

    def test_percent_quoted_prints(self):
        self.assertEqual(rm.norm_bps(0.6), 60.0)
        self.assertEqual(rm.norm_bps(1.47), 147.0)
        self.assertEqual(rm.norm_bps(69), 69)
        self.assertIsNone(rm.norm_bps(0))
        self.assertIsNone(rm.norm_bps("x"))

    def test_liquidity(self):
        self.assertEqual(rm.cds_liquidity(10, "2026-11-17", "2026-11-18"), 1.0)
        self.assertEqual(rm.cds_liquidity(4, "2026-11-17", "2026-11-18"), 0.5)
        self.assertEqual(rm.cds_liquidity(40, "2026-09-01", "2026-11-18"), 0.0)
        self.assertEqual(rm.cds_liquidity(10, None, "2026-11-18"), 0.0)


class Structural(unittest.TestCase):
    def test_distance_to_default(self):
        base = rm.merton_dd(100, 50, 0.30)
        self.assertGreater(base, rm.merton_dd(100, 80, 0.30))      # more debt, closer to default
        self.assertGreater(base, rm.merton_dd(100, 50, 0.60))      # more volatile, closer to default
        self.assertIsNone(rm.merton_dd(0, 50, 0.3))
        self.assertIsNone(rm.merton_dd(100, 50, None))

    def test_dd_map(self):
        self.assertEqual(rm.dd_notch(-1), 21.0)
        self.assertEqual(rm.dd_notch(1000), rm.DD_BEST)            # leverage alone never says AAA
        self.assertGreater(rm.dd_notch(1), rm.dd_notch(4))
        self.assertAlmostEqual(rm.dd_notch(4), 13.8 - 3.4 * math.log(4), places=6)

    def test_default_point(self):
        self.assertEqual(rm.default_point(100), 60.0)
        self.assertEqual(rm.default_point(100, 80), 60.0)          # 20 short-term + half of 80
        self.assertEqual(rm.default_point(100, 0), 100.0)
        self.assertIsNone(rm.default_point(0))


class Fundamentals(unittest.TestCase):
    STRONG = {"sector": "Consumer Defensive", "industry": "Household & Personal Products", "revenue": 80e9, "ebitda": 22e9,
              "debt": 30e9, "cash": 8e9, "cfo": 18e9, "capex": 3e9, "coupon": 3.0, "currentRatio": 1.0}
    WEAK = {"sector": "Consumer Cyclical", "industry": "Department Stores", "revenue": 4e9, "ebitda": 0.4e9,
            "debt": 3e9, "cash": 0.2e9, "fcf": -0.1e9, "coupon": 8.0, "currentRatio": 0.7}

    def test_strong_beats_weak(self):
        n_s, d_s = rm.corporate_scorecard(self.STRONG)
        n_w, d_w = rm.corporate_scorecard(self.WEAK)
        self.assertLess(n_s, 8)                                      # A range or better
        self.assertGreater(n_w, 12)                                  # high yield
        self.assertIn("liquidity", d_w)

    def test_cash_burn(self):
        n, d = rm.corporate_scorecard({"sector": "Healthcare", "industry": "Biotechnology", "revenue": 1e7, "ebitda": -2e8,
                                       "debt": 0, "cash": 3e8, "fcf": -2e8})
        self.assertEqual(d["rule"], "cash burn")
        self.assertEqual(n, 17.0)                                    # 1.5 years of runway: CCC+

    def test_captive_finance(self):
        oem = {"sector": "Consumer Cyclical", "industry": "Auto Manufacturers", "revenue": 180e9, "ebitda": 15e9, "debt": 130e9,
               "cash": 25e9, "fcf": 5e9, "coupon": 5.0}
        n_cap, d = rm.corporate_scorecard(oem)
        self.assertIn("captive", d)
        n_plain, _ = rm.corporate_scorecard(dict(oem, industry="Specialty Retail"))
        self.assertLess(n_cap, n_plain)

    def test_regulated_utility_table(self):
        u = {"sector": "Utilities", "industry": "Utilities - Regulated Electric", "revenue": 30e9, "ebitda": 15e9,
             "debt": 85e9, "cash": 0.5e9, "fcf": -6e9, "coupon": 4.3, "currentRatio": 0.7}
        n_u, d = rm.corporate_scorecard(u)
        self.assertEqual(d["table"], "low")
        self.assertNotIn("focfToDebtPct", d)                         # capex-funded rate base: FOCF not judged
        self.assertNotIn("liquidity", d)
        self.assertLess(n_u, 10)                                     # investment grade

    def test_banks_and_insurers(self):
        thin = rm.bank_scorecard({"assets": 500e9, "equity": 20e9, "roa": 0.002})[0]
        thick = rm.bank_scorecard({"assets": 500e9, "equity": 60e9, "roa": 0.012})[0]
        self.assertLess(thick, thin)
        self.assertGreaterEqual(rm.bank_scorecard({"assets": 500e9, "equity": 60e9, "roa": 0.012}, sov_n=12)[0], 11)
        self.assertEqual(rm.method_of("Financial Services", "Banks - Regional"), "bank")
        self.assertEqual(rm.method_of("Financial Services", "Insurance - Life"), "insurer")
        self.assertEqual(rm.method_of("Financial Services", "Insurance Brokers"), "corporate")
        self.assertEqual(rm.method_of("Financial Services", "Credit Services", 500e9, 50e9), "bank")
        self.assertEqual(rm.method_of("Financial Services", "Credit Services", 90e9, 35e9), "corporate")
        self.assertEqual(rm.method_of("Financial Services", "Shell Companies"), "shell")


class Composite(unittest.TestCase):
    def test_weights_and_agency(self):
        n, w, fl = rm.composite(8.0, 1.0, 6.0, 1.0, 7.0, 1.0)
        self.assertAlmostEqual(sum(w.values()), 1.0, places=2)
        self.assertTrue(6.0 < n < 8.0)
        n, w, fl = rm.composite(8.0, 1.0, 6.0, 1.0, 7.0, 1.0, agency=3)
        self.assertEqual((n, fl), (3.0, ["agency"]))
        self.assertEqual(rm.composite(8.0, 1.0)[0], None)            # a CDS print alone can't rate a company
        _, _, fl = rm.composite(18.0, 0.3, 6.0, 1.0, 7.0, 1.0)
        self.assertIn("cds-outlier", fl)

    def test_hysteresis(self):
        self.assertEqual(rm.publish(7.6, 8), 8)
        self.assertEqual(rm.publish(7.2, 8), 7)
        self.assertEqual(rm.publish(7.4, None), 7)
        self.assertIsNone(rm.publish(None, 8))


class TrendAndSignal(unittest.TestCase):
    def test_trend_signs(self):
        self.assertLess(rm.trend_score(cds_rel_1m=0.3, cds_liq=1.0), -0.9)     # 30 % wider: deteriorating
        self.assertGreater(rm.trend_score(cds_rel_1m=-0.15, cds_liq=1.0), 0.4)
        self.assertLess(rm.trend_score(mig30=2), 0)                            # downgraded two notches
        self.assertLess(rm.trend_score(iv_rel=0.2), 0)
        self.assertIsNone(rm.trend_score())

    def test_level_alone_never_triggers(self):
        for n in (1, 22):
            self.assertLess(abs(rm.credit_signal(n, None)), 0.33)
        self.assertLess(rm.credit_signal(9, -1.0), -0.6)
        self.assertEqual(rm.outlook(-0.3), "negative")
        self.assertEqual(rm.outlook(0.0, 2), "watch-neg")

    def test_lookthrough_coverage(self):
        self.assertIsNone(rm.look_through([("A", 1), ("B", 3)], {"A": {"n": 5}})["n"])   # 25 % rated
        lt = rm.look_through([("A", 1), ("B", 1)], {"A": {"n": 5, "tr": -0.5}, "B": {"n": 9, "tr": None}})
        self.assertEqual(lt["coverage"], 1.0)
        self.assertEqual(lt["tr"], -0.5)
        self.assertEqual(lt["ig"], 1.0)


class Sovereign(unittest.TestCase):
    def test_fx_and_blend(self):
        self.assertEqual(rm.fx_legs("EURUSD=X"), ("EUR", "USD"))
        self.assertEqual(rm.fx_legs("JPY=X"), ("USD", "JPY"))
        self.assertIsNone(rm.fx_legs("AAPL"))
        n, cov = rm.sovereign_blend({"A": 1, "B": 2}, {"A": 1, "B": None})
        self.assertIsNone(n)                                         # a third of the weight rated: no blend
        self.assertAlmostEqual(cov, 1 / 3)
        n, cov = rm.sovereign_blend({"A": 3, "B": 1}, {"A": 1, "B": 12})
        self.assertTrue(1 < n < 12)


if __name__ == "__main__":
    unittest.main()
