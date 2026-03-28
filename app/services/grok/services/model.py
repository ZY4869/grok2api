"""
Grok 妯″瀷绠＄悊鏈嶅姟
"""

from enum import Enum
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.exceptions import ValidationException

BASIC_POOL_NAME = "ssoBasic"
SUPER_POOL_NAME = "ssoSuper"
HEAVY_POOL_NAME = "ssoHeavy"
HEAVY_MODEL_ID = "grok-4-heavy"


class Tier(str, Enum):
    """妯″瀷妗ｄ綅"""

    BASIC = "basic"
    SUPER = "super"


class Cost(str, Enum):
    """璁¤垂绫诲瀷"""

    LOW = "low"
    HIGH = "high"


class ModelInfo(BaseModel):
    """妯″瀷淇℃伅"""

    model_id: str
    grok_model: str
    model_mode: str
    tier: Tier = Field(default=Tier.BASIC)
    cost: Cost = Field(default=Cost.LOW)
    display_name: str
    description: str = ""
    is_image: bool = False
    is_image_edit: bool = False
    is_video: bool = False
    use_mode_id: bool = False


class ModelService:
    """妯″瀷绠＄悊鏈嶅姟"""

    MODELS = [
        ModelInfo(
            model_id="grok-auto",
            grok_model="",
            model_mode="auto",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="Grok Auto",
            description="Automatically chooses Fast or Expert (grok-3/grok-4)",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id="grok-3-fast",
            grok_model="grok-3",
            model_mode="fast",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="Grok 3 Fast",
            description="Quick responses (grok-3)",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id="grok-4-expert",
            grok_model="grok-4",
            model_mode="expert",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="Grok 4 Expert",
            description="Thinks hard (grok-4)",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id=HEAVY_MODEL_ID,
            grok_model="grok-4",
            model_mode="heavy",
            tier=Tier.SUPER,
            cost=Cost.HIGH,
            display_name="Grok 4 Heavy",
            description="SuperGrok Heavy (grok-4)",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0-fast",
            grok_model="grok-3",
            model_mode="MODEL_MODE_FAST",
            cost=Cost.HIGH,
            display_name="Grok Image Fast",
            description="Imagine waterfall image generation model for chat completions",
            is_image=True,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0",
            grok_model="grok-3",
            model_mode="MODEL_MODE_FAST",
            cost=Cost.HIGH,
            display_name="Grok Image",
            description="Image generation model",
            is_image=True,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0-edit",
            grok_model="imagine-image-edit",
            model_mode="MODEL_MODE_FAST",
            cost=Cost.HIGH,
            display_name="Grok Image Edit",
            description="Image edit model",
            is_image_edit=True,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0-video",
            grok_model="grok-3",
            model_mode="MODEL_MODE_FAST",
            cost=Cost.HIGH,
            display_name="Grok Video",
            description="Video generation model",
            is_video=True,
        ),
    ]

    _map = {m.model_id: m for m in MODELS}

    @classmethod
    def get(cls, model_id: str) -> Optional[ModelInfo]:
        """鑾峰彇妯″瀷淇℃伅"""
        return cls._map.get(model_id)

    @classmethod
    def list(cls) -> list[ModelInfo]:
        """鑾峰彇鎵€鏈夋ā鍨?"""
        return list(cls._map.values())

    @classmethod
    def valid(cls, model_id: str) -> bool:
        """妯″瀷鏄惁鏈夋晥"""
        return model_id in cls._map

    @classmethod
    def is_mode_id(cls, model_id: str) -> bool:
        """鏄惁涓哄揩鎹锋ā寮忥紙浣跨敤 modeId 鏂瑰紡璇锋眰锛?"""
        model = cls.get(model_id)
        return model.use_mode_id if model else False

    @classmethod
    def to_grok(cls, model_id: str) -> Tuple[str, str]:
        """杞崲涓?Grok 鍙傛暟"""
        model = cls.get(model_id)
        if not model:
            raise ValidationException(f"Invalid model ID: {model_id}")
        return model.grok_model, model.model_mode

    @classmethod
    def pool_for_model(cls, model_id: str) -> str:
        """鏍规嵁妯″瀷閫夋嫨 Token 姹?"""
        if model_id == HEAVY_MODEL_ID:
            return HEAVY_POOL_NAME
        model = cls.get(model_id)
        if model and model.tier == Tier.SUPER:
            return SUPER_POOL_NAME
        return BASIC_POOL_NAME

    @classmethod
    def pool_candidates_for_model(cls, model_id: str) -> List[str]:
        """鎸変紭鍏堢骇杩斿洖鍙敤 Token 姹犲垪琛?"""
        if model_id == HEAVY_MODEL_ID:
            return [HEAVY_POOL_NAME]
        model = cls.get(model_id)
        if model and model.is_video:
            return [SUPER_POOL_NAME, HEAVY_POOL_NAME]
        return [BASIC_POOL_NAME, SUPER_POOL_NAME, HEAVY_POOL_NAME]


__all__ = [
    "BASIC_POOL_NAME",
    "SUPER_POOL_NAME",
    "HEAVY_POOL_NAME",
    "HEAVY_MODEL_ID",
    "ModelService",
]
