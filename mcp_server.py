"""
mcp_server.py — MCP server entry point.
Configures logging, imports the tool registry, and runs the stdio transport.
"""
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

# if sys.platform == "win32":
#     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

# Mirror server.py's logging setup. This runs as a SEPARATE process writing to
# the same logs.txt, so it must (a) honor LOG_LEVEL instead of forcing DEBUG,
# and (b) use the same RotatingFileHandler, or its writes would bypass rotation
# and grow the file unbounded.
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
_log_handler = RotatingFileHandler(
    "logs.txt",
    maxBytes=int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024))),
    backupCount=int(os.getenv("LOG_BACKUPS", "2")),
    encoding="utf-8",
)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_log_handler],
)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
# The MCP library logs every message/dispatch/response at DEBUG and request
# processing at INFO — pure per-call noise. Quiet it to WARNING.
logging.getLogger("mcp").setLevel(logging.WARNING)


from mcp.server.stdio import stdio_server  # noqa: E402
from tools.registry import app             # noqa: E402


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
