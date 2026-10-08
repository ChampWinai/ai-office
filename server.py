"""AI Office: BOSS -> DEV -> QA loop over Ollama, can read/write files in a user-chosen workspace.
Run: python server.py  (MOCK=1 for fake replies). Desktop window: python office_app.py"""
import json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent   # read-only resources
DATA = Path(os.environ.get("APPDATA", HERE)) / "AIOffice" if FROZEN else HERE  # memory + settings
DATA.mkdir(parents=True, exist_ok=True)
ROLES = json.load(open(HERE / "roles.json", encoding="utf-8"))
OLLAMA = os.environ.get("OLLAMA", "http://localhost:11434")
MOCK = os.environ.get("MOCK") == "1"
PORT = int(os.environ.get("PORT", 8000))
MAX_ROUNDS = 3
OFFICE_TESTS = os.environ.get("OFFICE_TESTS") == "1"
MEMORY, SETTINGS = DATA / "memory.md", DATA / "settings.json"
SKIP = {".git", "node_modules", "__pycache__", ".office_bak", "venv", ".venv", "dist", "build"}
TEXT = {".py", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".cs", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".bat", ".yml", ".yaml", ".toml", ".sql", ".xml"}


def load_ws():
    try:
        p = Path(json.load(open(SETTINGS, encoding="utf-8")).get("workspace", ""))
        return p if p.is_dir() else None
    except Exception:
        return None


WS = load_ws()


def set_ws(path):
    global WS
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("ไม่พบโฟลเดอร์: " + str(p))
    WS = p
    json.dump({"workspace": str(p)}, open(SETTINGS, "w", encoding="utf-8"))
    return p


def passed(fb):  # last "VERDICT: X" anywhere wins; plain leading PASS (mock) also counts; unparseable => FAIL
    v = re.findall(r"VERDICT:\s*\**\s*(PASS|FAIL)", fb, re.I)
    return v[-1].upper() == "PASS" if v else bool(re.match(r"\s*PASS", fb, re.I))


def clean_tests(block):
    """Keep only single-line asserts that compile: a small model's test block often has stray text."""
    ok = []
    for ln in block.splitlines():
        if ln.strip().startswith("assert "):
            try: compile(ln.strip(), "t", "exec"); ok.append(ln.strip())
            except SyntaxError: pass
    return "\n".join(ok)


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
    return None


def workspace_files():
    out = []
    for root, dirs, files in os.walk(WS):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if Path(f).suffix.lower() in TEXT:
                out.append(Path(root, f).relative_to(WS).as_posix())
        if len(out) > 200:
            break
    return sorted(out)[:200]


def workspace_context(task, with_contents):
    """File list (+ contents of files named in the task, or all if the folder is tiny) for the prompts."""
    if not WS:
        return ""
    files = workspace_files()
    txt = f"\n\n# WORKSPACE ({WS})\nFiles: {', '.join(files) or '(empty)'}\n"
    if with_contents:
        pick = [f for f in files if Path(f).name.lower() in task.lower() or f.lower() in task.lower()] or (files if len(files) <= 4 else [])
        budget = 16000
        for f in pick:
            body = (WS / f).read_text(encoding="utf-8", errors="replace")[:8000]
            if budget - len(body) < 0:
                break
            budget -= len(body)
            txt += f"\n--- {f} ---\n{body}\n--- end {f} ---\n"
    return txt


def write_files(reply, task=""):
    """Write fences that carry path=...; refuse paths outside the workspace; back up what gets overwritten."""
    done = []
    fs = fences(reply)
    named = re.search(r"[\w\-./]+\.(?:%s)\b" % "|".join(e[1:] for e in TEXT), task)  # filename mentioned in the request
    if WS and named and fs and not any(f["path"] for f in fs):  # DEV forgot path=: use the first code block for that name
        fs[0]["path"] = named.group(0)
    for f in fs:
        if not (WS and f["path"]):
            continue
        target = (WS / f["path"]).resolve()
        if not target.is_relative_to(WS.resolve()):
            done.append(f"ข้าม (อยู่นอกโฟลเดอร์งาน): {f['path']}")
            continue
        if target.exists():
            bak = WS / ".office_bak"
            bak.mkdir(exist_ok=True)
            shutil.copy2(target, bak / f"{time.strftime('%H%M%S')}_{target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["body"], encoding="utf-8")
        done.append(f["path"])
    return done


MOCK_REPLIES = {
    "BOSS": ["สเปก:\n1. ฟังก์ชัน add(a, b)\n2. คืนผลบวก\n3. รับเฉพาะตัวเลข", "สรุป: งานเสร็จตามสเปก ผ่านการตรวจแล้ว"],
    "DEV": ["```python path=add.py\ndef add(a, b):\n    return a + b\n```\nเขียนเสร็จแล้ว",
            "```python path=add.py\ndef add(a, b):\n    if not all(isinstance(x, (int, float)) for x in (a, b)):\n        raise TypeError('numbers only')\n    return a + b\n```\nแก้ตาม QA แล้ว"],
    "QA": ["FAIL\nไม่ตรวจชนิดข้อมูลของอินพุต", "PASS\nโค้ดตรงตามสเปก"],
}


def context():
    """Office rules (rules.md) + last lessons (memory.md) prepended to every system prompt."""
    rd = lambda p: open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    lessons = "\n".join(rd(MEMORY).splitlines()[-8:])
    return rd(HERE / "rules.md") + (f"\n# บทเรียนจากงานก่อนหน้า\n{lessons}\n" if lessons else "") + "\n"


def remember(task, rounds, ok, bugs):
    """Review loop: keep one line per job that had BUGs, so later jobs avoid the same mistakes."""
    if bugs and ok and not MOCK:  # only fixed bugs: QA's claims on jobs that never passed are too noisy to learn from
        line = f"- [PASS r{rounds}] {task[:50]!r}: " + " / ".join(b[:110] for b in bugs[:3])
        open(MEMORY, "a", encoding="utf-8").write(line.replace("\n", " ") + "\n")


def ask(role, messages, hard=False):
    """Yield text chunks from Ollama (or mock). hard=True picks the role's bigger model."""
    if MOCK:
        n = sum(1 for _ in messages if _["role"] == "assistant")  # prior turns => pick nth reply
        txt = MOCK_REPLIES[role][min(n, len(MOCK_REPLIES[role]) - 1)]
        for i in range(0, len(txt), 4):
            time.sleep(0.03)
            yield txt[i:i + 4]
        return
    cfg = ROLES[role]
    body = {"model": cfg.get("hard_model", cfg["model"]) if hard else cfg["model"], "stream": True,
            "messages": [{"role": "system", "content": context() + cfg["prompt"]}] + messages}
    req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if line.strip():
                yield json.loads(line).get("message", {}).get("content", "")


def run_code(code, tests):
    """Run DEV code alone (its self-tests), then code + BOSS tests (10s limit each, isolated, temp dir).
    Returns (report, ok); ok only depends on the code's own run: BOSS tests come from a small model and can be wrong."""
    if not code or MOCK:  # ponytail: no sandbox beyond timeout+tempdir (user accepted the risk)
        return "", True
    py = (shutil.which("python") or shutil.which("py")) if FROZEN else sys.executable  # frozen: sys.executable is the app itself
    if not py:
        return "ไม่พบ Python ในเครื่อง จึงรันโค้ดทดสอบไม่ได้ (ติดตั้ง Python เพื่อให้ QA ได้ผลรันจริง)", True
    def run(src):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "main.py")
            open(f, "w", encoding="utf-8").write(src)
            try:
                r = subprocess.run([py, "-I", f], cwd=d, capture_output=True, text=True, timeout=10)
                return r.returncode, f"exit={r.returncode}\n{(r.stdout + r.stderr)[-500:]}"
            except subprocess.TimeoutExpired:
                return 1, "timeout 10s"
    rc, rep = run(code)
    report = "CODE + SELF-TESTS: " + rep
    if tests:
        rc2, rep2 = run(code + "\n\n# --- office tests ---\n" + tests)
        report += "\nOFFICE TESTS (written by a small model, may be wrong - verify each failing assert against the request): " + rep2
    return report, rc == 0


def orchestrate(task, emit):
    hard = False

    def say(role, prompt, history=()):
        # mock picks reply by number of assistant turns in `history`
        emit(role=role, status="working")
        out = ""
        for tok in ask(role, list(history) + [{"role": "user", "content": prompt}], hard):
            out += tok
            emit(role=role, token=tok)
        emit(role=role, status="done")
        return out

    asst = lambda n: [{"role": "assistant", "content": "."}] * n  # mock-only turn counter
    spec = say("BOSS", f"คำสั่งจากลูกค้า: {task}{workspace_context(task, False)}\nเขียนสเปกงาน", asst(0) if MOCK else ())
    hard = bool(re.search(r"LEVEL:\s*HARD", spec, re.I))
    emit(note="ระดับงาน: " + ("HARD → ใช้โมเดลใหญ่" if hard else "EASY"))
    sb = fences(spec)
    # ponytail: BOSS-written tests are opt-in; a 7b BOSS wrote wrong asserts and QA/DEV bent to them. Enable with a stronger BOSS model.
    tests = clean_tests(sb[-1]["body"]) if sb and OFFICE_TESTS else ""
    wsctx = workspace_context(task, True)
    code, feedback, bugs, result, prev, ok = "", "", [], "", None, False
    for rnd in range(MAX_ROUNDS):
        emit(round=rnd + 1)
        redo = (f"\n\nPrevious code:\n{code}\n\nQA feedback:\n{feedback}"
                + (f"\n\nRUN RESULT of previous code:\n{result}" if result else "")) if feedback else ""
        code = say("DEV", f"Original request (authoritative, follow it exactly):\n{task}\n\nSpec:\n{spec}{wsctx}{redo}", asst(rnd) if MOCK else ())
        cur = runnable(code)
        if cur and cur == prev:  # DEV resent identical code: escalate once, then give up
            if hard:
                emit(note="DEV ส่งโค้ดเดิมซ้ำ หยุดวนรอบ")
                break
            hard = True
            emit(note="DEV ส่งโค้ดเดิมซ้ำ → เปลี่ยนเป็นโมเดลใหญ่")
        prev = cur
        result, run_ok = run_code(cur, tests)
        if result:
            emit(note="ผลรันโค้ด: " + result.replace("\n", " ")[:160])
        feedback = say("QA", f"Original request (authoritative):\n{task}\n\nSpec:\n{spec}\n\nCode:\n{code}"
                       + (f"\n\nACTUAL RUN RESULT (trust this over guessing):\n{result}" if result else ""), asst(rnd) if MOCK else ())
        bugs += re.findall(r"^[-\s]*(?:Requirement\s*)?[^\n]*?:\s*BUG\s*(.+)$", feedback, re.M)
        ok = passed(feedback) and run_ok  # a failing run overrides QA's PASS
        if passed(feedback) and not run_ok:
            emit(note="QA ให้ PASS แต่รันไม่ผ่าน → ตีกลับอัตโนมัติ")
            feedback += "\nSYSTEM: tests failed, verdict overridden to FAIL."
        if ok:
            break
    remember(task, rnd + 1, ok, bugs)
    if ok and WS:
        written = write_files(code, task)
        if written:
            emit(note="เขียนไฟล์ลงโฟลเดอร์งาน: " + ", ".join(written) + " (สำรองของเดิมไว้ใน .office_bak)", files=written)
        else:
            emit(note="ผ่านแล้ว แต่ไม่ได้เขียนไฟล์: ไม่ทราบชื่อไฟล์ (ระบุชื่อไฟล์ในคำสั่ง เช่น hello.py)")
    elif WS:
        emit(note="ไม่ผ่านการตรวจ จึงไม่เขียนไฟล์")
    verdict = "ผ่าน" if ok else f"ไม่ผ่านหลัง {rnd + 1} รอบ"
    say("BOSS", f"สรุปผลให้ลูกค้า ผลตรวจ: {verdict}\nQA:\n{feedback}\nCode:\n{code}", asst(1) if MOCK else ())
    emit(done=ok)


class H(BaseHTTPRequestHandler):
    def reply(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/workspace":
            return self.reply(200, {"path": str(WS) if WS else "", "files": workspace_files() if WS else []})
        self.reply(200, open(HERE / "index.html", "rb").read(), "text/html")

    def do_POST(self):
        # file writes are possible, so refuse cross-site requests (a web page POSTing to localhost)
        if self.headers.get("Origin") not in (None, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.reply(403, {"error": "forbidden origin"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/workspace":
            try:
                p = set_ws(body["path"])
                return self.reply(200, {"path": str(p), "files": workspace_files()})
            except Exception as e:
                return self.reply(400, {"error": str(e)})
        task = body["task"]  # NDJSON stream: one JSON event per line
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
        emit = lambda **e: (self.wfile.write((json.dumps(e, ensure_ascii=False) + "\n").encode()), self.wfile.flush())
        try:
            orchestrate(task, emit)
        except Exception as e:  # Ollama down, bad model name, client left...
            try: emit(error=f"{type(e).__name__}: {e}")
            except OSError: pass

    def log_message(self, *a): pass


def serve():
    return ThreadingHTTPServer(("127.0.0.1", PORT), H)  # localhost only: this server can write files


if __name__ == "__main__":
    print(f"http://localhost:{PORT}  ({'MOCK' if MOCK else 'Ollama ' + OLLAMA})  workspace={WS}")
    serve().serve_forever()
