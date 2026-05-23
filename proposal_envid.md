# Config-Defined Environment Runtime Specification

## Status

Proposed extension to the current configuration and runtime model.

## Goal

Allow one adapter-agent runtime to serve multiple isolated operating environments identified by `envid`.

Each environment may redefine:
- agent behavior;
- system prompt and instruction files;
- memory namespaces and retention scope;
- RAG corpus visibility;
- adapter defaults and routing behavior;
- any other configuration branch that is safe to override.

Typical example: one Telegram bot serves several chats or groups, and each group must use its own agent defaults, corpora, procedural memory, and markdown instruction set.

Hard requirement: no environment identifiers may be hardcoded in application code. All available `envid` values must be declared only in configuration.

## Assessment Of The Proposed Approach

The proposed `envids.items.<envid>` model is a good fit for this project.

Reasons:
- the project already uses one global config tree plus local per-agent overrides;
- adapters already have a routing step where environment selection can be added;
- memory and RAG already have explicit namespaces and filters, so `envid` can become an additional isolation key;
- the feature is configuration-driven and does not require cloning adapters or agents.

The main condition is that `envid` must be resolved once from configuration and then propagated through runtime as a first-class field. It should not be implemented as scattered ad hoc lookups in unrelated modules.

## Recommended Model

Use `envids` as the top-level configuration section.

All environment identifiers must come only from `envids.items`.

Application code must not:
- declare built-in `envid` values;
- assume existence of `default`, `main`, `telegram` or any other special environment id;
- branch on specific environment names.

Use this structure:

```json5
{
  "envids": {
    "enabled": true,
    "startup_order": ["env1", "env2"],
    "strict_matching": false,
    "items": {
      "env1": {
        "title": "Telegram group environment 1",
        "matching": {
          "adapters": {
            "telegram": {
              "chat_ids": [-1001001001001],
              "chat_usernames": [],
              "chat_types": ["group", "supergroup"]
            }
          }
        },
        "runtime": {
          "enabled": true
        },
        "config": {
          "agents": {
            "items": {
              "another_agent": {
                "instruction_files": [
                  "${AIBT_ROOT}/agent_files/common.md",
                  "${AIBT_ROOT}/agent_files/grouphelper.md"
                ],
                "rag": {
                  "corpora": ["shared", "grouphelper"]
                }
              }
            }
          },
          "adapters": {
            "items": {
              "telegram": {
                "default_agent": "another_agent"
              }
            }
          },
          "memory": {
            "path": "memory/runtime/env1"
          }
        }
      },
      "env2": {
        "title": "Telegram group environment 2",
        "matching": {
          "adapters": {
            "telegram": {
              "chat_ids": [-1002002002002],
              "chat_usernames": [],
              "chat_types": ["group", "supergroup"]
            }
          }
        },
        "runtime": {
          "enabled": true
        },
        "config": {
          "adapters": {
            "items": {
              "telegram": {
                "default_agent": "echo"
              }
            }
          }
        }
      }
    }
  }
}
```

## Mandatory Runtime Rule

Creating a new agent behavior variant must require only one configuration action:

Add a new section under `envids.items`.

That section may define:
- matching rules;
- config overrides for adapters and agents;
- instruction markdown files;
- memory and RAG settings;
- any other supported overrides.

No Python code changes should be required to add a new environment.

Also

1. no environment ids are embedded in code
  The runtime discovers all available environments from configuration at startup.

2. startup builds the environment registry once
  Adapters and agents then work only with resolved config and resolved `envid`.

3. `runtime` is separated from `config`
   Matching and storage isolation are runtime semantics, not plain config overlay.

4. matching rules should stay declarative
   Adapters may support different selectors later; the config should describe matching, not embed adapter-specific logic everywhere.

## Terms

- `envid`: stable environment identifier loaded from configuration.
- base config: config loaded from `config.json5` before any environment override is applied.
- effective config: result of applying the selected environment overlay to the base config.
- environment match: deterministic process that resolves one `envid` for an incoming event or runtime entrypoint.
- environment registry: in-memory list/map created at application startup from `envids.items`.

## Required Behavior

### 1. Environment Resolution

The system must resolve exactly one `envid` for each incoming runtime entry.

At application startup:
1. load `envids.items` from config;
2. build the environment registry from those keys only;
3. preserve declared item order or explicit `startup_order` for deterministic matching; (just follow the definition order if startup_order is not defined)
4. do not add implicit environments in code.

At adapter startup:
1. iterate configured environments in deterministic order or using `startup_order` defined for the adapter if exists;
2. apply `matching` rules sequentially for that adapter;
3. when rules match, activate that `envid` for the corresponding incoming event or adapter-bound context;
4. use the matched environment's merged config for adapter and agent startup/runtime.

Resolution order:
1. explicit `envid` passed by caller when such override is allowed by the adapter contract;
2. first matching configured environment in registry order;
3. request rejection when no environment matches and `strict_matching=true`;
4. adapter-level fallback to unmodified base config only when `strict_matching=false` and such fallback is explicitly enabled.

The selected `envid` must be attached to runtime context and preserved until task completion.

The code must never resolve environment by checking literal names such as `if envid == "env1"`.

### 2. Effective Config Construction

The system must build an effective config as:

`effective_config = deep_merge(base_config, envids.items[envid].config)`

Rules:
- merge dictionaries recursively;
- lists are replaced entirely unless a future explicit merge strategy is introduced;
- scalar values fully override base values;
- unknown keys are allowed unless validation rejects them;
- `envids` section itself is never merged into a child effective config as runtime business data.

The merge must be deterministic and side-effect free.

This merge procedure must be the only supported mechanism for environment-specific behavior changes.

### 2.1 Core-Owned Merge Procedure

Config overlay logic must live in the core runtime layer.

This requirement is mandatory because both adapters and agents use the same merge semantics.

Rules:
- one shared deep-merge implementation in core;
- no local merge implementations inside concrete adapter or agent classes;
- no duplicated merge helpers in feature modules;
- identical precedence rules for all component types.

Recommended core helper contract:
- `deep_merge_config(base: dict, overlay: dict) -> dict`
- `assemble_component_config(component_type: str, component_id: str, envid: str | None, root_config: dict, local_config: dict | None) -> dict`

`component_type` must support at least `agent` and `adapter`.

### 3. Adapter Integration

Adapters must be able to choose environment before agent selection.

For Telegram, matching should support at minimum:
- `chat_id`;
- `chat_username`;
- `chat_type` such as `private`, `group`, `supergroup`;
- optional direct adapter binding for one adapter instance.

After `envid` is resolved, adapter behavior must use the effective config of that environment, including overridden `default_agent`.

If the adapter supports long-lived startup initialization, it must initialize its runtime against the merged environment config selected by matching rules rather than against hardcoded environment assumptions.

### 3.1 Adapter Config Assembly Pipeline (Shared With Agents)

Adapter config must be assembled by the same core overlay procedure used for agents.

For adapter `<adapter_id>`, apply the same precedence model:
1. optional adapter-local config layer if defined;
2. main application `config.json5` adapter section (`adapters.items.<adapter_id>`);
3. environment overlay section (`envids.items.<envid>.config.adapters.items.<adapter_id>`).

Adapter classes must consume already assembled final config and must not implement custom overlay behavior.

### 4. Agent Integration

Agents must receive both:
- the effective config;
- a runtime context containing `envid`.

Important architectural rule:
- overlay logic is global runtime infrastructure;
- agent implementations must not contain internal `envid` overlay logic;
- agents must not read `envids` section directly;
- agents receive an already prepared final config object.

The environment may override per-agent settings under:

`envids.items.<envid>.config.agents.items.<agent_id>`

This includes:
- model selection;
- corpus allowlist;
- memory behavior;
- instruction files;
- agent-local feature switches.

### 4.1 Agent Config Assembly Pipeline (Global Overlay Logic)

Agent config must be assembled by one shared runtime procedure used by all systems.

The agent itself must not perform this assembly.

For agent `<agent_id>`, build config in this exact order:

1. Agent local config file layer
  Load agent local file `config.json5` from the agent directory.
  Treat its payload as if it was mounted under:

  `agents.items.<agent_id>.*`

  Example:
  local agent file payload:

  ```json5
  { "xxx": 1 }
  ```

  becomes runtime layer:

  ```json5
  { "agents": { "items": { "<agent_id>": { "xxx": 1 } } } }
  ```

2. Main application config layer
  Merge base root `config.json5` on top of layer 1.
  This includes `agents.items.<agent_id>` from the main config.

3. Environment overlay layer
  Merge selected environment section on top of layer 2:

  `envids.items.<envid>.config.agents.items.<agent_id>`

Final rule:
- last layer wins according to common deep-merge semantics;
- this same merge mechanism is used for all adapters and agents;
- adding a new behavior variant must require only adding/changing `envids.items.<envid>` configuration.

Recommended helper contract:
- `assemble_agent_config(agent_id, envid, root_config, agent_local_config) -> dict`

`assemble_agent_config` should return the final resolved section for one agent (equivalent to `agents.items.<agent_id>` after all overlays).

### 4.2 Base Class Ownership Of Config Assembly Interface

Procedures for creating resolved runtime config should be declared in base classes, not repeated in each concrete agent.

Required design rule:
- base runtime layer or base classes define the config-assembly interface;
- concrete agent classes only consume prepared config and implement business behavior;
- concrete agents do not redefine overlay order or merge semantics.

Recommended placement:
- shared config assembly methods in core runtime service and/or base classes (for example, `AgentBase` and `Adapter` integration points);
- concrete classes call these base-level procedures instead of implementing local config assembly.

### 5. Instruction Files

Instruction injection must support ordered file composition.

Recommended resolution order:
1. built-in agent system prompt;
2. optional base instruction files from agent config;
3. optional environment instruction files from env overlay;
4. optional runtime-generated context block.

Rules:
- files must be read from disk during initialization or cached with mtime-aware reload;
- missing file must fail clearly during validation or startup, not silently at inference time;
- file contents must be appended in declared order;
- the final system prompt must be traceable in logs at debug level without exposing secrets.

### 6. Memory Isolation

Memory isolation must include `envid` in namespace construction.

Current project state isolates memory mostly by `agent_id`. That is insufficient for multi-environment operation.

Recommended namespace pattern:

```text
("env", envid, "agent", agent_id, ...)
```

This rule applies to:
- semantic memory;
- episodic memory;
- procedural memory;
- session summaries;
- profile facts;
- LangGraph working namespaces where isolation matters.

If cross-environment sharing is needed, it must be explicit through shared corpus access or explicitly shared namespace policy, never by omission of `envid`.

### 7. RAG Isolation

Environment overlay must be able to redefine visible corpora per agent.

Effective corpus allowlist should be resolved from effective agent config after env merge.

This is preferable to storing corpora under separate env-only global switches because the project already models retrieval visibility at agent level.

### 8. Observability

All major logs should include `envid` when available.

At minimum log:
- environment resolution result;
- matching source;
- selected agent after env merge;
- memory namespace root;
- rejected requests due to missing or ambiguous environment.

### 9. Validation

`doctor.py` should validate:
- unique environment ids;
- all runtime environments are declared only in `envids.items`;
- valid match rule shape;
- deterministic matching order;
- no ambiguous automatic matches for the same adapter event when detectable statically;
- referenced instruction files exist;
- referenced agents exist after env overrides;
- corpora referenced by env-aware agent config are syntactically valid.

## Recommended Runtime API Changes

Introduce explicit runtime structures instead of passing plain loose dicts everywhere.

Minimum target shape:

```python
{
    "envid": "env1",
    "adapter": "telegram",
    "user_id": "12345",
    "chat_id": "-100123456",
    "task_id": "..."
}
```

Recommended internal helpers:
- `resolve_env(config, adapter_name, event_context) -> envid`
- `build_effective_config(config, envid) -> dict`
- `build_runtime_context(..., envid=...) -> dict`
- `load_environment_registry(config) -> OrderedDict[str, dict]`
- `assemble_component_config(component_type, component_id, envid, root_config, local_config) -> dict`

## Non-Goals

This feature should not:
- create separate Python adapter classes per environment;
- fork agent implementations only to change prompts or corpora;
- duplicate full config trees outside the merge model;
- silently share memory between environments.

## Migration Strategy

Introduce the feature in this order:

1. Add config schema and validation for `envids`.
2. Implement startup registry loading from `envids.items`.
3. Implement pure helper functions for environment resolution and effective config merge.
4. Move merge implementation to core and expose shared component-config assembly helpers.
5. Wire base classes to consume shared config assembly interfaces.
6. Integrate Telegram environment matching.
7. Propagate `envid` into orchestrator task context.
8. Update agent initialization and memory namespace builders to use `envid`.
9. Add instruction file loading support.
10. Add debug/status endpoints to show resolved environment and effective agent settings.

## Compatibility Notes

- If `envids.enabled` is false or `envids` section is absent, current behavior must stay unchanged.
- Existing single-environment installations should continue using base config without any implicit built-in environment id.
- Existing config keys under `agents`, `memory`, and `adapters` remain the base layer.

## Final Recommendation

The proposed direction is good and should be adopted.

The main refinement is this:

Do not implement environments as only a config subsection with ad hoc consumers. Implement them as a formal runtime concept with:
- deterministic `envid` resolution;
- startup loading of all environments strictly from `envids.items`;
- deep-merged effective config;
- mandatory memory namespace isolation by `envid`;
- ordered instruction file composition.

That version matches the current project architecture and scales cleanly to multiple Telegram groups, multiple adapters, and future per-customer or per-domain deployments.