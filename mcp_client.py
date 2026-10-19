import json , subprocess

class MCPError(Exception): pass

class MCPClient:
    def __init__(self, cmd, cwd, log_path="mcp-stderr.log"):
        self.proc = subprocess.Popen(cmd, cwd=cwd, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(log_path, "a"))      # debug side-channel → file
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "sre-gym", "version": "0.1"}})
        self.notify("notifications/initialized")

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")                                 # write one JSON line to proc.stdin, then flush
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})                                 # no id → no reply expected

    def request(self, method, params=None):
        self.next_id += 1
        my_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:                           # empty string = server died
                raise MCPError(f"server exited while waiting for {method}")
            reply = json.loads(line)
            if "method" in reply or reply.get("id") != my_id :                           # not the reply to MY request → skip it
                continue
            if "error" in reply:
                raise MCPError(reply["error"]["message"])
            return reply["result"]

    def list_tools(self):
        # MCP shape → the OpenAI-style shape your llm_call already sends
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
                for t in self.request("tools/list")["tools"]]

    def call_tool(self, name, args):
        result = self.request("tools/call", {"name": name, "arguments": args})
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c["text"])
            else:
                parts.append(f"[{c.get('type')} content ({c.get('mimeType', 'unknown type')}) not shown]")

        text = "\n".join(parts)
        return f"Error: {text}" if result.get("isError") else text                            # prefix "Error: " when result says isError

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)
