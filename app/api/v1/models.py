"""
Models API 路由
"""

from fastapi import APIRouter

from app.services.grok.services.model import ModelService
from app.services.token import get_token_manager
from app.services.token.model_access import HEAVY_MODEL_ID


router = APIRouter(tags=["Models"])


@router.get("/models")
async def list_models():
    """OpenAI 兼容 models 列表接口"""
    token_mgr = await get_token_manager()
    await token_mgr.reload_if_stale()

    data = [
        {
            "id": m.model_id,
            "object": "model",
            "created": 0,
            "owned_by": "grok2api@ZY4869",
        }
        for m in ModelService.list()
        if m.model_id != HEAVY_MODEL_ID
        or token_mgr.has_available_token_for_model(m.model_id)
    ]
    return {"object": "list", "data": data}


__all__ = ["router"]
