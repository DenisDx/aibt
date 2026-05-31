"""ChatGroupHelper2Agent: fast gate + delegate to chat_group_helper."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts import MessagesPlaceholder

from agents.base import AgentBase
from agents.chat_group_helper.agent import ChatGroupHelperAgent
from agents.message_utils import build_role_aware_messages
from agents.llm_factory import build_llm
from core.llm_wiretap import pop_llm_log_context
from core.llm_wiretap import push_llm_log_context
from core.logging_utils import log


class ChatGroupHelper2Agent(AgentBase):
    """Two-stage helper: gate with a fast model, then delegate accepted turns."""

    def __init__(self, app_config: dict[str, Any], agent_config: dict[str, Any] | None = None):
        self._delegate_cache: dict[str, ChatGroupHelperAgent] = {}
        super().__init__(app_config, agent_config)

    def build_chain(self):
        """Build gate chain that can output only __REPLY__ or __NO_REPLY__."""

        instruction_block = self._load_gate_instruction_block()
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

        gate = self._gate_config()
        has_tools, tools_value = self._llm_tools_value()
        if has_tools:
            llm = build_llm(
                self.app_config,
                provider=str(gate.get("provider", "")).strip() or None,
                model=str(gate.get("model", "")).strip() or None,
                tools=tools_value,
            )
        else:
            llm = build_llm(
                self.app_config,
                provider=str(gate.get("provider", "")).strip() or None,
                model=str(gate.get("model", "")).strip() or None,
            )
        return self.prompt | llm

    async def handle(self, query: str, context=None) -> dict[str, Any]:
        """Run gate decision and delegate accepted messages to smart helper."""

        ctx = context or {}
        envid = str(ctx.get("envid", "")).strip() or None

        llm_messages = self._build_llm_messages(query, ctx)
        gate = await self._run_gate(query, ctx, llm_messages, envid)

        if gate["token"] == "__NO_REPLY__":
            return {
                "result": "",
                "skip_send": True,
                "decision": {
                    "action": "ignore",
                    "reason": str(gate.get("reason", "gate_no_reply")),
                    "stage": "gate",
                    "error_code": gate.get("error_code", ""),
                },
                "memory": {},
            }

        result = await self._delegate_to_smart(query, ctx, envid)
        if isinstance(result, dict):
            decision = result.get("decision", {}) if isinstance(result.get("decision"), dict) else {}
            decision["gate"] = "accepted"
            decision["gate_reason"] = str(gate.get("reason", "gate_reply"))
            result["decision"] = decision
        return result

    async def _run_gate(
        self,
        query: str,
        ctx: dict[str, Any],
        messages: list[Any],
        envid: str | None,
    ) -> dict[str, Any]:
        """Run fast gate model and normalize decision token."""

        payload = {
            "query": query,
            "context": ctx,
            "memory_context": "",
            "messages": messages,
        }

        log_token = None
        if self._llm_logging_enabled():
            log_token = push_llm_log_context(
                agent_id=self.name,
                envid=envid,
                log_path=self._llm_log_path(),
                payload=payload,
            )

        try:
            out = await self.chain.ainvoke(payload)
            text = ChatGroupHelperAgent._extract_reply_text(getattr(out, "content", out))
            token = self._normalize_gate_token(text)
            if token:
                return {"token": token, "reason": "gate_llm"}

            log("agents", "warning", f"chat_group_helper2 invalid gate output: {text!r}")
            return {"token": "__NO_REPLY__", "reason": "gate_invalid_output"}
        except Exception as exc:
            err_code, err_text = self._classify_gate_error(exc)
            fail_mode = self._gate_fail_mode()
            action = "__REPLY__" if fail_mode == "fail_open" else "__NO_REPLY__"
            log(
                "agents",
                "warning",
                f"chat_group_helper2 gate failed: {err_code} ({err_text}); fail_mode={fail_mode}",
            )
            return {
                "token": action,
                "reason": f"gate_error_{fail_mode}",
                "error_code": err_code,
            }
        finally:
            if log_token is not None:
                pop_llm_log_context(log_token)

    async def _delegate_to_smart(self, query: str, ctx: dict[str, Any], envid: str | None) -> dict[str, Any]:
        """Delegate accepted message to existing chat_group_helper implementation."""

        delegate = self._get_delegate_agent(envid)
        return await delegate.handle(query, context=ctx)

    def _get_delegate_agent(self, envid: str | None) -> ChatGroupHelperAgent:
        """Return cached delegate instance for the current environment id."""

        key = envid or ""
        if key in self._delegate_cache:
            return self._delegate_cache[key]

        delegate_name = self._delegate_agent_name()
        if delegate_name == self.name:
            raise ValueError("delegate_agent cannot reference itself")

        cfg = AgentBase.assemble_runtime_config(self.app_config, delegate_name, envid=envid)
        delegate_cfg = {"name": delegate_name, **cfg}
        delegate = ChatGroupHelperAgent(self.app_config, delegate_cfg)
        self._delegate_cache[key] = delegate
        return delegate

    def _delegate_agent_name(self) -> str:
        """Read smart delegate id from config with safe default."""

        if not isinstance(self.agent_config, dict):
            return "chat_group_helper"
        name = str(self.agent_config.get("delegate_agent", "chat_group_helper") or "chat_group_helper").strip()
        return name or "chat_group_helper"

    def _gate_config(self) -> dict[str, Any]:
        """Return normalized gate config block."""

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        gate = cfg.get("gate", {}) if isinstance(cfg, dict) else {}
        if not isinstance(gate, dict):
            gate = {}
        return gate

    def _gate_fail_mode(self) -> str:
        """Return failure policy for gate errors: fail_closed or fail_open."""

        mode = str(self._gate_config().get("fail_mode", "fail_closed") or "fail_closed").strip().lower()
        if mode not in ("fail_closed", "fail_open"):
            return "fail_closed"
        return mode

    def _load_gate_instruction_block(self) -> str:
        """Load gate instruction markdown files and combine them."""

        files = self._gate_config().get("instruction_files", [])
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
                log("agents", "warning", f"chat_group_helper2 instruction file is missing: {path}")
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = handle.read().strip()
                if data:
                    chunks.append(data)
            except Exception as exc:
                log("agents", "warning", f"chat_group_helper2 failed to load instruction file {path}: {exc}")
        return "\n\n".join(chunks)

    def _history_messages_limit(self) -> int:
        """Return max recent messages count used for gate context."""

        cfg = self.agent_config if isinstance(self.agent_config, dict) else {}
        raw = cfg.get("recent_messages_limit", cfg.get("send_last", 50)) if isinstance(cfg, dict) else 50
        try:
            return max(1, min(200, int(raw)))
        except Exception:
            return 50

    def _build_llm_messages(self, query: str, ctx: dict[str, Any]) -> list[Any]:
        """Build role-aware message list equivalent to chat_group_helper."""
        return build_role_aware_messages(query, ctx, history_limit=self._history_messages_limit())

    @staticmethod
    def _normalize_gate_token(raw: Any) -> str | None:
        """Normalize gate output to allowed tokens only."""

        text = str(raw or "").strip()
        if not text:
            return None

        upper = text.upper()
        if upper == "__REPLY__":
            return "__REPLY__"
        if upper == "__NO_REPLY__":
            return "__NO_REPLY__"

        match = re.search(r"__(?:NO_)?REPLY__", upper)
        if not match:
            return None
        token = match.group(0)
        if token == "__REPLY__":
            return token
        if token == "__NO_REPLY__":
            return token
        return None

    @staticmethod
    def _classify_gate_error(exc: Exception) -> tuple[str, str]:
        """Classify gate errors for concise operational logs."""

        raw = str(exc).strip() or exc.__class__.__name__
        lowered = raw.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "llm_timeout", "provider timeout"
        if "connection" in lowered or "refused" in lowered:
            return "llm_connection", "provider connection error"
        if "rate limit" in lowered or "429" in lowered:
            return "llm_rate_limit", "provider rate limit"
        return "runtime_error", raw
