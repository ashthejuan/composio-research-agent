"""Regression: agent.llm._retry must stop after 3 attempts (tenacity `stop=` keyword, not positional)."""
import asyncio

import pytest
from tenacity import wait_none

from agent import llm


def test_retry_stops_after_three_attempts():
    calls = {"n": 0}

    @llm._retry
    async def always_fails():
        calls["n"] += 1
        raise RuntimeError("boom")

    always_fails.retry.wait = wait_none()  # no backoff in tests
    with pytest.raises(RuntimeError):
        asyncio.run(always_fails())
    assert calls["n"] == 3
