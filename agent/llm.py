"""LLM clients: OpenAI extractor (structured outputs) and Gemini verifier. No import-time calls."""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.config import get_settings, require

T = TypeVar("T", bound=BaseModel)
_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)


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
    """Gemini structured JSON output. Returns (parsed model, usage tokens)."""
    key = require("GEMINI_API_KEY")
    return await _verify(key, prompt, schema)


@_retry
async def _verify(key: str, prompt: str, schema: type[T]) -> tuple[T, dict[str, int]]:
    from google import genai

    s = get_settings()
    client = genai.Client(api_key=key)
    try:
        async with client.aio as aclient:
            resp = await aclient.models.generate_content(
                model=s.verifier_model,
                contents=prompt,
                config={"response_mime_type": "application/json", "response_schema": schema},
            )
    finally:
        client.close()
    parsed: Any = resp.parsed
    if not isinstance(parsed, schema):
        parsed = schema.model_validate_json(resp.text or "")
    um = resp.usage_metadata
    # Thinking tokens are billed as output on Gemini; count them so cost totals are honest.
    usage = {"input_tokens": getattr(um, "prompt_token_count", 0) or 0,
             "output_tokens": (getattr(um, "candidates_token_count", 0) or 0)
             + (getattr(um, "thoughts_token_count", 0) or 0)}
    return parsed, usage
