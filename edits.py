"""Workspace engine: tools the agents use to look at the project, the edits they propose, and how edits are
applied to a COPY of the project, checked, committed and undone.

Edits a programmer may write (several in one answer):
  1) a full file:            ```python path=src/util.py  ... whole file ... ```
  2) search/replace:         FILE: src/util.py
                             <<<<<<< SEARCH
                             old text (must occur exactly once)
                             =======
                             new text
                             >>>>>>> REPLACE
  3) file operations:        DELETE: old.py      RENAME: a.py -> b.py
  4) shell commands:         RUN: npm install    (the user must approve each job's commands)
Tools any agent may call (one per line, answered in the next turn):
  SEARCH: regex-or-words     READ: path  or  READ: path:120-260     LIST: folder
"""
import difflib, json, locale, os, re, shutil, subprocess, tempfile, time
from pathlib import Path

FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
EDIT = re.compile(r"FILE:\s*`?([^\n`]+?)`?\s*\n<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE", re.S)
CMD = re.compile(r"^[ \t]*(DELETE|RENAME|RUN):[ \t]*(.+?)[ \t]*$", re.M)
TOOL = re.compile(r"^[ \t]*(SEARCH|READ|LIST):[ \t]*(.+?)[ \t]*$", re.M)
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".office_bak", "venv", ".venv", "dist", "build",
               ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode"}
TEXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".json", ".md", ".txt", ".cs",
        ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".yml",
        ".yaml", ".toml", ".ini", ".cfg", ".sql", ".xml", ".vue", ".svelte", ".spec", ".env.example", ".gitignore"}
MAX_FILE = 2_000_000
MAX_FILES = 20_000


class EditError(ValueError):
    pass


# ---------- reading/writing without damaging the file ----------

def read_text(p):
    """Text with \\n newlines, plus the file's encoding and newline style so a rewrite keeps both."""
    raw = Path(p).read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    first = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    for enc in (first, locale.getpreferredencoding(False), "latin-1"):  # latin-1 never fails, so nothing is lost
        try:
            return raw.decode(enc).replace("\r\n", "\n"), enc, nl
        except (UnicodeDecodeError, LookupError):
            continue


def write_text(p, text, enc="utf-8", nl="\n"):
    data = text.replace("\r\n", "\n")
    if nl == "\r\n":
        data = data.replace("\n", "\r\n")
    try:
        raw = data.encode(enc)
    except UnicodeEncodeError:  # new characters the old encoding cannot hold
        raw = data.encode("utf-8")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_bytes(raw)


# ---------- the project's files ----------

def is_git(ws):
    return (Path(ws) / ".git").exists() and shutil.which("git") is not None


def project_files(ws):
    """Relative paths of the project's files: git's view (tracked + untracked, not ignored) or a filtered walk."""
    out = []
    if is_git(ws):
        r = subprocess.run(["git", "-C", str(ws), "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            out = [p for p in r.stdout.decode("utf-8", "replace").split("\0")
                   if p and not p.startswith((".office_bak/", ".git/")) and (Path(ws) / p).is_file()]
            return sorted(out)[:MAX_FILES]
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        out += [Path(root, f).relative_to(ws).as_posix() for f in files if not f.endswith(".pyc")]
        if len(out) >= MAX_FILES:
            break
    return sorted(out)[:MAX_FILES]


def is_text(rel):
    p = Path(rel)
    return p.suffix.lower() in TEXT or p.name in TEXT or p.name in ("Dockerfile", "Makefile", "README", "LICENSE")


def summary(files, limit=300):
    """File list for prompts: everything when small, otherwise folders with counts plus the first files."""
    if len(files) <= limit:
        return ", ".join(files) or "(ว่าง)"
    folders = {}
    for f in files:
        top = f.split("/")[0] if "/" in f else "."
        folders[top] = folders.get(top, 0) + 1
    tops = ", ".join(f"{k}/ ({v})" for k, v in sorted(folders.items()))
    return f"{len(files)} files. Folders: {tops}. First files: {', '.join(files[:limit])}. Use LIST/SEARCH to find more."


# ---------- tools ----------

def safe_target(root, rel):
    t = (Path(root) / rel.strip().strip("`'\"")).resolve()
    if not t.is_relative_to(Path(root).resolve()):
        raise EditError("path อยู่นอกโฟลเดอร์งาน: " + rel)
    return t


def tool_calls(reply):
    """SEARCH/READ/LIST lines outside code blocks."""
    return TOOL.findall(EDIT.sub("", FENCE.sub("", reply)))


def run_tool(ws, files, kind, arg, max_lines=400):
    arg = arg.strip().strip("`")
    if kind == "SEARCH":
        try:
            rx = re.compile(arg, re.I)
        except re.error:
            rx = re.compile(re.escape(arg), re.I)
        hits = []
        for rel in files:
            if not is_text(rel):
                continue
            try:
                lines = read_text(Path(ws) / rel)[0].split("\n")
            except OSError:
                continue
            hits += [f"{rel}:{i}: {line.strip()[:160]}" for i, line in enumerate(lines, 1) if rx.search(line)]
            if len(hits) >= 40:
                return "\n".join(hits[:40]) + "\n(ตัดไว้ที่ 40 รายการ ค้นให้แคบลงได้)"
        return "\n".join(hits) or "(ไม่พบ)"
    if kind == "LIST":
        prefix = "" if arg in (".", "/", "") else arg.strip("/") + "/"
        seen = sorted({f[len(prefix):].split("/")[0] + ("/" if "/" in f[len(prefix):] else "") for f in files if f.startswith(prefix)})
        return "\n".join(seen[:200]) or "(ว่างหรือไม่มีโฟลเดอร์นี้)"
    m = re.match(r"(.+?)(?::(\d+)(?:-(\d+))?)?$", arg)  # READ: path[:start-end]
    rel = m.group(1)
    p = safe_target(ws, rel)
    if not p.is_file():
        return f"(ไม่มีไฟล์ {rel})"
    lines = read_text(p)[0].split("\n")
    start = int(m.group(2) or 1)
    end = min(int(m.group(3) or len(lines)), start + max_lines - 1, len(lines))
    more = f"\n(ไฟล์มี {len(lines)} บรรทัด อ่านต่อ: READ: {rel}:{end + 1}-{end + max_lines})" if end < len(lines) else ""
    return f"--- {rel} (บรรทัด {start}-{end}) ---\n" + "\n".join(lines[start - 1:end]) + more


# ---------- edits ----------

def fences(text):
    """Code fences as dicts: lang, path (from 'path=...' on the fence line, or None), body."""
    out = []
    for info, body in FENCE.findall(text):
        m = re.search(r"path=(\S+)", info)
        if not m:  # small models sometimes put it as the first line of the body instead
            m = re.match(r"\s*(?:#\s*)?path=(\S+)[^\n]*\n", body)
            body = body[m.end():] if m else body
        out.append({"lang": (info.split() or [""])[0].lower(), "path": m.group(1).strip("\"'`") if m else None, "body": body})
    return out


def parse_ops(reply):
    """Changes in a reply: write / edit / delete / rename / run."""
    ops = [{"kind": "write", "path": f["path"], "body": f["body"]} for f in fences(reply) if f["path"]]
    found = EDIT.findall(reply)
    for path in dict.fromkeys(p.strip() for p, _, _ in found):  # one op per file, in the order they appear
        ops.append({"kind": "edit", "path": path, "pairs": [(s, r) for p, s, r in found if p.strip() == path]})
    for kind, arg in CMD.findall(EDIT.sub("", FENCE.sub("", reply))):
        arg = arg.strip().strip("`")
        if kind == "DELETE":
            ops.append({"kind": "delete", "path": arg})
        elif kind == "RENAME":
            m = re.match(r"`?(.+?)`?\s*(?:->|→|=>)\s*`?(.+?)`?$", arg)
            if m:
                ops.append({"kind": "rename", "path": m.group(1).strip(), "to": m.group(2).strip()})
        else:
            ops.append({"kind": "run", "cmd": arg})
    return ops


def apply_ops(root, ops):
    """Apply file ops inside root (a staging copy). RUN ops are left to the caller. Returns error messages."""
    errors = []
    for op in ops:
        try:
            if op["kind"] == "run":
                continue
            t = safe_target(root, op["path"])
            if op["kind"] == "delete":
                if not t.is_file():
                    raise EditError(f"{op['path']}: ไม่มีไฟล์ให้ลบ")
                t.unlink()
            elif op["kind"] == "rename":
                dst = safe_target(root, op["to"])
                if not t.is_file():
                    raise EditError(f"{op['path']}: ไม่มีไฟล์ให้เปลี่ยนชื่อ")
                if dst.exists():
                    raise EditError(f"{op['to']}: มีไฟล์นี้อยู่แล้ว")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(t), str(dst))
            elif op["kind"] == "write":
                enc, nl = (read_text(t)[1:] if t.is_file() else ("utf-8", "\n"))
                write_text(t, op["body"], enc, nl)
            else:
                if not t.is_file():
                    raise EditError(f"{op['path']}: ไม่มีไฟล์นี้ (ถ้าต้องการสร้างใหม่ ใช้ block เต็มไฟล์ path=...)")
                text, enc, nl = read_text(t)
                for s, r in op["pairs"]:
                    n = text.count(s)
                    if n != 1:
                        raise EditError(f"{op['path']}: ข้อความ SEARCH พบ {n} ครั้ง (ต้องพบพอดี 1 ครั้ง) ให้คัดลอกข้อความจากไฟล์ให้ตรงทุกตัวอักษร")
                    text = text.replace(s, r, 1)
                write_text(t, text, enc, nl)
        except (EditError, OSError) as e:
            errors.append(str(e))
    return errors


# ---------- staging copy, checks, diff, commit, undo ----------

def stage(ws, files):
    """Copy the project into a temp folder. Edits, commands and checks only ever touch this copy."""
    d = Path(tempfile.mkdtemp(prefix="aioffice_stage_"))
    copied = []
    for rel in files:
        src = Path(ws) / rel
        try:
            if src.stat().st_size > MAX_FILE:
                continue
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d / rel)
            copied.append(rel)
        except OSError:
            continue
    return d, copied


def _walk(root):
    out = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in IGNORE_DIRS]
        out += [Path(r, f).relative_to(root).as_posix() for f in files if not f.endswith(".pyc")]
    return out


def changes(ws, root, copied):
    """What the copy now differs in: (changed or added files, deleted files)."""
    changed, deleted = [], []
    for rel in copied:  # files that were copied: compare directly (also those inside folders the walk skips)
        a, b = Path(ws) / rel, Path(root) / rel
        if not b.exists():
            deleted.append(rel)
            continue
        try:
            sa, sb = a.stat(), b.stat()
            if (sa.st_size, sa.st_mtime_ns) != (sb.st_size, sb.st_mtime_ns) and a.read_bytes() != b.read_bytes():
                changed.append(rel)
        except OSError:
            changed.append(rel)
    base = set(copied)
    added = [r for r in _walk(root) if r not in base]
    if added and is_git(ws):  # build artifacts a command left behind are not changes
        r = subprocess.run(["git", "-C", str(ws), "check-ignore", "--stdin"], input="\n".join(added),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        ignored = set(r.stdout.split("\n"))
        added = [a for a in added if a not in ignored]
    return sorted(changed + added), sorted(deleted)


def run_cmds(root, cmds, timeout=120):
    out = []
    for c in cmds:
        try:
            r = subprocess.run(c, shell=True, cwd=root, capture_output=True, text=True, errors="replace", timeout=timeout)
            out.append(f"$ {c}\nexit={r.returncode}\n{(r.stdout + r.stderr)[-600:]}")
        except subprocess.TimeoutExpired:
            out.append(f"$ {c}\ntimeout {timeout}s")
    return "\n".join(out)


def run_checks(root, changed, test_cmd, pyexe):
    """Syntax checks for changed files (never executes them) and the project's own test command if set."""
    report, ok = [], True
    for rel in changed:
        p = Path(root) / rel
        if rel.endswith(".py") and pyexe:
            r = subprocess.run([pyexe, "-I", "-m", "py_compile", str(p)], capture_output=True, text=True, errors="replace", timeout=30)
            if r.returncode:
                ok = False
                report.append(f"syntax {rel}: " + (r.stderr or r.stdout)[-300:])
        elif rel.endswith(".json"):
            try:
                json.loads(read_text(p)[0])
            except ValueError as e:
                ok = False
                report.append(f"syntax {rel}: {e}")
        elif rel.endswith((".js", ".mjs", ".cjs")) and shutil.which("node"):
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, errors="replace", timeout=30)
            if r.returncode:
                ok = False
                report.append(f"syntax {rel}: " + (r.stderr or r.stdout)[-300:])
    if test_cmd:
        try:
            r = subprocess.run(test_cmd, shell=True, cwd=root, capture_output=True, text=True, errors="replace", timeout=300)
            ok = ok and r.returncode == 0
            report.append(f"tests `{test_cmd}` exit={r.returncode}\n{(r.stdout + r.stderr)[-800:]}")
        except subprocess.TimeoutExpired:
            ok = False
            report.append(f"tests `{test_cmd}`: timeout 300s")
    return ("\n".join(report) or "ไม่มีการตรวจเพิ่มเติม"), ok


def diff_for(ws, root, changed, deleted=(), limit=8000):
    out = ""
    for rel in changed:
        old_p = Path(ws) / rel
        old = read_text(old_p)[0] if old_p.exists() else ""
        new = read_text(Path(root) / rel)[0] if is_text(rel) or (Path(root) / rel).stat().st_size < 200_000 else "(ไฟล์ไบนารี)"
        out += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            f"a/{rel}" if old_p.exists() else "/dev/null", f"b/{rel}"))
    for rel in deleted:
        out += f"--- a/{rel}\n+++ /dev/null\n(ลบไฟล์)\n"
    return out[:limit]


def commit(ws, root, changed, deleted=()):
    """Copy checked files into the project and delete removed ones. Originals go to .office_bak/<run>/ for undo."""
    run = Path(ws) / ".office_bak" / time.strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    exclude = Path(ws) / ".git" / "info" / "exclude"
    if (Path(ws) / ".git").is_dir() and ".office_bak/" not in (exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""):
        exclude.parent.mkdir(parents=True, exist_ok=True)  # keep backups out of git without touching the user's .gitignore
        with open(exclude, "a", encoding="utf-8") as f:
            f.write("\n.office_bak/\n")
    manifest = []
    for rel in list(changed) + list(deleted):
        dst = Path(ws) / rel
        existed = dst.exists()
        if existed:
            (run / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, run / rel)
        manifest.append({"path": rel, "existed": existed, "deleted": rel in deleted})
        if rel in deleted:
            if existed:
                dst.unlink()
        else:
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
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(run / m["path"], dst)
        elif dst.exists():
            dst.unlink()
    (run / "manifest.json").rename(run / "undone.json")
    return run.name, [m["path"] for m in manifest]


def pick_files(task, notes, files, read=(), limit=10):
    """Files the job is about: read by an agent, named in the request/notes, or matching a word of the request."""
    text = task + "\n" + notes
    picked = [f for f in read if f in files]
    picked += [f for f in files if f not in picked and (f in text or ("/" not in f and Path(f).name in text) or
                                                        ("/" in f and Path(f).name in text and len(Path(f).name) > 6))]
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task.lower()))
    picked += [f for f in files if f not in picked and is_text(f) and Path(f).stem.lower() in words]
    return picked[:limit]


def file_bundle(ws, picked, budget=36000, per_file=20000):
    txt, left = "", budget
    for f in picked:
        p = Path(ws) / f
        if not p.is_file() or not is_text(f):
            continue
        body = read_text(p)[0]
        cut = body[:min(left, per_file)]
        note = f"\n(ตัดไว้ {len(cut)} จาก {len(body)} ตัวอักษร อ่านต่อด้วย READ: {f}:<บรรทัด>)" if len(cut) < len(body) else ""
        txt += f"\n--- {f} ---\n{cut}{note}\n--- end {f} ---\n"
        left -= len(cut)
        if left <= 0:
            break
    return txt


# ---------- git ----------

def git(ws, *args, timeout=60):
    r = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def git_status(ws):
    if not is_git(ws):
        return {"git": False}
    _, branch = git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    _, short = git(ws, "status", "--short")
    return {"git": True, "branch": branch, "dirty": len([l for l in short.splitlines() if l.strip()]), "status": short[:2000]}


def git_commit(ws, paths, message):
    """Commit exactly these paths. On main/master, a new branch is created first so the user's branch stays clean."""
    _, branch = git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    note = ""
    if branch in ("main", "master"):
        new = "ai-office/" + time.strftime("%Y%m%d-%H%M%S")
        code, out = git(ws, "checkout", "-b", new)
        if code:
            return f"สร้าง branch ไม่สำเร็จ: {out[:200]}"
        note = f"สร้าง branch {new} · "
    git(ws, "add", "-A", "--", *paths)
    code, out = git(ws, "commit", "-m", message, "--", *paths)
    return note + ("commit แล้ว" if code == 0 else f"commit ไม่สำเร็จ: {out[:200]}")
