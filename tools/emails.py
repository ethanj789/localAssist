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
    
    # Replace the breaking ASCII single quote with a typographic apostrophe.
    # This prevents the Thunderbird parser from terminating the argument prematurely.
    def escape_for_thunderbird(value: str) -> str:
        if not value:
            return ""
        return value.replace("'", "’")

    to_val = escape_for_thunderbird(to)
    subject_val = escape_for_thunderbird(subject)
    body_val = escape_for_thunderbird(body)

    cc_str = escape_for_thunderbird(",".join(cc)) if cc else ""
    bcc_str = escape_for_thunderbird(",".join(bcc)) if bcc else ""

    # Build the Thunderbird compose string
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
        # Launch Thunderbird as a non-blocking background process
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
