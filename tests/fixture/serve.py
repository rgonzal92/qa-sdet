"""The fixture app's server: the static pages of tests/fixture, plus what a static server can't answer (the shell's
routes, a GraphQL endpoint, slow answers). Standard library only: `python3 tests/fixture/serve.py [port]`."""

import json
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
MAX_DELAY_MS = 5000

# operationName -> the body answered with HTTP 200 (a GraphQL server reports its failures inside a 200)
GRAPHQL = {
    "Notes": {"data": {"notes": [{"id": 1, "text": "Lane 2 closed"}]}},
    "Zones": {"errors": [{"message": "Not allowed to read zones"}], "data": None},
    "SaveNote": {"data": {"saveNote": {"note": None, "errors": [{"message": "Text is too long"}]}}},
    "BulkUpdate": {
        "data": {
            "bulkUpdate": {
                "updated": 2,
                "failed": 1,
                "results": [
                    {"id": "note-1", "ok": True},
                    {"id": "note-2", "ok": True},
                    {"id": "note-3", "ok": False, "reason": "Locked"},
                ],
            }
        }
    },
}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        delay = parse_qs(url.query).get("delay", ["0"])[0]
        if delay.isdigit():
            time.sleep(min(int(delay), MAX_DELAY_MS) / 1000)
        if url.path.startswith("/console/"):  # every shell route is the shell page: it shows the module the path names
            self.path = "/shell.html"
        super().do_GET()

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        if urlsplit(self.path).path == "/graphql" and isinstance(body, dict):
            unknown = {"errors": [{"message": "unknown operation"}], "data": None}
            return self._json(200, GRAPHQL.get(body.get("operationName"), unknown))
        return self._json(404, {"error": "not found"})


def server(port=0):
    """The fixture server on 127.0.0.1 (port 0: a free one, read it from `.server_address`); the caller runs it."""
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(ROOT)))


if __name__ == "__main__":
    httpd = server(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"fixture app on http://127.0.0.1:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
