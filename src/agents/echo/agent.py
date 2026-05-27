"""EchoAgent: LangChain agent that returns user request in a stable textual form."""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from agents.base import AgentBase
from agents.llm_factory import build_llm


class EchoAgent(AgentBase):
    """Simple LLM-backed echo-like agent for integration testing."""

    def build_chain(self):
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are EchoAgent. Return the user text unchanged. No extra words. "
                    "Use memory context only for understanding; do not print it.\n{memory_context}",
                ),
                ("human", "{query}"),
            ]
        )
        has_tools, tools_value = self._llm_tools_value()
        if has_tools:
            llm = build_llm(self.app_config, tools=tools_value)
        else:
            llm = build_llm(self.app_config)
        return self.prompt | llm
