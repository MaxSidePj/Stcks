import unittest
import market

HIST = {"closes": [200.13, 181.60, 186.64], "dates": ["2026-11-04", "2026-11-05", "2026-11-06"]}


class Reconcile(unittest.TestCase):
    def test_lagging_screener_uses_daily_closes(self):
        t = market.technicals(HIST, 182.29, -0.08914)
        self.assertAlmostEqual(t["day_pct"], 186.64 / 181.60 - 1)
        self.assertEqual(t["price"], 186.64)
        self.assertEqual(t["as_of"], "2026-11-06")
        self.assertNotIn("drop_2026-11-07", market.price_flags(t, "", "2026-11-07"))

    def test_newer_live_screener_is_kept(self):
        t = market.technicals(HIST, 170.0, 170.0 / 186.64 - 1)
        self.assertAlmostEqual(t["price"], 170.0)
        self.assertIsNone(t["as_of"])
        self.assertIn("drop_2026-11-09", market.price_flags(t, "", "2026-11-09"))

    def test_real_drop_alerts_once_per_session_with_its_date(self):
        h = {"closes": [200.13, 181.60], "dates": ["2026-11-04", "2026-11-05"]}
        sat = market.price_flags(market.technicals(h, 181.60, None), "", "2026-11-07")
        sun = market.price_flags(market.technicals(h, 181.60, None), "", "2026-11-08")
        self.assertEqual(list(k for k in sat if k.startswith("drop")), ["drop_2026-11-05"])
        self.assertEqual(sat.keys(), sun.keys())
        self.assertIn("on Thu 05 Nov", sat["drop_2026-11-05"][1])


if __name__ == "__main__":
    unittest.main()
