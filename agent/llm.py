"""LLM clients: OpenAI extractor and Meta Model API verifier. No import-time calls."""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.config import get_settings, require

T = TypeVar("T", bound=BaseModel)
_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)

_META_BASE_URL = "https://api.meta.ai/v1"


async def extract_json(system: str, user: str, schema: type[T]) -> tuple[T, dict[str, int]]:
    """OpenAI Responses API structured output. Returns (parsed model, usage tokens)."""
    key = require("OPENAI_API_KEY")  # checked before the retry loop: a missing key is not transient
    return await _extract(key, system, user, schema)


@_retry
async def _extract(key: str, system: str, user: str, schema: type[T]) -> tuple[T, dict[str, int]]:
    from openai import AsyncOpenAI

    s = get_settings()
    async with AsyncOpenAI(api_key=key) as client:  # closes the pooled httpx client per call
        resp = await client.responses.parse(
            model=s.extractor_model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            text_format=schema,
        )
    if resp.output_parsed is None:
        raise RuntimeError("extractor returned no parsed output (refusal or truncation)")
    u = resp.usage
    usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0}
    return resp.output_parsed, usage


async def verify_claim(prompt: str, schema: type[T]) -> tuple[T, dict[str, int]]:
    """Meta Model API structured JSON (OpenAI-compatible). Returns (parsed model, usage tokens)."""
    key = require("META_API_KEY")
    return await _verify(key, prompt, schema)


@_retry
async def _verify(key: str, prompt: str, schema: type[T]) -> tuple[T, dict[str, int]]:
    from openai import AsyncOpenAI

    s = get_settings()
    async with AsyncOpenAI(api_key=key, base_url=_META_BASE_URL) as client:
        resp = await client.chat.completions.parse(
            model=s.verifier_model,
            messages=[{"role": "user", "content": prompt}],
            response_format=schema,
        )
    msg = resp.choices[0].message
    if msg.parsed is None:
        raise RuntimeError("verifier returned no parsed output (refusal or truncation)")
    u = resp.usage
    # Reasoning tokens are billed as completion/output on Muse Spark.
    usage = {
        "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(u, "completion_tokens", 0) or 0,
    }
    return msg.parsed, usage
