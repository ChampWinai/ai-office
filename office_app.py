"""AI Office desktop app: starts the local server and shows it in a native window (WebView2 on Windows).
Run: python office_app.py (Ollama must be running; its installer starts it at login)"""
import threading
import webview
import server


class Api:
    def pick_folder(self):  # called from the page: native folder dialog
        r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else ""


if __name__ == "__main__":
    srv = server.serve()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webview.create_window("AI Office", f"http://localhost:{server.PORT}", js_api=Api(), width=1400, height=900, min_size=(900, 600))
    webview.start()
    srv.shutdown()
