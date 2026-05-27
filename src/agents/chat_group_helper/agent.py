"""ChatGroupHelperAgent: non-intrusive helper for Telegram group chats."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.prompts import MessagesPlaceholder
from langchain_core.prompts import ChatPromptTemplate

from agents.base import AgentBase
from agents.llm_factory import build_llm
from core.logging_utils import log


class ChatGroupHelperAgent(AgentBase):
    """Group-focused assistant with reply policy and participant dossier updates."""

    def __init__(self, app_config: dict[str, Any], agent_config: dict[str, Any] | None = None):
        self._last_reply_ts_by_chat: dict[str, float] = {}
        self._unsolicited_reply_ts_by_chat: dict[str, list[float]] = {}
        super().__init__(app_config, agent_config)

    def build_chain(self):
        """Build LLM chain with optional external instruction files."""

        instruction_block = self._load_instruction_block()
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    f"{instruction_block}\n"
                    "Memory context:\n{memory_context}",
                ),
                MessagesPlaceholder("messages"),
            ]
        )
        has_tools, tools_value = self._llm_tools_value()
        if has_tools:
            llm = build_llm(self.app_config, tools=tools_value)
        else:
            llm = build_llm(self.app_config)
        return self.prompt | llm

    async def handle(self, query: str, context=None) -> dict[str, Any]:
        """Let LLM decide whether to reply and persist trace/profile updates."""

        ctx = context or {}
        envid = str(ctx.get("envid", "")).strip() or None
        llm_messages = self._build_llm_messages(query, ctx)
        llm_ctx = {**ctx, "messages": llm_messages}
        result = await super().handle(query, context=llm_ctx)
        text = self._extract_reply_text(result.get("result", ""))

        if self._is_no_reply_output(text):
            decision = {"action": "ignore", "reason": "llm_no_reply", "unsolicited": False}
            self._persist_decision_trace(query, ctx, decision, envid=envid)
            self._update_participant_dossier(query, ctx, decision, envid=envid)
            self.memory_tools.record_episode(
                agent_id=self.name,
                text=f"decision=ignore reason={decision['reason']} chat_id={ctx.get('chat_id')} user_id={ctx.get('user_id')} message_id={ctx.get('message_id')}",
                task_id=str(ctx.get("task_id", "")) or None,
                outcome="ignored",
                envid=envid,
            )
            return {
                "result": "",
                "skip_send": True,
                "decision": decision,
                "memory": result.get("memory", {}),
            }

        decision = {"action": "reply", "reason": "llm_reply", "unsolicited": False}
        self._persist_decision_trace(query, ctx, decision, envid=envid)
        self._update_participant_dossier(query, ctx, decision, envid=envid)
        result["result"] = text
        result["decision"] = decision
        return result

    @staticmethod
    def _extract_reply_text(raw_result: Any) -> str:
        """Normalize model reply text.

        Input: raw chain result (usually string content).
        Output: plain text for Telegram send path.

        If the reply is a JSON object encoded as string, tries to extract
        one of: text, message, content.
        """

        text = str(raw_result or "").strip()
        if not text:
            return ""

        try:
            parsed = json.loads(text)
        except Exception:
            return text

        if not isinstance(parsed, dict):
            return text

        for key in ("text", "message", "content"):
            value = parsed.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                clean = value.strip()
                if clean:
                    return clean
                continue
            clean = str(value).strip()
            if clean:
                return clean

        return text

    @staticmethod
    def _is_no_reply_output(text: str) -> bool:
        """Return true when model explicitly decides to skip this turn."""

        clean = str(text or "").strip()
        return clean == "__NO_REPLY__" or clean == "" or clean.lower() in ("no reply", "no response", "skip", "none", "n/a", "no answer") or clean.lower() in ("timeout", "connection error.") or clean.lower().startswith("error:") or clean.endswith("__NO_REPLY__")

    def _history_messages_limit(self) -> int:
        """Return max number of recent chat messages to include into LLM input."""

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        raw = cfg.get("recent_messages_limit", cfg.get("send_last", 50)) if isinstance(cfg, dict) else 50
        try:
            return max(1, min(200, int(raw)))
        except Exception:
            return 50

    def _build_llm_messages(self, query: str, ctx: dict[str, Any]) -> list[Any]:
        """Build model input from recent history preserving user/assistant roles."""

        recent = ctx.get("recent_messages", [])
        items: list[dict[str, Any]] = []
        if isinstance(recent, list):
            limit = self._history_messages_limit()
            items = [x for x in recent[-limit:] if isinstance(x, dict)]

        if not items:
            items = [
                {
                    "role": "user",
                    "message_id": ctx.get("message_id", ""),
                    "user_id": ctx.get("user_id", "unknown"),
                    "display_name": ctx.get("display_name", ""),
                    "username": ctx.get("username", ""),
                    "text": query,
                }
            ]

        out: list[Any] = []
        for item in items:
            text = str(item.get("text", "") or "").strip()
            if not text:
                continue
            role = str(item.get("role", "user") or "user").strip().lower()
            message_id = str(item.get("message_id", "") or "").strip()
            user_id = str(item.get("user_id", "") or "unknown")
            name = str(item.get("display_name", "") or item.get("username", "") or f"user_{user_id}").strip()
            username = str(item.get("username", "") or "").strip().lstrip("@")
            payload = json.dumps(
                {
                    "message_id": message_id,
                    "user_id": user_id,
                    "name": name,
                    "username": username,
                    "text": text,
                },
                ensure_ascii=False,
            )
            if role == "assistant":
                out.append(AIMessage(content=payload))
            else:
                out.append(
                    HumanMessage(
                        content=payload
                    )
                )
        return out or [HumanMessage(content=query)]

    def _reply_policy(self) -> dict[str, Any]:
        """Return normalized reply policy from config with defaults."""

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        policy = cfg.get("reply_policy", {}) if isinstance(cfg, dict) else {}
        if not isinstance(policy, dict):
            policy = {}
        return {
            "default_mode": str(policy.get("default_mode", "mentioned_or_addressed") or "mentioned_or_addressed"),
            "mention_names": [str(x).strip().lower() for x in policy.get("mention_names", ["helper", "assistant"]) if str(x).strip()],
            "cooldown_sec": max(0, int(policy.get("cooldown_sec", 5))),
            "max_unsolicited_replies_per_hour": max(0, int(policy.get("max_unsolicited_replies_per_hour", 6))),
            "allow_unsolicited": bool(policy.get("allow_unsolicited", True)),
        }

    def _decide_reply(self, query: str, ctx: dict[str, Any]) -> dict[str, Any]:
        """Decide whether to reply based on chat context and policy."""

        policy = self._reply_policy()
        chat_type = str(ctx.get("chat_type", "")).strip().lower()
        chat_id = str(ctx.get("chat_id", "unknown")).strip() or "unknown"
        mode = str(policy.get("default_mode", "mentioned_or_addressed")).strip().lower()

        if chat_type not in ("group", "supergroup"):
            return {"action": "reply", "reason": "non_group_chat", "unsolicited": False}

        text = str(query or "")
        mentioned = bool(ctx.get("mentioned", False))
        direct_address = bool(ctx.get("direct_address", False))
        help_request = self._is_help_request(text)

        if not mentioned and not direct_address:
            lowered = text.lower()
            if any(name and re.search(rf"(^|[\s,:]){re.escape(name)}([\s,:!?]|$)", lowered) for name in policy["mention_names"]):
                direct_address = True

        triggered = mentioned or direct_address or help_request

        if mode in ("never", "disabled", "ignore_all"):
            triggered = False
        elif mode == "always":
            triggered = True
        elif mode == "mentioned_only":
            triggered = mentioned

        now_ts = time.time()
        cooldown = int(policy.get("cooldown_sec", 0))
        last_ts = float(self._last_reply_ts_by_chat.get(chat_id, 0.0))
        if triggered and cooldown > 0 and now_ts - last_ts < cooldown:
            return {
                "action": "ignore",
                "reason": "cooldown",
                "unsolicited": False,
                "cooldown_left_sec": round(cooldown - (now_ts - last_ts), 2),
            }

        if triggered:
            return {
                "action": "reply",
                "reason": "addressed_or_help",
                "unsolicited": not (mentioned or direct_address),
            }

        if not policy.get("allow_unsolicited", True):
            return {"action": "ignore", "reason": "policy_not_triggered", "unsolicited": False}

        # Keep unsolicited responses rare and only for clear questions.
        if not self._looks_like_question(text):
            return {"action": "ignore", "reason": "policy_not_triggered", "unsolicited": False}

        limit = int(policy.get("max_unsolicited_replies_per_hour", 0))
        recent = [ts for ts in self._unsolicited_reply_ts_by_chat.get(chat_id, []) if now_ts - ts < 3600]
        self._unsolicited_reply_ts_by_chat[chat_id] = recent
        if limit > 0 and len(recent) >= limit:
            return {"action": "ignore", "reason": "unsolicited_rate_limit", "unsolicited": False}

        if cooldown > 0 and now_ts - last_ts < cooldown:
            return {
                "action": "ignore",
                "reason": "cooldown",
                "unsolicited": False,
                "cooldown_left_sec": round(cooldown - (now_ts - last_ts), 2),
            }

        return {"action": "reply", "reason": "unsolicited_question", "unsolicited": True}

    def _mark_reply_usage(self, ctx: dict[str, Any], unsolicited: bool) -> None:
        """Track per-chat reply usage for cooldown and unsolicited limits."""

        chat_id = str(ctx.get("chat_id", "unknown")).strip() or "unknown"
        now_ts = time.time()
        self._last_reply_ts_by_chat[chat_id] = now_ts
        if unsolicited:
            history = self._unsolicited_reply_ts_by_chat.get(chat_id, [])
            history.append(now_ts)
            self._unsolicited_reply_ts_by_chat[chat_id] = [ts for ts in history if now_ts - ts < 3600]

    def _persist_decision_trace(
        self,
        query: str,
        ctx: dict[str, Any],
        decision: dict[str, Any],
        envid: str | None,
    ) -> None:
        """Persist lightweight decision trace for observability and debugging."""

        try:
            self.memory_tools.remember_fact(
                agent_id=self.name,
                text=(
                    f"decision={decision.get('action')} reason={decision.get('reason')} "
                    f"adapter={ctx.get('adapter')} chat_id={ctx.get('chat_id')} "
                    f"message_id={ctx.get('message_id')} text={str(query)[:180]}"
                ),
                scope="decision_trace",
                importance=0.4,
                envid=envid,
            )
        except Exception as e:
            log("agents", "warning", f"chat_group_helper trace persist failed: {e}")

    def _update_participant_dossier(
        self,
        query: str,
        ctx: dict[str, Any],
        decision: dict[str, Any],
        envid: str | None,
    ) -> None:
        """Update participant dossier facts in profile namespace."""

        chat_id = str(ctx.get("chat_id", "")).strip()
        user_id = str(ctx.get("user_id", "")).strip()
        if not chat_id or not user_id:
            return

        profile_id = f"{chat_id}:{user_id}"
        username = str(ctx.get("username", "")).strip()
        text = str(query or "")
        interaction_style = "short" if len(text) <= 90 else "long"
        facts = [
            f"stable_ids chat_id={chat_id} user_id={user_id}",
            f"interaction_style={interaction_style} last_decision={decision.get('action')}:{decision.get('reason')}",
        ]
        if username:
            facts.append(f"username={username}")

        pref = self._extract_preference_hint(text)
        if pref:
            facts.append(f"preference_hint={pref}")

        topic = self._extract_topic_hint(text)
        if topic:
            facts.append(f"topic_hint={topic}")

        try:
            for fact in facts:
                self.memory_tools.remember_profile_fact(
                    agent_id=self.name,
                    profile_id=profile_id,
                    text=fact,
                    scope="participant_dossier",
                    importance=0.45,
                    envid=envid,
                )
        except Exception as e:
            log("agents", "warning", f"chat_group_helper dossier update failed: {e}")

    @staticmethod
    def _is_help_request(text: str) -> bool:
        """Detect explicit asks for help in RU/EN with lightweight heuristics."""

        lowered = text.lower()
        keywords = (
            "help",
            "assist",
            "can you",
            "could you",
            "помоги",
            "подскажи",
            "помощ",
        )
        return any(word in lowered for word in keywords)

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        """Detect question-like messages to throttle unsolicited reactions."""

        lowered = text.lower()
        if "?" in lowered:
            return True
        starters = ("how ", "what ", "why ", "when ", "where ", "кто ", "что ", "как ", "почему ")
        return any(lowered.strip().startswith(prefix) for prefix in starters)

    @staticmethod
    def _extract_preference_hint(text: str) -> str | None:
        """Extract simple user preference hints from one message."""

        patterns = (
            r"\bi prefer\s+([^\.,;!?]{3,80})",
            r"\bi like\s+([^\.,;!?]{3,80})",
            r"\bмне нравится\s+([^\.,;!?]{3,80})",
            r"\bпредпочитаю\s+([^\.,;!?]{3,80})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _extract_topic_hint(text: str) -> str | None:
        """Extract a compact topic hint from hashtags or first noun-like token."""

        hash_match = re.search(r"#([A-Za-z0-9_\-]{2,40})", text)
        if hash_match:
            return hash_match.group(1).strip().lower()

        tokens = re.findall(r"[A-Za-zА-Яа-я0-9_\-]{4,40}", text)
        if not tokens:
            return None
        stop = {
            "please",
            "could",
            "would",
            "think",
            "about",
            "пожалуйста",
            "можете",
            "нужно",
        }
        for token in tokens:
            t = token.lower()
            if t not in stop:
                return t
        return None

    def _load_instruction_block(self) -> str:
        """Load optional instruction markdown files and combine them in order."""

        files = self.agent_config.get("instruction_files", []) if isinstance(self.agent_config, dict) else []
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            return ""

        chunks: list[str] = []
        for item in files:
            path = str(item).strip()
            if not path:
                continue
            if not os.path.exists(path):
                log("agents", "warning", f"chat_group_helper instruction file is missing: {path}")
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = handle.read().strip()
                if data:
                    chunks.append(data)
            except Exception as e:
                log("agents", "warning", f"chat_group_helper failed to load instruction file {path}: {e}")
        return "\n\n".join(chunks)
