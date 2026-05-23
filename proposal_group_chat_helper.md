
## Chat Group Helper Agent Specification

### Purpose

`chat_group_helper` is an assistant agent for group chats.

### Configuration Source

Base agent config is defined in (IT IS A STANDARD FOR ALL AGENTS):

`agents.items.chat_group_helper`

agents settings:
#### instruction_files:
    `agents.items.chat_group_helper.instruction_files.`
    md file with behavior description, goes to the 'system' section of the mesages context
    default value (to be set in ./src/agents/chat_group_helper/config.json5) : "${AIBT_ROOT}/agent_files/chat_group_helper.md"
#### reply_policy:    
    described below


### Required Capabilities

1. Message reaction decision
   On each incoming group message, the agent must decide whether to reply.

   Default expectation:
   - do not reply to most messages;
   - prefer replying when directly addressed, mentioned, or clearly asked for help;
   - allow policy tuning by config.

   Recommended config fields:

   ```json5
   {
     "agents": {
       "items": {
         "chat_group_helper": {
           "reply_policy": {
             "default_mode": "mentioned_or_addressed",
             "mention_names": ["helper", "assistant"],
             "cooldown_sec": 5,
             "max_unsolicited_replies_per_hour": 6
           }
         }
       }
     }
   }
   ```

2. Group history ingest on join
   When the bot is added to a group, it must load available group history and store it in memory for future context.

  Requirements:
   - ingest history as episodic memory with source metadata;
   - run chunking/indexing for long history fragments into RAG where needed;
   - deduplicate by message id and timestamp to avoid repeated imports;
  - mark ingest provenance (`adapter=telegram`, `chat_id`, `import_type=initial_join`).

3. Full memory stack usage
   The agent must work with all project memory channels:
   - own episodic memory;
   - semantic and procedural memory;
   - session summaries;
   - RAG corpora search and retrieval.

    All memory access must be scoped per chat and runtime context to avoid cross-group leakage.

4. Participant dossier management
   The agent must maintain participant profiles in memory.

   Dossier should include at minimum:
   - stable user identifiers;
   - observed preferences and topics;
   - interaction style hints;
   - notable constraints or recurring requests.

   Profile records must be stored in scoped namespaces, for example:

   ```text
  ("agent", "chat_group_helper", "profiles", chat_id, user_id)
   ```

### Runtime Context Requirements For This Agent

For each processed message, runtime context should include at minimum:

```python
{
    "adapter": "telegram",
    "chat_id": "...",
    "chat_type": "group",
    "message_id": "...",
    "user_id": "...",
    "username": "...",
    "mentioned": True,
    "direct_address": False,
    "task_id": "..."
}
```

### Minimal Decision Flow

For each incoming group message:
1. evaluate reply policy (`reply` or `ignore`);
2. if `ignore`, still optionally store compact episodic signal;
3. if `reply`, run memory+RAG enriched response generation;
4. update participant dossier from new evidence;
5. persist trace with `chat_id` and decision reason.

### Non-Functional Constraints

- The agent must remain mostly non-intrusive in active group chats.
- Reply policy thresholds must be fully configurable via `agents.items.chat_group_helper`.
