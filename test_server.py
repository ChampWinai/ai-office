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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
