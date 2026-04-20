import json
import subprocess
import sys
import threading
import asyncio


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

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc.wait()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    # def _recv(self) -> dict:
    #     line = self._proc.stdout.readline()
    #     return json.loads(line)
    def _recv_response(self, expected_id: int) -> dict:
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise EOFError("MCP server closed stdout")
            msg = json.loads(line)
            if "id" in msg and msg["id"] == expected_id:
                return msg
            # else it's a notification — just discard and keep reading

    async def call_tool(self, name: str, arguments: dict) -> str:
        def _call():
            with self._lock:
                msg_id = self._next_id()
                self._send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}
                })
                return self._recv_response(msg_id)

        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
        if "error" in resp:
            return f"Tool error: {resp['error']}"
        contents = resp.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in contents if c.get("type") == "text")

    async def list_tools(self) -> list[dict]:
        def _list():
            with self._lock:
                msg_id = self._next_id()
                self._send({
                    "jsonrpc": "2.0", "id": msg_id,
                    "method": "tools/list", "params": {}
                })
                return self._recv_response(msg_id)  

        resp = await asyncio.get_event_loop().run_in_executor(None, _list)
        return resp.get("result", {}).get("tools", [])