"""
Grok 模型管理服务
"""

from enum import Enum
from typing import Optional, Tuple, List
from pydantic import BaseModel, Field

from app.core.exceptions import ValidationException


class Tier(str, Enum):
    """模型档位"""

    BASIC = "basic"
    SUPER = "super"


class Cost(str, Enum):
    """计费类型"""

    LOW = "low"
    HIGH = "high"


class ModelInfo(BaseModel):
    """模型信息"""

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
    """模型管理服务"""

    MODELS = [
        # ── 快捷模式（与 Grok 网页一致，使用 modeId 方式请求）──
        ModelInfo(
            model_id="auto",
            grok_model="",
            model_mode="auto",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="Auto",
            description="Automatically chooses Fast or Expert",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id="fast",
            grok_model="grok-3",
            model_mode="fast",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="Fast",
            description="Quick responses - Grok 4.20",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id="expert",
            grok_model="grok-4",
            model_mode="expert",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="Expert",
            description="Thinks hard - Grok 4.20",
            use_mode_id=True,
        ),
        ModelInfo(
            model_id="heavy",
            grok_model="grok-4",
            model_mode="heavy",
            tier=Tier.SUPER,
            cost=Cost.HIGH,
            display_name="Heavy",
            description="SuperGrok Heavy - Grok 4.20",
            use_mode_id=True,
        ),
        # ── 具体模型（旧接口，使用 modelName + modelMode 方式请求）──
        ModelInfo(
            model_id="grok-3",
            grok_model="grok-3",
            model_mode="MODEL_MODE_GROK_3",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-3",
        ),
        ModelInfo(
            model_id="grok-4",
            grok_model="grok-4",
            model_mode="MODEL_MODE_GROK_4",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4",
        ),
        ModelInfo(
            model_id="grok-4-thinking",
            grok_model="grok-4",
            model_mode="MODEL_MODE_GROK_4_THINKING",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="GROK-4-THINKING",
        ),
        ModelInfo(
            model_id="grok-4-heavy",
            grok_model="grok-4",
            model_mode="MODEL_MODE_HEAVY",
            tier=Tier.SUPER,
            cost=Cost.HIGH,
            display_name="GROK-4-HEAVY",
        ),
        # ── 图片 / 视频模型 ──
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
        """获取模型信息"""
        return cls._map.get(model_id)

    @classmethod
    def list(cls) -> list[ModelInfo]:
        """获取所有模型"""
        return list(cls._map.values())

    @classmethod
    def valid(cls, model_id: str) -> bool:
        """模型是否有效"""
        return model_id in cls._map

    @classmethod
    def is_mode_id(cls, model_id: str) -> bool:
        """是否为快捷模式（使用 modeId 方式请求）"""
        model = cls.get(model_id)
        return model.use_mode_id if model else False

    @classmethod
    def to_grok(cls, model_id: str) -> Tuple[str, str]:
        """转换为 Grok 参数"""
        model = cls.get(model_id)
        if not model:
            raise ValidationException(f"Invalid model ID: {model_id}")
        return model.grok_model, model.model_mode

    @classmethod
    def pool_for_model(cls, model_id: str) -> str:
        """根据模型选择 Token 池"""
        model = cls.get(model_id)
        if model and model.tier == Tier.SUPER:
            return "ssoSuper"
        return "ssoBasic"

    @classmethod
    def pool_candidates_for_model(cls, model_id: str) -> List[str]:
        """按优先级返回可用 Token 池列表"""
        model = cls.get(model_id)
        if model and model.tier == Tier.SUPER:
            return ["ssoSuper"]
        # 基础模型优先使用 basic 池，缺失时可回退到 super 池
        return ["ssoBasic", "ssoSuper"]


__all__ = ["ModelService"]
