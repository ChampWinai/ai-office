"""AI Office: departments (plan -> build -> review) over Ollama, read/write files in a user-chosen workspace.
Run: python server.py (web) or python office_app.py (desktop window)"""
import base64, difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import edits as E
import extras as X

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
            "models": ollama_tags() or [], "provider": public_provider(), "test_cmd": proj().get("test_cmd", ""),
            "git_commit": bool(proj().get("git_commit")), "num_ctx": int(CFG.get("num_ctx", 16384)),
            "hook_before": proj().get("hook_before", ""), "hook_post_edit": proj().get("hook_post_edit", ""),
            "hook_after_write": proj().get("hook_after_write", ""), "web": CFG.get("web", True),
            "search": {"provider": (CFG.get("search") or {}).get("provider", ""), "url": (CFG.get("search") or {}).get("url", ""),
                       "has_key": bool((CFG.get("search") or {}).get("key"))},
            "mcp": CFG.get("mcp") or {}}


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


def ask_api(p, system, messages, usage=None):
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
            if usage is not None and ev.get("type") == "message_start":
                usage["in"] = ev.get("message", {}).get("usage", {}).get("input_tokens", 0)
            if usage is not None and (ev.get("usage") or {}).get("output_tokens"):
                usage["out"] = ev["usage"]["output_tokens"]
            if usage is not None and (ev.get("usage") or {}).get("completion_tokens"):
                usage["in"], usage["out"] = ev["usage"].get("prompt_tokens", 0), ev["usage"]["completion_tokens"]
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
                               capture_output=True, text=True, errors="replace")
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


# ---------- project settings, memory, rules ----------

WORKSPACE_TOOLS = """SEARCH: <regex or words>            -> matching lines as path:line: text (max 40)
READ: <path>   or   READ: <path>:<start>-<end>   -> file content (max 400 lines per call)
LIST: <folder>                      -> entries of a folder ("." for the project root)"""
WEB_TOOLS = """WEB: <search words>                -> web search results (title, url, snippet)
FETCH: <https url>                  -> a public web page as text (docs, issues, changelogs)"""
VISION_HINTS = ("vl", "llava", "vision", "gemma3", "minicpm", "moondream", "pixtral")


def tools_help(servers):
    """Tool list for agents allowed to look around: workspace tools, web tools, and the configured MCP tools."""
    parts = ["\n# TOOLS - look before deciding. One tool per line, nothing else on that line. After tool lines, stop: "
             "the results arrive in the next message. Never guess the content of a file or page you have not read."]
    if WS:
        parts.append(WORKSPACE_TOOLS)
    if CFG.get("web", True):
        parts.append(WEB_TOOLS)
    if servers:
        parts.append(X.mcp_help(servers))
    return "\n".join(p for p in parts if p) + "\n"


def to_provider(messages, kind):
    """Messages may carry images ([{"mime", "data"(base64)}]); each provider wants them in its own shape."""
    out = []
    for m in messages:
        imgs = m.get("images") or []
        if not imgs:
            out.append({"role": m["role"], "content": m["content"]})
        elif kind == "ollama":
            out.append({"role": m["role"], "content": m["content"], "images": [i["data"] for i in imgs]})
        elif kind == "anthropic":
            out.append({"role": m["role"], "content": [{"type": "image", "source": {"type": "base64", "media_type": i["mime"], "data": i["data"]}}
                                                       for i in imgs] + [{"type": "text", "text": m["content"]}]})
        else:
            out.append({"role": m["role"], "content": [{"type": "text", "text": m["content"]}]
                        + [{"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}} for i in imgs]})
    return out
RULE_FILES = ("AGENTS.md", "CLAUDE.md", "AIOFFICE.md")


def proj():
    """Settings that belong to the current project: test command, git auto-commit, job history."""
    return CFG.setdefault("projects", {}).setdefault(str(WS) if WS else "-", {})


def project_rules():
    """The project's own instructions file (like CLAUDE.md), read by every department."""
    for name in RULE_FILES:
        p = (WS / name) if WS else None
        if p and p.is_file():
            return f"\n# PROJECT RULES ({name})\n" + E.read_text(p)[0][:8000] + "\n"
    return ""


def record(task, ok, files, summary, secs):
    h = proj().setdefault("history", [])
    h.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "task": task[:200], "ok": ok, "files": files[:20],
              "summary": summary.strip()[:600], "secs": secs})
    del h[:-20]
    save_settings()


def memory_text():
    """The last jobs in this project, so a request like 'continue from before' has context."""
    h = proj().get("history", [])[-3:]
    if not h:
        return ""
    lines = [f"- {j['ts']} [{'ผ่าน' if j['ok'] else 'ไม่ผ่าน'}] {j['task']} | files: {', '.join(j['files']) or '-'} | {j['summary'][:300]}" for j in h]
    return ("\n\n# PREVIOUS JOBS IN THIS PROJECT (oldest first) - background only: do not redo them unless the request asks\n"
            + "\n".join(lines) + "\n")


def passed(fb):  # last "VERDICT: X" anywhere wins; unparseable => FAIL
    v = re.findall(r"VERDICT:\s*\**\s*(PASS|FAIL)", fb, re.I)
    return bool(v) and v[-1].upper() == "PASS"


def runnable(text):
    """First python fence = the code we execute for QA (no-workspace mode)."""
    for f in E.fences(text):
        if f["lang"] in ("", "python", "py") or (f["path"] or "").endswith(".py"):
            return f["body"]


# ---------- the office ----------

def ask(role, messages, hard=False, usage=None, help_text=""):
    """Yield text chunks from the active provider; fills usage with token counts when the provider reports them."""
    cfg = ROLES[role]
    system = ((HERE / "rules.md").read_text(encoding="utf-8") + project_rules() + "\n" + cfg["prompt"]
              + help_text)
    p = provider()
    if p["type"] != "ollama":
        yield from ask_api(p, system, to_provider(messages, p["type"]), usage)
        return
    body = {"model": cfg.get("hard_model", cfg["model"]) if hard else cfg["model"], "stream": True,
            "options": {"num_ctx": int(CFG.get("num_ctx", 16384))},  # Ollama's default window (2-4k) silently cuts long prompts
            "messages": [{"role": "system", "content": system}] + to_provider(messages, "ollama")}
    req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            if not line.strip():
                continue
            j = json.loads(line)
            if j.get("done") and usage is not None:
                usage["in"], usage["out"] = j.get("prompt_eval_count", 0), j.get("eval_count", 0)
            yield j.get("message", {}).get("content", "")


def run_code(code):
    """Run DEV's snippet and its self-tests (10s limit, isolated mode, temp dir). No-workspace mode only."""
    if not code:
        return "", True
    py = (shutil.which("python") or shutil.which("py")) if FROZEN else sys.executable  # frozen: sys.executable is the app itself
    if not py:
        return "ไม่พบ Python ในเครื่อง จึงรันโค้ดทดสอบไม่ได้ (ติดตั้ง Python เพื่อให้ QA ได้ผลรันจริง)", True
    with tempfile.TemporaryDirectory() as d:  # ponytail: no sandbox beyond timeout+tempdir (user accepted the risk)
        f = os.path.join(d, "main.py")
        open(f, "w", encoding="utf-8").write(code)
        try:
            r = subprocess.run([py, "-I", f], cwd=d, capture_output=True, text=True, errors="replace", timeout=10)
            return f"exit={r.returncode}\n{(r.stdout + r.stderr)[-500:]}", r.returncode == 0
        except subprocess.TimeoutExpired:
            return "timeout 10s", False


def wait_decision(timeout=300):
    """Block until the page answers the last question (approve/reject), Stop is pressed, or it times out."""
    for _ in range(timeout):
        if CANCEL.is_set():
            raise Cancelled()
        if DECIDED.wait(1):
            DECIDED.clear()
            return DECISION[0]
    return False


def orchestrate(task, emit, approve=False, mode="edit", attachments=None):
    """mode: "edit" (change files), "ask" (read-only answer), "plan" (the user approves the plan before any edit).
    attachments: [{"name", "type", "data"(base64)}] - text files go into the prompts, images to the models that see."""
    hard = False
    CANCEL.clear()
    DECIDED.clear()
    t0 = time.time()
    files = E.project_files(WS) if WS else []
    read_paths, tokens = set(), {"n": 0}
    servers = X.mcp_servers(CFG.get("mcp")) if CFG.get("mcp") else {}
    for name, s in servers.items():
        if isinstance(s, str):
            emit(note=f"MCP {name}: {s}")
    att_text, images = "", []
    for a in (attachments or [])[:8]:
        raw = base64.b64decode(a.get("data") or "")[:5_000_000]
        if (a.get("type") or "").startswith("image/"):
            images.append({"mime": a["type"], "data": base64.b64encode(raw).decode()})
        else:
            att_text += f"\n--- {a.get('name', 'file')} ---\n{raw.decode('utf-8', 'replace')[:20000]}\n"
    if CFG.get("web", True):  # links the user wrote are read for the team up front (small models rarely FETCH on their own)
        for url in list(dict.fromkeys(re.findall(r"https?://[^\s<>\"')]+", task)))[:2]:
            try:
                att_text += f"\n--- เนื้อหาจาก {url} ---\n{X.fetch(url, 20000)}\n"
                emit(note="อ่านลิงก์: " + url)
            except Exception as e:
                emit(note=f"อ่านลิงก์ไม่ได้: {url} ({e})")
    if att_text:
        att_text = "\n\n# ATTACHMENTS FROM THE USER" + att_text
    if images:
        emit(note=f"แนบรูป {len(images)} รูป")
        seeing = ("PLAN",) if mode == "ask" else ("BOSS", "DEV")  # the roles that receive the pictures
        if provider()["type"] == "ollama" and not any(h in ROLES[r]["model"].lower() for r in seeing for h in VISION_HINTS):
            emit(note="โมเดลที่ตั้งไว้อาจมองรูปไม่เห็น: เลือกโมเดลที่รองรับรูป (เช่น qwen2.5vl, llava) หรือใช้ API เช่น Gemini")

    def say(role, prompt, phase=None, tools=0, pics=None):
        """One agent turn; with tools>0 it may use SEARCH/READ/LIST/WEB/FETCH/MCP up to that many times first."""
        emit(role=role, status="working", phase=phase or ROLES[role].get("phase"))
        messages, out = [{"role": "user", "content": prompt, "images": pics or []}], ""
        help_text = tools_help(servers) if tools else ""
        for turn in range(tools + 1):
            usage, part = {"in": 0, "out": 0}, ""
            for tok in ask(role, messages, hard, usage, help_text):
                if CANCEL.is_set():
                    raise Cancelled()
                part += tok
                emit(role=role, token=tok)
            tokens["n"] += (usage["in"] or sum(len(m["content"]) for m in messages) // 3) + (usage["out"] or len(part) // 3)
            out += part
            calls = E.tool_calls(part)[:6] if turn < tools else []
            if not calls:
                break
            results = []
            for kind, arg in calls:
                try:
                    if kind == "READ" and arg.strip().startswith(("http://", "https://")):
                        kind = "FETCH"  # models sometimes READ a link
                    if kind in ("SEARCH", "READ", "LIST"):
                        res = E.run_tool(WS, files, kind, arg) if WS else "(ยังไม่ได้เลือกโฟลเดอร์งาน)"
                    elif kind in ("WEB", "FETCH") and not CFG.get("web", True):
                        res = "(ผู้ใช้ปิดการใช้เว็บไว้)"
                    elif kind == "WEB":
                        res = X.web_search(arg, CFG.get("search"))
                    elif kind == "FETCH":
                        res = X.fetch(arg.strip("<> `"))
                    else:
                        res = X.mcp_call(servers, arg)
                except Exception as e:  # a tool failing (network, bad path, MCP error) is information, not a crash
                    res = f"(ผิดพลาด: {type(e).__name__}: {e})"
                if kind == "READ":
                    read_paths.add(re.sub(r":\d+(-\d+)?$", "", arg.strip().strip("`")))
                results.append(f"[{kind}: {arg}]\n{res}")
            emit(role=role, token="\n\n🔎 " + " · ".join(f"{k} {a[:60]}" for k, a in calls) + "\n\n")
            messages += [{"role": "assistant", "content": part},
                         {"role": "user", "content": "TOOL RESULTS:\n" + "\n\n".join(results)[:30000]
                          + "\n\nContinue. When you have enough information, give your final answer without tool lines."}]
        emit(role=role, status="done", tokens=tokens["n"])
        return out

    memory = memory_text()
    wsctx = f"\n\n# WORKSPACE ({WS})\nFiles: {E.summary(files)}\n" if WS else ""

    if mode == "ask":  # read-only: the analyst looks around and answers
        bundle = E.file_bundle(WS, E.pick_files(task, "", files)) if WS else ""
        answer = say("PLAN", f"QUESTION from the user. Use the TOOLS that fit before answering: project files (SEARCH/READ/LIST) for questions "
                             f"about this code, WEB/FETCH for outside facts, and MCP tools when the user names one; "
                             f"then answer clearly in Thai, citing file paths and function names. Do NOT propose edits and do not end with a FILES line.\n"
                             f"{task}{att_text}{memory}{wsctx}" + (f"\n\nFILES THAT MAY BE RELEVANT:{bundle}" if bundle else ""),
                     phase="plan", tools=6, pics=images)
        record(task, True, [], answer, round(time.time() - t0))
        emit(done=True, tokens=tokens["n"])
        return

    if WS and proj().get("hook_before") and mode != "ask":
        emit(note="hook ก่อนเริ่มงาน: " + E.run_cmds(WS, [proj()["hook_before"]], timeout=300).replace("\n", " ")[:400])
    spec = say("BOSS", f"คำสั่งจากลูกค้า: {task}{att_text}{memory}{wsctx}\nเขียนสเปกงาน", pics=images)
    hard = bool(re.search(r"LEVEL:\s*HARD", spec, re.I))
    emit(note="ระดับงาน: " + ("HARD → ใช้โมเดลใหญ่" if hard else "EASY"))
    notes = ""  # plan-phase departments look at the project and think before anyone edits
    for name, cfg in ROLES.items():
        if cfg.get("phase") == "plan":
            out = say(name, f"Original request (authoritative):\n{task}{att_text}\n\nSpec:\n{spec}{memory}{wsctx}"
                            + (f"\n\nEarlier departments said:\n{notes}" if notes else "")
                            + ("\n\nUse the tools to find the relevant code, then end your answer with one line: FILES: path1, path2" if WS else ""),
                      tools=4)
            notes += f"\n\n[{cfg['th']}]\n{out}"
    if mode == "plan":
        emit(plan_review=True)
        if not wait_decision():
            emit(note="ยกเลิก: ไม่อนุมัติแผน ไม่มีการแก้ไฟล์")
            record(task, False, [], "ผู้ใช้ไม่อนุมัติแผน", round(time.time() - t0))
            emit(done=False, tokens=tokens["n"])
            return
        approve = True

    def edit_loop():
        """Workspace mode: DEV proposes edits -> applied to a COPY -> commands + checks -> QA reads the diff -> approve -> commit."""
        nonlocal hard
        picked = E.pick_files(task, notes, files, read_paths)
        if picked:
            emit(note="อ่านไฟล์: " + ", ".join(picked))
        bundle = E.file_bundle(WS, picked)
        test_cmd = proj().get("test_cmd", "")
        pyexe = (shutil.which("python") or shutil.which("py")) if FROZEN else sys.executable
        named = [f for f in files if f in task or (len(Path(f).name) > 4 and Path(f).name in task)]  # the request names them
        allowed_cmds, denied_cmds = set(), set()
        feedback, result, last, ok, changed, deleted, root, rnd, diff = "", "", "", False, [], [], None, 0, ""
        try:
            for rnd in range(MAX_ROUNDS):
                emit(round=rnd + 1)
                redo = f"\n\nYour previous answer:\n{last}\n\nQA FEEDBACK:\n{feedback}\n\nCHECK RESULT:\n{result}" if feedback else ""
                last = say("DEV", f"Original request (authoritative, follow it exactly):\n{task}{att_text}\n\nSpec:\n{spec}"
                                  f"\n\nDepartment notes (follow the change plan and security rules):{notes}{memory}"
                                  f"{wsctx}\n\nCURRENT FILES:{bundle}{redo}", tools=2, pics=images)
                ops = E.parse_ops(last)
                m = re.search(r"[\w\-./]+\.\w+", task)
                hit = next((f for f in files if f in task or Path(f).name in task), None) or (m.group(0) if m else None)
                loose = sorted((f for f in E.fences(last) if not f["path"] and f["body"].strip()), key=lambda f: len(f["body"]), reverse=True)
                if not ops and hit and loose:  # a bare code block (any language) for the file the request names...
                    old = WS / hit
                    old_lines = len(E.read_text(old)[0].splitlines()) if old.is_file() else 0
                    if len(loose[0]["body"].splitlines()) >= 0.6 * old_lines:  # ...but only if it is the whole file, not a fragment
                        ops = [{"kind": "write", "path": hit, "body": loose[0]["body"]}]
                cmds = [o["cmd"] for o in ops if o["kind"] == "run"]
                new_cmds = [c for c in cmds if c not in allowed_cmds and c not in denied_cmds]
                if new_cmds:  # every shell command is the user's call, even outside approve mode
                    emit(confirm_run=new_cmds)
                    (allowed_cmds if wait_decision() else denied_cmds).update(new_cmds)
                if root:
                    shutil.rmtree(root, ignore_errors=True)
                root, copied = E.stage(WS, files)
                errs = E.apply_ops(root, ops)
                run_out = E.run_cmds(root, [c for c in cmds if c in allowed_cmds])
                denied = [c for c in cmds if c in denied_cmds]
                if denied:  # the user's choice, not a defect: the rest of the change is still judged on its own
                    run_out = (run_out + "\n" if run_out else "") + "ผู้ใช้ไม่อนุญาตให้รันคำสั่ง (ข้ามไป ไม่ต้องรันอีก): " + ", ".join(denied)
                if proj().get("hook_post_edit") and ops:  # e.g. a formatter; runs on the copy, its changes are reviewed too
                    run_out = (run_out + "\n" if run_out else "") + "[hook หลังแก้]\n" + E.run_cmds(root, [proj()["hook_post_edit"]], timeout=300)
                changed, deleted = E.changes(WS, root, copied)
                missing = [f for f in named if f not in changed and f not in deleted]
                if missing:
                    errs.append("คำสั่งระบุไฟล์เหล่านี้ แต่ยังไม่มีการแก้: " + ", ".join(missing))
                if not ops:
                    errs = ["ไม่พบการแก้ไข: ตอบด้วย block เต็มไฟล์ (path=...), SEARCH/REPLACE, DELETE/RENAME หรือ RUN"]
                report, checks_ok = E.run_checks(root, changed, test_cmd, pyexe)
                result = "\n".join(errs + ([run_out] if run_out else []) + [report])
                emit(note="ตรวจในสำเนา: " + (", ".join(changed + [f"ลบ {d}" for d in deleted]) or "ไม่มีไฟล์เปลี่ยน")
                     + (f" · ข้อผิดพลาด {len(errs)}" if errs else ""))
                if errs:
                    emit(note="แก้ไม่สำเร็จ: " + " | ".join(errs)[:400])
                diff = E.diff_for(WS, root, changed, deleted) if (changed or deleted) else ""
                if diff:
                    emit(note=f"diff รอบที่ {rnd + 1} (ยังไม่เขียนจริง)", diff=diff)
                feedback = say("QA", f"Original request (authoritative):\n{task}\n\nSpec:\n{spec}\n\nDIFF:\n{diff}\n\nCHECK RESULT:\n{result}")
                ok = passed(feedback) and checks_ok and not errs and bool(changed or deleted)
                if passed(feedback) and not ok:
                    emit(note="QA ให้ PASS แต่การตรวจไม่ผ่าน → ตีกลับอัตโนมัติ")
                    feedback += "\nSYSTEM: checks failed, verdict overridden to FAIL."
                if ok:
                    break
            if ok:
                emit(note="แก้ไขไฟล์: " + ", ".join(changed + [f"ลบ {d}" for d in deleted]), diff=diff)
                declined = False
                if approve:
                    emit(approve=True, files=changed + deleted)
                    declined = not wait_decision()
                    if declined:
                        emit(note="ไม่เขียนไฟล์ (ไม่อนุมัติหรือหมดเวลา)")
                if not declined:
                    run = E.commit(WS, root, changed, deleted)
                    emit(note=f"เขียน {len(changed)} ไฟล์, ลบ {len(deleted)} ไฟล์ (สำรองของเดิมไว้ที่ .office_bak/{run})",
                         files=changed + deleted, applied=run)
                    if proj().get("hook_after_write"):
                        emit(note="hook หลังเขียนไฟล์: " + E.run_cmds(WS, [proj()["hook_after_write"]], timeout=300).replace("\n", " ")[:600])
                    if proj().get("git_commit") and E.is_git(WS):
                        emit(note="git: " + E.git_commit(WS, changed + deleted, "AI Office: " + task[:72]))
                else:
                    ok = False
            else:
                emit(note="ไม่ผ่านการตรวจ จึงไม่เขียนไฟล์")
        finally:
            if root:
                shutil.rmtree(root, ignore_errors=True)
        return ok, rnd, feedback, last, changed + deleted

    if WS:
        ok, rnd, feedback, code, touched = edit_loop()
    else:
        code, feedback, result, seen, ok, rnd, touched = "", "", "", set(), False, 0, []
        for rnd in range(MAX_ROUNDS):
            emit(round=rnd + 1)
            redo = f"\n\nPrevious code:\n{code}\n\nQA feedback:\n{feedback}\n\nRUN RESULT of previous code:\n{result}" if feedback else ""
            code = say("DEV", f"Original request (authoritative, follow it exactly):\n{task}\n\nSpec:\n{spec}"
                              f"\n\nDepartment notes (follow the change plan and security rules):{notes}{redo}")
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
        emit(note="ไม่ได้เลือกโฟลเดอร์งาน: โค้ดแสดงอย่างเดียว ไม่เขียนไฟล์")
    summary = say("BOSS", f"สรุปผลให้ลูกค้า ผลตรวจ: {'ผ่าน' if ok else f'ไม่ผ่านหลัง {rnd + 1} รอบ'}\nQA:\n{feedback}\nCode:\n{code[:4000]}", phase="report")
    record(task, ok, touched, summary, round(time.time() - t0))
    emit(done=ok, tokens=tokens["n"])


# ---------- HTTP ----------

JOB = threading.Lock()  # one job at a time


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
            return self.reply(200, {"path": str(WS) if WS else "", "count": len(E.project_files(WS)) if WS else 0})
        if self.path == "/config":
            return self.reply(200, config())
        if self.path == "/history":
            return self.reply(200, proj().get("history", [])[::-1])
        if self.path == "/detect_test":
            return self.reply(200, {"cmd": E.detect_test_cmd(WS) if WS else ""})
        if self.path == "/git":
            return self.reply(200, E.git_status(WS) if WS else {"git": False})
        if self.path == "/setup/status":
            return self.reply(200, setup_status())
        if self.path == "/update":
            return self.reply(200, UPDATE)
        self.reply(200, open(HERE / "index.html", "rb").read(), "text/html")

    def do_POST(self):
        # file writes are possible, so refuse cross-site requests (a web page POSTing to localhost)
        if self.headers.get("Origin") not in (None, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.reply(403, {"error": "forbidden origin"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        try:
            if self.path == "/workspace":
                set_ws(body["path"])
                return self.reply(200, {"path": str(WS), "count": len(E.project_files(WS))})
            if self.path == "/models":
                set_model(body["role"], body["kind"], body["name"])
                return self.reply(200, {"ok": True})
            if self.path == "/provider":
                set_provider(body)
                return self.reply(200, {"ok": True})
            if self.path == "/provider/test":
                return self.reply(200, {"reply": test_provider(body)})
            if self.path == "/undo":
                if not WS:
                    raise ValueError("ยังไม่ได้เลือกโฟลเดอร์งาน")
                run, files = E.undo_last(WS)
                return self.reply(200, {"run": run, "files": files})
            if self.path == "/settings":
                if "test_cmd" in body:
                    proj()["test_cmd"] = (body.get("test_cmd") or "").strip()
                if "git_commit" in body:
                    proj()["git_commit"] = bool(body["git_commit"])
                if "num_ctx" in body:
                    CFG["num_ctx"] = max(2048, int(body["num_ctx"]))
                for k in ("hook_before", "hook_post_edit", "hook_after_write"):
                    if k in body:
                        proj()[k] = (body[k] or "").strip()
                if "web" in body:
                    CFG["web"] = bool(body["web"])
                if "search" in body:
                    sv = body["search"] or {}
                    old_key = (CFG.get("search") or {}).get("key", "")
                    CFG["search"] = {"provider": sv.get("provider", ""), "url": (sv.get("url") or "").strip(),
                                     "key": (sv.get("key") or "").strip() or old_key}  # empty field keeps the saved key
                if "mcp" in body:
                    mcp = json.loads(body["mcp"]) if isinstance(body["mcp"], str) else body["mcp"]
                    if not isinstance(mcp, dict) or any(not isinstance(v, dict) or "command" not in v for v in mcp.values()):
                        raise ValueError('MCP ต้องเป็น JSON แบบ {"ชื่อ": {"command": "...", "args": [...]}}')
                    CFG["mcp"] = mcp
                save_settings()
                return self.reply(200, {"ok": True})
            if self.path == "/run_test":
                cmd = proj().get("test_cmd", "")
                if not (WS and cmd):
                    raise ValueError("ยังไม่ได้ตั้งคำสั่งทดสอบหรือโฟลเดอร์งาน")
                return self.reply(200, {"output": E.run_cmds(WS, [cmd], timeout=300)})
            if self.path == "/mcp/test":
                st = X.mcp_servers(CFG.get("mcp"))
                return self.reply(200, {k: (v if isinstance(v, str) else [t["name"] for t in v.tools]) for k, v in st.items()})
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
            if not JOB.acquire(blocking=False):
                return self.reply(409, {"error": "มีงานกำลังรันอยู่ รอให้เสร็จหรือกดหยุดก่อน"})
            try:
                return self.stream(lambda emit: orchestrate(body["task"], emit, bool(body.get("approve")), body.get("mode", "edit"), body.get("attachments")))
            finally:
                JOB.release()
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
