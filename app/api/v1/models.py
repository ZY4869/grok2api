"""
Models API 路由
"""

from fastapi import APIRouter

from app.services.grok.services.model import ModelService
from app.services.token import get_token_manager
from app.services.token.model_access import (
    FREE_TEXT_MODELS,
    HEAVY_TEXT_MODELS,
    SUPER_TEXT_MODELS,
)


router = APIRouter(tags=["Models"])


@router.get("/models")
async def list_models():
    """OpenAI 兼容 models 列表接口"""
    token_mgr = await get_token_manager()
    await token_mgr.reload_if_stale()

    data = [
        {
            "id": m.model_id,
            "display_name": m.display_name,
            "object": "model",
            "created": 0,
            "owned_by": "grok2api@ZY4869",
        }
        for m in ModelService.list()
        if (
            m.model_id not in FREE_TEXT_MODELS
            and m.model_id not in SUPER_TEXT_MODELS
            and m.model_id not in HEAVY_TEXT_MODELS
        )
        or token_mgr.has_available_token_for_model(m.model_id)
    ]
    return {"object": "list", "data": data}


__all__ = ["router"]
