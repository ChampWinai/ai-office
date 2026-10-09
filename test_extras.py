"""Tests for web/MCP tools and image message shapes. Run: python test_extras.py (no Ollama or internet needed)."""
import sys
from pathlib import Path

import extras
import server


def test_fetch_refuses_local_and_non_http():
    for url in ("http://localhost:8000/", "http://127.0.0.1/", "http://192.168.1.1/", "http://[::1]/", "file:///c:/windows/win.ini"):
        try:
            extras.fetch(url)
        except ValueError:
            continue
        raise AssertionError("not blocked: " + url)


def test_html_to_text():
    t = extras.html_to_text("<html><head><title>x</title></head><body><script>bad()</script><p>Hello&nbsp;<b>world</b></p></body></html>")
    assert "Hello" in t and "world" in t and "bad()" not in t


def test_search_needs_configuration():
    assert "ยังไม่ได้ตั้งค่า" in extras.web_search("anything", {})


def test_mcp_roundtrip_with_fake_server():
    fake = str(Path(__file__).with_name("fake_mcp.py"))
    servers = extras.mcp_servers({"fake": {"command": sys.executable, "args": [fake]}})
    try:
        assert [t["name"] for t in servers["fake"].tools] == ["add", "echo"]
        assert 'MCP: fake.add {"a": ..., "b": ...}' in extras.mcp_help(servers)
        assert extras.mcp_call(servers, "fake.add {1234, 4321}") == "5555"  # loose positional style
        assert extras.mcp_call(servers, 'fake.add {"a": 2, "b": 40}') == "42"
        assert extras.mcp_call(servers, 'fake.echo {"text": "สวัสดี"}') == "สวัสดี"
    finally:
        extras.mcp_servers({})  # closes it
    bad = extras.mcp_servers({"nope": {"command": "no-such-program-xyz"}})
    assert isinstance(bad["nope"], str) and "เปิดไม่ได้" in bad["nope"]


def test_images_in_each_provider_shape():
    msgs = [{"role": "user", "content": "what is this", "images": [{"mime": "image/png", "data": "QUJD"}]}]
    assert server.to_provider(msgs, "ollama")[0]["images"] == ["QUJD"]
    a = server.to_provider(msgs, "anthropic")[0]["content"]
    assert a[0]["source"]["data"] == "QUJD" and a[-1] == {"type": "text", "text": "what is this"}
    o = server.to_provider(msgs, "openai")[0]["content"]
    assert o[1]["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert server.to_provider([{"role": "user", "content": "hi"}], "openai") == [{"role": "user", "content": "hi"}]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
