import json
import logging
from types import SimpleNamespace

log = logging.getLogger(__name__)


def _extract_tool_call_from_text(content: str, available_tools: list[dict]) -> SimpleNamespace | None:
    """Detect when a model emits a tool call as plain text instead of a structured
    tool_call.  Handles patterns like:
        tool_name{key: "value", ...}
        tool_name({"key": "value", ...})
        tool_name{"key": "value"}
    Returns a SimpleNamespace matching the tool_call shape, or None."""
    import re

    if not content:
        return None

    tool_names = {t["function"]["name"] for t in available_tools}

    # Strip surrounding whitespace and any markdown fencing
    stripped = content.strip()
    # Some models (gemma4) emit <|"|> instead of actual quote characters
    stripped = stripped.replace('<|"|>', '"')
    log.debug("_extract_tool_call_from_text | checking content (len=%d): %r", len(stripped), stripped[:200])
    # Remove thinking/reasoning preamble — look for tool call anywhere in the text
    for name in tool_names:
        # Patterns: name{...}  or  name({...})  or  name( {...} )
        patterns = [
            rf'{re.escape(name)}\s*\(\s*(\{{.*\}})\s*\)',   # name({...})
            rf'{re.escape(name)}\s*(\{{.*\}})',              # name{...}
        ]
        for pattern in patterns:
            match = re.search(pattern, stripped, re.DOTALL)
            if match:
                json_str = match.group(1)
                try:
                    args = json.loads(json_str)
                    if isinstance(args, dict):
                        return SimpleNamespace(
                            id=f"call_{name}_extracted",
                            function=SimpleNamespace(
                                name=name,
                                arguments=json.dumps(args),
                            ),
                        )
                except json.JSONDecodeError:
                    # Try fixing common issues: unquoted keys, single quotes
                    try:
                        fixed = json_str.replace("'", '"')
                        # Quote unquoted keys: {action:"v"} -> {"action":"v"}
                        fixed = re.sub(r'(\{|,)\s*([a-zA-Z_]\w*)\s*:', r'\1"\2":', fixed)
                        args = json.loads(fixed)
                        if isinstance(args, dict):
                            return SimpleNamespace(
                                id=f"call_{name}_extracted",
                                function=SimpleNamespace(
                                    name=name,
                                    arguments=json.dumps(args),
                                ),
                            )
                    except json.JSONDecodeError:
                        continue

    return None
