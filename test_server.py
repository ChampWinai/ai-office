"""Smoke checks for parsing and path safety. Run: python test_server.py (no Ollama needed)."""
import tempfile
from pathlib import Path
import server


def test_passed():
    assert server.passed("analysis...\nVERDICT: PASS")
    assert not server.passed("VERDICT: FAIL")
    assert not server.passed("VERDICT: PASS\nlater\nVERDICT: FAIL")  # last verdict wins
    assert not server.passed("no verdict here")


def test_fences_and_runnable():
    text = "spec\n```python path=a.py\nx = 1\n```\n```\nplain\n```"
    fs = server.fences(text)
    assert fs[0]["path"] == "a.py" and fs[0]["body"] == "x = 1\n"
    assert server.runnable(text) == "x = 1\n"


def test_path_as_first_body_line():
    fs = server.fences("```python\npath=b.py\ny = 2\n```")
    assert fs[0]["path"] == "b.py" and fs[0]["body"] == "y = 2\n"


def test_refuses_escape():
    with tempfile.TemporaryDirectory() as d:
        server.WS = Path(d)
        try:
            server.plan_writes("```python path=../evil.py\nx = 1\n```", "")
        except ValueError:
            return
        raise AssertionError("path outside workspace was not refused")


def test_plan_diff_apply():
    with tempfile.TemporaryDirectory() as d:
        server.WS = Path(d)
        plan = server.plan_writes("```python path=ok.py\nz = 3\n```", "")
        assert "+z = 3" in server.diff_text(plan)
        assert server.apply_writes(plan) == ["ok.py"]
        assert (Path(d) / "ok.py").read_text(encoding="utf-8") == "z = 3\n"




def test_departments_and_setup_list():
    phases = {n: c["phase"] for n, c in server.ROLES.items()}
    assert phases["PLAN"] == "plan" and phases["SEC"] == "plan" and phases["DEV"] == "build" and phases["QA"] == "review"
    assert all(c.get("hard_model") for c in server.ROLES.values())
    assert set(server.needed(False)) <= set(server.needed(True))


def test_version_compare_and_no_install_from_source():
    assert server._ver("1.0.10") > server._ver("1.0.9")
    assert server.FROZEN is False
    try:
        server.install_update(lambda **e: None)
    except ValueError:
        return
    raise AssertionError("install_update must refuse outside the packaged app")


import http.server, json as _json, threading


class _Fake(http.server.BaseHTTPRequestHandler):
    """Tiny stand-in for an Anthropic-style and an OpenAI-style endpoint (SSE, plus a plain-JSON mode)."""
    def do_POST(self):
        req = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer k1":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"bad key"}'); return
        if self.path.endswith("/v1/messages") and req.get("system"):
            events = [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "คำ"}},
                      {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ตอบ"}}]
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for e in events:
                self.wfile.write(f"event: x\ndata: {_json.dumps(e)}\n\n".encode())
        elif self.path.endswith("/v1/chat/completions"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for t in ["ok", "-openai"]:
                self.wfile.write(f"data: {_json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass


def _fake_base():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/llm"


def test_anthropic_stream_and_bad_key():
    base = _fake_base()
    p = {"type": "anthropic", "base_url": base, "model": "Qwen3-Next", "api_key": "k1"}
    assert "".join(server.ask_api(p, "sys", [{"role": "user", "content": "hi"}])) == "คำตอบ"
    try:
        "".join(server.ask_api(dict(p, api_key="wrong"), "sys", [{"role": "user", "content": "hi"}]))
    except RuntimeError as e:
        assert "401" in str(e)
        return
    raise AssertionError("bad key should raise")


def test_openai_stream_and_done_marker():
    base = _fake_base()
    p = {"type": "openai", "base_url": base, "model": "m", "api_key": "k1"}
    assert "".join(server.ask_api(p, "sys", [{"role": "user", "content": "hi"}])) == "ok-openai"


def test_openai_url_rules():
    assert server.openai_url("https://api.groq.com/openai/v1") == "https://api.groq.com/openai/v1/chat/completions"
    assert server.openai_url("https://generativelanguage.googleapis.com/v1beta/openai/") == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert server.openai_url("http://localhost:1234") == "http://localhost:1234/v1/chat/completions"


def test_provider_validation():
    saved = dict(server.CFG)
    try:
        for bad in ({"type": "anthropic", "base_url": "ftp://x", "model": "m"}, {"type": "openai", "base_url": "https://x", "model": ""}, {"type": "nope"}):
            try:
                server.set_provider(bad)
            except ValueError:
                continue
            raise AssertionError("should refuse " + str(bad))
    finally:
        server.CFG.clear(); server.CFG.update(saved)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
