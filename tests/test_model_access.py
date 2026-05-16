import unittest

from app.services.token.model_access import (
    FREE_ACCESS,
    HEAVY_ACCESS,
    SUPER_ACCESS,
    required_access_for_model,
    token_supports_model_with_reason,
    token_text_access_state,
)
from app.services.token.models import TokenInfo


def _token(
    tier: str | None = None,
    *,
    pool_quota: dict | None = None,
) -> TokenInfo:
    token = TokenInfo(token="token-test")
    if tier is not None:
        token.real_tier = tier
    if pool_quota is not None:
        token.real_quota = pool_quota
    return token


class ModelAccessTests(unittest.TestCase):
    def test_required_access_for_text_models(self):
        self.assertEqual(required_access_for_model("grok-3-fast"), FREE_ACCESS)
        self.assertEqual(required_access_for_model("grok-auto"), SUPER_ACCESS)
        self.assertEqual(required_access_for_model("grok-4-expert"), SUPER_ACCESS)
        self.assertEqual(required_access_for_model("grok-4-heavy"), HEAVY_ACCESS)

    def test_token_text_access_state_maps_super_and_heavy_tiers(self):
        self.assertEqual(
            token_text_access_state(_token("SUBSCRIPTION_TIER_GROK_PRO")),
            SUPER_ACCESS,
        )
        self.assertEqual(
            token_text_access_state(_token("SUBSCRIPTION_TIER_SUPER_GROK_PRO")),
            HEAVY_ACCESS,
        )
        self.assertEqual(
            token_text_access_state(_token("SUBSCRIPTION_TIER_X_BASIC")),
            FREE_ACCESS,
        )

    def test_token_supports_model_unknown_real_tier_falls_back_to_pool(self):
        supported, reason = token_supports_model_with_reason(
            _token(),
            "grok-auto",
            pool_name="ssoSuper",
        )
        self.assertTrue(supported)
        self.assertEqual(reason, "unknown_real_tier_pool_fallback")

    def test_token_supports_model_denies_super_when_real_tier_is_free(self):
        supported, reason = token_supports_model_with_reason(
            _token("SUBSCRIPTION_TIER_X_BASIC"),
            "grok-auto",
            pool_name="ssoSuper",
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "real_tier_lacks_super")

    def test_token_supports_model_denies_heavy_when_pool_mismatch(self):
        supported, reason = token_supports_model_with_reason(
            _token("SUBSCRIPTION_TIER_SUPER_GROK_PRO"),
            "grok-4-heavy",
            pool_name="ssoSuper",
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "pool_mismatch_heavy")

    def test_token_supports_model_allows_heavy_when_real_tier_and_pool_match(self):
        supported, reason = token_supports_model_with_reason(
            _token("SUBSCRIPTION_TIER_SUPER_GROK_PRO"),
            "grok-4-heavy",
            pool_name="ssoHeavy",
        )
        self.assertTrue(supported)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
