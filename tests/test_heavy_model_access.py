import unittest
from unittest.mock import AsyncMock, patch

from app.api.v1 import models as models_api
from app.core.exceptions import AppException, ErrorType
from app.services.grok.services.chat import ChatService
from app.services.grok.services.model import HEAVY_POOL_NAME
from app.services.grok.services.responses import ResponsesService
from app.services.token.manager import TokenManager
from app.services.token.model_access import (
    HEAVY_ACCESS_ERROR_CODE,
    HEAVY_MODEL_ID,
)
from app.services.token.models import TokenInfo
from app.services.token.pool import TokenPool


def _token(token: str, *, quota: int = 10) -> TokenInfo:
    return TokenInfo(token=token, quota=quota)


def _build_manager(*, super_tokens=None, basic_tokens=None, heavy_tokens=None) -> TokenManager:
    mgr = TokenManager()
    mgr.initialized = True
    mgr.pools = {}

    basic_pool = TokenPool("ssoBasic")
    for info in basic_tokens or []:
        basic_pool.add(info)
    mgr.pools["ssoBasic"] = basic_pool

    super_pool = TokenPool("ssoSuper")
    for info in super_tokens or []:
        super_pool.add(info)
    mgr.pools["ssoSuper"] = super_pool

    heavy_pool = TokenPool(HEAVY_POOL_NAME)
    for info in heavy_tokens or []:
        heavy_pool.add(info)
    mgr.pools[HEAVY_POOL_NAME] = heavy_pool
    return mgr


class _DeniedManager:
    async def reload_if_stale(self):
        return None

    def has_entitled_token_for_model(self, model_id: str) -> bool:
        return False

    def model_access_denial_reason(self, model_id: str) -> str:
        return "no_heavy_pool_tokens"


class TokenManagerHeavyModelTests(unittest.TestCase):
    def test_has_entitled_token_for_heavy_requires_manual_heavy_pool(self):
        mgr = _build_manager(
            super_tokens=[_token("token-super")],
            heavy_tokens=[_token("token-heavy")],
        )

        self.assertTrue(mgr.has_entitled_token_for_model(HEAVY_MODEL_ID))

        mgr_without_heavy = _build_manager(
            super_tokens=[_token("token-super")]
        )
        self.assertFalse(mgr_without_heavy.has_entitled_token_for_model(HEAVY_MODEL_ID))
        self.assertEqual(
            mgr_without_heavy.model_access_denial_reason(HEAVY_MODEL_ID),
            "no_heavy_pool_tokens",
        )

    def test_has_available_token_for_heavy_ignores_local_quota_field(self):
        mgr = _build_manager(
            heavy_tokens=[_token("token-heavy", quota=0)]
        )

        self.assertTrue(mgr.has_entitled_token_for_model(HEAVY_MODEL_ID))
        self.assertTrue(mgr.has_available_token_for_model(HEAVY_MODEL_ID))


class ModelsRouteHeavyFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_models_hides_heavy_without_available_heavy_token(self):
        mgr = _build_manager(
            super_tokens=[_token("token-super")]
        )
        mgr.reload_if_stale = AsyncMock(return_value=None)

        with patch(
            "app.api.v1.models.get_token_manager",
            new=AsyncMock(return_value=mgr),
        ):
            payload = await models_api.list_models()

        ids = [item["id"] for item in payload["data"]]
        self.assertNotIn(HEAVY_MODEL_ID, ids)

    async def test_list_models_shows_heavy_with_available_heavy_token(self):
        mgr = _build_manager(
            heavy_tokens=[_token("token-heavy")]
        )
        mgr.reload_if_stale = AsyncMock(return_value=None)

        with patch(
            "app.api.v1.models.get_token_manager",
            new=AsyncMock(return_value=mgr),
        ):
            payload = await models_api.list_models()

        ids = [item["id"] for item in payload["data"]]
        self.assertIn(HEAVY_MODEL_ID, ids)


class HeavyModelRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_service_denies_heavy_without_entitled_token(self):
        mgr = _DeniedManager()

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=mgr),
        ):
            with self.assertRaises(AppException) as context:
                await ChatService.completions(
                    model=HEAVY_MODEL_ID,
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                )

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.error_type, ErrorType.PERMISSION.value)
        self.assertEqual(context.exception.code, HEAVY_ACCESS_ERROR_CODE)

    async def test_responses_service_denies_heavy_without_entitled_token(self):
        mgr = _DeniedManager()

        with patch(
            "app.services.grok.services.chat.get_token_manager",
            new=AsyncMock(return_value=mgr),
        ):
            with self.assertRaises(AppException) as context:
                await ResponsesService.create(
                    model=HEAVY_MODEL_ID,
                    input_value="hello",
                    stream=False,
                )

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.code, HEAVY_ACCESS_ERROR_CODE)


if __name__ == "__main__":
    unittest.main()
