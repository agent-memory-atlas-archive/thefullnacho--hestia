"""A reply may only say something was done when a tool did it; a bare link gets a fixed answer.

Both from a real morning: a pasted Facebook link got "imported into your recipe collection as
a draft" and "Remind to start the seeds January 2" got "Reminder #23 set", with no tool call
behind either. Nothing was saved, and nobody would have known until January.
"""
from __future__ import annotations

import asyncio

import pytest

import hestia


def _run(monkeypatch, user_text, reply):
    calls = []

    async def chat(*_):
        calls.append(1)
        return {"content": reply}
    monkeypatch.setattr(hestia, "_ollama_chat", chat)
    answer = asyncio.run(hestia.run_agent([{"role": "user", "content": user_text}]))
    return answer, calls


@pytest.mark.parametrize("user_text, reply", [
    ("Remind to start the seeds January 2, 2027",
     "Reminder #23 set — I'll ping your phone Fri Jan 2 at 9:00 AM: start the seeds"),
    ("here's the winter sowing write-up from the extension office",
     "I have imported the information from that link into your recipe collection as a draft for you to review."),
    ("milk", "I've added milk to your shopping list."),
    ("dentist friday at 2", "Done, it's on the calendar for Friday."),
])
def test_a_claimed_action_with_no_tool_call_is_never_shown(monkeypatch, user_text, reply):
    answer, _ = _run(monkeypatch, user_text, reply)
    assert answer == hestia._UNDONE_CLAIM


@pytest.mark.parametrize("reply", [
    "You could set a reminder for early January to amend the soil.",
    "Would you like me to add that to your shopping list?",
    "Blueberries like acidic soil, around pH 4.5 to 5.5.",
    "I've put together a summary of the winter sowing method below.",
])
def test_advice_offers_and_answers_pass_through(monkeypatch, reply):
    answer, _ = _run(monkeypatch, "when should I amend the blueberry soil", reply)
    assert answer == reply


@pytest.mark.parametrize("text", [
    "https://www.facebook.com/share/r/19Z9cwYhxX/?mibextid=wwXIfr",
    "Winter sowing link: https://www.facebook.com/share/r/1HnrUqGzPz/?mibextid=wwXIfr",
])
def test_a_bare_link_gets_the_import_instructions_without_the_model(monkeypatch, text):
    answer, calls = _run(monkeypatch, text, "I have imported it into your recipe collection.")
    assert answer == hestia._LINK_ONLY_REPLY
    assert calls == []


def test_a_link_with_a_request_still_reaches_the_model(monkeypatch):
    answer, calls = _run(monkeypatch, "save this recipe https://example.com/chili",
                         "Which part should I keep?")
    assert calls and answer == "Which part should I keep?"


def test_an_undone_claim_stays_out_of_the_thread():
    assert hestia._UNDONE_CLAIM in hestia._NO_LEARN
