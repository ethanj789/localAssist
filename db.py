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
        # Migrate: add thinking_content column for storing thinking tokens
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN thinking_content TEXT")
        except Exception:
            pass  # column already exists
        # Migrate: add images column — stores a JSON list of relative paths to
        # image files saved on disk (NOT inline base64). See image_store.py.
        # LEGACY: superseded by the generalized `files` column below. Kept only
        # so the one-time data migration (migrate_images_to_files) can read it.
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN images TEXT")
        except Exception:
            pass  # column already exists
        # Migrate: add files column — stores a JSON list of lean *file objects*
        # for uploaded attachments (PDF/images/...). See file_store.py:
        #   {"type","path":<file_id>,"name","pages","hasText"}
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN files TEXT")
        except Exception:
            pass  # column already exists
        conn.commit()
    # One-time data migration: fold any legacy `images` rows into `files` and
    # restructure the on-disk files from chat_images/ into chat_files/.
    migrate_images_to_files()


def migrate_images_to_files():
    """Convert legacy ``messages.images`` rows (flat JSON path lists pointing at
    ``chat_images/``) into the new ``messages.files`` object shape, moving each
    image into the ``chat_files/<conv>/<file_id>/`` layout.

    Idempotent: only touches rows that still have ``images`` set and no ``files``
    yet. A row whose images all fail to migrate is cleared of ``images`` anyway
    so it isn't retried forever (the source files are gone)."""
    import file_store

    with get_db_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT id, conversation_id, images FROM messages "
                "WHERE images IS NOT NULL AND images != '' "
                "AND (files IS NULL OR files = '')"
            ).fetchall()
        except Exception:
            return  # columns not present yet (shouldn't happen post-init)

        migrated_rows = 0
        for r in rows:
            try:
                legacy_paths = json.loads(r["images"])
            except (json.JSONDecodeError, TypeError):
                legacy_paths = None
            if not legacy_paths:
                # Nothing usable — clear so we don't rescan it next boot.
                conn.execute("UPDATE messages SET images = NULL WHERE id = ?", (r["id"],))
                continue

            conv_id = r["conversation_id"]
            file_objs = []
            for p in legacy_paths:
                obj = file_store.migrate_legacy_image(conv_id, p)
                if obj:
                    file_objs.append(obj)

            files_str = json.dumps(file_objs) if file_objs else None
            # Clear legacy images regardless so the migration is one-shot.
            conn.execute(
                "UPDATE messages SET files = ?, images = NULL WHERE id = ?",
                (files_str, r["id"]),
            )
            migrated_rows += 1
        if migrated_rows:
            conn.commit()
            import logging
            logging.getLogger(__name__).info(
                "db: migrated %d message row(s) from images → files", migrated_rows
            )

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


# def resolve_conversation_provider(conversation_id: str, fallback_provider: str) -> str:
#     conv = get_conversation(conversation_id)
#     if not conv:
#         return fallback_provider

#     stored_provider = conv.get("provider")
#     if stored_provider:
#         return stored_provider

#     inferred_provider = infer_provider_from_messages(get_messages(conversation_id))
#     if not inferred_provider:
#         inferred_provider = fallback_provider

#     update_conversation_provider(conversation_id, inferred_provider)
#     return inferred_provider
def resolve_conversation_provider(conversation_id: str, fallback_provider: str) -> str:
    conv = get_conversation(conversation_id)
    if not conv:
        return fallback_provider

    return conv.get("provider") or fallback_provider


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
            "SELECT role, content, tool_calls, tool_call_id, thinking_content, images, files FROM messages WHERE conversation_id = ? ORDER BY id ASC",
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
            if r["thinking_content"]:
                msg["thinking_content"] = r["thinking_content"]
            # files is a JSON list of lean file objects (see file_store.py),
            # kept as a separate key so it never pollutes the text sent to the
            # model. The agent rebuilds page-image base64 from these on each
            # turn (file_store.page_rel_paths); extracted text already lives in
            # `content` (spliced at upload), so it's never re-injected here.
            if r["files"]:
                try:
                    file_objs = json.loads(r["files"])
                    if file_objs:
                        msg["files"] = file_objs
                except (json.JSONDecodeError, TypeError):
                    pass
            # LEGACY fallback: a row not yet folded into `files` (e.g. migration
            # couldn't move the source). Expose the old path list so nothing
            # silently vanishes; the agent tolerates both shapes.
            elif r["images"]:
                try:
                    paths = json.loads(r["images"])
                    if paths:
                        msg["images"] = paths
                except (json.JSONDecodeError, TypeError):
                    pass
            messages.append(msg)
        return messages

def add_message(conversation_id: str, role: str, content: str | None, tool_calls: list | None = None, tool_call_id: str | None = None, thinking_content: str | None = None, files: list | None = None):
    now = datetime.utcnow().isoformat()
    tool_calls_str = json.dumps(tool_calls) if tool_calls else None
    files_str = json.dumps(files) if files else None
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, thinking_content, files, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, tool_calls_str, tool_call_id, thinking_content, files_str, now)
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
    # Remove the conversation's stored files so they don't orphan on disk.
    # Both the new file store and the legacy image store (for any un-migrated
    # chat_images/ folders) are cleaned.
    for mod in ("file_store", "image_store"):
        try:
            store = __import__(mod)
            if mod == "file_store":
                store.delete_conversation_files(conversation_id)
            else:
                store.delete_conversation_images(conversation_id)
        except Exception:
            pass

def clear_all_conversations():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM conversations")
        conn.commit()
    for mod in ("file_store", "image_store"):
        try:
            store = __import__(mod)
            if mod == "file_store":
                store.delete_all_files()
            else:
                store.delete_all_images()
        except Exception:
            pass

def cleanup_old_conversations(days=30):
    import datetime as dt
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    with get_db_connection() as conn:
        conn.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))
        conn.commit()
    # Prune file/image folders whose conversation no longer exists in the DB.
    try:
        remaining_ids = [r["id"] for r in get_conversations()]
    except Exception:
        remaining_ids = []
    for mod in ("file_store", "image_store"):
        try:
            store = __import__(mod)
            if mod == "file_store":
                store.prune_orphaned_files(remaining_ids)
            else:
                store.prune_orphaned_images(remaining_ids)
        except Exception:
            pass
