import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import quantity


class PoissonCdfTest(unittest.TestCase):
    def test_k_below_zero_is_zero(self):
        self.assertEqual(quantity.poisson_cdf(-1, 5.0), 0.0)
        self.assertEqual(quantity.poisson_cdf(-1, 0.0), 0.0)

    def test_lambda_zero_cdf_is_one_for_k_ge_zero(self):
        self.assertEqual(quantity.poisson_cdf(0, 0.0), 1.0)
        self.assertEqual(quantity.poisson_cdf(7, 0.0), 1.0)

    def test_known_values(self):
        self.assertEqual(quantity.poisson_cdf(0, 3.0), math.exp(-3.0))
        self.assertAlmostEqual(quantity.poisson_cdf(4, 10.0),
                               0.029252688076961075, places=12)
        self.assertAlmostEqual(quantity.poisson_cdf(10, 10.0),
                               0.5830397501929855, places=12)

    def test_monotone_and_bounded(self):
        prev = -1.0
        for k in range(0, 25):
            v = quantity.poisson_cdf(k, 5.0)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
            self.assertGreaterEqual(v, prev)
            prev = v

    def test_large_k_is_one(self):
        self.assertAlmostEqual(quantity.poisson_cdf(500, 10.0), 1.0,
                               places=12)

    def test_negative_lambda_raises(self):
        with self.assertRaises(ValueError):
            quantity.poisson_cdf(1, -1.0)

    def test_approx_path_sane_above_700(self):
        # lam > 700 uses the normal approximation; stays in [0, 1] and sane
        v = quantity.poisson_cdf(10000, 10000.0)
        self.assertGreater(v, 0.4)
        self.assertLess(v, 0.6)
        self.assertLessEqual(quantity.poisson_cdf(650, 701.0),
                             quantity.poisson_cdf(750, 701.0))


class PoissonSfTest(unittest.TestCase):
    def test_sf_is_one_minus_cdf_k_minus_1(self):
        for k, lam in ((5, 10.0), (1, 3.0), (0, 3.0), (20, 4.5)):
            self.assertAlmostEqual(quantity.poisson_sf(k, lam),
                                   1.0 - quantity.poisson_cdf(k - 1, lam),
                                   places=12)

    def test_sf_at_zero_and_below_is_one(self):
        self.assertEqual(quantity.poisson_sf(0, 3.0), 1.0)
        self.assertEqual(quantity.poisson_sf(-3, 3.0), 1.0)

    def test_known_value(self):
        self.assertAlmostEqual(quantity.poisson_sf(5, 10.0),
                               0.9707473119230389, places=12)


class MaxPoissonTotalTest(unittest.TestCase):
    def test_pinned_values(self):
        cases = [
            # (lam, conf, expected)  — largest m with P(X >= m) >= conf
            (0.0, 0.95, 0),
            (0.05, 0.95, 0),
            (2.9, 0.95, 0),
            (3.0, 0.95, 1),
            (10.0, 0.95, 5),
            (100.0, 0.95, 84),
            (2.3, 0.90, 0),
            (2.31, 0.90, 1),
            (4.6, 0.99, 0),
            (4.61, 0.99, 1),
        ]
        for lam, conf, want in cases:
            with self.subTest(lam=lam, conf=conf):
                self.assertEqual(quantity.max_poisson_total(lam, conf), want)

    def test_confidence_ranges_at_lambda_100(self):
        self.assertIn(quantity.max_poisson_total(100.0, 0.99),
                      range(74, 81))
        self.assertEqual(quantity.max_poisson_total(100.0, 0.95), 84)
        self.assertIn(quantity.max_poisson_total(100.0, 0.90),
                      range(84, 91))
        self.assertLessEqual(quantity.max_poisson_total(100.0, 0.99),
                             quantity.max_poisson_total(100.0, 0.95))
        self.assertLessEqual(quantity.max_poisson_total(100.0, 0.95),
                             quantity.max_poisson_total(100.0, 0.90))

    def test_lambda_monotone(self):
        prev = -1
        for lam in (1.0, 2.5, 3.0, 5.0, 10.0, 25.5, 100.0, 250.0):
            v = quantity.max_poisson_total(lam, 0.95)
            self.assertGreaterEqual(v, prev)
            prev = v

    def test_approx_path_range_above_700(self):
        self.assertIn(quantity.max_poisson_total(1000.0, 0.95),
                      range(945, 953))
        self.assertIn(quantity.max_poisson_total(10000.0, 0.95),
                      range(9800, 10060))

    def test_invalid_args_raise(self):
        with self.assertRaises(ValueError):
            quantity.max_poisson_total(-1.0, 0.95)
        with self.assertRaises(ValueError):
            quantity.max_poisson_total(10.0, 0.0)
        with self.assertRaises(ValueError):
            quantity.max_poisson_total(10.0, 1.0)
        with self.assertRaises(ValueError):
            quantity.max_poisson_total(10.0, 1.5)


class MaxPostQuantityTest(unittest.TestCase):
    def test_pinned_values(self):
        self.assertEqual(quantity.max_post_quantity(3.0, 0, 0.95), 1)
        self.assertEqual(quantity.max_post_quantity(3.0, 1, 0.95), 0)
        self.assertEqual(quantity.max_post_quantity(10.0, 4, 0.95), 1)
        self.assertEqual(quantity.max_post_quantity(10.0, 5, 0.95), 0)
        self.assertEqual(quantity.max_post_quantity(0.0, 0, 0.95), 0)

    def test_ahead_beyond_total_never_goes_negative(self):
        self.assertEqual(quantity.max_post_quantity(10.0, 999, 0.95), 0)

    def test_negative_ahead_clamped(self):
        self.assertEqual(quantity.max_post_quantity(3.0, -5, 0.95),
                         quantity.max_post_quantity(3.0, 0, 0.95))


class AchievedConfidenceTest(unittest.TestCase):
    def test_known_value(self):
        self.assertAlmostEqual(quantity.achieved_confidence(10.0, 0, 5),
                               0.9707473119230389, places=12)
        self.assertAlmostEqual(quantity.achieved_confidence(10.0, 4, 1),
                               0.9707473119230389, places=12)

    def test_zero_quantity_zero_ahead_is_one(self):
        self.assertEqual(quantity.achieved_confidence(10.0, 0, 0), 1.0)

    def test_zero_lambda_guarantees_nothing(self):
        self.assertEqual(quantity.achieved_confidence(0.0, 0, 1), 0.0)

    def test_negative_args_clamp_to_zero(self):
        self.assertEqual(quantity.achieved_confidence(10.0, -3, -2), 1.0)

    def test_max_post_is_at_confidence_and_next_is_below(self):
        for lam, ahead, conf in ((10.0, 0, 0.95), (100.0, 40, 0.95),
                                 (5.0, 0, 0.90), (100.0, 0, 0.99),
                                 (1000.0, 900, 0.95)):
            with self.subTest(lam=lam, ahead=ahead, conf=conf):
                q = quantity.max_post_quantity(lam, ahead, conf)
                got = quantity.achieved_confidence(lam, ahead, q)
                self.assertGreaterEqual(got, conf - 1e-9)
                if q > 0 or ahead > 0:
                    below = quantity.achieved_confidence(lam, ahead, q + 1)
                    self.assertLess(below, conf)


class PriceStretchTest(unittest.TestCase):
    def test_at_or_below_market_is_full_demand(self):
        self.assertEqual(quantity.price_stretch(50000, 50000), 1.0)
        self.assertEqual(quantity.price_stretch(40000, 50000), 1.0)

    def test_above_market_scales_down(self):
        self.assertEqual(quantity.price_stretch(100000, 50000), 0.5)
        self.assertAlmostEqual(quantity.price_stretch(150000, 100000),
                               2.0 / 3.0, places=6)

    def test_clamped_to_tenth(self):
        self.assertEqual(quantity.price_stretch(500000, 50000), 0.1)
        self.assertEqual(quantity.price_stretch(10 ** 12, 50000), 0.1)

    def test_missing_market_value_is_full_demand(self):
        self.assertEqual(quantity.price_stretch(500000, None), 1.0)
        self.assertEqual(quantity.price_stretch(500000, 0), 1.0)
        self.assertEqual(quantity.price_stretch(500000, -5), 1.0)


class ExpectedDaysTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(quantity.expected_days(50, 100.0), 0.5)
        self.assertEqual(quantity.expected_days(0, 100.0), 0.0)
        self.assertAlmostEqual(quantity.expected_days(7, 3.0), 7.0 / 3.0,
                               places=12)

    def test_zero_or_negative_rate_is_infinite(self):
        self.assertTrue(math.isinf(quantity.expected_days(10, 0.0)))
        self.assertTrue(math.isinf(quantity.expected_days(10, -5.0)))


class FlowTest(unittest.TestCase):
    def test_realistic_commodity_flow(self):
        # TSM region-wide sold/day 850 (region CSV is already a whole-region
        # total - no realm scaling), price under market, wall ahead 700
        lam_day, ahead = 850.0, 700
        stretch = quantity.price_stretch(45000, 50000)
        lam_eff = lam_day * stretch
        q = quantity.max_post_quantity(lam_eff, ahead, 0.95)
        self.assertGreater(q, 0)
        self.assertGreaterEqual(
            quantity.achieved_confidence(lam_eff, ahead, q), 0.95 - 1e-9)
        self.assertLess(quantity.achieved_confidence(lam_eff, ahead, q + 1),
                        0.95)
        self.assertLess(quantity.expected_days(ahead + q, lam_eff), 1.0)


class DaysForConfidenceTest(unittest.TestCase):
    def test_single_unit_closed_form(self):
        # P(X >= 1) = 1 - exp(-lam*d) >= conf  ->  d = -ln(1 - conf) / lam
        self.assertAlmostEqual(
            quantity.days_for_confidence(0.1, 0, 1, 0.95),
            math.log(20.0) / 0.1, delta=0.02)
        self.assertAlmostEqual(
            quantity.days_for_confidence(0.1, 0, 1, 0.90),
            math.log(10.0) / 0.1, delta=0.02)

    def test_definition_round_trip(self):
        # smallest d where achieved(lam*d, ahead, qty) >= conf,
        # and a slightly smaller d falls below conf
        for lam, ahead, qty, conf in ((10.0, 0, 5, 0.95), (3.0, 2, 3, 0.95),
                                      (250.0, 10, 30, 0.99)):
            with self.subTest(lam=lam, ahead=ahead, qty=qty, conf=conf):
                d = quantity.days_for_confidence(lam, ahead, qty, conf)
                self.assertGreaterEqual(
                    quantity.achieved_confidence(lam * d, ahead, qty),
                    conf - 1e-9)
                self.assertLess(
                    quantity.achieved_confidence(lam * (d - 0.05), ahead,
                                                 qty), conf)

    def test_zero_lambda_is_infinite(self):
        self.assertTrue(math.isinf(
            quantity.days_for_confidence(0.0, 0, 1, 0.95)))

    def test_zero_qty_is_zero_days(self):
        self.assertEqual(quantity.days_for_confidence(10.0, 0, 0, 0.95), 0.0)

    def test_more_units_or_ahead_never_fewer_days(self):
        base = quantity.days_for_confidence(10.0, 0, 1, 0.95)
        self.assertGreater(quantity.days_for_confidence(10.0, 0, 4, 0.95),
                           base)
        self.assertGreater(quantity.days_for_confidence(10.0, 3, 1, 0.95),
                           base)

    def test_hot_item_sells_same_day(self):
        # region-wide 305k/day: 500 units are 95% sold essentially at once
        d = quantity.days_for_confidence(305079.5, 0, 500, 0.95)
        self.assertLess(d, 0.01)

    def test_invalid_args_raise(self):
        with self.assertRaises(ValueError):
            quantity.days_for_confidence(-1.0, 0, 1, 0.95)
        with self.assertRaises(ValueError):
            quantity.days_for_confidence(10.0, 0, 1, 0.0)
        with self.assertRaises(ValueError):
            quantity.days_for_confidence(10.0, 0, 1, 1.5)


if __name__ == "__main__":
    unittest.main()
