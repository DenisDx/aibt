# Proposal: group_chat_helper2 (Two-Stage Multi-Agent Chat Helper)

## 1. Goal
Create a new chat helper agent that behaves like `chat_group_helper`, but uses a two-stage decision pipeline:
1. Fast and cheap model decides whether the message should be processed.
2. If accepted, the smart responder generates the final answer (or can still return `__NO_REPLY__`).

The design must minimize code duplication and keep behavior consistent with existing Telegram/group workflows.

## 2. Key Requirements
- Keep existing adapter flow (`telegram -> orchestrator -> agent`) unchanged.
- Stage 1 must return only one of:
  - `__REPLY__`
  - `__NO_REPLY__`
- Stage 2 may still return:
  - `__NO_REPLY__`
  - normal final response text
- Preserve current handling semantics for `__NO_REPLY__` (skip send).
- Keep history/context behavior equivalent to `chat_group_helper`.

## 3. Architecture

### Gate + delegate
Create a new agent (for example, `chat_group_helper2`) that:
1. runs fast gate decision;
2. delegates accepted messages to existing `chat_group_helper`.

Pros:
- reuses proven smart-stage behavior and memory logic;
- minimal code duplication;
- easier rollout and rollback;
- cleaner separation of concerns.

Cons:
- one extra orchestration hop (negligible compared to smart model latency).

## 4. Design

### 4.1 New agent role
`chat_group_helper2` becomes a lightweight gate/orchestrator agent:
- builds the same role-aware message context as `chat_group_helper`;
- calls FAST model with gate codex;
- if `__NO_REPLY__` -> returns skip result immediately;
- if `__REPLY__` -> forwards original query/context to `chat_group_helper`.

### 4.2 Smart stage
`chat_group_helper` remains the smart stage and keeps all existing behavior:
- memory enrichment and retrieval;
- final response generation;
- optional `__NO_REPLY__`.

This avoids introducing a second smart implementation path.

## 5. Configuration Proposal

## 5.1 `.env` additions
```env
FAST_LLM_PROVIDER=default
FAST_LLM_MODEL=qwen3.5:4b
SMART_LLM_PROVIDER=default
SMART_LLM_MODEL=juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m
```

### 5.2 `config.json5` agent section for new agent
```json5
{
  "agents": {
    "items": {
      "chat_group_helper2": {
        "enabled": true,

        "gate": {
          "provider": "${FAST_LLM_PROVIDER:-default}",
          "model": "${FAST_LLM_MODEL:-sorc/qwen3.5-claude-4.6-opus-q4:0.8b}",
          "instruction_files": [
            "${AIBT_ROOT}/agent_files/chat_group_helper2.md"
          ]
        },

        "delegate_agent": "chat_group_helper",

        "recent_messages_limit": 50,
        "logging": { "log_llm": true }
      }
    }
  }
}
```

Notes:
- Smart model/provider is intentionally not configured in `chat_group_helper2`.
- Smart-stage behavior is fully delegated to `chat_group_helper` and its own config.

## 6. Prompt/Codex Files
- `agent_files/chat_group_helper2.md`
  - strict contract: output only `__REPLY__` or `__NO_REPLY__`.
  - no explanations, no extra text.
- Smart-stage codex remains in existing `chat_group_helper` instruction files.

## 7. Runtime Flow
1. Adapter sends message/context to `chat_group_helper2`.
2. Gate stage calls FAST model:
   - output `__NO_REPLY__` -> return `skip_send=true`.
   - output `__REPLY__` -> continue.
3. Smart stage:
  - forward to `chat_group_helper`.
4. If smart stage returns `__NO_REPLY__`, return skip-send.
5. Else return final response text.

## 8. Error/Overload Behavior
Given overload is normal:
- fast stage failures should produce concise, classified operational errors (`timeout`, `connection`, `rate limit`) without traceback spam;
- default-safe behavior for gate failure should be configurable:
  - `fail_open` (forward to smart stage), or
  - `fail_closed` (skip reply).

Recommended default for groups: `fail_closed` to avoid noisy false-positive replies during degradation.

## 9. Why this is optimal
This proposal preserves existing stable behavior while adding inexpensive front filtering.
It avoids duplicating the smart agent stack, keeps maintenance low, and allows future migration to fully direct dual-model mode if needed.

## 10. Minimal Implementation Plan
1. Add new agent `chat_group_helper2` with gate logic only.
2. Add configurable model override support for per-agent stage model selection.
3. Add `.env.example` keys and config examples.
4. Add gate codex file and tests:
   - gate outputs only allowed tokens;
   - delegate path parity with existing helper;
   - no-reply and timeout behavior.
5. Roll out with `delegate_agent=chat_group_helper`.
