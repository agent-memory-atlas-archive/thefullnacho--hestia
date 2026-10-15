"""The utterance log: off by default, words only, tagged by window, one row per real turn."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import hestia
import utterance_store


@pytest.fixture
def brain(db, monkeypatch, tmp_path):
    monkeypatch.setattr(hestia.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hestia.note_taker, "ENABLED", False)

    async def agent(messages, *, trace):
        return "Which room?"
    monkeypatch.setattr(hestia, "run_agent", agent)
    return hestia


def _post(*bodies):
    async def scenario():
        transport = httpx.ASGITransport(app=hestia.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return [await client.post("/v1/chat/completions", **b) for b in bodies]
    return asyncio.run(scenario())


def test_off_unless_the_flag_is_set(brain, monkeypatch):
    monkeypatch.delenv("HESTIA_UTTERANCE_LOG", raising=False)
    _post({"json": {"messages": [{"role": "user", "content": "lights off"}]}})
    assert utterance_store.since() == []


def test_voice_turn_logs_the_words_and_the_question_it_answers(brain, monkeypatch):
    monkeypatch.setenv("HESTIA_UTTERANCE_LOG", "1")
    ha = {"User-Agent": "HomeAssistant/2026.10.1 aiohttp/3.12 Python/3.13"}
    _post({"json": {"messages": [{"role": "user", "content": "lights off"}]}, "headers": ha},
          {"json": {"messages": [{"role": "user", "content": "lights off"},
                                 {"role": "assistant", "content": "Which room?"},
                                 {"role": "user", "content": "kitchen"}]}, "headers": ha})
    rows = utterance_store.since()
    assert [(r["source"], r["text"], r["prior"]) for r in rows] == [
        ("voice", "lights off", None), ("voice", "kitchen", "Which room?")]


def test_app_turn_is_tagged_app_and_a_retry_is_not_logged_twice(brain, monkeypatch):
    monkeypatch.setenv("HESTIA_UTTERANCE_LOG", "1")
    body = {"json": {"thread_id": "main", "messages": [{"role": "user", "content": "we're out of milk"}]},
            "headers": {"Idempotency-Key": "key-milk-123456"}}
    _post(body, body)
    rows = utterance_store.since()
    assert [(r["source"], r["text"]) for r in rows] == [("app", "we're out of milk")]


def test_a_failing_log_never_costs_the_turn(brain, monkeypatch):
    monkeypatch.setenv("HESTIA_UTTERANCE_LOG", "1")

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(utterance_store, "record", broken)
    (resp,) = _post({"json": {"messages": [{"role": "user", "content": "lights off"}]}})
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Which room?"


def test_unknown_clients_are_other():
    assert utterance_store.source_of("curl/8.5", False) == "other"
    assert utterance_store.source_of("Mozilla/5.0", True) == "app"
