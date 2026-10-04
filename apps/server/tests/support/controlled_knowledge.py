import hmac
import json
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Lock, Thread
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import SecretStr


class BoundedHTTPServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


class OwnedKnowledgeServer:
    """One exact authorized workspace with a test-owned known-empty catalog."""

    def __init__(self, workspace: UUID) -> None:
        self.workspace = workspace
        self.token = SecretStr(uuid4().hex)
        self._calls = []
        self._lock = Lock()
        self._stop = Event()
        self._closed = False
        self._thread = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                path = urlsplit(self.path).path
                parts = path.split("/")
                workspace_id = None
                if len(parts) >= 5 and parts[1:4] == ["api", "v1", "workspaces"]:
                    with suppress(ValueError):
                        workspace_id = UUID(parts[4])
                exact_path = f"/api/v1/workspaces/{owner.workspace}/knowledge/search"
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""),
                    "Bearer " + owner.token.get_secret_value(),
                ):
                    status, code = 401, "permission_denied"
                elif workspace_id is not None and workspace_id != owner.workspace:
                    status, code = 403, "permission_denied"
                elif self.command != "GET" or path != exact_path:
                    status, code = 404, "reference_not_found"
                else:
                    status, code = 200, None
                body = (
                    {"items": [], "next_cursor": None}
                    if code is None
                    else {
                        "error": {
                            "code": code,
                            "message": "Controlled request denied",
                            "details": {},
                        }
                    }
                )
                with owner._lock:
                    owner._calls.append(
                        {
                            "method": self.command,
                            "path": path,
                            "workspace_id": None
                            if workspace_id is None
                            else str(workspace_id),
                            "status": status,
                            "item_count": 0 if status == 200 else None,
                        }
                    )
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = BoundedHTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 0.05
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> None:
        if self._closed or self._thread is not None:
            raise RuntimeError("Owned external fixture cannot be restarted")

        def serve():
            while not self._stop.is_set():
                self._server.handle_request()

        self._thread = Thread(target=serve, name="n17-owned-knowledge-http")
        self._thread.start()

    def close(self) -> bool:
        if self._closed:
            return True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            if self._thread.is_alive():
                return False
        self._server.server_close()
        self._closed = True
        return True

    def facts(self):
        with self._lock:
            calls = [dict(call) for call in self._calls]
        return {
            "workspace_id": str(self.workspace),
            "known_empty_catalog": True,
            "thread_alive": self._thread is not None and self._thread.is_alive(),
            "closed": self._closed,
            "calls": calls,
        }
