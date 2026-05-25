import sqlite3
import json
import uuid
from datetime import datetime
from pathlib import Path

DB_FILE = Path(__file__).parent / "chat_history.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        # Migrate: add provider column if it doesn't exist yet
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN provider TEXT")
        except Exception:
            pass  # column already exists
        conn.commit()

def create_conversation(title="New Chat", provider: str | None = None) -> str:
    conv_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, provider, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conv_id, title, provider, now, now)
        )
        conn.commit()
    return conv_id

def get_conversations() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, provider, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

def get_conversation(conversation_id: str) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, title, provider, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,)
        ).fetchone()
        return dict(row) if row else None


def infer_provider_from_messages(messages: list[dict]) -> str | None:
    for message in messages:
        tool_calls = message.get("tool_calls") or []
        for tool_call in tool_calls:
            if isinstance(tool_call, dict) and "id" in tool_call:
                return "groq"
            if isinstance(tool_call, dict) and "function" in tool_call:
                return "ollama"
    return None


def resolve_conversation_provider(conversation_id: str, fallback_provider: str) -> str:
    conv = get_conversation(conversation_id)
    if not conv:
        return fallback_provider

    stored_provider = conv.get("provider")
    if stored_provider:
        return stored_provider

    inferred_provider = infer_provider_from_messages(get_messages(conversation_id))
    if not inferred_provider:
        inferred_provider = fallback_provider

    update_conversation_provider(conversation_id, inferred_provider)
    return inferred_provider


def get_conversation_title(conversation_id: str) -> str | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT title FROM conversations WHERE id = ?",
            (conversation_id,)
        ).fetchone()
        return row["title"] if row else None

def get_messages(conversation_id: str) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls, tool_call_id FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,)
        ).fetchall()
        
        messages = []
        for r in rows:
            msg = {
                "role": r["role"],
                "content": r["content"]
            }
            if r["tool_calls"]:
                msg["tool_calls"] = json.loads(r["tool_calls"])
            if r["tool_call_id"]:
                msg["tool_call_id"] = r["tool_call_id"]
            messages.append(msg)
        return messages

def add_message(conversation_id: str, role: str, content: str | None, tool_calls: list | None = None, tool_call_id: str | None = None):
    now = datetime.utcnow().isoformat()
    tool_calls_str = json.dumps(tool_calls) if tool_calls else None
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, tool_calls_str, tool_call_id, now)
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id)
        )
        conn.commit()

def update_conversation_title(conversation_id: str, title: str):
    now = datetime.utcnow().isoformat()
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conversation_id)
        )
        conn.commit()

def update_conversation_provider(conversation_id: str, provider: str):
    """Record which provider (groq|ollama) was used for this conversation."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE conversations SET provider = ? WHERE id = ?",
            (provider, conversation_id)
        )
        conn.commit()

def update_message_content(conversation_id: str, tool_call_id: str, new_content: str):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE messages SET content = ? WHERE conversation_id = ? AND tool_call_id = ?",
            (new_content, conversation_id, tool_call_id)
        )
        conn.commit()

def delete_conversation(conversation_id: str):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()

def clear_all_conversations():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM conversations")
        conn.commit()

def cleanup_old_conversations(days=30):
    import datetime as dt
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    with get_db_connection() as conn:
        conn.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))
        conn.commit()
