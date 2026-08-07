"""
token_guard.py -- last-line-of-defense trimming so no message list sent
through an adapter's completion call can exceed the model's context
window, regardless of which caller built that list.

This is deliberately NOT the same object as LMNode._trim_to_token_limit
in src/nodes/lm_node.py. That one manages a NODE's own persistent
self.messages history proactively, every preprocess() cycle, trimming
the actual stored conversation so it doesn't grow unbounded over time.

This one is reactive and stateless: given any list of messages about to
be sent to the model -- LMNode's history, the casual-brainstorm
conversation, the refractory recovery conversation, a future robot
loop's hand-built context, anything -- trim a COPY down to size right
before it goes out over the wire. It never mutates the caller's list
and it never touches a node's own history; it only decides what
actually gets transmitted for THIS one call.

Both exist on purpose. LMNode's version keeps normal conversations from
ballooning turn over turn. This one is the guarantee that even a caller
that forgot to trim, or a code path LMNode's own trimming doesn't cover
(there are several -- see docker_gemma4_adapter.py's complete_async),
can never actually overflow the context window.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.tools.tokenizer import count_tokens

_CONFIG_PATH = Path(__file__).parent.parent.parent / 'config' / 'config.json'


def _load_lm_config() -> dict:
    with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)['lm_config']


def _max_input_tokens(lm_config: dict) -> int:
    context_window = lm_config['context_window']
    max_parallel = lm_config.get('max_parallel_predictions', 1)
    max_output = lm_config['max_output_tokens']
    safety_buffer = lm_config['safety_buffer']
    effective_context = context_window // max_parallel
    return effective_context - max_output - safety_buffer


async def trim_messages_to_fit(
    messages: list[dict],
    preserve_head: int = 1,
    lm_config: dict | None = None,
) -> list[dict]:
    """Returns a (possibly shortened) COPY of messages that fits within
    the configured context window. Never mutates the input list.

    Args:
        messages: full message list as it's about to be sent, e.g.
            [system, user, assistant, user, assistant, ..., user]
        preserve_head: how many leading messages are never dropped
            (normally 1 -- the system prompt). Everything after that is
            treated as droppable turn pairs, oldest first, same as
            LMNode._trim_to_token_limit.
        lm_config: pass an already-loaded lm_config to avoid re-reading
            config.json on every single call; loads it fresh if omitted.

    If tokenization itself fails (count_tokens returns -1, e.g. the
    tokenizer endpoint is unreachable), this returns messages unchanged
    -- same fail-open behavior as LMNode's version, since refusing to
    send anything at all is worse than risking an over-limit request
    the server itself may still reject or truncate.
    """
    if lm_config is None:
        lm_config = _load_lm_config()

    max_input_tokens = _max_input_tokens(lm_config)

    trimmed = list(messages)
    current_tokens = await count_tokens(trimmed)

    if current_tokens <= 0:
        # Tokenization failed -- fail open rather than mangling a
        # message list we can't actually measure.
        return trimmed

    if current_tokens <= max_input_tokens:
        return trimmed

    while current_tokens > max_input_tokens and len(trimmed) > preserve_head:
        head = trimmed[:preserve_head]
        tail = trimmed[preserve_head:]

        if len(tail) < 2:
            # Can't drop a full pair without losing everything droppable --
            # stop here rather than looping forever on an odd leftover.
            break

        tail = tail[2:]  # drop oldest user/assistant pair
        trimmed = head + tail
        current_tokens = await count_tokens(trimmed)

    return trimmed
