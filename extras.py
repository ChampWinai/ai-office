"""Outside-world tools for the agents: web search, web page fetch, and MCP servers (stdio JSON-RPC).

Tool lines the agents may write:
  WEB: search words                 -> top results (title, url, snippet) via Brave Search API or SearXNG
  FETCH: https://example.com/page   -> the page as plain text (public addresses only)
  MCP: server.tool {"arg": "value"} -> call a tool of a configured MCP server
"""
import html, ipaddress, json, queue, re, shutil, socket, subprocess, threading, time, urllib.parse, urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AIOffice"
NO_WINDOW = 0x08000000


# ---------- web ----------

def check_public(url):
    """Only http(s) to public addresses: a fetched page must never reach this machine or the LAN (it could hit the
    app's own localhost API)."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("ใช้ได้เฉพาะลิงก์ http/https")
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"ไม่อนุญาตที่อยู่ภายใน: {u.hostname}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)  # a public page must not bounce us to localhost
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def _get(url, timeout=15, limit=2_000_000):
    check_public(url)
    with _opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        raw = r.read(limit)
        charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), r.headers.get("Content-Type", "")


def html_to_text(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def web_search(query, cfg, n=8):
    """Search through an API the user configured: Brave Search (key) or a SearXNG instance (url).
    Scraping a search page is not used: those pages answer bots with a challenge."""
    cfg = cfg or {}
    if cfg.get("provider") == "brave" and cfg.get("key"):
        req = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?count=%d&q=%s" % (n, urllib.parse.quote(query)),
                                     headers={"Accept": "application/json", "X-Subscription-Token": cfg["key"], "User-Agent": UA})
        rows = json.load(urllib.request.urlopen(req, timeout=15)).get("web", {}).get("results", [])
        hits = [(r.get("title", ""), r.get("url", ""), r.get("description", "")) for r in rows]
    elif cfg.get("provider") == "searxng" and cfg.get("url"):
        # the user's own instance may live on localhost, so it is not run through check_public
        req = urllib.request.Request(cfg["url"].rstrip("/") + "/search?format=json&q=" + urllib.parse.quote(query), headers={"User-Agent": UA})
        rows = json.load(urllib.request.urlopen(req, timeout=15)).get("results", [])
        hits = [(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in rows]
    else:
        return "(ยังไม่ได้ตั้งค่าการค้นเว็บ: ใส่ Brave Search API key หรือ URL ของ SearXNG ในหน้า ⚙ โปรเจกต์ ระหว่างนี้ใช้ FETCH: <url> เปิดหน้าเอกสารที่รู้ที่อยู่ได้)"
    out = [f"- {html_to_text(t)}\n  {u}\n  {html_to_text(d)[:300]}" for t, u, d in hits[:n]]
    return "\n".join(out) or "(ไม่พบผลลัพธ์)"


def fetch(url, limit=8000):
    page, ctype = _get(url)
    text = html_to_text(page) if "html" in ctype or "<html" in page[:500].lower() else page
    return text[:limit] + (f"\n(ตัดไว้ {limit} ตัวอักษร)" if len(text) > limit else "")


# ---------- MCP (Model Context Protocol) over stdio ----------

class MCPServer:
    """One MCP server process. Requests are JSON-RPC lines; a reader thread queues the replies."""

    def __init__(self, name, spec):
        cmd = shutil.which(spec["command"]) or spec["command"]  # npx -> npx.cmd on Windows
        self.name, self.id, self.lock, self.q = name, 0, threading.Lock(), queue.Queue()
        self.p = subprocess.Popen([cmd, *spec.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                  env={**__import__("os").environ, **spec.get("env", {})}, creationflags=NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "ai-office", "version": "1"}}, timeout=60)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = self.request("tools/list", {}).get("tools", [])

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                continue  # servers sometimes log plain text to stdout

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method, params, timeout=120):
        with self.lock:
            self.id += 1
            self._send({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params})
            end = time.time() + timeout
            while time.time() < end:
                try:
                    msg = self.q.get(timeout=max(0.1, end - time.time()))
                except queue.Empty:
                    break
                if msg.get("id") == self.id:
                    if "error" in msg:
                        raise RuntimeError(f"MCP {self.name}: {msg['error'].get('message', msg['error'])}")
                    return msg.get("result", {})
            raise TimeoutError(f"MCP {self.name}: ไม่ตอบภายใน {timeout} วินาที")

    def call(self, tool, args):
        r = self.request("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        return (text or json.dumps(r, ensure_ascii=False))[:8000]

    def alive(self):
        return self.p.poll() is None

    def close(self):
        if self.alive():
            self.p.kill()


_servers = {}


def mcp_servers(config):
    """Start (or reuse) every configured server. Returns {name: server or error text}."""
    out = {}
    for name, spec in (config or {}).items():
        s = _servers.get(name)
        if s and s.alive():
            out[name] = s
            continue
        try:
            _servers[name] = out[name] = MCPServer(name, spec)
        except Exception as e:  # missing program, bad args, crash on start
            out[name] = f"เปิดไม่ได้: {type(e).__name__}: {e}"
    for name in list(_servers):  # servers removed from the settings
        if name not in (config or {}):
            _servers.pop(name).close()
    return out


def mcp_help(servers):
    lines = []
    for name, s in servers.items():
        if isinstance(s, str):
            continue
        for t in s.tools[:30]:
            props = ", ".join(f'"{p}": ...' for p in (t.get("inputSchema") or {}).get("properties", {}))
            lines.append(f"  MCP: {name}.{t['name']} {{{props}}}   - {(t.get('description') or '')[:160]}")
    return ("MCP: <server>.<tool> {json arguments on the same line}   -> call a tool:\n" + "\n".join(lines)) if lines else ""


def mcp_call(servers, arg):
    m = re.match(r"\s*([\w\-]+)\.([\w\-./]+)\s*(\{.*\})?\s*$", arg)
    if not m:
        raise ValueError("รูปแบบ: MCP: server.tool {\"arg\": ...}")
    s = servers.get(m.group(1))
    if not s or isinstance(s, str):
        raise ValueError(f"ไม่มี MCP server ชื่อ {m.group(1)}")
    raw = m.group(3) or "{}"
    try:
        args = json.loads(raw)
    except ValueError:  # models often write {1, 2} or {a: 1}: map positional values onto the schema's fields
        tool = next((t for t in s.tools if t["name"] == m.group(2)), {})
        names = list((tool.get("inputSchema") or {}).get("properties", {}))
        parts = [p.strip().strip("'\"") for p in raw.strip("{} ").split(",") if p.strip()]
        args = {}
        for i, p in enumerate(parts):
            k, _, v = p.partition(":") if ":" in p else ("", "", p)
            key = k.strip().strip("'\"") or (names[i] if i < len(names) else f"arg{i}")
            v = v.strip().strip("'\"")
            args[key] = int(v) if re.fullmatch(r"-?\d+", v) else float(v) if re.fullmatch(r"-?\d+\.\d+", v) else v
    return s.call(m.group(2), args)
