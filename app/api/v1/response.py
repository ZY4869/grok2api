"""
Responses API 路由 (OpenAI compatible).
"""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core.call_log import begin_call_log, wrap_call_log_stream
from app.core.exceptions import ValidationException
from app.core.logger import logger
from app.services.grok.services.responses import (
    ResponsesService,
    _coerce_input_to_messages,
)
from app.services.grok.utils.prompt_debug import summarize_chat_messages


router = APIRouter(tags=["Responses"])


class ResponseCreateRequest(BaseModel):
    model: str = Field(..., description="Model name")
    input: Optional[Any] = Field(None, description="Input content")
    instructions: Optional[str] = Field(None, description="System instructions")
    stream: Optional[bool] = Field(False, description="Stream response")
    max_output_tokens: Optional[int] = Field(None, description="Max output tokens")
    temperature: Optional[float] = Field(None, description="Sampling temperature")
    top_p: Optional[float] = Field(None, description="Nucleus sampling")
    tools: Optional[List[Dict[str, Any]]] = Field(None, description="Tool definitions")
    tool_choice: Optional[Union[str, Dict[str, Any]]] = Field(None, description="Tool choice")
    parallel_tool_calls: Optional[bool] = Field(True, description="Allow parallel tool calls")
    reasoning: Optional[Dict[str, Any]] = Field(None, description="Reasoning options")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata")
    user: Optional[str] = Field(None, description="User identifier")
    store: Optional[bool] = Field(None, description="Store response")
    previous_response_id: Optional[str] = Field(None, description="Previous response id")
    truncation: Optional[str] = Field(None, description="Truncation behavior")

    class Config:
        extra = "allow"


@router.post("/responses")
async def create_response(body: ResponseCreateRequest, request: Request):
    begin_call_log(
        "responses.create",
        trace_id=getattr(request.state, "trace_id", ""),
        model=body.model,
    )
    messages = _coerce_input_to_messages(body.input)
    if body.instructions:
        messages = [{"role": "system", "content": body.instructions}] + messages
    logger.info(
        "Responses request prompt summary",
        extra={
            "trace_id": getattr(request.state, "trace_id", ""),
            "model": body.model,
            "stream": bool(body.stream),
            "prompt_summary": summarize_chat_messages(messages),
        },
    )

    if not body.model:
        raise ValidationException(message="model is required", param="model", code="invalid_request_error")

    if body.input is None:
        raise ValidationException(message="input is required", param="input", code="invalid_request_error")

    reasoning_effort = None
    if isinstance(body.reasoning, dict):
        reasoning_effort = body.reasoning.get("effort") or body.reasoning.get("reasoning_effort")

    result = await ResponsesService.create(
        model=body.model,
        input_value=body.input,
        instructions=body.instructions,
        stream=bool(body.stream),
        temperature=body.temperature,
        top_p=body.top_p,
        tools=body.tools,
        tool_choice=body.tool_choice,
        parallel_tool_calls=body.parallel_tool_calls,
        reasoning_effort=reasoning_effort,
        max_output_tokens=body.max_output_tokens,
        metadata=body.metadata,
        user=body.user,
        store=body.store,
        previous_response_id=body.previous_response_id,
        truncation=body.truncation,
    )

    if body.stream:
        return StreamingResponse(
            wrap_call_log_stream(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return JSONResponse(content=result)


__all__ = ["router"]
