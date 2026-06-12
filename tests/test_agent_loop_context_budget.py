"""Regression for the agent-loop mid-round context overflow.

The agent loop trimmed context once at turn start, then appended a tool
result every round and re-called the LLM with no further trimming. A large
result (a Home Assistant `api_call` returning entity-state JSON) pushed the
next round's request past the model window:

    llamacpp returned HTTP 400: request (16666 tokens) exceeds the
    available context size (16384 tokens)

The fix re-trims `messages` against a window-scaled budget at the top of
every round, capped at DEFAULT_HEADROOM of the window so estimate_tokens'
under-count on dense JSON can't tip the real request over. These tests
exercise the exact budget+trim composition the loop performs (pure, no
network / no stream_agent_loop).
"""
from src.context_compactor import trim_for_context, estimate_tokens
from src.context_budget import compute_input_token_budget, DEFAULT_HEADROOM

CTX = 16384


def _midloop_budget(soft, explicit):
    """Mirror the budget the agent loop computes each round."""
    return min(
        compute_input_token_budget(soft, CTX, explicit),
        int(CTX * DEFAULT_HEADROOM),
    )


def test_budget_keeps_headroom_even_with_oversized_explicit_budget():
    # An explicit budget larger than the window clamps to the window in
    # compute_input_token_budget (no headroom) — the min() floor restores it,
    # so the real request always lands under the ceiling.
    budget = _midloop_budget(soft=64000, explicit=True)
    assert budget <= int(CTX * DEFAULT_HEADROOM)
    assert budget < CTX  # never the full window


def test_default_budget_scales_to_headroom():
    # The common path: default 6000 soft budget, not explicitly set.
    budget = _midloop_budget(soft=6000, explicit=False)
    assert budget == int(CTX * DEFAULT_HEADROOM)


def test_large_tool_result_trimmed_under_window():
    # Reproduce the crash shape: a near-budget conversation plus a fat tool
    # result that, untrimmed, exceeds the window.
    reserve = 1024
    msgs = [{"role": "system", "content": "You are JARVIS. " * 50}]
    # ~40 older turns, ~1000 chars each (~300 est tokens each => ~12k tokens).
    for i in range(40):
        msgs.append({"role": "user", "content": f"old turn {i}: " + ("x" * 1000)})
        msgs.append({"role": "assistant", "content": f"reply {i}: " + ("y" * 200)})
    # The fat Home Assistant api_call result (12KB cap) near the end, then the
    # current user turn.
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "api_call", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1",
                 "content": '{"entity_id":"switch.switch01","state":"on"} ' * 250})
    msgs.append({"role": "user", "content": "is switch01 on?"})

    budget = _midloop_budget(soft=6000, explicit=False)
    assert estimate_tokens(msgs) > budget  # precondition: would overflow

    trimmed = trim_for_context(msgs, budget, reserve_tokens=reserve)

    # The core guarantee: the trimmed request fits the window with headroom.
    assert estimate_tokens(trimmed) <= budget
    assert estimate_tokens(trimmed) < CTX
    # The current turn and the latest tool result survive; an old turn is gone.
    contents = " ".join(str(m.get("content", "")) for m in trimmed)
    assert "is switch01 on?" in contents
    assert "switch.switch01" in contents
    assert "old turn 0:" not in contents
    # System prompt preserved.
    assert any(m.get("role") == "system" for m in trimmed)


def test_noop_when_already_under_budget():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    budget = _midloop_budget(soft=6000, explicit=False)
    out = trim_for_context(msgs, budget, reserve_tokens=1024)
    assert out == msgs  # untouched
