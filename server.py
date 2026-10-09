"""AI Office: departments (plan -> build -> review) over Ollama, read/write files in a user-chosen workspace.
Run: python server.py (web) or python office_app.py (desktop window)"""
import difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = os.environ.get("AIOFFICE_VERSION", "1.0.3")
REPO = "ChampWinai/ai-office"
FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent   # read-only resources
DATA = Path(os.environ.get("APPDATA", HERE)) / "AIOffice" if FROZEN else HERE  # settings
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS = DATA / "settings.json"
ROLES = json.load(open(HERE / "roles.json", encoding="utf-8"))
OLLAMA = os.environ.get("OLLAMA", "http://localhost:11434")
OLLAMA_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
PORT = int(os.environ.get("PORT", 8000))
MAX_ROUNDS = 3
NO_WINDOW = 0x08000000
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
UPDATE = {}  # filled by a background check at startup


class Cancelled(Exception):
    pass


# ---------- settings & models ----------

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


def ollama_tags():
    """Installed model names, or None when Ollama is not answering."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except (OSError, ValueError, KeyError):
        return None


def config():
    keys = ("th", "room", "color", "hair", "phase", "model", "hard_model")
    return {"version": VERSION, "roles": {r: {k: c.get(k) for k in keys} for r, c in ROLES.items()},
            "models": ollama_tags() or [], "provider": public_provider()}


# ---------- AI provider: local Ollama, or an API (Anthropic-compatible / OpenAI-compatible) ----------

def key_host(url):
    return urllib.parse.urlparse(url).netloc


def provider():
    """Active provider; its key is looked up by host, so switching presets keeps each key."""
    p = dict(CFG.get("provider") or {"type": "ollama"})
    if p["type"] != "ollama":
        p["api_key"] = (CFG.get("keys") or {}).get(key_host(p.get("base_url", "")), "")
    return p


def public_provider():
    """What the page may see: never the key itself."""
    p = provider()
    return {"type": p["type"], "base_url": p.get("base_url", ""), "model": p.get("model", ""), "has_key": bool(p.get("api_key"))}


def set_provider(body):
    t = body.get("type", "ollama")
    if t not in ("ollama", "anthropic", "openai"):
        raise ValueError("ประเภทไม่รองรับ")
    if t == "ollama":
        CFG["provider"] = {"type": "ollama"}
    else:
        url = (body.get("base_url") or "").strip().rstrip("/")
        model = (body.get("model") or "").strip()
        if not re.match(r"https?://", url):
            raise ValueError("Base URL ต้องขึ้นต้นด้วย http:// หรือ https://")
        if not model:
            raise ValueError("ใส่ชื่อโมเดล")
        key = (body.get("api_key") or "").strip()
        if key:  # a new key replaces the saved one for this host; an empty field keeps it
            CFG.setdefault("keys", {})[key_host(url)] = key
        CFG["provider"] = {"type": t, "base_url": url, "model": model}
    save_settings()


def openai_url(base):
    """Use the base as typed (Groq .../openai/v1, Gemini .../v1beta/openai, LM Studio .../v1); only a bare host gets /v1."""
    base = base.rstrip("/")
    if urllib.parse.urlparse(base).path in ("", "/"):
        base += "/v1"
    return base + "/chat/completions"


def ask_api(p, system, messages):
    """Stream text from an Anthropic-compatible or OpenAI-compatible endpoint (SSE, or plain JSON as fallback)."""
    base = p["base_url"].rstrip("/")
    if p["type"] == "anthropic":
        url = (base if base.endswith("/v1") else base + "/v1") + "/messages"
        body = {"model": p["model"], "max_tokens": 4096, "stream": True, "system": system, "messages": messages}
        headers = {"Authorization": f"Bearer {p.get('api_key', '')}", "anthropic-version": "2023-06-01"}  # Claude Code's ANTHROPIC_AUTH_TOKEN style
    else:
        url = openai_url(base)
        body = {"model": p["model"], "stream": True, "messages": [{"role": "system", "content": system}] + messages}
        headers = {"Authorization": f"Bearer {p.get('api_key', '')}"}
    # Cloudflare-fronted APIs (e.g. Groq) reject the default "Python-urllib" agent, so identify the app instead
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "User-Agent": f"AIOffice/{VERSION} (+https://github.com/{REPO})", **headers})
    for attempt in range(4):  # rate limits and "high demand" (429/5xx) are usually temporary: back off and retry
        try:
            r = urllib.request.urlopen(req, timeout=600)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise RuntimeError(f"API ตอบ {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None
    with r:
        if "json" in (r.headers.get("Content-Type") or ""):  # server ignored stream=true
            data = json.load(r)
            if p["type"] == "anthropic":
                yield "".join(c.get("text", "") for c in data.get("content", []))
            else:
                yield data["choices"][0]["message"]["content"]
            return
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if p["type"] == "anthropic":
                if ev.get("type") == "error":
                    raise RuntimeError(ev.get("error", {}).get("message", str(ev)))
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    yield ev["delta"]["text"]
            else:
                choices = ev.get("choices") or []
                if choices:
                    yield choices[0].get("delta", {}).get("content") or ""


def test_provider(body):
    t = body.get("type", "ollama")
    if t == "ollama":
        tags = ollama_tags()
        if tags is None:
            raise ValueError("Ollama ไม่ตอบ (ตรวจว่าเปิดอยู่)")
        return f"Ollama พร้อม ({len(tags)} โมเดล)"
    url = (body.get("base_url") or provider().get("base_url", "")).strip().rstrip("/")
    p = {"type": t, "base_url": url, "model": body.get("model") or provider().get("model", ""),
         "api_key": (body.get("api_key") or "").strip() or (CFG.get("keys") or {}).get(key_host(url), "")}
    out = "".join(ask_api(p, "Reply with one word: OK", [{"role": "user", "content": "ping"}]))
    return out.strip()[:200] or "(ตอบว่าง)"


# ---------- environment setup (Ollama + models) ----------

def needed(pull_hard):
    out = []
    for c in ROLES.values():
        out.append(c["model"])
        if pull_hard and c.get("hard_model"):
            out.append(c["hard_model"])
    return list(dict.fromkeys(out))


def setup_status():
    if provider()["type"] != "ollama":  # API provider: nothing local to install
        return {"provider": provider()["type"], "ollama": True, "winget": True, "missing": [], "missing_hard": []}
    tags = ollama_tags()
    have = set(tags or [])
    return {"provider": "ollama", "ollama": tags is not None, "winget": bool(shutil.which("winget")),
            "missing": [m for m in needed(False) if m not in have],
            "missing_hard": [m for m in needed(True) if m not in have and m not in needed(False)]}


def setup_env(emit, pull_hard):
    """Install Ollama (winget, per-user, no admin), start it, pull the models the roles need."""
    if provider()["type"] != "ollama":
        emit(step="done", msg="ใช้ API ภายนอก ไม่ต้องติดตั้ง Ollama")
        return
    if ollama_tags() is None:
        if not OLLAMA_EXE.exists() and not shutil.which("ollama"):
            if not shutil.which("winget"):
                raise RuntimeError("ไม่พบ winget (มีใน Windows 10/11) ติดตั้ง Ollama เองได้ที่ https://ollama.com/download")
            emit(step="ollama", state="installing", msg="กำลังติดตั้ง Ollama (winget)…")
            p = subprocess.run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
                                "--accept-package-agreements", "--accept-source-agreements"],
                               capture_output=True, text=True)
            if not OLLAMA_EXE.exists() and not shutil.which("ollama"):
                raise RuntimeError(f"ติดตั้ง Ollama ไม่สำเร็จ (winget exit {p.returncode})")
        exe = str(OLLAMA_EXE) if OLLAMA_EXE.exists() else "ollama"
        emit(step="ollama", state="starting", msg="เปิด Ollama…")
        subprocess.Popen([exe, "serve"], creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if ollama_tags() is not None:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Ollama เปิดไม่ขึ้นภายใน 60 วินาที")
    emit(step="ollama", state="ok", msg="Ollama พร้อมแล้ว")
    have = set(ollama_tags() or [])
    for m in needed(pull_hard):
        if m in have:
            continue
        emit(step="pull", model=m, pct=0, msg="ดาวน์โหลด")
        req = urllib.request.Request(OLLAMA + "/api/pull", json.dumps({"name": m, "stream": True}).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(f"ดาวน์โหลด {m} ไม่สำเร็จ: {ev['error']}")
                if ev.get("total"):
                    emit(step="pull", model=m, pct=round(ev.get("completed", 0) * 100 / ev["total"]), msg=ev.get("status", ""))
        emit(step="pull", model=m, pct=100, msg="เสร็จ")
    emit(step="done", msg="ตั้งค่าเสร็จแล้ว")


# ---------- auto update (GitHub Releases) ----------

def _ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def check_update():
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json"})
        rel = json.load(urllib.request.urlopen(req, timeout=8))
        latest = rel["tag_name"].lstrip("v")
        a = next((x for x in rel["assets"] if x["name"].endswith("-win64.zip")), None)
        asset = a and {"url": a["browser_download_url"], "name": a["name"], "size": a["size"],
                       "sha256": (a.get("digest") or "").split(":")[-1]}
        return {"current": VERSION, "latest": latest, "frozen": FROZEN,
                "available": bool(asset) and _ver(latest) > _ver(VERSION), "asset": asset}
    except Exception as e:  # offline or rate-limited: the app just runs without the banner
        return {"current": VERSION, "available": False, "error": str(e)}


def download_update(asset, emit=None):
    """Download the release zip, verify its sha256 against GitHub's digest, unpack. Returns the new app folder."""
    if not asset.get("sha256"):
        raise ValueError("ไม่มี checksum ของไฟล์ จึงไม่อัปเดต")
    stage = Path(tempfile.mkdtemp(prefix="aioffice_upd_"))
    zp = stage / asset["name"]
    if emit:
        emit(step="update", msg="ดาวน์โหลด " + asset["name"] + "…")
    with urllib.request.urlopen(asset["url"], timeout=600) as r, open(zp, "wb") as f:
        shutil.copyfileobj(r, f)
    if hashlib.sha256(zp.read_bytes()).hexdigest() != asset["sha256"]:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError("checksum ไม่ตรง ยกเลิกการอัปเดต")
    shutil.unpack_archive(str(zp), str(stage))
    new = stage / "AIOffice"
    if not (new / "AIOffice.exe").exists():
        raise ValueError("ไฟล์อัปเดตไม่ถูกต้อง")
    return new


def install_update(emit):
    if not FROZEN:
        raise ValueError("รันจากซอร์ส: อัปเดตด้วย git pull แทน")
    if not UPDATE.get("available"):
        raise ValueError("ไม่มีเวอร์ชันใหม่")
    new = download_update(UPDATE["asset"], emit)
    app = Path(sys.executable).parent
    bat = Path(tempfile.gettempdir()) / "aioffice_update.bat"
    bat.write_text(
        "@echo off\r\nchcp 65001 >nul\r\ntimeout /t 3 /nobreak >nul\r\n"
        f'robocopy "{new}" "{app}" /E /R:3 /W:1 /NFL /NDL /NJH /NJS >nul\r\n'
        f'start "" "{app / "AIOffice.exe"}"\r\n'
        f'rmdir /S /Q "{new.parent}"\r\n', encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000008 | 0x00000200, close_fds=True)  # detached
    emit(step="update", state="restart", msg="กำลังเปิดเวอร์ชันใหม่…")
    threading.Timer(1.5, lambda: os._exit(0)).start()  # the updater relaunches the app after we exit


# ---------- workspace files ----------

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


# ---------- the office ----------

def ask(role, messages, hard=False):
    """Yield text chunks from the active provider. hard=True picks the role's bigger model (Ollama only;
    an API provider uses its one model for every role)."""
    cfg = ROLES[role]
    system = (HERE / "rules.md").read_text(encoding="utf-8") + "\n" + cfg["prompt"]
    p = provider()
    if p["type"] != "ollama":
        yield from ask_api(p, system, messages)
        return
    body = {"model": cfg.get("hard_model", cfg["model"]) if hard else cfg["model"], "stream": True,
            "messages": [{"role": "system", "content": system}] + messages}
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

    def say(role, prompt, phase=None):
        emit(role=role, status="working", phase=phase or ROLES[role].get("phase"))
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
    notes = ""  # plan-phase departments think before anyone edits; their notes go to DEV
    for name, cfg in ROLES.items():
        if cfg.get("phase") == "plan":
            out = say(name, f"Original request (authoritative):\n{task}\n\nSpec:\n{spec}{wsctx}"
                            + (f"\n\nEarlier departments said:\n{notes}" if notes else ""))
            notes += f"\n\n[{cfg['th']}]\n{out}"
    code, feedback, result, seen, ok = "", "", "", set(), False
    for rnd in range(MAX_ROUNDS):
        emit(round=rnd + 1)
        redo = f"\n\nPrevious code:\n{code}\n\nQA feedback:\n{feedback}\n\nRUN RESULT of previous code:\n{result}" if feedback else ""
        code = say("DEV", f"Original request (authoritative, follow it exactly):\n{task}\n\nSpec:\n{spec}"
                          f"\n\nDepartment notes (follow the change plan and security rules):{notes}{wsctx}{redo}")
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
            backed = " (สำรองของเดิมไว้ใน .office_bak)" if any(p["old"] for p in plan) else ""
            emit(note="เขียนไฟล์ลงโฟลเดอร์งาน: " + ", ".join(written) + backed, files=written)
        elif not ok:
            emit(note="ไม่ผ่านการตรวจ จึงไม่เขียนไฟล์")
        elif not plan:
            emit(note="ผ่านแล้ว แต่ไม่ได้เขียนไฟล์: ไม่ทราบชื่อไฟล์ (ระบุชื่อไฟล์ในคำสั่ง เช่น hello.py)")
    say("BOSS", f"สรุปผลให้ลูกค้า ผลตรวจ: {'ผ่าน' if ok else f'ไม่ผ่านหลัง {rnd + 1} รอบ'}\nQA:\n{feedback}\nCode:\n{code}", phase="report")
    emit(done=ok)


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def reply(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.end_headers()
        self.wfile.write(data)

    def stream(self, fn):
        """NDJSON stream: one JSON event per line."""
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
        emit = lambda **e: (self.wfile.write((json.dumps(e, ensure_ascii=False) + "\n").encode()), self.wfile.flush())
        try:
            fn(emit)
        except Cancelled:
            emit(note="หยุดงานแล้ว")
            emit(done=False)
        except Exception as e:  # Ollama down, bad model name, bad path, client left...
            try: emit(error=f"{type(e).__name__}: {e}")
            except OSError: pass

    def do_GET(self):
        if self.path == "/workspace":
            return self.reply(200, {"path": str(WS) if WS else "", "files": workspace_files() if WS else []})
        if self.path == "/config":
            return self.reply(200, config())
        if self.path == "/setup/status":
            return self.reply(200, setup_status())
        if self.path == "/update":
            return self.reply(200, UPDATE)
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
            if self.path == "/provider":
                set_provider(body)
                return self.reply(200, {"ok": True})
            if self.path == "/provider/test":
                return self.reply(200, {"reply": test_provider(body)})
            if self.path == "/stop":
                CANCEL.set()
                return self.reply(200, {"ok": True})
            if self.path == "/decide":
                DECISION[0] = bool(body.get("ok"))
                DECIDED.set()
                return self.reply(200, {"ok": True})
        except Exception as e:
            return self.reply(400, {"error": str(e)})
        if self.path == "/":
            return self.stream(lambda emit: orchestrate(body["task"], emit, bool(body.get("approve"))))
        if self.path == "/setup":
            return self.stream(lambda emit: setup_env(emit, bool(body.get("pull_hard"))))
        if self.path == "/update/install":
            return self.stream(install_update)
        self.reply(404, {"error": "not found"})

    def log_message(self, *a): pass


def serve():
    threading.Thread(target=lambda: UPDATE.update(check_update()), daemon=True).start()  # auto-check on start
    return ThreadingHTTPServer(("127.0.0.1", PORT), H)  # localhost only: this server can write files


if __name__ == "__main__":
    print(f"http://localhost:{PORT}  (Ollama {OLLAMA})  workspace={WS}")
    serve().serve_forever()
