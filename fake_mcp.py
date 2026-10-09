"""A tiny MCP server over stdio, used by the tests (and as an example). Tools: add(a, b), echo(text)."""
import json
import sys

TOOLS = [
    {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "echo", "description": "Return the text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue  # notifications need no answer
    method, params = msg["method"], msg.get("params", {})
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        a = params.get("arguments", {})
        text = str(a.get("a", 0) + a.get("b", 0)) if params["name"] == "add" else a.get("text", "")
        result = {"content": [{"type": "text", "text": text}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unknown method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
