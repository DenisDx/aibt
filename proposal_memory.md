# Memory Architecture Proposal

Use a dedicated `src/memory/` module with a single public facade for agents and adapters.

Recommended implementation:
- `LangGraph checkpointer` for thread state and message history (`working memory`).
- `LangGraph Store` on PostgreSQL for durable namespaces.
- `LangMem` for extraction, write-back, consolidation, and recall tools.

use memory directory in the project root for store all kinds for memory data;
BUT 
documents are stored in the external storages described in the congig.json5 file
all the indexes and internal data are in the ./memory directory

Namespaces: (data to be stored in the PROJECT_ROOT/memory/)
- `agent/{agent_id}/semantic`
- `agent/{agent_id}/episodic`
- `agent/{agent_id}/procedural`
- `agent/{agent_id}/summaries`
- `agent/{agent_id}/profiles/{user_or_chat_id}` for user/channel facts when needed.

Behavior:
- New raw events first go to episodic memory.
- LangMem extracts durable facts into semantic memory only when confidence is high enough.
- Procedural memory stores stable agent instructions, preferences, and reusable execution rules.
- Working memory stays bounded in the checkpointer; old threads are summarized into `summaries`.

Cron compression jobs:
1. summarize inactive threads into session summaries;
2. merge near-duplicate semantic facts;
3. promote useful episodic items into semantic memory;
4. demote/archive low-value episodic items.

Minimal agent tools:
- `recall_memory(query, scope, limit)`
- `remember_fact(text, scope, importance)`
- `record_episode(text, task_id, outcome)`
- `get_procedural_memory(limit)`
- `update_procedural_memory(text, reason)`

Storage rule:
- the module owns all schemas and APIs; agents never read database tables directly.


====================


Implement document memory as an isolated `src/memory/rag/` module with three layers.

Layers:
1. Raw document store
   - immutable source files under `data/documents/<corpus_id>/<doc_id>/<version>/`;
   - agents can request the original document text or metadata through API tools, not through direct file paths.
2. Registry
   - PostgreSQL tables for `corpora`, `documents`, `document_versions`, `chunks`, `chunk_links`, `ingest_jobs`.
3. Retrieval index
   - dense vectors in `pgvector`;
   - lexical index in PostgreSQL full-text search (`tsvector`) or BM25-compatible index;
   - hybrid ranking by reciprocal-rank fusion of dense and sparse results.

Why this is optimal here:
- one PostgreSQL container already exists in the project;
- `pgvector` keeps dense retrieval inside the same durable system;
- PostgreSQL metadata + FTS reduces extra infrastructure;
- the raw store remains the source of truth and can be rebuilt safely.

Corpora:
- support many corpora via `corpus_id`;
- each agent config lists allowed corpora, for example `agents.items.<agent_id>.rag.corpora`;
- one shared corpus and several private corpora can coexist.

Ingestion pipeline:
1. receive document or external source reference;
2. save immutable raw copy;
3. extract text and metadata;
4. chunk by document-aware strategy;
5. compute embeddings;
6. build/update lexical index;
7. write corpus summary cards for fast agent startup.

Important startup rule:
- the agent must not load the whole corpus into prompt context;
- instead it receives compact corpus summary cards and uses hybrid retrieval for recall.

** loading work **:
Processing of new documents should be performed when the system is idle; that is, launched on cron, with a limit on the amount of work per launch

## Agent Tool Surface For Memory And Documents

Recommended tool set for agents:

Document/RAG tools:
- `search_docs(query, corpora=None, filters=None, limit=8)`
- `get_document(doc_id, version=None, mode="source|text|summary")`
- `list_corpora()`
- `list_documents(corpus_id, filter=None, limit=50)`
- `ingest_document(source, corpus_id, title=None, tags=None)`
- `delete_document(doc_id)` only for privileged/system agents

Memory tools:
- `recall_memory(query, scope=None, limit=8)`
- `remember_fact(text, scope=None, importance=0.5)`
- `record_episode(text, task_id=None, outcome=None)`
- `get_session_summary(thread_id)`
- `get_agent_profile(agent_id)`

Implementation notes:
- every tool calls the memory facade, not storage-specific code;
- every result returns ids, scores, source metadata, and short snippets;
- write tools must log provenance (`who`, `when`, `why`, `source`).

## Recommended Initial Module Layout
```markdown
src/memory/
  api.py              # public facade used by agents/adapters
  schemas.py          # typed models for memory and documents
  store.py            # PostgreSQL connection and repositories
  langmem_manager.py  # LangMem extraction/consolidation policies
  cron_tasks.py       # compression and cleanup jobs
  tools.py            # agent tool wrappers
  rag/
    ingest.py
    retrieve.py
    parsers.py
    summaries.py
```

## Minimal Phase Plan
Phase A:
- PostgreSQL schemas, pgvector, raw document store, `search_docs/get_document/ingest_document`.

Phase B:
- LangGraph checkpointer + LangGraph Store + LangMem memory tools.

Phase C:
- cron summarization, semantic consolidation, episodic promotion/demotion.

Phase D:
- WebUI memory inspection for corpora, documents, summaries, and agent memory namespaces.


## Important notes
1. ALL python code works inside venv
2. All settings in the file config.json5 file, memory section.