from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.settings import settings
from app.tools import web_search


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None, raise_json: bool = False) -> None:
        self.status_code = status_code
        self._data = json_data
        self._raise_json = raise_json

    def json(self) -> Any:
        if self._raise_json:
            raise ValueError("not json")
        return self._data


class _FakeAsyncClient:
    def __init__(self, *, response: _FakeResponse | None = None, exc: Exception | None = None, **_: Any) -> None:
        self._response = response
        self._exc = exc

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tv-test-key")


async def test_happy_path(monkeypatch, shared_ctx, fake_llm):
    response = _FakeResponse(
        200,
        {
            "results": [
                {"title": "T1", "url": "https://example.com/1", "content": "snippet one", "score": 0.9},
                {"title": "T2", "url": "https://example.com/2", "content": "snippet two", "score": 0.8},
            ]
        },
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response=response, **kw))

    result = await web_search.run(shared_ctx, fake_llm)

    assert result.success is True
    assert result.error_code is None
    assert isinstance(result.data, list) and len(result.data) == 2
    assert result.data[0] == {
        "title": "T1",
        "url": "https://example.com/1",
        "snippet": "snippet one",
        "relevance_score": 0.9,
    }


async def test_timeout(monkeypatch, shared_ctx, fake_llm):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _FakeAsyncClient(exc=httpx.TimeoutException("slow"), **kw),
    )
    result = await web_search.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "TIMEOUT"


async def test_empty(monkeypatch, shared_ctx, fake_llm):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _FakeAsyncClient(response=_FakeResponse(200, {"results": []}), **kw),
    )
    result = await web_search.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "EMPTY"
    assert result.data == []


async def test_malformed_no_key(monkeypatch, shared_ctx, fake_llm):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    # If we hit the network it would raise — but we shouldn't.
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: pytest.fail("HTTP call attempted")
    )
    result = await web_search.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"


async def test_malformed_stub_sentinel(monkeypatch, shared_ctx, fake_llm):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "stub-not-used-in-slice-0")
    result = await web_search.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"


async def test_malformed_4xx_non_json(monkeypatch, shared_ctx, fake_llm):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _FakeAsyncClient(response=_FakeResponse(401, raise_json=True), **kw),
    )
    result = await web_search.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"
