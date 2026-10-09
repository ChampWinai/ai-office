"""Tests for the workspace engine (edits.py). Run: python test_edits.py (no Ollama needed)."""
import sys
import tempfile
from pathlib import Path

import edits

SEARCH_REPLACE = (
    "FILE: src/a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
    "```python path=new.py\nprint('n')\n```\n"
    "FILE: src/a.py\n<<<<<<< SEARCH\ny = 1\n=======\ny = 3\n>>>>>>> REPLACE\n"
    "DELETE: old.py\nRENAME: a.txt -> b.txt\nRUN: npm install\n"
    "```sh\nRUN: inside-a-block-is-ignored\n```\n"
)


def _ws(files):
    d = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(body)
    return d


def test_parse_all_op_kinds():
    ops = edits.parse_ops(SEARCH_REPLACE)
    assert [(o["kind"], o.get("path") or o.get("cmd")) for o in ops] == [
        ("write", "new.py"), ("edit", "src/a.py"), ("delete", "old.py"), ("rename", "a.txt"), ("run", "npm install")]
    assert ops[1]["pairs"] == [("x = 1", "x = 2"), ("y = 1", "y = 3")] and ops[3]["to"] == "b.txt"


def test_ambiguous_search_is_refused():
    root = _ws({"a.py": b"x = 1\nx = 1\n"})
    errs = edits.apply_ops(root, [{"kind": "edit", "path": "a.py", "pairs": [("x = 1\n", "x = 9\n")]}])
    assert errs and "พบ 2 ครั้ง" in errs[0]


def test_crlf_and_encoding_are_kept():
    thai = "ชื่อ = 'สวัสดี'\r\nx = 1\r\n".encode("cp874")
    root = _ws({"a.py": thai, "lf.py": b"a = 1\nb = 2\n"})
    assert not edits.apply_ops(root, [{"kind": "edit", "path": "a.py", "pairs": [("x = 1", "x = 2")]},
                                      {"kind": "edit", "path": "lf.py", "pairs": [("b = 2", "b = 3")]}])
    assert (root / "a.py").read_bytes() == "ชื่อ = 'สวัสดี'\r\nx = 2\r\n".encode("cp874")
    assert (root / "lf.py").read_bytes() == b"a = 1\nb = 3\n"  # LF file stays LF on Windows


def test_path_outside_workspace_is_refused():
    root = _ws({})
    errs = edits.apply_ops(root, [{"kind": "write", "path": "../evil.py", "body": "x"}])
    assert errs and "นอกโฟลเดอร์" in errs[0]


def test_stage_changes_commit_undo_with_delete_and_rename():
    ws = _ws({"keep.py": b"old\n", "gone.py": b"bye\n", "a.txt": b"A\n"})
    files = edits.project_files(ws)
    root, copied = edits.stage(ws, files)
    errs = edits.apply_ops(root, edits.parse_ops(
        "```python path=keep.py\nnew\n```\n```python path=added.py\nhello\n```\nDELETE: gone.py\nRENAME: a.txt -> b.txt\n"))
    assert not errs, errs
    changed, deleted = edits.changes(ws, root, copied)
    assert changed == ["added.py", "b.txt", "keep.py"] and deleted == ["a.txt", "gone.py"], (changed, deleted)
    assert "(ลบไฟล์)" in edits.diff_for(ws, root, changed, deleted)
    edits.commit(ws, root, changed, deleted)
    assert (ws / "keep.py").read_text() == "new\n" and not (ws / "gone.py").exists() and (ws / "b.txt").exists()
    _, touched = edits.undo_last(ws)
    assert (ws / "keep.py").read_text() == "old\n" and (ws / "gone.py").exists() and (ws / "a.txt").exists()
    assert not (ws / "added.py").exists() and not (ws / "b.txt").exists() and len(touched) == 5
    try:
        edits.undo_last(ws)
    except ValueError:
        return
    raise AssertionError("a second undo should find nothing left")


def test_checks_catch_syntax_errors():
    root = _ws({"bad.py": b"def f(:\n", "bad.json": b"{oops"})
    report, ok = edits.run_checks(root, ["bad.py", "bad.json"], "", sys.executable)
    assert not ok and "syntax bad.py" in report and "syntax bad.json" in report


def test_tools_search_read_list():
    ws = _ws({"src/cleaner.py": b"def clean_temp():\n    return 1\n", "README.md": b"# hi\n"})
    files = edits.project_files(ws)
    assert "src/cleaner.py:1: def clean_temp():" in edits.run_tool(ws, files, "SEARCH", "clean_temp")
    assert "return 1" in edits.run_tool(ws, files, "READ", "src/cleaner.py:2-2")
    assert edits.run_tool(ws, files, "LIST", ".").split("\n") == ["README.md", "src/"]
    assert edits.tool_calls("SEARCH: foo\n```\nREAD: not-a-call\n```\nREAD: a.py:1-5") == [("SEARCH", "foo"), ("READ", "a.py:1-5")]


def test_pick_files_by_name_read_and_word():
    files = ["src/cleaner.py", "src/window.py", "README.md"]
    assert edits.pick_files("fix cleaner.py", "", files) == ["src/cleaner.py"]
    assert "src/window.py" in edits.pick_files("fix the window layout", "", files)
    assert edits.pick_files("x", "", files, read={"README.md"}) == ["README.md"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
