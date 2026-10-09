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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
