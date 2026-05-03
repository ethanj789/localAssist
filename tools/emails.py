import logging
from mcp.server import Server
import subprocess
from mcp import types
import os

log = logging.getLogger(__name__)
app = Server("local-search-mcp")

def _draft_email(
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> list[types.TextContent]:
    
    def escape_for_thunderbird(value: str) -> str:
        """Sanitize field values safely for Thunderbird's -compose syntax."""
        if not value:
            return ""
        return (
            value
            .replace("'", "’")  # Fix the typo: replace ASCII quote with typographic apostrophe
            .replace("\n", " ")  # Replace newlines with spaces to avoid breaking the command line
            .replace("\r", "")
        )

    # 1. Process standard fields. Commas are safely allowed because the entire field 
    # value is enclosed in single quotes for Thunderbird's internal parser.
    to_val = escape_for_thunderbird(to)
    subject_val = escape_for_thunderbird(subject)
    body_val = escape_for_thunderbird(body)

    # 2. Process email lists. Join the list items with a literal comma, then escape.
    cc_str = escape_for_thunderbird(",".join(cc)) if cc else ""
    bcc_str = escape_for_thunderbird(",".join(bcc)) if bcc else ""

    # 3. Assemble the compose string. Comma delimiters group fields together.
    # Single quotes wrap the values so internal commas do not break parsing.
    compose_parts = [
        f"to='{to_val}'",
        f"subject='{subject_val}'",
        f"body='{body_val}'"
    ]
    if cc_str:
        compose_parts.append(f"cc='{cc_str}'")
    if bcc_str:
        compose_parts.append(f"bcc='{bcc_str}'")

    compose_arg = ",".join(compose_parts)

    tb_path = os.getenv("THUNDERBIRD_PATH", r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe")

    try:
        # Pass the arguments securely to Thunderbird as a subprocess list
        subprocess.Popen([tb_path, "-compose", compose_arg])
        
        return [
            types.TextContent(
                type="text",
                text=f"Draft successfully opened in Thunderbird with subject: '{subject}'"
            )
        ]
    except Exception as e:
        log.error(f"Failed to open Thunderbird: {e}")
        return [
            types.TextContent(
                type="text",
                text=f"Failed to open Thunderbird: {str(e)}"
            )
        ]
