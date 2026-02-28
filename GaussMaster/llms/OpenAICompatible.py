"""OpenAI-compatible LLM adapter."""
import asyncio
import functools
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple, Union

from requests.exceptions import JSONDecodeError

from GaussMaster.common.http import create_requests_session
from GaussMaster.llms import llm_registry
from GaussMaster.llms.base.BaseLLM import BaseLLM
from GaussMaster.llms.llm_utils import check_message, LLMRole, LLMType

_LOG = logging.getLogger(__name__)


def _try_parse_sse_completion(raw_text: str) -> Optional[Tuple[str, dict]]:
    """Parse OpenAI-style chat.completion SSE (data: {...} lines). Returns None if no SSE chunks."""
    content_parts: list[str] = []
    chunks_seen = 0
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].lstrip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        chunks_seen += 1
        obj_type = obj.get("object")
        choices = obj.get("choices") or []
        if not choices:
            continue
        first = choices[0]
        if obj_type == "chat.completion.chunk":
            delta = first.get("delta") or {}
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
        elif obj_type == "chat.completion":
            message = first.get("message") or {}
            piece = message.get("content")
            if piece:
                content_parts.append(piece)
    if chunks_seen == 0:
        return None
    return "".join(content_parts), {}


@llm_registry(name='OpenAICompatible')
class OpenAICompatible(BaseLLM):
    """OpenAI-compatible chat/completions adapter."""

    def __init__(self, model, url, temperature=0, stream=False, api_key=None, request_defaults=None):
        super().__init__(model, url, temperature, stream)
        self.api_key = api_key
        self.request_defaults = request_defaults or {}

    @property
    def llm_type(self) -> str:
        return LLMType.OPENAI_COMPATIBLE

    @check_message
    async def invoke(self, prompt: Union[list, str]) -> tuple:
        if isinstance(prompt, str):
            prompt = [{LLMRole.USER: prompt}]
        payload = {
            "model": self.name,
            "messages": prompt
        }
        payload.update(self.request_defaults)
        if "temperature" not in payload:
            payload["temperature"] = self.temperature
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        with ThreadPoolExecutor() as pool:
            llm_response = functools.partial(
                _request_completion,
                self.url,
                headers,
                payload
            )
            response = await asyncio.get_running_loop().run_in_executor(pool, llm_response)
        if response.status_code != 200:
            return f"OpenAI-compatible service error: {response.status_code}", {}
        raw_text = (response.text or "").strip()
        if not raw_text:
            _LOG.warning(
                "OpenAI-compatible HTTP %s with empty body; url=%s headers=%s",
                response.status_code,
                self.url,
                dict(response.headers),
            )
            return (
                "OpenAI-compatible service returned HTTP 200 with an empty body; "
                "check api_url, gateway, and upstream service logs.",
                {},
            )
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in content_type or raw_text.lstrip().startswith("data:"):
            sse = _try_parse_sse_completion(raw_text)
            if sse is not None:
                return sse
        try:
            body = response.json()
        except JSONDecodeError:
            sse = _try_parse_sse_completion(raw_text)
            if sse is not None:
                return sse
            preview = raw_text[:800] + ("..." if len(raw_text) > 800 else "")
            _LOG.warning(
                "OpenAI-compatible non-JSON body (HTTP %s); url=%s preview=%r",
                response.status_code,
                self.url,
                preview,
            )
            return (
                f"OpenAI-compatible service returned non-JSON body (HTTP {response.status_code}): {preview}",
                {},
            )
        choices = body.get("choices", [])
        if not choices:
            return "", {}
        message = choices[0].get("message", {})
        content = message.get("content", "")
        function_call = message.get("function_call", {}) or {}
        return content, function_call


def _request_completion(url, headers, payload):
    with create_requests_session(ssl_context=None) as session:
        return session.post(url, json=payload, headers=headers, timeout=120)
