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


def test_empty_path_block_joins_next_block_and_blank_write_is_refused():
    ops = edits.parse_ops("```csharp\npath=A.cs\n```\n\n```csharp\nclass A {}\n```")
    assert ops == [{"kind": "write", "path": "A.cs", "body": "class A {}\n"}], ops
    root = _ws({"A.cs": b"class A { int x; }\n"})
    errs = edits.apply_ops(root, [{"kind": "write", "path": "A.cs", "body": "  \n"}])
    assert errs and "เนื้อหาว่าง" in errs[0] and (root / "A.cs").read_bytes() == b"class A { int x; }\n"


def test_paths_small_models_write_outside_the_fence_line():
    reopened = "```javascript\npath=src/slugify.js\n```javascript\nfunction slugify(s) {}\n```"
    headings = "### shop/discount.py\n```python\ndef f(): pass\n```\n**app.py**:\n```python\nx = 1\n```\nFILE: `a.rs`\n```rust\nfn a() {}\n```"
    prose = "Here is the test:\n```js\ntest()\n```"
    assert [(o["path"], o["body"]) for o in edits.parse_ops(reopened)] == [("src/slugify.js", "function slugify(s) {}\n")]
    for reply in ("```python\nFILE: stats.py\n```python\ndef m(): pass\n```", "```python\nFILE: stats.py\ndef m(): pass\n```",
                  "```\nFILE: stats.py\n```python\ndef m(): pass\n```"):
        assert [(o["path"], o["body"]) for o in edits.parse_ops(reply)] == [("stats.py", "def m(): pass\n")], reply
    assert [o["path"] for o in edits.parse_ops(headings)] == ["shop/discount.py", "app.py", "a.rs"]
    assert edits.parse_ops(prose) == []
    plural = "FILES: shop/discount.py\n\n```python\n# shop/discount.py\n\ndef f(): pass\n```\n```python\n# v1.2\nx = 1\n```"
    assert [(o["path"], o["body"]) for o in edits.parse_ops(plural)] == [("shop/discount.py", "\ndef f(): pass\n")]
    fenced_edit = "FILE: a.py\n```python\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n```"
    assert edits.parse_ops(fenced_edit) == [{"kind": "edit", "path": "a.py", "pairs": [("x = 1", "x = 2")]}]


def test_fragment_is_spliced_into_the_file():
    big = "import os\n\n\n" + "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(10)) + "MAX = 5\n"
    root = _ws({"big.py": big.encode(), "prod.py": b"TIMEOUT = 30\nDEBUG = False\n",
                "lib.rs": b"pub fn a() -> u32 {\n    1\n}\n\npub fn b() -> u32 {\n    2\n}\n\npub fn c() -> u32 {\n    3\n}\n\npub fn d() -> u32 {\n    4\n}\n"})
    assert not edits.apply_ops(root, [
        {"kind": "write", "path": "big.py", "body": "import sys\n\ndef f3():\n    return sys.maxsize\n\ndef g():\n    return 0\n"},
        {"kind": "write", "path": "prod.py", "body": "TIMEOUT = 60\n"},
        {"kind": "write", "path": "lib.rs", "body": "pub fn b() -> u32 {\n    20\n}\n"}])
    out = (root / "big.py").read_text()
    assert "import os\nimport sys\n" in out and "return sys.maxsize" in out and "return 3\n" not in out
    assert "def f9():\n    return 9" in out and out.rstrip().endswith("return 0") and "MAX = 5" in out
    assert (root / "prod.py").read_text() == "TIMEOUT = 60\nDEBUG = False\n"
    rs = (root / "lib.rs").read_text()
    assert "    20\n" in rs and "    2\n" not in rs and "pub fn d()" in rs
    errs = edits.apply_ops(root, [{"kind": "write", "path": "big.py", "body": "print('hi')\n"}])
    assert errs and "บางส่วน" in errs[0]


def test_search_tolerates_whitespace_and_skips_already_applied():
    root = _ws({"q.js": b"test('fifo', () => { const q = 1; });\nmodule.exports = {};\n"})
    errs = edits.apply_ops(root, [{"kind": "edit", "path": "q.js", "pairs": [
        ("test('fifo', () => {\n  const q = 1;\n});", "test('fifo', () => {\n  const q = 2;\n});"),
        ("module.exports = {};", "module.exports = { q };"), ("module.exports = {};", "module.exports = { q };")]}])
    assert not errs, errs
    assert (root / "q.js").read_text() == "test('fifo', () => {\n  const q = 2;\n});\nmodule.exports = { q };\n"


def test_whole_file_like():
    old = "fn a() {\n    1\n}\n\n#[test]\nfn t() {}\n"
    assert edits.whole_file_like(old, "fn a() {\n    2\n}\n\n#[test]\nfn t() {}\n#[test]\nfn u() {}\n")
    assert not edits.whole_file_like(old, "test('peek', () => {});\n")


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


def test_checks_for_other_languages():
    root = _ws({"a.toml": b"x = [1,\n", "b.xml": b"<a><b></a>", "ok.toml": b"x = 1\n",
                "c.js": b"function f( {\n", "d.js": b"const x = 1;\n", "e.unknown": b"whatever"})
    report, ok = edits.run_checks(root, ["a.toml", "b.xml", "ok.toml", "e.unknown"], "", sys.executable)
    assert not ok and "syntax a.toml" in report and "syntax b.xml" in report and "ok.toml" not in report
    if edits.program("node"):
        report, ok = edits.run_checks(root, ["c.js"], "", sys.executable)
        assert not ok and "syntax c.js" in report
        assert edits.run_checks(root, ["d.js"], "", sys.executable)[1]


def test_detect_test_command():
    assert edits.detect_test_cmd(_ws({"package.json": b'{"scripts": {"test": "jest"}}'})) == "npm test --silent"
    assert edits.detect_test_cmd(_ws({"package.json": b'{"scripts": {"test": "echo \\"Error: no test specified\\""}}'})) == ""
    assert edits.detect_test_cmd(_ws({"go.mod": b"module x\n"})) == "go test ./..."
    assert edits.detect_test_cmd(_ws({"App.csproj": b"<Project/>"})) == "dotnet test --nologo"
    assert edits.detect_test_cmd(_ws({"Cargo.toml": b"[package]\n"})) == "cargo test -q"
    py = edits.detect_test_cmd(_ws({"tests/test_a.py": b"", "app.py": b""}))
    assert py in ("python -m pytest -q", "python -m unittest discover -q -s tests -t ."), py


def test_tools_search_read_list():
    ws = _ws({"src/cleaner.py": b"def clean_temp():\n    return 1\n", "README.md": b"# hi\n"})
    files = edits.project_files(ws)
    assert "src/cleaner.py:1: def clean_temp():" in edits.run_tool(ws, files, "SEARCH", "clean_temp")
    assert "src/cleaner.py:1:" in edits.run_tool(ws, files, "SEARCH", "clean_temp src/cleaner.py")
    assert edits.run_tool(ws, files, "SEARCH", "clean_temp README.md") == "(ไม่พบ)"
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
