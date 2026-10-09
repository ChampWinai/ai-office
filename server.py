"""AI Office: BOSS -> DEV -> QA loop over Ollama, can read/write files in a user-chosen workspace.
Run: python server.py  (web) or python office_app.py (desktop window)"""
import difflib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent   # read-only resources
DATA = Path(os.environ.get("APPDATA", HERE)) / "AIOffice" if FROZEN else HERE  # settings
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS = DATA / "settings.json"
ROLES = json.load(open(HERE / "roles.json", encoding="utf-8"))
OLLAMA = os.environ.get("OLLAMA", "http://localhost:11434")
PORT = int(os.environ.get("PORT", 8000))
MAX_ROUNDS = 3
SKIP = {".git", "node_modules", "__pycache__", ".office_bak", "venv", ".venv", "dist", "build"}
TEXT = {".py", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".cs", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".bat", ".yml", ".yaml", ".toml", ".sql", ".xml"}


def load_settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CFG = load_settings()
for _role, _over in CFG.get("models", {}).items():  # model picks from the web page override roles.json
    if _role in ROLES:
        ROLES[_role].update(_over)
_ws = CFG.get("workspace")
WS = Path(_ws) if _ws and Path(_ws).is_dir() else None
CANCEL, DECIDED = threading.Event(), threading.Event()  # Stop button, approve/reject buttons
DECISION = [False]


class Cancelled(Exception):
    pass


def save_settings():
    json.dump(CFG, open(SETTINGS, "w", encoding="utf-8"), ensure_ascii=False)


def set_ws(path):
    global WS
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("ไม่พบโฟลเดอร์: " + str(p))
    WS = p
    CFG["workspace"] = str(p)
    save_settings()


def set_model(role, kind, name):
    if role not in ROLES or kind not in ("model", "hard_model"):
        raise ValueError("ตำแหน่งหรือประเภทโมเดลไม่ถูกต้อง")
    ROLES[role][kind] = name
    CFG.setdefault("models", {}).setdefault(role, {})[kind] = name
    save_settings()


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except (OSError, ValueError, KeyError):
        return []


def passed(fb):  # last "VERDICT: X" anywhere wins; unparseable => FAIL
    v = re.findall(r"VERDICT:\s*\**\s*(PASS|FAIL)", fb, re.I)
    return bool(v) and v[-1].upper() == "PASS"


def fences(text):
    """Code fences as dicts: lang, path (from 'path=...' on the fence line, or None), body."""
    out = []
    for info, body in re.findall(r"```([^\n`]*)\n(.*?)```", text, re.S):
        m = re.search(r"path=(\S+)", info)
        if not m:  # small models sometimes put it as the first line of the body instead
            m = re.match(r"\s*(?:#\s*)?path=(\S+)[^\n]*\n", body)
            body = body[m.end():] if m else body
        out.append({"lang": (info.split() or [""])[0].lower(), "path": m.group(1).strip("\"'") if m else None, "body": body})
    return out


def runnable(text):
    """First python fence = the code we execute for QA."""
    for f in fences(text):
        if f["lang"] in ("", "python", "py") or (f["path"] or "").endswith(".py"):
            return f["body"]


def workspace_files():
    out = []
    for root, dirs, files in os.walk(WS):
        dirs[:] = [d for d in dirs if d not in SKIP]
        out += [Path(root, f).relative_to(WS).as_posix() for f in files if Path(f).suffix.lower() in TEXT]
        if len(out) > 200:
            break
    return sorted(out)[:200]


def workspace_context(task, with_contents):
    """File list (+ contents of files named in the task) for the prompts."""
    if not WS:
        return ""
    files = workspace_files()
    txt = f"\n\n# WORKSPACE ({WS})\nFiles: {', '.join(files) or '(empty)'}\n"
    if with_contents:
        for f in files:
            if Path(f).name.lower() in task.lower():
                txt += f"\n--- {f} ---\n{(WS / f).read_text(encoding='utf-8', errors='replace')[:8000]}\n--- end {f} ---\n"
    return txt


def plan_writes(reply, task):
    """Files the reply wants to write (path=...). Refuses paths outside the workspace."""
    fs = fences(reply)
    named = re.search(r"[\w\-./]+\.(?:%s)\b" % "|".join(e[1:] for e in TEXT), task)  # filename mentioned in the request
    if named and fs and not any(f["path"] for f in fs):  # DEV forgot path=: use the first code block for that name
        fs[0]["path"] = named.group(0)
    plan = []
    for f in fs:
        if not f["path"]:
            continue
        target = (WS / f["path"]).resolve()
        if not target.is_relative_to(WS.resolve()):
            raise ValueError("path อยู่นอกโฟลเดอร์งาน: " + f["path"])
        old = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        plan.append({"path": f["path"], "target": target, "body": f["body"], "old": old})
    return plan


def diff_text(plan):
    out = ""
    for p in plan:
        out += "".join(difflib.unified_diff(p["old"].splitlines(True), p["body"].splitlines(True),
                                            f"a/{p['path']}" if p["old"] else "/dev/null", f"b/{p['path']}"))
    return out[:4000]


def apply_writes(plan):
    """Write the planned files; the old version of each goes to .office_bak first."""
    for p in plan:
        t = p["target"]
        if t.exists():
            (WS / ".office_bak").mkdir(exist_ok=True)
            shutil.copy2(t, WS / ".office_bak" / f"{time.strftime('%H%M%S')}_{t.name}")
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(p["body"], encoding="utf-8")
    return [p["path"] for p in plan]


def ask(role, messages, hard=False):
    """Yield text chunks from Ollama. hard=True picks the role's bigger model."""
    cfg = ROLES[role]
    body = {"model": cfg.get("hard_model", cfg["model"]) if hard else cfg["model"], "stream": True,
            "messages": [{"role": "system", "content": (HERE / "rules.md").read_text(encoding="utf-8") + "\n" + cfg["prompt"]}] + messages}
    req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if line.strip():
                yield json.loads(line).get("message", {}).get("content", "")


def run_code(code):
    """Run DEV's code and its self-tests (10s limit, isolated mode, temp dir). Returns (report, ok)."""
    if not code:
        return "", True
    py = (shutil.which("python") or shutil.which("py")) if FROZEN else sys.executable  # frozen: sys.executable is the app itself
    if not py:
        return "ไม่พบ Python ในเครื่อง จึงรันโค้ดทดสอบไม่ได้ (ติดตั้ง Python เพื่อให้ QA ได้ผลรันจริง)", True
    with tempfile.TemporaryDirectory() as d:  # ponytail: no sandbox beyond timeout+tempdir (user accepted the risk)
        f = os.path.join(d, "main.py")
        open(f, "w", encoding="utf-8").write(code)
        try:
            r = subprocess.run([py, "-I", f], cwd=d, capture_output=True, text=True, timeout=10)
            return f"exit={r.returncode}\n{(r.stdout + r.stderr)[-500:]}", r.returncode == 0
        except subprocess.TimeoutExpired:
            return "timeout 10s", False


def wait_decision(timeout=300):
    """Block until the page approves/rejects (or Stop is pressed, or it times out)."""
    for _ in range(timeout):
        if CANCEL.is_set():
            raise Cancelled()
        if DECIDED.wait(1):
            return DECISION[0]
    return False


def orchestrate(task, emit, approve=False):
    hard = False
    CANCEL.clear()  # one job at a time: the page disables RUN while a job runs
    DECIDED.clear()

    def say(role, prompt):
        emit(role=role, status="working")
        out = ""
        for tok in ask(role, [{"role": "user", "content": prompt}], hard):
            if CANCEL.is_set():
                raise Cancelled()
            out += tok
            emit(role=role, token=tok)
        emit(role=role, status="done")
        return out

    spec = say("BOSS", f"คำสั่งจากลูกค้า: {task}{workspace_context(task, False)}\nเขียนสเปกงาน")
    hard = bool(re.search(r"LEVEL:\s*HARD", spec, re.I))
    emit(note="ระดับงาน: " + ("HARD → ใช้โมเดลใหญ่" if hard else "EASY"))
    wsctx = workspace_context(task, True)
    code, feedback, result, seen, ok = "", "", "", set(), False
    for rnd in range(MAX_ROUNDS):
        emit(round=rnd + 1)
        redo = f"\n\nPrevious code:\n{code}\n\nQA feedback:\n{feedback}\n\nRUN RESULT of previous code:\n{result}" if feedback else ""
        code = say("DEV", f"Original request (authoritative, follow it exactly):\n{task}\n\nSpec:\n{spec}{wsctx}{redo}")
        cur = runnable(code)
        if cur and cur in seen:  # DEV resent identical code: escalate once, then give up
            if hard:
                emit(note="DEV ส่งโค้ดเดิมซ้ำ หยุดวนรอบ")
                break
            hard = True
            emit(note="DEV ส่งโค้ดเดิมซ้ำ → เปลี่ยนเป็นโมเดลใหญ่")
        seen.add(cur)
        result, run_ok = run_code(cur)
        if result:
            emit(note="ผลรันโค้ด: " + result.replace("\n", " ")[:160])
        feedback = say("QA", f"Original request (authoritative):\n{task}\n\nSpec:\n{spec}\n\nCode:\n{code}"
                       + (f"\n\nACTUAL RUN RESULT (trust this over guessing):\n{result}" if result else ""))
        ok = passed(feedback) and run_ok  # a failing run overrides QA's PASS
        if passed(feedback) and not run_ok:
            emit(note="QA ให้ PASS แต่รันไม่ผ่าน → ตีกลับอัตโนมัติ")
            feedback += "\nSYSTEM: tests failed, verdict overridden to FAIL."
        if ok:
            break
    if WS:
        plan = plan_writes(code, task) if ok else []
        declined = False
        if plan:
            emit(note="แก้ไขไฟล์: " + ", ".join(p["path"] for p in plan), diff=diff_text(plan))
        if plan and approve:
            emit(approve=True, files=[p["path"] for p in plan])
            declined = not wait_decision()
            if declined:
                emit(note="ไม่เขียนไฟล์ (ไม่อนุมัติหรือหมดเวลา)")
        if plan and not declined:
            written = apply_writes(plan)
            emit(note="เขียนไฟล์ลงโฟลเดอร์งาน: " + ", ".join(written) + " (สำรองของเดิมไว้ใน .office_bak)", files=written)
        elif not ok:
            emit(note="ไม่ผ่านการตรวจ จึงไม่เขียนไฟล์")
        elif not plan:
            emit(note="ผ่านแล้ว แต่ไม่ได้เขียนไฟล์: ไม่ทราบชื่อไฟล์ (ระบุชื่อไฟล์ในคำสั่ง เช่น hello.py)")
    say("BOSS", f"สรุปผลให้ลูกค้า ผลตรวจ: {'ผ่าน' if ok else f'ไม่ผ่านหลัง {rnd + 1} รอบ'}\nQA:\n{feedback}\nCode:\n{code}")
    emit(done=ok)


class H(BaseHTTPRequestHandler):
    def reply(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/workspace":
            return self.reply(200, {"path": str(WS) if WS else "", "files": workspace_files() if WS else []})
        if self.path == "/models":
            return self.reply(200, {"models": ollama_models(),
                                    "roles": {r: {"model": c["model"], "hard_model": c.get("hard_model", c["model"])} for r, c in ROLES.items()}})
        self.reply(200, open(HERE / "index.html", "rb").read(), "text/html")

    def do_POST(self):
        # file writes are possible, so refuse cross-site requests (a web page POSTing to localhost)
        if self.headers.get("Origin") not in (None, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.reply(403, {"error": "forbidden origin"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            if self.path == "/workspace":
                set_ws(body["path"])
                return self.reply(200, {"path": str(WS), "files": workspace_files()})
            if self.path == "/models":
                set_model(body["role"], body["kind"], body["name"])
                return self.reply(200, {"ok": True})
            if self.path == "/stop":
                CANCEL.set()
                return self.reply(200, {"ok": True})
            if self.path == "/decide":
                DECISION[0] = bool(body.get("ok"))
                DECIDED.set()
                return self.reply(200, {"ok": True})
        except Exception as e:
            return self.reply(400, {"error": str(e)})
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
        emit = lambda **e: (self.wfile.write((json.dumps(e, ensure_ascii=False) + "\n").encode()), self.wfile.flush())
        try:
            orchestrate(body["task"], emit, bool(body.get("approve")))  # NDJSON stream: one JSON event per line
        except Cancelled:
            emit(note="หยุดงานแล้ว")
            emit(done=False)
        except Exception as e:  # Ollama down, bad model name, bad path, client left...
            try: emit(error=f"{type(e).__name__}: {e}")
            except OSError: pass

    def log_message(self, *a): pass


def serve():
    return ThreadingHTTPServer(("127.0.0.1", PORT), H)  # localhost only: this server can write files


if __name__ == "__main__":
    print(f"http://localhost:{PORT}  (Ollama {OLLAMA})  workspace={WS}")
    serve().serve_forever()
