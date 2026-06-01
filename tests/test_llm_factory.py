"""Unit checks for LLM factory model request parameters."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agents.llm_factory import build_llm


class LlmFactoryTest(unittest.TestCase):
    """Verify model-level request parameter resolution and overrides."""

    def test_build_llm_uses_selected_model_request_defaults(self) -> None:
        config = {
            "models": {
                "active_provider": "default",
                "active_model": "model-a",
                "providers": {
                    "default": {
                        "baseUrl": "https://example.invalid/v1",
                        "apiKey": "secret",
                        "models": [
                            {
                                "id": "model-a",
                                "name": "Model A",
                                "temperature": 0.4,
                                "top_p": 0.9,
                                "repetition_penalty": 1.2,
                                "max_tokens": 2000,
                                "seed": 42,
                                "presence_penalty": 0.1,
                                "frequency_penalty": 0.2,
                                "top_k": 40,
                                "min_p": 0.05,
                            }
                        ],
                    }
                },
            }
        }

        with patch("agents.llm_factory.ChatOpenAI", side_effect=lambda **kwargs: kwargs):
            built = build_llm(config)

        self.assertEqual(built["model"], "model-a")
        self.assertEqual(built["temperature"], 0.4)
        self.assertEqual(built["top_p"], 0.9)
        self.assertEqual(built["max_tokens"], 2000)
        self.assertEqual(built["seed"], 42)
        self.assertEqual(built["presence_penalty"], 0.1)
        self.assertEqual(built["frequency_penalty"], 0.2)
        self.assertEqual((built.get("model_kwargs") or {}).get("repetition_penalty"), 1.2)
        self.assertEqual((built.get("model_kwargs") or {}).get("top_k"), 40)
        self.assertEqual((built.get("model_kwargs") or {}).get("min_p"), 0.05)

    def test_build_llm_explicit_overrides_win_over_model_defaults(self) -> None:
        config = {
            "models": {
                "active_provider": "default",
                "active_model": "model-a",
                "providers": {
                    "default": {
                        "baseUrl": "https://example.invalid/v1",
                        "apiKey": "secret",
                        "models": [
                            {
                                "id": "model-a",
                                "name": "Model A",
                                "temperature": 0.4,
                                "top_p": 0.9,
                                "repetition_penalty": 1.2,
                                "max_tokens": 2000,
                                "seed": 42,
                                "presence_penalty": 0.1,
                                "frequency_penalty": 0.2,
                                "top_k": 40,
                                "min_p": 0.05,
                            }
                        ],
                    }
                },
            }
        }

        with patch("agents.llm_factory.ChatOpenAI", side_effect=lambda **kwargs: kwargs):
            built = build_llm(
                config,
                temperature=0.1,
                top_p=0.5,
                repetition_penalty=1.05,
                max_tokens=777,
                seed=7,
                presence_penalty=0.3,
                frequency_penalty=0.4,
                top_k=12,
                min_p=0.07,
            )

        self.assertEqual(built["temperature"], 0.1)
        self.assertEqual(built["top_p"], 0.5)
        self.assertEqual(built["max_tokens"], 777)
        self.assertEqual(built["seed"], 7)
        self.assertEqual(built["presence_penalty"], 0.3)
        self.assertEqual(built["frequency_penalty"], 0.4)
        self.assertEqual((built.get("model_kwargs") or {}).get("repetition_penalty"), 1.05)
        self.assertEqual((built.get("model_kwargs") or {}).get("top_k"), 12)
        self.assertEqual((built.get("model_kwargs") or {}).get("min_p"), 0.07)

    def test_build_llm_does_not_send_unset_request_params(self) -> None:
        config = {
            "models": {
                "active_provider": "default",
                "active_model": "model-a",
                "providers": {
                    "default": {
                        "baseUrl": "https://example.invalid/v1",
                        "apiKey": "secret",
                        "models": [
                            {
                                "id": "model-a",
                                "name": "Model A",
                            }
                        ],
                    }
                },
            }
        }

        with patch("agents.llm_factory.ChatOpenAI", side_effect=lambda **kwargs: kwargs):
            built = build_llm(config)

        self.assertNotIn("temperature", built)
        self.assertNotIn("top_p", built)
        self.assertNotIn("max_tokens", built)
        self.assertNotIn("seed", built)
        self.assertNotIn("presence_penalty", built)
        self.assertNotIn("frequency_penalty", built)
        self.assertEqual(built.get("model_kwargs"), None)


if __name__ == "__main__":
    unittest.main()
