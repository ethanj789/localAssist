import json
import subprocess
import sys
import threading
import asyncio
import selectors
import logging

log = logging.getLogger(__name__)


class MCPClient:
    def __init__(self, cmd: str):
        self.cmd = cmd.split()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._msg_id = 0

    def start(self):
        self._proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        init_id = self._next_id()

        self._send({
            "jsonrpc": "2.0", "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "local-agent", "version": "0.1"}
            }
        })
        self._recv_response(init_id)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        log.info("MCP client initialized successfully")

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc.wait()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _send(self, msg: dict):
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("MCP server process is not running")
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _recv_response(self, expected_id: int, timeout: float = 30.0) -> dict:
        """Read lines from MCP subprocess stdout until we get the expected response.
        Times out after `timeout` seconds to prevent deadlocking the server."""
        import time
        deadline = time.monotonic() + timeout
        while True:
            if self._proc.poll() is not None:
                raise RuntimeError(f"MCP server process exited with code {self._proc.returncode}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for MCP response id={expected_id}")
            # Use a short readline with polling to respect timeout
            line = self._proc.stdout.readline()
            if not line:
                raise EOFError("MCP server closed stdout")
            msg = json.loads(line)
            if "id" in msg and msg["id"] == expected_id:
                return msg
            # else it's a notification — just discard and keep reading

    async def call_tool(self, name: str, arguments: dict) -> str:
        def _call():
            acquired = self._lock.acquire(timeout=60)
            if not acquired:
                raise TimeoutError("MCP client lock timeout (call_tool)")
            try:
                msg_id = self._next_id()
                self._send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}
                })
                return self._recv_response(msg_id, timeout=120)
            finally:
                self._lock.release()

        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
        if "error" in resp:
            return f"Tool error: {resp['error']}"
        contents = resp.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in contents if c.get("type") == "text")

    async def list_tools(self) -> list[dict]:
        def _list():
            acquired = self._lock.acquire(timeout=30)
            if not acquired:
                raise TimeoutError("MCP client lock timeout (list_tools)")
            try:
                msg_id = self._next_id()
                self._send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "method": "tools/list", "params": {}
                })
                return self._recv_response(msg_id)
            finally:
                self._lock.release()

        resp = await asyncio.get_event_loop().run_in_executor(None, _list)
        return resp.get("result", {}).get("tools", [])