import tempfile
import unittest
from pathlib import Path

import db


class ProviderStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.original_db_file = db.DB_FILE
        db.DB_FILE = Path(self.tmp_dir.name) / "chat_history.db"
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.original_db_file

    def test_create_conversation_persists_provider(self):
        groq_conv = db.create_conversation(provider="groq")
        ollama_conv = db.create_conversation(provider="ollama")

        self.assertEqual(db.get_conversation(groq_conv)["provider"], "groq")
        self.assertEqual(db.get_conversation(ollama_conv)["provider"], "ollama")

    def test_get_messages_round_trips_both_tool_structures(self):
        groq_conv = db.create_conversation(provider="groq")
        ollama_conv = db.create_conversation(provider="ollama")

        groq_tool_calls = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query":"hello"}'
                },
            }
        ]
        ollama_tool_calls = [
            {
                "function": {
                    "name": "fetch_webpage",
                    "arguments": {"url": "https://example.com"}
                }
            }
        ]

        db.add_message(groq_conv, "assistant", None, tool_calls=groq_tool_calls)
        db.add_message(ollama_conv, "assistant", None, tool_calls=ollama_tool_calls)

        groq_messages = db.get_messages(groq_conv)
        ollama_messages = db.get_messages(ollama_conv)

        self.assertEqual(groq_messages[-1]["tool_calls"][0]["id"], "call_abc123")
        self.assertEqual(groq_messages[-1]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(ollama_messages[-1]["tool_calls"][0]["function"]["name"], "fetch_webpage")
        self.assertEqual(ollama_messages[-1]["tool_calls"][0]["function"]["arguments"]["url"], "https://example.com")

    def test_resolve_provider_infers_missing_provider_from_legacy_tool_calls(self):
        legacy_conv = db.create_conversation(provider=None)

        legacy_tool_calls = [
            {
                "id": "legacy_call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query":"legacy"}'
                },
            }
        ]
        db.add_message(legacy_conv, "assistant", None, tool_calls=legacy_tool_calls)

        resolved = db.resolve_conversation_provider(legacy_conv, "ollama")

        self.assertEqual(resolved, "groq")
        self.assertEqual(db.get_conversation(legacy_conv)["provider"], "groq")
