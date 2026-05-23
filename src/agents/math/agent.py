"""MathAgent: LangChain agent that solves arithmetic requests with an LLM."""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from agents.base import AgentBase
from agents.llm_factory import build_llm


class MathAgent(AgentBase):
    """LLM-backed math helper agent."""

    def build_chain(self):
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are MathAgent. Solve arithmetic tasks accurately. "
                    "Return concise answer. If expression is invalid, explain error. "
                    "If memory context includes relevant formulas or constraints, use them. "
                    "Do not print memory context directly.\n{memory_context}",
                ),
                ("human", "{query}"),
            ]
        )
        llm = build_llm(self.app_config)
        return self.prompt | llm
