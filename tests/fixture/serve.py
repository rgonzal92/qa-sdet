"""The fixture app's server: the static pages of tests/fixture, plus what a static server can't answer (the shell's
routes, a GraphQL endpoint, a notes API, slow answers). Standard library only: `python3 tests/fixture/serve.py [port]`."""

import itertools
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


NOTES = {}  # id -> {id, name, priority}: in memory, gone with the server
NOTE_IDS = itertools.count(1)
RESERVED = {"error": "The name reserved belongs to the system"}


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
        if url.path == "/api/notes":
            return self._json(200, list(NOTES.values()))
        if url.path.startswith("/console/"):  # every shell route is the shell page: it shows the module the path names
            self.path = "/shell.html"
        return super().do_GET()

    def _body(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    def _note(self):
        """The note a /api/notes/<id> path names, or None."""
        head, _, tail = urlsplit(self.path).path.rpartition("/")
        return NOTES.get(int(tail)) if head == "/api/notes" and tail.isdigit() else None

    def do_POST(self):
        body, path = self._body(), urlsplit(self.path).path
        if path == "/graphql":
            unknown = {"errors": [{"message": "unknown operation"}], "data": None}
            return self._json(200, GRAPHQL.get(body.get("operationName"), unknown))
        if path != "/api/notes":
            return self._json(404, {"error": "not found"})
        name = str(body.get("name") or "").strip()
        if not name or name.lower() == "reserved":
            return self._json(422, RESERVED if name else {"error": "A note needs a name"})
        note = {"id": next(NOTE_IDS), "name": name, "priority": body.get("priority") or "normal"}
        NOTES[note["id"]] = note
        if len(name) > 60:  # planted: the note is stored, then the answer fails (a long name is a tester's first try)
            return self._json(502, {"error": "Bad gateway"})
        return self._json(201, note)

    def do_PUT(self):
        note, body = self._note(), self._body()
        if note is None:
            return self._json(404, {"error": "not found"})
        name = str(body.get("name") or "").strip()
        if not name or name.lower() == "reserved":
            return self._json(422, RESERVED if name else {"error": "A note needs a name"})
        note["name"] = name  # planted: the priority is answered as sent and never stored
        return self._json(200, {**note, "priority": body.get("priority") or note["priority"]})

    def do_DELETE(self):
        note = self._note()
        if note is None:
            return self._json(404, {"error": "not found"})
        del NOTES[note["id"]]
        self.send_response(204)
        self.end_headers()
        return None


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
