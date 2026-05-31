"""Unit checks for resilient JSON extraction in memoryd API."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from memoryd.api import _extract_json_payload


class MemorydApiJsonParseTest(unittest.TestCase):
    """Validate JSON extraction from model output variations."""

    def test_extracts_plain_json_array(self) -> None:
        payload = _extract_json_payload('[{"type":"news","operation":"INSERT","title":"x","text":"y"}]')
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["type"], "news")

    def test_extracts_json_array_with_tool_prefix(self) -> None:
        text = '<|"|>}<tool_call|>[{"type":"news","operation":"INSERT","title":"x","text":"y"}]'
        payload = _extract_json_payload(text)
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["operation"], "INSERT")


if __name__ == "__main__":
    unittest.main()
