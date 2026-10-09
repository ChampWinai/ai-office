"""Edit engine for the workspace: parse the model's edits, apply them to a COPY of the project, check, then commit.

Two formats the programmer may use (both work in one answer):
  1) a full file:            ```python path=src/util.py  ... whole file ... ```
  2) search/replace edits:   FILE: src/util.py
                             <<<<<<< SEARCH
                             old text (must occur exactly once)
                             =======
                             new text
                             >>>>>>> REPLACE
"""
import difflib, json, os, re, shutil, subprocess, tempfile, time
from pathlib import Path

FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
EDIT = re.compile(r"FILE:\s*`?([^\n`]+?)`?\s*\n<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE", re.S)


class EditError(ValueError):
    pass


def fences(text):
    """Code fences as dicts: lang, path (from 'path=...' on the fence line, or None), body."""
    out = []
    for info, body in FENCE.findall(text):
        m = re.search(r"path=(\S+)", info)
        if not m:  # small models sometimes put it as the first line of the body instead
            m = re.match(r"\s*(?:#\s*)?path=(\S+)[^\n]*\n", body)
            body = body[m.end():] if m else body
        out.append({"lang": (info.split() or [""])[0].lower(), "path": m.group(1).strip("\"'") if m else None, "body": body})
    return out


def parse_ops(reply):
    """All changes in a reply, in order: {"kind": "write", "path", "body"} or {"kind": "edit", "path", "pairs"}."""
    ops = [{"kind": "write", "path": f["path"], "body": f["body"]} for f in fences(reply) if f["path"]]
    found = EDIT.findall(reply)
    for path in dict.fromkeys(p.strip() for p, _, _ in found):  # one op per file, in the order they appear
        ops.append({"kind": "edit", "path": path, "pairs": [(s, r) for p, s, r in found if p.strip() == path]})
    return ops


def safe_target(root, rel):
    t = (Path(root) / rel).resolve()
    if not t.is_relative_to(Path(root).resolve()):
        raise EditError("path อยู่นอกโฟลเดอร์งาน: " + rel)
    return t


def apply_ops(root, ops):
    """Apply ops inside root (a staging copy). Returns (changed paths, error messages)."""
    changed, errors = [], []
    for op in ops:
        try:
            t = safe_target(root, op["path"])
            if op["kind"] == "write":
                new = op["body"]
            else:
                if not t.exists():
                    raise EditError(f"{op['path']}: ไม่มีไฟล์นี้ (ถ้าต้องการสร้างใหม่ ใช้ block เต็มไฟล์ path=...)")
                new = t.read_text(encoding="utf-8", errors="replace")
                for s, r in op["pairs"]:
                    n = new.count(s)
                    if n != 1:
                        raise EditError(f"{op['path']}: ข้อความ SEARCH พบ {n} ครั้ง (ต้องพบพอดี 1 ครั้ง) ให้คัดลอกข้อความจากไฟล์ให้ตรงทุกตัวอักษร")
                    new = new.replace(s, r, 1)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(new, encoding="utf-8")
            if op["path"] not in changed:
                changed.append(op["path"])
        except (EditError, OSError) as e:
            errors.append(str(e))
    return changed, errors


def stage(ws, skip, limit_bytes=2_000_000, max_files=2000):
    """Copy the project into a temp folder. Edits and checks only ever touch this copy."""
    d = Path(tempfile.mkdtemp(prefix="aioffice_stage_"))
    n = 0
    for root, dirs, files in os.walk(ws):
        dirs[:] = [x for x in dirs if x not in skip]
        for f in files:
            src = Path(root, f)
            if src.stat().st_size > limit_bytes:
                continue
            dst = d / src.relative_to(ws)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
            if n >= max_files:
                return d
    return d


def run_checks(root, changed, test_cmd, pyexe):
    """Syntax-check changed .py files (never executes them) and run the project's own test command if set."""
    report, ok = [], True
    for rel in changed:
        if rel.endswith(".py") and pyexe:
            r = subprocess.run([pyexe, "-I", "-m", "py_compile", str(Path(root) / rel)], capture_output=True, text=True, timeout=30)
            if r.returncode:
                ok = False
                report.append(f"syntax {rel}: " + (r.stderr or r.stdout)[-300:])
    if test_cmd:
        try:
            r = subprocess.run(test_cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=180)
            ok = ok and r.returncode == 0
            report.append(f"tests `{test_cmd}` exit={r.returncode}\n{(r.stdout + r.stderr)[-800:]}")
        except subprocess.TimeoutExpired:
            ok = False
            report.append(f"tests `{test_cmd}`: timeout 180s")
    return ("\n".join(report) or "ไม่มีการตรวจเพิ่มเติม"), ok


def diff_for(ws, root, changed, limit=6000):
    out = ""
    for rel in changed:
        old_p, new_p = Path(ws) / rel, Path(root) / rel
        old = old_p.read_text(encoding="utf-8", errors="replace") if old_p.exists() else ""
        new = new_p.read_text(encoding="utf-8", errors="replace")
        out += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            f"a/{rel}" if old else "/dev/null", f"b/{rel}"))
    return out[:limit]


def commit(ws, root, changed):
    """Copy the checked files into the project. Originals go to .office_bak/<run>/ with a manifest for undo."""
    run = Path(ws) / ".office_bak" / time.strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    manifest = []
    for rel in changed:
        dst = Path(ws) / rel
        existed = dst.exists()
        if existed:
            (run / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, run / rel)
        manifest.append({"path": rel, "existed": existed})
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / rel, dst)
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return run.name


def undo_last(ws):
    """Restore the newest run that has not been undone yet."""
    bak = Path(ws) / ".office_bak"
    runs = sorted(p for p in bak.glob("*") if (p / "manifest.json").exists()) if bak.exists() else []
    if not runs:
        raise ValueError("ไม่มีการแก้ไขให้ย้อนกลับ")
    run = runs[-1]
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    for m in manifest:
        dst = Path(ws) / m["path"]
        if m["existed"]:
            shutil.copy2(run / m["path"], dst)
        elif dst.exists():
            dst.unlink()
    (run / "manifest.json").rename(run / "undone.json")
    return run.name, [m["path"] for m in manifest]


def pick_files(task, notes, files, limit=8):
    """Files the job is about: named in the request or the department notes, or matching a word of the request."""
    text = task + "\n" + notes
    picked = [f for f in files if f in text or Path(f).name in text]
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task.lower()))
    picked += [f for f in files if f not in picked and Path(f).stem.lower() in words]
    return picked[:limit]


def file_bundle(ws, picked, budget=24000):
    txt, left = "", budget
    for f in picked:
        body = (Path(ws) / f).read_text(encoding="utf-8", errors="replace")[:min(left, 8000)]
        left -= len(body)
        txt += f"\n--- {f} ---\n{body}\n--- end {f} ---\n"
        if left <= 0:
            break
    return txt
