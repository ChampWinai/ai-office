"""Run the benchmark against a private copy of the app (own port and settings, uses the local Ollama models in roles.json).
Usage: python bench/run.py [task_id ...]   Results: bench/out/results.json, per-task transcript.md and events.json."""
import json, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

from tasks import TASKS

SRC = Path(__file__).resolve().parent.parent
B = Path(__file__).parent
APP, OUT = B / "app", B / "out"
PORT = 8011
URL = f"http://127.0.0.1:{PORT}"


def post(path, body, stream=False):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=3600)
    return r if stream else json.loads(r.read() or b"{}")


def main(ids):
    shutil.rmtree(APP, ignore_errors=True)
    APP.mkdir(parents=True)
    for f in ["server.py", "edits.py", "extras.py", "roles.json", "rules.md", "index.html"]:
        shutil.copy(SRC / f, APP / f)
    srv = subprocess.Popen([sys.executable, "server.py"], cwd=APP, env={**__import__("os").environ, "PORT": str(PORT)},
                           stdout=open(B / "server.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            urllib.request.urlopen(URL + "/config", timeout=2); break
        except Exception:
            time.sleep(0.3)
    results = []
    try:
        for t in [t for t in TASKS if not ids or t["id"] in ids]:
            results.append(one(t))
            json.dump(results, open(OUT / "results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    finally:
        srv.terminate()
    for r in results:
        print(f"{'PASS' if r['hidden_ok'] else 'FAIL'}  app_ok={r['app_ok']!s:5} rounds={r['rounds']} {r['secs']:>4}s  {r['id']}  {r['why'][:150]}")


def one(t):
    d = OUT / t["id"]
    shutil.rmtree(d, ignore_errors=True)
    for sub in ("orig", "ws"):
        for rel, body in t["files"].items():
            p = d / sub / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode())
    ws = d / "ws"
    post("/workspace", {"path": str(ws)})
    post("/settings", {"test_cmd": t.get("test_cmd", "")})
    t0, log, cur, texts = time.time(), [], {}, []
    rounds, app_ok, tokens = 0, None, 0
    for line in post("/", {"task": t["task"], "mode": "edit"}, stream=True):
        e = json.loads(line)
        if "token" in e:
            cur[e["role"]] = cur.get(e["role"], "") + e["token"]; continue
        if e.get("status") == "done":
            texts.append((e["role"], cur.pop(e["role"], "")))
        if "round" in e: rounds = e["round"]
        if "confirm_run" in e or "approve" in e:
            post("/decide", {"ok": True})
        if "done" in e: app_ok, tokens = e["done"], e.get("tokens", 0)
        log.append(e)
    ok, why = t["check"](ws)
    (d / "events.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "transcript.md").write_text("\n\n".join(f"## {r}\n{x}" for r, x in texts), encoding="utf-8")
    r = dict(id=t["id"], app_ok=app_ok, hidden_ok=ok, why=why, rounds=rounds, secs=round(time.time() - t0), tokens=tokens,
             notes=[e["note"] for e in log if "note" in e], errors=[e["error"] for e in log if "error" in e])
    print(("PASS " if ok else "FAIL ") + t["id"], r["secs"], "s", flush=True)
    return r


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    main(sys.argv[1:])
