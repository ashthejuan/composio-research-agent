"""LLM clients: OpenAI extractor and Meta Model API verifier. No import-time calls."""
from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import get_settings, require

T = TypeVar("T", bound=BaseModel)


def _retryable(e: BaseException) -> bool:
    """Retry transient failures only; 4xx client errors (bad key, bad request, unknown model) never succeed on retry."""
    try:
        from openai import APIStatusError

        if isinstance(e, APIStatusError) and e.status_code < 500 and e.status_code not in (408, 409, 429):
            return False
    except ImportError:  # pragma: no cover
        pass
    return True


_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True,
               retry=retry_if_exception(_retryable))

_VERIFIER_MAX_TOKENS = (4096, 16384)  # Muse spends tokens on reasoning; retry once with a bigger budget


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
    key = require("META_API_KEY")  # checked before the retry loop
    return await _verify(key, prompt, schema)


def _add_usage(total: dict[str, int], u: object) -> None:
    total["input_tokens"] += getattr(u, "prompt_tokens", 0) or 0
    total["output_tokens"] += getattr(u, "completion_tokens", 0) or 0
    details = getattr(u, "completion_tokens_details", None)
    r = getattr(details, "reasoning_tokens", 0) or 0
    if r:
        total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + r


@_retry
async def _verify(key: str, prompt: str, schema: type[T]) -> tuple[T, dict[str, int]]:
    from openai import AsyncOpenAI, BadRequestError, LengthFinishReasonError
    from pydantic import ValidationError

    s = get_settings()
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    # reasoning_tokens are already included in output_tokens (completion_tokens) and reported separately.
    async with AsyncOpenAI(api_key=key, base_url=s.verifier_base_url) as client:
        for max_tokens in _VERIFIER_MAX_TOKENS:
            # Meta supports json_schema structured outputs (probed live); fall back to json_object if rejected.
            try:
                resp = await client.chat.completions.parse(
                    model=s.verifier_model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format=schema,
                    max_tokens=max_tokens,
                )
            except BadRequestError as e:
                if "model" in str(e).lower():
                    raise
                return await _verify_json_mode(client, s.verifier_model, prompt, schema, usage)
            except LengthFinishReasonError as e:  # SDK raises (not returns) when finish_reason == "length"
                _add_usage(usage, getattr(e.completion, "usage", None))
                continue  # reasoning ate the budget; retry bigger
            except ValidationError:
                return await _verify_json_mode(client, s.verifier_model, prompt, schema, usage)
            _add_usage(usage, resp.usage)
            choice = resp.choices[0]
            if choice.message.parsed is not None:
                return choice.message.parsed, usage
            if choice.finish_reason == "length" or not choice.message.content:
                continue  # null/empty content: reasoning ate the budget; retry bigger
            raise RuntimeError(f"verifier returned no parsed output (finish_reason={choice.finish_reason})")
    raise RuntimeError(
        f"verifier returned empty content after max_tokens={_VERIFIER_MAX_TOKENS[-1]} (finish_reason=length)"
    )


async def _verify_json_mode(client, model: str, prompt: str, schema: type[T], usage: dict[str, int]) -> tuple[T, dict[str, int]]:
    from pydantic import ValidationError

    system = "Respond with a single JSON object only, matching this JSON schema:\n" + json.dumps(schema.model_json_schema())
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    last: Exception | None = None
    for _ in range(2):  # one repair retry
        resp = await client.chat.completions.create(
            model=model, messages=messages, response_format={"type": "json_object"}, max_tokens=_VERIFIER_MAX_TOKENS[-1]
        )
        _add_usage(usage, resp.usage)
        content = resp.choices[0].message.content
        if not content:
            last = RuntimeError(f"verifier returned empty content (finish_reason={resp.choices[0].finish_reason})")
            continue
        try:
            return schema.model_validate_json(content), usage
        except ValidationError as e:
            last = e
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"Invalid against the schema ({str(e)[:300]}). Return corrected JSON only."},
            ]
    raise RuntimeError(f"verifier output failed validation after repair retry: {last}")
