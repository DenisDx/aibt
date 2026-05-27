# How to Enable All Memory Types for `envid-telegram-bot2`

This guide enables all memory channels for Telegram bot traffic routed to `envid-telegram-bot2`:

- Semantic memory
- Episodic memory
- Procedural memory
- Summaries
- Profiles
- RAG document memory

## 1. Enable global memory block in `config.json5`

If your root config does not already contain a top-level `memory` section, add it.

```json5
"memory": {
  "enabled": true,
  "path": "memory/runtime",
  "document_storages": {
    "items": {
      "default": {
        "type": "filesystem",
        "root": "${AIBT_ROOT}/data/documents_external",
        "read_only": false
      }
    }
  },
  "rag": {
    "enabled": true,
    "ingest": {
      "max_jobs_per_tick": 3,
      "chunk_size": 1200,
      "chunk_overlap": 120
    },
    "retrieval": {
      "default_limit": 8,
      "embedding_dim": 256,
      "lexical_weight": 0.45,
      "dense_weight": 0.55,
      "lexical_top_k": 32,
      "dense_top_k": 32
    }
  },
  "langmem": {
    "enabled": true,
    "summary_inactive_after_hours": 24,
    "archive_after_hours": 72,
    "retain_recent_episodes": 150,
    "min_semantic_importance": 0.7,
    "max_semantic_promotions_per_tick": 8,
    "max_archives_per_tick": 8
  }
}
```

Notes:
- `rag.enabled=true` enables document search memory.
- `langmem.enabled=true` enables periodic summaries/semantic promotions/archives.
- `memory.enabled=true` is required for all memory APIs.

## 2. Add memory override inside `envid-telegram-bot2`

In `config.json5`, under:

`envids.items.envid-telegram-bot2.config`

add/merge this block:

```json5
"memory": {
  "enabled": true,
  "rag": {
    "enabled": true
  },
  "langmem": {
    "enabled": true,
    "summary_inactive_after_hours": 12,
    "archive_after_hours": 48,
    "retain_recent_episodes": 200,
    "min_semantic_importance": 0.65,
    "max_semantic_promotions_per_tick": 12,
    "max_archives_per_tick": 12
  }
}
```

This ensures that when Telegram routing resolves to `envid-telegram-bot2`, memory and maintenance settings are applied from this overlay.

## 3. Ensure target agents are enabled in this envid

Under the same `envid-telegram-bot2.config`, keep both helper agents enabled:

```json5
"agents": {
  "items": {
    "chat_group_helper": {
      "enabled": true,
      "rag": {
        "corpora": ["shared", "bot2-private"]
      }
    },
    "chat_group_helper2": {
      "enabled": true,
      "recent_messages_limit": 80,
      "rag": {
        "corpora": ["shared", "bot2-private"]
      }
    }
  }
}
```

Why this matters:
- `chat_group_helper2` delegates to `chat_group_helper`, so both should be enabled.
- Per-agent `rag.corpora` constrains which corpora are searchable in this envid.

## 4. Keep Telegram routing for this envid explicit

For your current setup, keep `chat_ids` mapping inside:

```json5
"envids": {
  "items": {
    "envid-telegram-bot2": {
      "matching": {
        "adapters": {
          "telegram": {
            "chat_ids": [-5090882532]
          }
        }
      },
      "runtime": { "enabled": true },
      "config": {
        "adapters": {
          "items": {
            "telegram": {
              "default_agent": "chat_group_helper2"
            }
          }
        }
      }
    }
  }
}
```

## 5. Create required directories

Most runtime memory directories are auto-created, but create these explicitly once:

```bash
mkdir -p data/documents_external
mkdir -p memory/runtime
mkdir -p memory/runtime/corpora
mkdir -p memory/runtime/env
```

After first traffic + cron runs, the system will populate subpaths like:

- `memory/runtime/env/envid-telegram-bot2/agent/<agent_id>/...`
- `memory/runtime/corpora/...`

## 6. Add files/documents for RAG memory

Put source documents into:

- `data/documents_external/`

Then ingest them through WebUI Memory page (recommended), or via memory ingest API.

Suggested corpora:

- `shared`
- `bot2-private`

## 7. Restart service and validate config

```bash
systemctl --user restart aibt
./venv/bin/python src/core/doctor.py
```

`doctor.py` should report valid `envids` and no memory config errors.

## 8. Trigger all memory types in practice

To populate all memory namespaces for `envid-telegram-bot2`:

1. Send Telegram messages in the mapped chat (`chat_id=-5090882532`) to create episodic memory.
2. Use repeated stable facts/preferences in conversation to create semantic/profile entries.
3. Keep bot active so helper agents record decisions/episodes.
4. Wait for cron cycle (`src/core/cron.py`) to run `memory.cron_tasks.run_memory_cron` and generate summaries/semantic promotions.

## 9. Verify data by namespace

Use WebUI Memory namespace browser and check for agent scopes in `envid-telegram-bot2`:

- `semantic`
- `episodic`
- `procedural`
- `summaries`
- `profiles`

Also check logs for periodic maintenance messages:

- `logs/memory.log`
- `logs/cron.log`

Look for lines containing:

- `langmem maintenance summaries=... semantic_promotions=... archives=...`

## 10. Optional seed files (advanced)

Not required, but possible for bootstrap/migration workflows.

Legacy JSONL namespace files can be placed under:

`memory/runtime/env/<envid>/agent/<agent_id>/`

Examples:

- `semantic.jsonl`
- `episodic.jsonl`
- `procedural.jsonl`
- `summaries.jsonl`
- `profiles_<profile_id>.jsonl`

Each line must be a valid JSON object.

---

## Minimal patch checklist for your current config

1. Add top-level `memory` block (if absent).
2. In `envid-telegram-bot2.config`, add `memory` override with `enabled=true`, `rag.enabled=true`, `langmem.enabled=true`.
3. In `envid-telegram-bot2.config.agents.items`, enable both `chat_group_helper` and `chat_group_helper2` and set `rag.corpora`.
4. Ensure `default_agent` under `envid-telegram-bot2` points to `chat_group_helper2`.
5. Create `data/documents_external/`, restart service, run doctor, ingest docs, verify namespaces in WebUI.
