"""verify_claim with a mocked AsyncOpenAI (no network)."""
import asyncio
from types import SimpleNamespace as NS

import openai
import pytest
from pydantic import BaseModel
from tenacity import wait_none

from agent import llm
from agent.config import Settings, verifier_model_hint


class P(BaseModel):
    ok: bool


def _resp(parsed=None, content="x", finish="stop", reasoning=7):
    return NS(
        choices=[NS(message=NS(parsed=parsed, content=content), finish_reason=finish)],
        usage=NS(prompt_tokens=11, completion_tokens=20, completion_tokens_details=NS(reasoning_tokens=reasoning)),
    )


def _install(monkeypatch, responses):
    calls = []

    class Client:
        def __init__(self, **kw):
            calls.append(("init", kw))
            self.chat = NS(completions=NS(parse=self.parse, create=self.parse))

        async def parse(self, **kw):
            calls.append(("call", kw))
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    monkeypatch.setenv("META_API_KEY", "test-key")
    llm._verify.retry.wait = wait_none()
    return calls


def test_parses_and_maps_usage(monkeypatch):
    calls = _install(monkeypatch, [_resp(parsed=P(ok=True))])
    out, usage = asyncio.run(llm.verify_claim("hi", P))
    assert out.ok is True
    assert usage == {"input_tokens": 11, "output_tokens": 20, "reasoning_tokens": 7}
    assert calls[0][1]["base_url"] == "https://api.meta.ai/v1"


def test_null_content_retries_with_bigger_budget(monkeypatch):
    calls = _install(monkeypatch, [_resp(content=None, finish="length"), _resp(parsed=P(ok=False))])
    out, usage = asyncio.run(llm.verify_claim("hi", P))
    assert out.ok is False
    assert [c[1]["max_tokens"] for c in calls if c[0] == "call"] == [4096, 16384]
    assert usage["input_tokens"] == 22


def test_null_content_always_raises_clear_error(monkeypatch):
    _install(monkeypatch, [_resp(content=None, finish="length")] * 6)
    with pytest.raises(RuntimeError, match="empty content"):
        asyncio.run(llm.verify_claim("hi", P))


def test_missing_key_raises_without_client(monkeypatch):
    calls = _install(monkeypatch, [])
    monkeypatch.delenv("META_API_KEY")
    with pytest.raises(RuntimeError, match="META_API_KEY"):
        asyncio.run(llm.verify_claim("hi", P))
    assert calls == []


def test_defaults_and_hint():
    s = Settings()
    assert s.verifier_model == "muse-spark-1.3-contributor"
    assert s.verifier_base_url == "https://api.meta.ai/v1"
    assert verifier_model_hint("muse-spark-1.3-contributor") is None
    assert "meta-spark-1.3-contributor" in verifier_model_hint("meta-spark-1.3-contributor")


def test_length_finish_error_retries_with_bigger_budget(monkeypatch):
    err = openai.LengthFinishReasonError(completion=NS(usage=NS(prompt_tokens=5, completion_tokens=6,
                                                                completion_tokens_details=None)))
    calls = _install(monkeypatch, [err, _resp(parsed=P(ok=True))])
    out, usage = asyncio.run(llm.verify_claim("hi", P))
    assert out.ok and [c[1]["max_tokens"] for c in calls if c[0] == "call"] == [4096, 16384]
    assert usage["input_tokens"] == 16


def test_client_errors_not_retried(monkeypatch):
    import httpx

    req = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
    e = openai.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)
    calls = _install(monkeypatch, [e, e, e])
    with pytest.raises(openai.AuthenticationError):
        asyncio.run(llm.verify_claim("hi", P))
    assert len([c for c in calls if c[0] == "call"]) == 1
