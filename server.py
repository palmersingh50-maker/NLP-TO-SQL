"""NL2SQL Studio - 100% local. No AI, no internet. Run: python3 server.py"""
import json, os, re, sys, threading, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "engine"))
from nl2sql import translate, SAMPLE_SCHEMA

PORT = int(os.environ.get("PORT", "8765"))

def risks(sql):
    out = []
    for stmt in filter(None, (s.strip() for s in sql.split(";"))):
        u = stmt.upper()
        if re.match(r"(DELETE|UPDATE)\b", u) and " WHERE " not in u:
            out.append("No WHERE clause: this will affect every row.")
        if re.search(r"\b(DROP|TRUNCATE)\b", u):
            out.append("This permanently removes data or structures.")
        elif re.match(r"DELETE\b", u):
            out.append("Destructive query: double-check the condition and back up first.")
    return list(dict.fromkeys(out))

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/status":
            return self._send(200, {"engine": "local", "sample_schema": SAMPLE_SCHEMA})
        self._send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            res = translate(d.get("text", ""), d.get("schema", ""), d.get("dialect", "PostgreSQL"))
        except Exception as e:
            res = {"sql": f"-- Could not understand that request ({e}).", "explanation": "Try rephrasing, or check the Schema box."}
        res["warnings"] = risks(res["sql"])
        self._send(200, res)
    def log_message(self, *a): pass

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"NL2SQL Studio running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
