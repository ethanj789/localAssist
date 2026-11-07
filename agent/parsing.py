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

    # Python-kwargs call form: name(key='value', key2="value2", ...)
    # Some models emit tool calls as a Python function call rather than JSON.
    # This has no surrounding { } so the patterns above miss it. Parse the
    # kwargs into an args dict. Only string values are supported (what tools
    # here take); bare numbers/booleans are also accepted.
    names_alt = "|".join(re.escape(n) for n in tool_names)
    kwargs_call = re.search(
        rf'(?<![\w.])({names_alt})\s*\(([^)]*)\)',
        stripped,
        re.DOTALL,
    ) if tool_names else None
    if kwargs_call:
        name = kwargs_call.group(1)
        inner = kwargs_call.group(2).strip()
        if name and inner:
            # Match key=<quoted string> | key=number | key=true/false/none
            pair_re = re.compile(
                r"""(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([-\d.]+|true|false|none))""",
                re.IGNORECASE,
            )
            args = {}
            for m in pair_re.finditer(inner):
                k = m.group(1)
                if m.group(2) is not None:
                    args[k] = m.group(2).encode().decode("unicode_escape")
                elif m.group(3) is not None:
                    args[k] = m.group(3).encode().decode("unicode_escape")
                else:
                    raw = m.group(4)
                    low = raw.lower()
                    if low in ("true", "false"):
                        args[k] = (low == "true")
                    elif low == "none":
                        args[k] = None
                    else:
                        try:
                            args[k] = int(raw) if raw.lstrip("-").isdigit() else float(raw)
                        except ValueError:
                            args[k] = raw
            if args:
                return SimpleNamespace(
                    id=f"call_{name}_extracted",
                    function=SimpleNamespace(
                        name=name,
                        arguments=json.dumps(args),
                    ),
                )

    return None
