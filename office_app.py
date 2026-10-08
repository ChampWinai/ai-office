"""AI Office desktop app: starts the local server and shows it in a native window (WebView2 on Windows).
Also makes sure Ollama is running. Run: python office_app.py"""
import os, subprocess, threading, urllib.request
import webview
import server


class Api:
    def pick_folder(self):  # called from the page: native folder dialog
        r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else ""


def ensure_ollama():
    try:
        urllib.request.urlopen(server.OLLAMA + "/api/tags", timeout=2)
    except OSError:
        exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        if os.path.exists(exe):  # ponytail: no wait/retry; the page shows an error if it is not up yet
            subprocess.Popen([exe, "serve"], creationflags=0x08000000)  # CREATE_NO_WINDOW


if __name__ == "__main__":
    ensure_ollama()
    srv = server.serve()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webview.create_window("AI Office", f"http://localhost:{server.PORT}", js_api=Api(), width=1400, height=900, min_size=(900, 600))
    webview.start()
    srv.shutdown()
