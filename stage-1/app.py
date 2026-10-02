"""HTTP plumbing: routing, JSON encoding, bearer-token extraction.

The stdlib ``http.server`` gives us a threaded server for free, which is
enough for 50 concurrent requests under the spec's 5 s per-request budget.
Each request is handled on its own thread and shares a single ``State``
guarded by an ``RLock``; see ``operations.py`` for the locking model.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import operations
import validation
from state_core import State
from validation import HttpError


# One process-wide state container. Initialised at startup.
STATE: State = State()


# ---- routing --------------------------------------------------------------


_PATH_RE = re.compile(r"^/reservations/(?P<reference>[A-Z0-9]{6,12})$")
_CANCEL_RE = re.compile(r"^/reservations/(?P<reference>[A-Z0-9]{6,12})/cancel$")
_PATCH_RE = re.compile(r"^/reservations/(?P<reference>[A-Z0-9]{6,12})$")


def _match(method: str, path: str) -> Optional[tuple[str, dict]]:
    """Return ``(route_name, groups)`` if the request matches a route, else None."""
    if method == "GET":
        if path == "/health":
            return ("health", {})
        if path == "/restaurants":
            return ("restaurants.list", {})
        m = re.match(r"^/restaurants/([^/]+)$", path)
        if m:
            return ("restaurants.get", {"rid": m.group(1)})
        if path == "/availability":
            return ("availability", {})
        if path == "/reservations":
            return ("reservations.list", {})
        m = _PATH_RE.match(path)
        if m:
            return ("reservations.get", {"reference": m.group("reference")})
    elif method == "POST":
        if path == "/_test/reset":
            return ("reset", {})
        if path == "/_test/import":
            return ("import", {})
        if path == "/auth/signup":
            return ("auth.signup", {})
        if path == "/auth/login":
            return ("auth.login", {})
        if path == "/reservations":
            return ("reservations.create", {})
        if path == "/reservation-moves":
            return ("reservation-moves", {})
        m = _CANCEL_RE.match(path)
        if m:
            return ("reservations.cancel", {"reference": m.group("reference")})
    elif method == "PATCH":
        m = _PATCH_RE.match(path)
        if m:
            return ("reservations.patch", {"reference": m.group("reference")})
    if path == "/_test/export":
        return ("export", {})
    return None


# ---- HTTP handler ---------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Keep the log line short; the harness captures stdout/stderr separately.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    # ---- low-level response helpers ------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_error(self, status: int, code: str, message: Optional[str] = None) -> None:
        self._send_json(status, {"error": {"code": code,
                                           "message": message or code}})

    # ---- request parsing ------------------------------------------------

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _bearer_token(self) -> Optional[str]:
        auth = self.headers.get("Authorization")
        if not auth:
            return None
        if not auth.lower().startswith("bearer "):
            return None
        return auth[7:].strip() or None

    # ---- dispatch -------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        try:
            route = _match(method, path)
        except Exception as exc:
            return self._send_error(500, "internal", str(exc))
        if route is None:
            return self._send_error(404, "not_found", "no such route")
        name, params = route
        try:
            self._call(name, params, parse_qs(parsed.query))
        except HttpError as exc:
            return self._send_error(exc.status, exc.code, exc.message)
        except operations.OperationError as exc:
            return self._send_error(exc.status, exc.code, exc.message)
        except Exception as exc:
            return self._send_error(500, "internal", str(exc))

    def _call(self, name: str, params: dict, query: dict) -> None:
        if name == "health":
            return self._send_json(200, {"status": "ok"})

        body_raw = self._body() if name != "health" else b""
        if name in ("reset", "import", "auth.signup", "auth.login",
                    "reservations.create", "reservations.patch",
                    "reservation-moves"):
            body = validation.parse_json_body(body_raw)
        else:
            body = {}

        if name == "reset":
            operations.load_fixture(STATE, body)
            return self._send_json(204, None)
        if name == "export":
            result = operations.export_state(STATE)
            return self._send_json(result.status, result.body)
        if name == "import":
            result = operations.import_state(STATE, body)
            return self._send_json(result.status, result.body)

        if name == "auth.signup":
            result = operations.signup(STATE, body)
            return self._send_json(result.status, result.body)
        if name == "auth.login":
            result = operations.login(STATE, body)
            return self._send_json(result.status, result.body)

        # Public restaurant/availability endpoints: no token required.
        if name == "restaurants.list":
            result = operations.list_restaurants(STATE)
            return self._send_json(result.status, result.body)
        if name == "restaurants.get":
            result = operations.get_restaurant(STATE, params["rid"])
            return self._send_json(result.status, result.body)
        if name == "availability":
            q = {k: v[0] for k, v in query.items()}
            result = operations.availability(
                STATE,
                restaurant_id=q.get("restaurant_id"),
                date=q.get("date"),
                party_size=q.get("party_size"),
            )
            return self._send_json(result.status, result.body)

        # Everything below requires a valid bearer token.
        token = self._bearer_token()
        user_id = operations.authenticate(STATE, token)

        if name == "reservations.create":
            key = validation.check_idempotency_key(
                self.headers.get("Idempotency-Key"))
            result = operations.create_reservation(
                STATE, user_id=user_id, body=body, idempotency_key=key)
            return self._send_json(result.status, result.body)
        if name == "reservations.list":
            result = operations.list_reservations(STATE, user_id=user_id)
            return self._send_json(result.status, result.body)
        if name == "reservations.get":
            result = operations.get_reservation(
                STATE, user_id=user_id, reference=params["reference"])
            return self._send_json(result.status, result.body)
        if name == "reservations.cancel":
            result = operations.cancel_reservation(
                STATE, user_id=user_id, reference=params["reference"])
            return self._send_json(result.status, result.body)
        if name == "reservations.patch":
            result = operations.patch_reservation(
                STATE, user_id=user_id, reference=params["reference"],
                body=body)
            return self._send_json(result.status, result.body)
        if name == "reservation-moves":
            key = validation.check_idempotency_key(
                self.headers.get("Idempotency-Key"))
            result = operations.moves(
                STATE, user_id=user_id, body=body, idempotency_key=key)
            return self._send_json(result.status, result.body)

        return self._send_error(404, "not_found", "route not implemented")

    # ---- verb hooks -----------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")


# ---- server entry point ---------------------------------------------------


class Server(ThreadingHTTPServer):
    """A stdlib HTTP server tuned for the harness's concurrency budget."""

    # 50 concurrent in-flight requests with burst retry; the default 5
    # drops connections well below that.
    request_queue_size = 256
    daemon_threads = True


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    print(f"tablekeeper stage-1 listening on 0.0.0.0:{port}", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
