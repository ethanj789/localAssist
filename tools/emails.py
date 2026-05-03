import logging
from mcp.server import Server
from mcp import types
import subprocess
from mcp import types
import os

log = logging.getLogger(__name__)

app = Server("local-search-mcp")


#use thunderbird.exe -compose to open thunderbird editor
# "C:\Program Files\Mozilla Thunderbird\thunderbird.exe" -compose "to='exampleEmail@example.com',subject='hi',body=hello%20body%20text"
def draft_email(
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> list[types.TextContent]:
    
    # --- Inner formatting helper ---
    def format_body(raw_body: str) -> str:
        if not raw_body:
            return ""
        # 1. Escape single quotes since single quotes wrap the parameter value
        # 2. Convert literal newlines to '\\n' strings so Thunderbird preserves line breaks
        return raw_body.replace("'", "\\'").replace("\n", "\\n")

    # Clean the arguments and set empty string defaults
    to_val = to.replace("'", "\\'") if to else ""
    subject_val = subject.replace("'", "\\'") if subject else ""
    body_val = format_body(body)

    # Convert Python lists to comma-separated strings for Thunderbird
    cc_val = ",".join(cc).replace("'", "\\'") if cc else ""
    bcc_val = ",".join(bcc).replace("'", "\\'") if bcc else ""

    # Build the Thunderbird compose string dynamically
    compose_parts = [
        f"to='{to_val}'",
        f"subject='{subject_val}'",
        f"body='{body_val}'"
    ]
    if cc_val:
        compose_parts.append(f"cc='{cc_val}'")
    if bcc_val:
        compose_parts.append(f"bcc='{bcc_val}'")

    compose_args = ",".join(compose_parts)

    # Path to the executable (Use raw string or double backslashes)
    # tb_path = r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe"
    tb_path = os.getenv("THUNDERBIRD_PATH", r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe")
    
    try:
        # Launch Thunderbird without blocking the Python execution
        subprocess.Popen([tb_path, "-compose", compose_args])
        
        return [
            types.TextContent(
                type="text",
                text=f"Draft successfully opened in Thunderbird with subject: '{subject}'"
            )
        ]
    except Exception as e:
        return [
            types.TextContent(
                type="text",
                text=f"Failed to open Thunderbird: {str(e)}"
            )
        ]
