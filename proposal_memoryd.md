# Memoryd Enhancement Specification

## Goal

Introduce memoryd as an alternative long-term memory subsystem that can run alongside the existing memory module.

Memoryd must:
- be an autonomous module with minimal dependencies and a clear portability path to other projects with similar config structure;
- store data in PostgreSQL;
- provide synchronous memory retrieval for prompt context building;
- provide asynchronous memory updates through a queue and background processing;
- isolate memory by MUID namespace, with optional default MUID fallback.

## Scope

In scope:
- memoryd configuration model;
- runtime lifecycle in core startup and cron;
- queue-driven async update pipeline;
- LLM contract for grouped memory updates;
- retention enforcement by record count and total content size;
- WebUI extension with a dedicated memoryd tab.

Out of scope:
- replacing existing memory module immediately;
- full migration tooling from existing memory data;
- multi-node distributed queue.

## Terms

- MUID: memory namespace identifier.
- Type: logical memory type (semantic, episodic, todo, procedural, summary, profiles, news, etc).
- Memory record: one stored memory unit inside one MUID and one type.
- Source context: user/assistant conversation context before system/tool injection.
- Update task: async job that converts source context + final response into record mutations.

## High-Level Architecture

1. Core startup initializes memoryd service if enabled.
2. Orchestrator/agent flow:
   - before final LLM call: retrieve memory context fragment via memoryd;
   - after final response: enqueue update task with source context, final response, and MUID.
3. Cron/background worker:
   - fetch pending tasks;
   - execute up to max_sim_task in parallel;
   - run LLM extraction/update requests;
   - apply DB mutations;
   - enforce retention limits.
4. Immediate enqueue trigger:
  - after a task is enqueued, worker performs an immediate dispatch attempt;
  - this trigger is additional to periodic cron and post-completion dispatch.

## Integration Model

Memoryd is additive and can coexist with current memory:
- existing memory APIs remain unchanged;
- memoryd has its own facade and storage schema;
- memory and memoryd are enabled independently; if both are enabled, both are used in the same request flow.

Effective configuration note:
- enablement checks use effective config after envid overlay/override is applied.

Type activation rules:
- memory subsystem is active only when memory.enabled is true;
- inside memory, a type is active only when memory.<type>.enabled is true in effective config;
- if memory.enabled is true, only enabled memory types are connected.

- memoryd subsystem is active only when memoryd.enabled is true;
- inside memoryd, a type is active only when both conditions are true:
  - memoryd.items.<type> exists;
  - memoryd.items.<type>.enabled is true in effective config;
- if memoryd.enabled is true, only such enabled and defined memoryd item types are connected.

Example (memoryd semantic type is connected only when all are true):
1. memoryd.enabled is true;
2. memoryd.items.semantic exists;
3. memoryd.items.semantic.enabled is true.

## Runtime Components

### core starter

Responsibilities:
- read and validate memoryd config block;
- initialize memoryd module and DB schema;
- expose shared singleton service to orchestrator, adapters, and cron.

Input: root_dir, full app config.
Output: initialized memoryd runtime service.

### Orchestrator/Agent: read stage

Call after base conversation context is assembled and before system/tools/memory injection.

Function: get_memoryd_context(...)
use named arguments 
- Input:
  - muid: string | null
  - types: optional list[string]
  - limit per type (optional)
  - rendering mode (text/markdown; md by default)
- Output:
  - text fragment to append to model context
  - metadata (selected records count, truncation flags)

If muid is null, use memoryd.muid from effective config.
If types is null, use all allowed types

Agent-level attach policy:
- each agent may define `memoryd.context_types` in its agent config;
- this list controls which memoryd types are attached into prompt context;
- if `memoryd.context_types` is missing, default is all enabled memoryd item types in effective config;
- effective context attach set is: `context_types ∩ enabled(memoryd.items)`.

### Orchestrator/Agent: write stage

Call after final assistant response is produced.

Function: enqueue_memoryd_update(...)
- Input:
  - source_context (user/assistant only, no system/tool sections); required; not named
  - final_response; required; not named
  - muid: string | null (named, not required)
  - caller_tag: string | null (named, not required, default null)
  - optional type set requested for this task
- Output:
  - {ok, task_id} on success
  - {ok=false, error} on failure

Queue dedup/cancel behavior is config-driven and scoped by MUID + caller_tag.
Replacement/cancellation is allowed only when both MUID and caller_tag are equal.
Special case: caller_tag null is treated as unique per task, so two tasks with null caller_tag are different and do not replace each other.

Why caller_tag is needed (short):
- one MUID can be used by different agents/components;
- different caller_tag values prevent cross-agent queue displacement;
- for high-frequency chat updates, the same caller_tag can safely replace stale pending tasks before execution starts.

Agent-level update policy:
- each agent may define `memoryd.update_types` in its agent config;
- this list controls which memoryd types are sent to `enqueue_memoryd_update(...)` and matched in `memoryd.requests`;
- if `memoryd.update_types` is missing, default is all enabled auto-writable memoryd item types;
- `manual_only=true` and `external_writer=true` item types are excluded from auto update set;
- effective update set is: `update_types ∩ enabled(memoryd.items) ∩ auto_writable`.

Important consistency note:
- `context_types` and `update_types` are intentionally independent;
- `context_types` may be wider than `update_types` (for example, include manual/external types that are readable but not auto-updated).

### Cron/worker stage

Function: run_memoryd_tick(...)
- Input: root_dir, full app config
- Output: counters {picked, started, done, failed, pruned}

Actions:
0. Enabled-gate rule for worker trigger:
  - worker dispatch must not be blocked only because base `memoryd.enabled=false`;
  - if pending tasks exist, worker must process them;
  - rationale: tasks may be enqueued from request flows where memoryd was enabled by effective envid overlay.
1. Check execution capacity.
  - For provider=openaix, queue-state check is mandatory before starting each task.
  - Endpoint: /v1/providers/{provider}/models/{model}/queue-state
  - Pass priority=memoryd.memory_task_prio (or effective task priority).
  - Start task only when queue-state.can_run_now is true; otherwise keep task pending.
  - If queue-state endpoint is unavailable or returns non-2xx, do not start task and keep it pending for next tick.
2. Start up to max_sim_task tasks.
3. For each finished task, apply retention constraints.
4. If capacity remains and queue is not empty, continue dispatch.

Dispatch timing rule:
- dispatch is triggered not only by periodic cron tick, but also immediately on task completion;
- as soon as one running task finishes and a slot is free, worker may start the next eligible pending task in the same processing cycle;
- do not wait for the next cron tick to refill capacity.
- pending queue presence has priority over base enabled-flag for cron dispatch decision.
- after each enqueue, worker must perform one immediate dispatch attempt for pending queue.

## Configuration Specification

Add top-level config section memoryd:

Important note about types:
- memory types are dynamic and user-defined;
- `items` and `requests[*].types` are not a fixed built-in enum;
- the JSON below shows only an example of a possible type set.

```json5
{
  "agents": {
    "items": {
      "chat_group_helper": {
        "memoryd": {
          "context_types": ["semantic", "episodic", "summaries", "profiles"],
          "update_types": ["semantic", "episodic", "summaries", "profiles"]
        }
      }
    }
  },
  "logging": {
    "levels": {
      "memoryd": "info"
    }
  },
  "memoryd": {
    "enabled": true,
    "provider": "default",
    "model": "${LLM_MODEL:-gpt-4o-mini}",
    "max_sim_task": 1,
    "memory_task_prio": 8,
    "muid": "default",
    "queue": {
      "cancel_policy": "cancel_previous_same_muid", // cancel_previous_same_muid | keep_all | replace_if_pending
      "max_pending": 10000
    },
    "requests": [
      {
        "types": ["semantic", "episodic", "todo", "summary"],
        "request_file": "${AIBT_ROOT}/agent_files/memoryd_semantic_episodic_todo_summary.md"
      },
      {
        "types": ["semantic", "episodic", "profiles"],
        "request_file": "${AIBT_ROOT}/agent_files/memoryd_semantic_episodic_profiles.md"
      }
    ],
    "items": {
      "semantic": {
        "request_file": "${AIBT_ROOT}/agent_files/memoryd_semantic.md",
        "max_record_count": 50,
        "max_content_length": 8196,
        "enabled": true
      },
      "episodic": {
        "max_record_count": 30,
        "max_content_length": 8196,
        "enabled": true
      },
      "todo": {
        "max_record_count": 30,
        "max_content_length": 8196,
        "enabled": true
      },
      "procedural": {
        "enabled": true,
        "manual_only": true
      },
      "summary": {
        "max_record_count": 1,
        "enabled": true
      },
      "profiles": {
        "max_record_count": 50,
        "max_content_length": 8196,
        "enabled": true
      },
      "news": {
        "enabled": true,
        "external_writer": true
      }
    }
  }
}
```

Selection rules:
- v1 (first revision): grouped request matching uses exact set equality only;
- matching is checked against effective enabled type set for current envid;
- if no exact group is found, memory update processing is not executed for this task;
- in that case, worker writes a warning log with envid/muid/caller_tag and requested type set.

Future enhancement (not in v1):
- support subset matching and deterministic partitioning of requested types into multiple groups;
- use maximum absorption strategy (cover full requested set with minimal number of LLM calls);
- tie-breakers must be deterministic (for example: largest group first, then stable lexical order).

## Data Model

Current candidate payload from requirements:

```json
{
  "id": "db primary key",
  "ts": "ISO UTC timestamp",
  "type": "episodic",
  "muid": "namespace id",
  "title": "record title",
  "text": "record body",
  "importance": 7
}
```

Recommended DB-first model:
- memoryd_records
  - id (bigserial pk)
  - muid (text, indexed)
  - type (text, indexed)
  - title (text)
  - body (text)
  - importance (smallint 0..9)
  - tags (jsonb, optional)
  - created_at (timestamptz)
  - updated_at (timestamptz)
- memoryd_tasks
  - task_id (uuid pk)
  - muid (text, indexed)
  - caller_tag (text, nullable, indexed)
  - requested_types (jsonb array)
  - source_context (jsonb or text)
  - final_response (text)
  - status (pending|running|done|failed|canceled|skipped)
  - prio (int)
  - created_at, updated_at, started_at, finished_at
  - retry_count, error

requested_types normalization/storage rule:
- before persistence, collect types into an array, normalize to lowercase, deduplicate, then sort;
- serialize the normalized array with standard JSON serializer and store as json/jsonb array;
- on read, deserialize JSON back to array;
- sorted normalized form is canonical for exact-set matching and deterministic logs/comparisons.

Delete policy for stage 1:
- stage 1 uses hard delete for record removal operations;
- no `is_deleted` flag in stage 1 schema.

V1 DB constraints and indexes (recommended):
- constraints:
  - `importance` check in range 0..9;
  - `status` check to allowed values (`pending|running|done|failed|canceled|skipped`);
  - `retry_count >= 0`;
  - `created_at` and `updated_at` are NOT NULL with default `now()`.
- indexes:
  - `memoryd_records (muid, type, updated_at desc)`;
  - `memoryd_tasks (status, prio desc, created_at)` for dispatch;
  - `memoryd_tasks (muid, caller_tag, status)` for replacement/cancel checks.

## LLM Update Contract

Worker sends to LLM:
- prompt from request_file;
- source context;
- final response;
- current records snapshot for involved MUID/types (bounded by safe limit).

LLM returns JSON array of mutations.

Request message construction rule:
- memoryd instructions from `request_file` must be placed into exactly one `role=system` message;
- the remaining `messages` entries must be built with the same ordinary-request context builder used by chat agents, preserving `role=user` and `role=assistant` exactly;
- do not inject memoryd instructions into `role=user` messages;
- keep the single system message as the only system role in the request.

Accepted mutation format:

```json
[
  {
    "id": 1234,
    "type": "semantic",
    "operation": "UPDATE",
    "title": "new title",
    "text": "new text",
    "importance": 6
  },
  {
    "type": "episodic",
    "operation": "INSERT",
    "title": "weather",
    "text": "user reported rain",
    "importance": 2
  },
  {
    "id": 1234,
    "operation": "DELETE"
  }
]
```

Normalization rules:
- if operation missing and id missing: treat as INSERT (type required);
- if operation missing and id exists:
  - non-empty text => UPDATE
  - empty/missing text => DELETE
- missing importance => default 5
- type may be omitted when at least one condition is true:
  - id is provided and existing record resolves type;
  - task `types` set contains exactly one element (use that single type).

Title-based operations without id:
- UPDATE and DELETE by `title` are allowed when `id` is not provided.
- target type must be known (explicitly provided or resolved by rules above).
- lookup scope is `(muid, type)` with exact title match.
- if exactly one record matches: apply operation to that record.
- if zero records match: cancel this mutation (no-op) and write warning log.
- if more than one record matches (ambiguous title): cancel this mutation and write warning log.



## Retention Enforcement

For each MUID + type:
1. Enforce max_record_count.
2. Enforce max_content_length on total title+body size.

Removal order when over limit:
- lower importance removed first;
- if equal importance, older records removed first.

This enforcement runs after each completed task and may also run as periodic integrity sweep.

max_content_length accounting rule:
- count in Unicode characters (not UTF-8 bytes).
- rationale: memory limit intent is closer to prompt/token budget behavior, and character count is a better practical approximation than byte count.

## MUID Semantics

- Every operation is scoped by MUID.
- Caller may pass explicit MUID.
- If caller passes null, memoryd default MUID is used.
- One agent in one envid may use multiple MUIDs in one request flow.
- MUID is a text identifier (stored as `text` in DB).
- Canonical validation for stage 1:
  - length: 1..128 characters;
  - allowed characters: `a-z`, `0-9`, `_`, `-`, `:`, `.`;
  - no spaces;
  - recommended normalization: trim and lowercase before storage/lookup.
- Example valid values: `envid`, `envid:group_id`, `env-alpha:chat42`.

## Queue Semantics

Task enqueue policy for same MUID + caller_tag:
- configurable cancel/replace strategy;
- cancellation applies to pending tasks; running task cancellation is best-effort (no running tasks cancellation in V1).
- replacement/cancellation matching key is (muid, caller_tag).
- null caller_tag does not match another null caller_tag (null is treated as unique per task).

Dispatch policy:
- periodic cron tick is a trigger for worker processing;
- task completion is an additional immediate trigger for next dispatch when capacity is available.

Recommended default:
- cancel_previous_same_muid for pending tasks with the same caller_tag;
- keep currently running task;
- enqueue new task as latest state.

Example:
- Agent A enqueues with muid=X, caller_tag="agent_a": pending tasks for (X, "agent_a") may be replaced.
- Agent B enqueues with the same muid=X, caller_tag="agent_b": tasks are independent from Agent A.
- Two tasks with muid=X and caller_tag=null: tasks are independent and do not replace each other.

Execution ordering guarantee:
- for the same `muid`, execution order must be preserved (FIFO by enqueue order) regardless of `caller_tag`;
- task dispatch/completion for a later task with the same `muid` must not overtake an earlier one;
- for different `muid` values, execution order may be arbitrary and parallel.

Concurrency conflict handling (same MUID):
- if runtime detects a same-MUID ordering conflict/race, write warning log;
- perform exactly one retry for the affected task;
- if the retry still cannot satisfy ordering, fail/drop task according to V1 error policy.

## API Surface

Suggested service methods:
- memoryd_get_context(muid, types=None, render="markdown", limit_per_type=20)
  - Input: selection params; if `types=None`, include all available enabled types.
  - Output: context text and metadata.
- memoryd_enqueue_update(source_context, final_response, muid=None, caller_tag=None, types=None)
  - Input: conversation and response; queue replacement applies only for matching muid + caller_tag; if `types=None`, use all available enabled types.
  - Output: task receipt.
- memoryd_run_tick(limit=None)
  - Input: optional dispatch limit.
  - Output: processing counters.
- memoryd_list_records(muid, types=None, offset=0, limit=100)
  - Input: filters; if `types=None`, include all available enabled types.
  - Output: record page.
- memoryd_upsert_record(...)
  - Input: manual insert/update payload.
  - Output: saved record.

Naming convention rule:
- object/service methods do not require `memoryd_` prefix;
- free functions and cross-module helpers should keep `memoryd_` prefix for clarity.

## WebUI Requirements

Add dedicated tab: memoryd.

Capabilities:
1. Select envid from known environments.
2. Build/show MUID list for selected envid.
3. Select MUID and optional type filters.
4. Request and display records from DB.
5. Optional actions (phase 2): manual edit/delete/importance update and reprocess trigger.

Backend endpoints (proposed):
- GET /api/memoryd/envids
- GET /api/memoryd/muids?envid=...
- GET /api/memoryd/records?muid=...&types=...
- POST /api/memoryd/tasks/enqueue
- POST /api/memoryd/tasks/run

Access control policy:
- V1: no additional access control checks for memoryd tab and memoryd API operations.
- Future enhancement: role-based/admin ACL may be added later.

## Logging and Observability

Use dedicated memoryd log type with level override in logging.levels.memoryd.
- when `memoryd.log_llm` is enabled, write raw transport payloads to `logs/memoryd_llm.jsonl` exactly as sent to and received from the LLM endpoint;
- do not synthesize or normalize the request/response bodies for that file;
- the request entry must contain the exact HTTP body sent to the endpoint, and the response entry must contain the exact HTTP body received from the endpoint.

Log events:
- task lifecycle transitions;
- LLM request/response parse errors;
- retention pruning summary;
- cron tick summary.

Metrics to expose:
- queue depth;
- tasks by status;
- average task latency;
- pruned records count.

## Error Handling

- Invalid config: fail fast at startup with clear diagnostics.
- Invalid LLM JSON: mark task failed, store parse error, do not mutate records.
- Partial DB mutation failure: rollback transaction per task.
- Missing request_file: mark task skipped or failed by policy.

Retry policy matrix:
- V1: always fail/drop task and write error log (no retries for any error class).
- Future versions:
  - 5xx errors: retries are allowed by retry policy.
  - all other error classes (transport, parse, validation, DB conflict): fail/drop task and write error log.

## Compatibility and Rollout Plan

Phase 1:
- config schema and DB schema;
- enqueue + worker + retention;
- read API for context injection.

Phase 2:
- grouped requests resolver;
- webui memoryd tab read-only;
- coexistence toggles with existing memory.

Phase 3:
- write actions in webui;
- robust retries and dead-letter handling;
- migration helper from memory namespaces to memoryd format (optional).

## Open TBD List

- none.
