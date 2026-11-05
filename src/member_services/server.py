"""Small HTTP API so a rep conversation can span several turns."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from member_services.config import load_settings
from member_services.service import Assistant

ASSISTANT: Assistant | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "MemberAssistant/1.0"

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "as_of": ASSISTANT.as_of.isoformat()})
        parts = [part for part in self.path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["v1", "conversations"]:
            session = ASSISTANT.sessions.get(parts[2])
            if session is None:
                return self._send(404, {"error": "not_found"})
            return self._send(
                200,
                {
                    "conversation_id": session.conversation_id,
                    "tenant": session.tenant,
                    "member_id": session.member_id,
                    "steps": session.steps,
                    "cost_usd": session.cost_usd,
                    "trace": session.events,
                },
            )
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        parts = [part for part in self.path.split("/") if part]
        body = self._body()
        if parts == ["v1", "conversations"]:
            try:
                session = ASSISTANT.open(body["tenant"], body["member_id"])
            except KeyError:
                return self._send(404, {"error": "unknown_tenant"})
            return self._send(
                201,
                {"conversation_id": session.conversation_id, "tenant": session.tenant, "member_id": session.member_id},
            )
        if len(parts) == 4 and parts[:2] == ["v1", "conversations"] and parts[3] == "messages":
            if parts[2] not in ASSISTANT.sessions:
                return self._send(404, {"error": "not_found"})
            result = ASSISTANT.turn(parts[2], body.get("text", ""))
            return self._send(200, result)
        return self._send(404, {"error": "not_found"})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _send(self, status: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


def serve(host: str = "127.0.0.1", port: int = 8088):
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main():
    global ASSISTANT
    parser = argparse.ArgumentParser(description="Member-services HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    settings = load_settings()
    ASSISTANT = Assistant(settings)
    host, port = args.host, args.port
    httpd = serve(host, port)
    print(f"Member assistant listening on http://{host}:{port}  (as of {ASSISTANT.as_of})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
