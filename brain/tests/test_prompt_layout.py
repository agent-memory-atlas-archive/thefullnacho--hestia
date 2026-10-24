"""Prompt layout: "tail" keeps the system message fixed so the history stays a cached prefix."""
from __future__ import annotations

import hestia
from prompt import SYSTEM_PROMPT

HISTORY = [{"role": "user", "content": "What breed is Biscuit?"},
           {"role": "assistant", "content": "A corgi."},
           {"role": "user", "content": "Which bag has the coffee?"}]
PROMPT = SYSTEM_PROMPT + "\n\nCurrent date/time: Saturday October 24 2026, 06:12.\n--- MEMORY ---\norange bag"


def test_head_is_the_default_and_unchanged(monkeypatch):
    monkeypatch.setattr(hestia, "PROMPT_LAYOUT", "head")
    out = hestia._layout(PROMPT, list(HISTORY))
    assert out[0] == {"role": "system", "content": PROMPT}
    assert out[1:] == HISTORY


def test_tail_fixes_the_system_message_and_carries_context_on_the_latest_turn(monkeypatch):
    monkeypatch.setattr(hestia, "PROMPT_LAYOUT", "tail")
    out = hestia._layout(PROMPT, list(HISTORY))
    assert out[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert out[1:3] == HISTORY[:2]          # history untouched: the cacheable prefix
    last = out[-1]["content"]
    assert last.startswith(hestia._CONTEXT_OPEN) and "06:12" in last and "orange bag" in last
    assert last.endswith("\n\nWhich bag has the coffee?")   # the user's words come last


def test_two_turns_share_everything_before_the_newest_message(monkeypatch):
    monkeypatch.setattr(hestia, "PROMPT_LAYOUT", "tail")
    first = hestia._layout(PROMPT, list(HISTORY[:1]))
    later = PROMPT.replace("06:12", "06:14")
    second = hestia._layout(later, [*HISTORY[:2], {"role": "user", "content": "Thanks"}])
    assert second[0] == first[0] and second[1] == HISTORY[0]


def test_tail_falls_back_when_the_prompt_is_not_the_house_prompt(monkeypatch):
    monkeypatch.setattr(hestia, "PROMPT_LAYOUT", "tail")
    out = hestia._layout("a test prompt", list(HISTORY))
    assert out[0] == {"role": "system", "content": "a test prompt"} and out[1:] == HISTORY


def test_tail_with_no_extra_context_adds_no_wrapper(monkeypatch):
    monkeypatch.setattr(hestia, "PROMPT_LAYOUT", "tail")
    out = hestia._layout(SYSTEM_PROMPT, list(HISTORY))
    assert out[-1] == HISTORY[-1]

