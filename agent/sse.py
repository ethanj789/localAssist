import json


def _sse(event: str, data: str) -> str:
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"
