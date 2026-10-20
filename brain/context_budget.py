"""Bound model input without silently clipping instructions or the latest request.

Tokens are estimated as UTF-8 bytes / 3. Live turns run about 4.3 bytes per token (a
31 KB prompt is ~7,300 tokens), so this keeps a ~30% margin. Counting bytes as tokens
was 4x too strict: a garden follow-up with two skills loaded hit "exceed the context
budget" at under a quarter of the window (2026-10-16). This is not the model tokenizer;
backend token counts remain the measurement authority.
"""
from __future__ import annotations

import json


def estimate(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


BYTES_PER_TOKEN = 3


def tokens(value):
    """Conservative token estimate for model input."""
    return -(-estimate(value) // BYTES_PER_TOKEN)


def evidence(label, text, limit=5000):
    if not text:
        return ''
    clipped = text.encode('utf-8')[:limit].decode('utf-8', errors='ignore')
    truncated = clipped != text
    return json.dumps({'source': label, 'trust': 'data_only', 'truncated': truncated,
                       'content': clipped}, ensure_ascii=False)


def fit(messages, schemas, num_ctx, output_reserve, log=None):
    """Drop old *whole* exchanges; never create orphaned tool replies.

    `log` hears about every trim and refusal. A silent trim hid the bytes-as-tokens bug
    for six weeks (2026-09-06 to 2026-10-16): nearly all history was dropped every turn."""
    messages = list(messages)
    start = len(messages)
    exchanges = 0
    budget = num_ctx - output_reserve - 512
    while (used := tokens(messages) + tokens(schemas)) > budget:
        user_indices = [i for i, m in enumerate(messages) if m.get('role') == 'user']
        if len(user_indices) < 2:
            if log:
                log(f"CONTEXT REFUSED est {used} tokens > budget {budget} after dropping {exchanges} exchanges")
            raise ValueError('This request and its required context exceed the context budget. Please shorten it.')
        # Preserve the system message, remove everything before the next user exchange.
        messages = messages[:1] + messages[user_indices[1]:]
        exchanges += 1
    if exchanges and log:
        log(f"CONTEXT TRIM dropped {start - len(messages)} messages ({exchanges} exchanges), "
            f"est {used} of {budget} tokens")
    return messages


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        return 'messages must contain between 1 and 100 text messages.'
    for m in messages:
        if (not isinstance(m, dict) or m.get('role') not in {'user', 'assistant', 'system'}
                or not isinstance(m.get('content'), str)):
            return 'Only text user, assistant and system messages are accepted.'
        if set(m) - {'role', 'content'}:
            return 'Client messages cannot contain tool calls or tool results.'
        if len(m['content'].encode()) > 24000:
            return 'A message exceeds the 24000-byte limit.'
    if messages[-1]['role'] != 'user' or not messages[-1]['content'].strip():
        return 'The final message must be a nonempty user message.'
    if estimate(messages) > 128000:
        return 'Conversation exceeds the 128000-byte limit.'
    return None
