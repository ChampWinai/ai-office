"""Tests for the workspace edit engine (edits.py). Run: python test_edits.py (no Ollama needed)."""
import sys
import tempfile
from pathlib import Path

import edits

SEARCH_REPLACE = (
    "FILE: src/a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
    "```python path=new.py\nprint('n')\n```\n"
    "FILE: src/a.py\n<<<<<<< SEARCH\ny = 1\n=======\ny = 3\n>>>>>>> REPLACE\n"
)


def test_parse_search_replace_and_full_file():
    ops = edits.parse_ops(SEARCH_REPLACE)
    assert [(o["kind"], o["path"]) for o in ops] == [("write", "new.py"), ("edit", "src/a.py")]
    assert ops[1]["pairs"] == [("x = 1", "x = 2"), ("y = 1", "y = 3")]


def test_ambiguous_search_is_refused_then_applied_once():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        op = [{"kind": "edit", "path": "src/a.py", "pairs": [("x = 1\n", "x = 9\n")]}]
        changed, errs = edits.apply_ops(root, op)
        assert changed == [] and errs and "พบ 2 ครั้ง" in errs[0]
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        changed, errs = edits.apply_ops(root, op)
        assert changed == ["src/a.py"] and not errs
        assert (root / "src" / "a.py").read_text(encoding="utf-8") == "x = 9\n"


def test_path_outside_workspace_is_refused():
    with tempfile.TemporaryDirectory() as d:
        changed, errs = edits.apply_ops(Path(d), [{"kind": "write", "path": "../evil.py", "body": "x"}])
        assert changed == [] and errs and "นอกโฟลเดอร์" in errs[0]


def test_commit_and_undo_restore_files():
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as st:
        ws, st = Path(ws), Path(st)
        (ws / "keep.py").write_text("old\n", encoding="utf-8")
        (st / "keep.py").write_text("new\n", encoding="utf-8")
        (st / "added.py").write_text("hello\n", encoding="utf-8")
        edits.commit(ws, st, ["keep.py", "added.py"])
        assert (ws / "keep.py").read_text(encoding="utf-8") == "new\n" and (ws / "added.py").exists()
        _, files = edits.undo_last(ws)
        assert (ws / "keep.py").read_text(encoding="utf-8") == "old\n"
        assert not (ws / "added.py").exists() and sorted(files) == ["added.py", "keep.py"]
        try:
            edits.undo_last(ws)
        except ValueError:
            return
        raise AssertionError("a second undo should find nothing left")


def test_checks_catch_syntax_error():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "bad.py").write_text("def f(:\n", encoding="utf-8")
        report, ok = edits.run_checks(Path(d), ["bad.py"], "", sys.executable)
        assert not ok and "syntax bad.py" in report


def test_pick_files_by_name_and_word():
    files = ["src/cleaner.py", "src/window.py", "README.md"]
    assert edits.pick_files("fix cleaner.py", "", files) == ["src/cleaner.py"]
    assert "src/window.py" in edits.pick_files("fix the window layout", "", files)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
