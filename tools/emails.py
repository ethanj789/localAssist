import logging
from mcp.server import Server
from mcp import types
import os
import json

log = logging.getLogger(__name__)
app = Server("local-search-mcp")

async def _draft_email(
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> list[types.TextContent]:
    
    # Assemble the action data
    action_data = {
        "type": "mail",
        "data": {
            "to": to,
            "subject": subject,
            "body": body,
            "cc": cc or [],
            "bcc": bcc or []
        }
    }

    # Generate the sentinel string
    action_sentinel = f"\n__ACTION_BUTTON__:{json.dumps(action_data)}"

    return [
        types.TextContent(
            type="text",
            text=f"Email draft prepared for {to} with subject: '{subject}'.{action_sentinel}"
        )
    ]
