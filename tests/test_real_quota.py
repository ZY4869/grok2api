import unittest

from app.services.token.real_quota import (
    _build_summary,
    _normalize_rate_limit,
    _normalize_subscription,
    _pick_real_tier,
)


class RealQuotaHelperTests(unittest.TestCase):
    def test_pick_real_tier_prefers_highest_active_subscription(self):
        subscriptions = [
            _normalize_subscription(
                {
                    "tier": "SUBSCRIPTION_TIER_X_BASIC",
                    "status": "SUBSCRIPTION_STATUS_ACTIVE",
                }
            ),
            _normalize_subscription(
                {
                    "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                    "status": "SUBSCRIPTION_STATUS_ACTIVE",
                    "stripe": {
                        "subscriptionType": "monthly",
                        "currentPeriodEnd": 1711111111,
                    },
                }
            ),
        ]

        tier, label = _pick_real_tier(subscriptions)

        self.assertEqual(tier, "SUBSCRIPTION_TIER_GROK_PRO")
        self.assertEqual(label, "SuperGrok")
        self.assertEqual(subscriptions[1]["subscription_type"], "monthly")
        self.assertEqual(subscriptions[1]["current_period_end"], 1711111111)

    def test_build_summary_uses_tokens_and_queries(self):
        summary = _build_summary(
            {
                "grok-3": _normalize_rate_limit(
                    {
                        "remainingTokens": 120,
                        "totalTokens": 1000,
                        "waitTimeSeconds": 0,
                    }
                ),
                "grok-4": _normalize_rate_limit(
                    {
                        "remainingQueries": 6,
                        "totalQueries": 10,
                        "waitTimeSeconds": 30,
                    }
                ),
            }
        )

        self.assertIn("grok-3: 120/1000", summary)
        self.assertIn("grok-4: 6/10", summary)


if __name__ == "__main__":
    unittest.main()
