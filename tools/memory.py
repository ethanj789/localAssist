import datetime
import logging
from config import get_raw_memory, save_memory

log = logging.getLogger(__name__)

# Keys reserved by the manual/base-info section. The agent may READ these but
# must never create, overwrite, or delete them through the tool — they are the
# user's own base info and are managed by the user only.
def _manual_keys(data: dict) -> set:
    return set((data.get("manual") or {}).keys())


def _normalize_key(key: str) -> str:
    """
    Keys are the identity of a preference. Normalize to a stable, snake_case-ish
    form so 'Temperature Unit' and 'temperature_unit' collapse to one entry.
    """
    return "_".join(key.strip().lower().split())


async def _manage_memory(action: str, key: str = None, value: str = None):
    """
    Keyed user-preference store.

    Preferences live as key -> value pairs under 'agent_managed'. Management is
    purely by key:
      - 'write'     upsert: create key if new, overwrite its value if it exists.
      - 'delete'    remove the entry for 'key'.
      - 'clear_all' remove every agent-managed entry (manual base info untouched).

    The 'manual' section (user base info) is read-only to the agent: its keys
    cannot be written to or deleted here.
    """
    data = get_raw_memory()
    agent_managed = data.get("agent_managed")

    # Migrate legacy list-of-slots shape ({index, content, created}) to a keyed
    # dict on first touch. Legacy free-text entries have no key, so they are
    # dropped — they were the todo-list sprawl this redesign removes.
    if not isinstance(agent_managed, dict):
        agent_managed = {}
        data["agent_managed"] = agent_managed

    if action == "write":
        if not key or not value:
            return "Error: both 'key' and 'value' are required for 'write'."
        k = _normalize_key(key)
        if not k:
            return "Error: 'key' is empty after normalization."
        if k in _manual_keys(data):
            return f"Error: '{k}' is user base info (manual) and cannot be changed here."

        existed = k in agent_managed
        created = agent_managed.get(k, {}).get("created") if existed else None
        agent_managed[k] = {
            "value": value,
            "created": created or datetime.datetime.now().isoformat(),
            "updated": datetime.datetime.now().isoformat(),
        }
        data["agent_managed"] = agent_managed
        save_memory(data)
        verb = "Updated" if existed else "Saved"
        return f"{verb} preference '{k}' = '{value}'."

    elif action == "delete":
        if not key:
            return "Error: 'key' is required for 'delete'."
        k = _normalize_key(key)
        if k in _manual_keys(data):
            return f"Error: '{k}' is user base info (manual) and cannot be deleted here."
        if k not in agent_managed:
            available = ", ".join(agent_managed.keys()) or "(none)"
            return f"Error: no preference with key '{k}'. Existing keys: {available}"
        removed = agent_managed.pop(k)
        data["agent_managed"] = agent_managed
        save_memory(data)
        return f"Deleted preference '{k}' (was '{removed.get('value', '')}')."

    elif action == "clear_all":
        data["agent_managed"] = {}
        save_memory(data)
        return "Cleared all agent-managed preferences."

    else:
        return f"Error: unknown action '{action}'."
