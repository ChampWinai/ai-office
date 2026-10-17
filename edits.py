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
TOOL = re.compile(r"^[ \t]*(SEARCH|READ|LIST|WEB|FETCH|MCP):[ \t]*(.+?)[ \t]*$", re.M)
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
        word, _, where = arg.rpartition(" ")  # "SEARCH: name path/or/folder" = search only there
        if word.strip() and any(f == where or f.startswith(where.rstrip("/") + "/") for f in files):
            arg, files = word.strip(), [f for f in files if f == where or f.startswith(where.rstrip("/") + "/")]
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

PATHLINE = re.compile(r"^[ \t>#*\-]*(?:(?:FILES?|Files?|files?|path|Path)\s*[:=]\s*)?[`*\"']*([\w.\-/\\]+\.\w+)[`*\"']*[ \t]*:?[ \t]*$")


def _reopened(text):
    """'```js' / 'FILE: a.js' (or path=a.js) / '```js' then code: the path got its own fence line and the next one re-opens
    the block (a fence with a language never closes one). Rewritten to '```js path=a.js'."""
    lines, out, inside, i = text.split("\n"), [], False, 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("```"):
            pm = i + 2 < len(lines) and not inside and re.match(r"\s*(?:#|//)?\s*(?:path=|FILE:\s*)`?([^\s`]+)`?\s*$", lines[i + 1])
            if pm and re.match(r"\s*```\w+\s*$", lines[i + 2]):
                out.append(f"```{ln[3:].strip() or lines[i + 2].strip()[3:]} path={pm.group(1)}")
                inside, i = True, i + 3
                continue
            inside = not inside
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def fences(text):
    """Code fences as dicts: lang, path, body. The path comes from 'path=...' on the fence line, the first body line,
    or a line right above the fence that is only a file name (e.g. 'FILE: a.py', '### a.py', '**a.py**:')."""
    text = _reopened(text)
    out, prev = [], 0
    for f in FENCE.finditer(text):
        info, body = f.group(1), f.group(2)
        above = [ln for ln in text[prev:f.start()].split("\n") if ln.strip()]
        prev = f.end()
        m = re.search(r"path=(\S+)", info)
        if not m:  # small models sometimes put it as the first line of the body instead
            m = (re.match(r"\s*(?:#|//)?\s*(?:path=|FILE:[ \t]*)`?([^\s`]+)`?[^\n]*\n", body)
                 or re.match(r"\s*(?:#|//)[ \t]*([\w.\-/]*[\w\-]\.[A-Za-z]\w{0,4})[ \t]*\n", body))  # "# shop/cart.py"
            body = body[m.end():] if m else body
        if not m and above:
            m = PATHLINE.match(above[-1])
        if "<<<<<<< SEARCH" in body:  # search/replace inside a fence is an edit, never a whole file
            m = None
        out.append({"lang": (info.split() or [""])[0].lower(), "path": m.group(1).strip("\"'`").replace("\\", "/") if m else None, "body": body})
    for i, f in enumerate(out[:-1]):  # an empty block holding only "path=x" followed by the code in its own block
        if f["path"] and not f["body"].strip() and not out[i + 1]["path"]:
            out[i + 1]["path"], f["path"] = f["path"], None
    return out


def parse_ops(reply):
    """Changes in a reply: write / edit / delete / rename / run."""
    ops = [{"kind": "write", "path": f["path"], "body": f["body"]} for f in fences(reply) if f["path"]]
    found = EDIT.findall(re.sub(r"(FILE:[^\n]*\n)[ \t]*```[^\n]*\n(?=<<<<<<< SEARCH)", r"\1", reply))  # FILE line, then a fence
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


def loose_find(text, s):
    """(start, end) of the whole lines of text that equal s when all whitespace is ignored, if exactly one place does."""
    idx = [i for i, c in enumerate(text) if not c.isspace()]
    norm, ns = "".join(text[i] for i in idx), "".join(s.split())
    if not ns or norm.count(ns) != 1:
        return None
    k = norm.find(ns)
    a, b = idx[k], idx[k + len(ns) - 1] + 1
    ls, le = text.rfind("\n", 0, a) + 1, (text.find("\n", b) + 1 or len(text) + 1) - 1
    return (ls if not text[ls:a].strip() else a), (le if not text[b:le].strip() else b)


HEAD = re.compile(r"^(?:(?:export|pub(?:\([\w:]+\))?|async|static|public|private|protected|default|unsafe)\s+)*"
                  r"(?:def|class|function|fn|func|struct|enum|impl|interface|trait|type|const|let|var|mod|namespace|module)\s+(?:\([^)]*\)\s*)?(\w+)|^(\w+)\s*(?::[^=\n]*)?=(?!=)")
IMPORT = re.compile(r"^\s*(?:import|from|using|use|#include|require|package)\b|^\s*(?:const|let|var)\s+.*=\s*require\(")


def units(lines):
    """Top-level definitions (functions, classes, constants...) as {name: (start, end)} line ranges, any language."""
    out, i = {}, 0
    while i < len(lines):
        m = HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = i, _extent(lines, i)
        while start and lines[start - 1].startswith(("@", "#[")):  # decorators / attributes belong to the definition below
            start -= 1
        out.setdefault(m.group(1) or m.group(2), (start, end))
        i = end
    return out


def _extent(lines, i):
    """Index after the definition starting at line i: indentation for Python blocks, bracket balance otherwise."""
    depth, j = 0, i
    while j < len(lines):
        depth += sum(lines[j].count(c) for c in "([{") - sum(lines[j].count(c) for c in ")]}")
        j += 1
        if depth > 0:
            continue
        if lines[j - 1].rstrip().endswith(":") and re.match(r"(?:async\s+)?(?:def|class)\b", lines[i]):  # Python block
            while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace()):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            return j
        nxt = next((ln for ln in lines[j:] if ln.strip()), "")
        if not nxt.lstrip().startswith("{"):  # not an Allman-style "{" on the next line
            return j
    return j


def is_fragment(old, new):
    """New content that leaves out some of the old file's top-level definitions and is much shorter: a fragment.
    ponytail: a real request to delete most of a file looks the same; DEV then has to use SEARCH/REPLACE or DELETE."""
    ol, nl = [x for x in old.split("\n") if x.strip()], [x for x in new.split("\n") if x.strip()]
    return bool(ol) and len(nl) < 0.6 * len(ol) and bool(set(units(old.split("\n"))) - set(units(new.split("\n"))))


def splice(old, frag):
    """Put the definitions of frag into old: replace those with the same name, append new ones, add missing imports.
    None if frag has text that is neither a definition, an import, nor already in old."""
    ol, fl = old.split("\n"), frag.strip("\n").split("\n")
    fu, ou = units(fl), units(ol)
    if not fu:
        return None
    inside = {k for a, b in fu.values() for k in range(a, b)}
    have = {x.strip() for x in ol}
    loose = [x for k, x in enumerate(fl) if k not in inside and x.strip() and x.strip() not in have]
    if any(not IMPORT.match(x) for x in loose):
        return None
    for name, (a, b) in sorted(((n, ou[n]) for n in fu if n in ou), key=lambda u: -u[1][0]):
        ol[a:b] = fl[fu[name][0]:fu[name][1]]
    for name in [n for n in fu if n not in ou]:
        ol += [""] * (2 if ol[-1].strip() else 1) + fl[fu[name][0]:fu[name][1]] + [""]
    last_import = max((k for k, x in enumerate(ol) if IMPORT.match(x)), default=-1)
    ol[last_import + 1:last_import + 1] = loose
    return "\n".join(ol)


def whole_file_like(old, body):
    """A code block without a path counts as the new version of old only if it keeps most of old's lines."""
    ol = {x.strip() for x in old.split("\n") if x.strip()}
    return bool(ol) and len(ol & {x.strip() for x in body.split("\n")}) >= 0.5 * len(ol)


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
                if not op["body"].strip() and t.is_file() and t.stat().st_size:
                    raise EditError(f"{op['path']}: เนื้อหาว่าง จึงไม่เขียนทับ (ถ้าต้องการลบไฟล์ให้ใช้ DELETE:)")
                old, enc, nl = read_text(t) if t.is_file() else ("", "utf-8", "\n")
                body = op["body"]
                if is_fragment(old, body):  # only some functions of a bigger file: put them in place, keep the rest
                    body = splice(old, body)
                    if body is None:
                        raise EditError(f"{op['path']}: ส่งมาแค่บางส่วนของไฟล์ ให้ส่งทั้งไฟล์ หรือใช้ FILE + SEARCH/REPLACE")
                write_text(t, body, enc, nl)
            else:
                if not t.is_file():
                    raise EditError(f"{op['path']}: ไม่มีไฟล์นี้ (ถ้าต้องการสร้างใหม่ ใช้ block เต็มไฟล์ path=...)")
                text, enc, nl = read_text(t)
                for s, r in op["pairs"]:
                    n = text.count(s)
                    if n == 1:
                        text = text.replace(s, r, 1)
                    elif n == 0 and r.strip() and text.count(r) == 1:
                        continue  # already applied in an earlier round
                    elif n == 0 and (span := loose_find(text, s)):
                        text = text[:span[0]] + r.strip("\n") + text[span[1]:]
                    else:
                        raise EditError(f"{op['path']}: ข้อความ SEARCH พบ {n} ครั้ง (ต้องพบพอดี 1 ครั้ง) ให้คัดลอกข้อความจากไฟล์ให้ตรงทุกตัวอักษร")
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


# ---------- checks for every language ----------

# extension -> (program, args before the file). Syntax-only checks that need no project setup: a failure blocks the write.
STRICT = {".js": ("node", ["--check"]), ".mjs": ("node", ["--check"]), ".cjs": ("node", ["--check"]),
          ".php": ("php", ["-l"]), ".rb": ("ruby", ["-c"]), ".go": ("gofmt", ["-e", "-l"]), ".lua": ("luac", ["-p"])}
# Checks that may fail for reasons outside the change (missing headers or packages): reported to QA as warnings only.
LOOSE = {".sh": ("bash", ["-n"]), ".c": ("gcc", ["-fsyntax-only"]), ".cpp": ("g++", ["-fsyntax-only"]),
         ".cc": ("g++", ["-fsyntax-only"]), ".h": ("gcc", ["-fsyntax-only"])}
# (extensions, marker files, command) - whole-project compile checks, once per job, warnings only
PROJECT = [({".ts", ".tsx"}, ("tsconfig.json",), ["tsc", "--noEmit", "-p", "."]),
           ({".rs"}, ("Cargo.toml",), ["cargo", "check", "-q"]),
           ({".go"}, ("go.mod",), ["go", "vet", "./..."]),
           ({".cs"}, ("*.csproj", "*.sln"), ["dotnet", "build", "--nologo", "-v", "q"]),
           ({".java"}, ("pom.xml",), ["mvn", "-q", "-o", "compile"]),
           ({".kt", ".java"}, ("gradlew.bat", "gradlew"), ["gradlew", "-q", "compileJava"])]


def program(name, root=None):
    """A program on PATH, or the project's own node_modules/.bin or wrapper copy."""
    if root:
        for cand in (Path(root) / "node_modules" / ".bin" / (name + ".cmd"), Path(root) / "node_modules" / ".bin" / name,
                     Path(root) / (name + ".bat"), Path(root) / name):
            if cand.is_file():
                return str(cand)
    return shutil.which(name)


def _run(cmd, cwd, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, f"timeout {timeout}s"


def check_file(root, rel, pyexe):
    """(status, message): status is "ok", "error" (blocks the write), "warn" (for QA only) or "skip"."""
    p = Path(root) / rel
    ext = p.suffix.lower()
    try:
        if ext == ".py":
            if not pyexe:
                return "skip", "ไม่มี Python"
            code, out = _run([pyexe, "-I", "-m", "py_compile", str(p)], root)
            return ("error", out[-300:]) if code else ("ok", "")
        if ext == ".json":
            json.loads(read_text(p)[0])
        elif ext == ".toml":
            import tomllib
            tomllib.loads(read_text(p)[0])
        elif ext in (".xml", ".csproj", ".props", ".config", ".xaml"):
            import xml.etree.ElementTree as ET
            ET.fromstring(read_text(p)[0].encode("utf-8"))
        elif ext in (".yml", ".yaml"):
            try:
                import yaml
            except ImportError:
                return "skip", "ไม่มี PyYAML"
            list(yaml.safe_load_all(read_text(p)[0]))
        elif ext == ".ps1":
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if not ps:
                return "skip", "ไม่มี PowerShell"
            script = ("$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e);"
                      "if($e){$e|%%{$_.Message};exit 1}" % str(p).replace("'", "''"))
            code, out = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], root)
            return ("warn", out[-300:]) if code else ("ok", "")
        elif ext in STRICT or ext in LOOSE:
            prog, args = STRICT.get(ext) or LOOSE[ext]
            exe = program(prog, root)
            if not exe:
                return "skip", f"ไม่มี {prog} ในเครื่อง"
            code, out = _run([exe, *args, str(p)], root)
            if ext == ".go" and not code and out:  # gofmt -l lists files that only need formatting
                return "ok", ""
            return (("error" if ext in STRICT else "warn"), out[-300:]) if code else ("ok", "")
        else:
            return "skip", ""
        return "ok", ""
    except (ValueError, SyntaxError) as e:  # json/toml/xml/yaml parse errors
        return "error", str(e)[:300]
    except Exception as e:  # e.g. yaml.YAMLError
        return "error", f"{type(e).__name__}: {str(e)[:300]}"


def run_checks(root, changed, test_cmd, pyexe):
    """Per-file syntax checks for every language we know (never executes the project's code), whole-project
    compile checks as warnings, then the project's own test command if set (must exit 0)."""
    report, ok = [], True
    for rel in changed:
        status, msg = check_file(root, rel, pyexe)
        if status == "error":
            ok = False
            report.append(f"syntax {rel}: {msg}")
        elif status == "warn":
            report.append(f"warning {rel}: {msg}")
        elif status == "skip" and msg:
            report.append(f"(ข้ามการตรวจ {rel}: {msg})")
    exts = {Path(c).suffix.lower() for c in changed}
    for kinds, markers, cmd in PROJECT:
        if exts & kinds and any(list(Path(root).glob(m)) for m in markers):
            exe = program(cmd[0], root)
            if exe:
                code, out = _run([exe, *cmd[1:]], root, timeout=300)
                if code:
                    report.append(f"warning `{' '.join(cmd)}` exit={code}\n{out[-800:]}")
    if test_cmd:
        try:
            r = subprocess.run(test_cmd, shell=True, cwd=root, capture_output=True, text=True, errors="replace", timeout=300)
            ok = ok and r.returncode == 0
            report.append(f"tests `{test_cmd}` exit={r.returncode}\n{(r.stdout + r.stderr)[-800:]}")
        except subprocess.TimeoutExpired:
            ok = False
            report.append(f"tests `{test_cmd}`: timeout 300s")
    return ("\n".join(report) or "ไม่มีการตรวจเพิ่มเติม"), ok


def detect_test_cmd(ws):
    """A test command guessed from the project's build files (the user still saves it)."""
    ws = Path(ws)
    if (ws / "package.json").is_file():
        try:
            test = json.loads(read_text(ws / "package.json")[0]).get("scripts", {}).get("test", "")
        except ValueError:
            test = ""
        if test and "no test specified" not in test:
            return "npm test --silent"
    if (ws / "Cargo.toml").is_file():
        return "cargo test -q"
    if (ws / "go.mod").is_file():
        return "go test ./..."
    if list(ws.glob("*.sln")) or list(ws.glob("*.csproj")):
        return "dotnet test --nologo"
    if (ws / "pom.xml").is_file():
        return "mvn -q test"
    if (ws / "gradlew.bat").is_file() or (ws / "gradlew").is_file():
        return "gradlew test -q"
    if (ws / "composer.json").is_file() and (ws / "vendor" / "bin" / "phpunit").exists():
        return "vendor/bin/phpunit"
    if (ws / "Gemfile").is_file() and (ws / "spec").is_dir():
        return "bundle exec rspec"
    py_tests = list(ws.glob("test_*.py")) + list(ws.glob("tests/test_*.py")) + list(ws.glob("tests/**/test_*.py"))
    if py_tests:
        uses_pytest = shutil.which("pytest") or "pytest" in "".join(
            read_text(f)[0] for f in (ws / "pyproject.toml", ws / "requirements.txt", ws / "setup.cfg", ws / "pytest.ini") if f.is_file())
        if uses_pytest:
            return "python -m pytest -q"
        return "python -m unittest discover -q" + (" -s tests -t ." if (ws / "tests").is_dir() and not list(ws.glob("test_*.py")) else "")
    return ""


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
