"""Own coverage for ledger gaps the harness sample doesn't exercise.

Boots the actual HTTP server in a thread and drives it over a real socket.
Run from inside the container's working dir: ``python -m pytest tests`` or
``python tests/test_app.py``.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPResponse
from urllib.parse import urlencode

import app as appmod
import operations
import state_core
import validation

# Importing app runs main() only as a side-effect of being a script; here
# we just take the server class and handler.


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server() -> tuple[appmod.Server, str, int]:
    port = _free_port()
    server = appmod.Server(("127.0.0.1", port), appmod.Handler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    host = "127.0.0.1"
    return server, host, port


def _request(conn: HTTPConnection, method: str, path: str,
             body: dict | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict]:
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"} if body is not None else {}
    if headers:
        h.update(headers)
    conn.request(method, path, body=payload, headers=h)
    resp: HTTPResponse = conn.getresponse()
    raw = resp.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return resp.status, parsed


def _reset() -> None:
    operations.load_fixture(appmod.STATE, {})


def _default_fixture() -> dict:
    weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return {
        "users": [],
        "restaurants": [{
            "id": "rest_001",
            "name": "Test Bistro",
            "timezone": "UTC",
            "opening_hours": [{"weekday": wd, "opens": "00:00", "closes": "23:59"}
                              for wd in weekday_codes],
            "tables": [{"id": "tbl_a", "label": "A", "capacity": 4}],
            "slot_minutes": 30,
            "reservation_duration_minutes": 60,
            "cancellation_cutoff_minutes": 60,
        }],
        "reservations": [],
    }


def _bootstrap(port: int) -> tuple[str, str, str]:
    """Sign up one user and return (token, restaurant_id, table_id)."""
    operations.load_fixture(appmod.STATE, _default_fixture())
    conn = HTTPConnection("127.0.0.1", port)
    email = f"u{uuid.uuid4().hex[:8]}@x.io"
    status, body = _request(conn, "POST", "/auth/signup",
                            {"email": email, "password": "secret123",
                             "display_name": "U"})
    assert status == 201, body
    token = body["token"]
    status, body = _request(conn, "GET", "/restaurants/rest_001",
                            headers={"Authorization": f"Bearer {token}"})
    assert status == 200, body
    tid = body["tables"][0]["id"]
    conn.close()
    return token, "rest_001", tid


# ---- DST Berlin fall-back --------------------------------------------------


def test_berlin_fallback_resolves_to_first_occurrence() -> None:
    server, host, port = _start_server()
    try:
        # Build a Berlin restaurant with weekday-agnostic opening hours so
        # the test is not date-dependent.
        weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        with appmod.STATE.lock():
            appmod.STATE._restaurants["rest_b"] = {
                "id": "rest_b", "name": "Berlin Bistro",
                "timezone": "Europe/Berlin",
                "opening_hours": [{"weekday": wd,
                                   "opens": "00:00", "closes": "23:59"}
                                  for wd in weekday_codes],
                "tables": [{"id": "tbl_b1", "label": "B1", "capacity": 4}],
                "slot_minutes": 30,
                "reservation_duration_minutes": 90,
                "cancellation_cutoff_minutes": 60,
            }
        conn = HTTPConnection(host, port)
        email = f"u{uuid.uuid4().hex[:8]}@x.io"
        status, body = _request(conn, "POST", "/auth/signup",
                                {"email": email, "password": "secret123",
                                 "display_name": "U"})
        token = body["token"]
        # 2024-10-27 in Berlin has a fall-back at 03:00 -> 02:00. Local
        # 02:30 is ambiguous; the harness expects the first occurrence
        # (UTC 00:30), not the second (UTC 01:30).
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": "rest_b", "table_id": "tbl_b1",
             "starts_at_local": "2024-10-27T02:30",
             "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "key1"})
        assert status == 201, body
        # UTC offset for 2024-10-27 02:30 first occurrence = +02:00.
        assert body["starts_at"].endswith("+02:00"), body
    finally:
        server.shutdown()


# ---- Export/Import round-trip --------------------------------------------


def test_export_import_preserves_state() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        # Place a reservation.
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-01-15T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "k1"})
        assert status == 201, body
        ref1 = body["reference"]

        status, snap = _request(conn, "GET", "/_test/export")
        assert status == 200
        assert snap["track"] == "tablekeeper"
        assert snap["format_version"] == 1

        # Reset, then import.
        operations.load_fixture(appmod.STATE, _default_fixture())
        status, body = _request(conn, "POST", "/_test/import", snap)
        assert status == 204, body
        status, body = _request(
            conn, "GET", f"/reservations/{ref1}",
            headers={"Authorization": f"Bearer {token}"})
        assert status == 200, body
        assert body["reference"] == ref1
    finally:
        server.shutdown()


# ---- Idempotent replay after cancel --------------------------------------


def test_idempotent_replay_after_cancel_returns_original_body() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-02-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "k1"})
        assert status == 201, body
        ref = body["reference"]
        original = dict(body)

        # Cancel.
        status, body = _request(conn, "POST", f"/reservations/{ref}/cancel",
                                headers={"Authorization": f"Bearer {token}"})
        assert status == 200, body
        assert body["status"] == "cancelled"

        # Replay same idempotency key. Per D5, the replay returns the
        # ORIGINAL 201 body (with status=confirmed) and status 200,
        # not the post-cancel snapshot.
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-02-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "k1"})
        assert status == 200, body
        assert body == original, body
    finally:
        server.shutdown()


# ---- Query validation ----------------------------------------------------


def test_query_integer_scientific_notation_rejected() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "GET",
            "/availability?" + urlencode(
                {"restaurant_id": rid, "date": "2099-01-15",
                 "party_size": "1e9"}),
            headers={"Authorization": f"Bearer {token}"})
        assert status == 422, body
    finally:
        server.shutdown()


def test_party_size_bool_rejected() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-03-01T18:00", "party_size": True},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "k1"})
        assert status == 422, body
    finally:
        server.shutdown()


def test_missing_idempotency_key_is_400() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-04-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}"})
        assert status == 400, body
        assert body["error"]["code"] == "missing_idempotency_key"
    finally:
        server.shutdown()


# ---- Wrong-type ID fields return 400 (Finding 1) -------------------------


def test_wrong_type_restaurant_id_is_malformed_request() -> None:
    """Per §5: a non-string ``restaurant_id`` is 400 ``malformed_request``,
    not 422 ``validation_failed``. Missing field (``None``) is 422."""
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        for bad in [17, True, []]:
            status, body = _request(
                conn, "POST", "/reservations",
                {"restaurant_id": bad, "table_id": tid,
                 "starts_at_local": "2099-06-01T18:00", "party_size": 2},
                {"Authorization": f"Bearer {token}",
                 "Idempotency-Key": f"k{bad}"})
            assert status == 400, (bad, body)
            assert body["error"]["code"] == "malformed_request", (bad, body)
        # Missing field → 422 validation_failed (not 400).
        status, body = _request(
            conn, "POST", "/reservations",
            {"table_id": tid,
             "starts_at_local": "2099-06-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "kmissing"})
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed", body
    finally:
        server.shutdown()


def test_wrong_type_table_id_is_malformed_request() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": 17,
             "starts_at_local": "2099-07-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "k1"})
        assert status == 400, body
        assert body["error"]["code"] == "malformed_request", body
    finally:
        server.shutdown()


def test_wrong_type_table_id_in_moves_is_malformed_request() -> None:
    """Same wrong-type rule applies to ``table_id`` inside
    ``POST /reservation-moves``."""
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        # Make a reservation to move.
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-08-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "create1"})
        assert status == 201, body
        ref = body["reference"]
        # Move with a non-string table_id.
        status, body = _request(
            conn, "POST", "/reservation-moves",
            {"moves": [{"reference": ref, "table_id": 17}]},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "move1"})
        assert status == 400, body
        assert body["error"]["code"] == "malformed_request", body
    finally:
        server.shutdown()


def test_wrong_type_reference_in_moves_is_malformed_request() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "POST", "/reservation-moves",
            {"moves": [{"reference": 17, "table_id": tid}]},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "movebad"})
        assert status == 400, body
        assert body["error"]["code"] == "malformed_request", body
    finally:
        server.shutdown()


# ---- Idempotency is path-scoped (Finding 2) ------------------------------


def test_idempotency_key_reused_on_different_path_is_a_fresh_request() -> None:
    """Per §7: replay = same user + same key + same method+path+body.
    The same key on a different path must be treated as a new request."""
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        # Make a reservation with idempotency key "K".
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-09-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "K"})
        assert status == 201, body
        ref = body["reference"]

        # Reuse key "K" on /reservation-moves with a valid (same-body, but
        # different path) move. This must NOT replay the previous receipt;
        # it must run the move normally and succeed.
        status, body = _request(
            conn, "POST", "/reservation-moves",
            {"moves": [{"reference": ref}]},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "K"})
        assert status == 201, body
        assert "reservations" in body
    finally:
        server.shutdown()


def test_idempotency_key_reused_same_path_different_body_is_409() -> None:
    """Per §7: same key, same path, different body → 409 ``idempotency_key_reuse``."""
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        # First call: book a reservation.
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-10-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "SAME"})
        assert status == 201, body

        # Second call: same key, same path, different body → 409.
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-10-01T18:30", "party_size": 4},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "SAME"})
        assert status == 409, body
        assert body["error"]["code"] == "idempotency_key_reuse", body
    finally:
        server.shutdown()


# ---- Reset clears everything ----------------------------------------------


def test_reset_yields_disjoint_state() -> None:
    server, host, port = _start_server()
    try:
        conn = HTTPConnection(host, port)
        token, rid, tid = _bootstrap(port)
        status, body = _request(
            conn, "POST", "/reservations",
            {"restaurant_id": rid, "table_id": tid,
             "starts_at_local": "2099-05-01T18:00", "party_size": 2},
            {"Authorization": f"Bearer {token}",
             "Idempotency-Key": "k1"})
        ref = body["reference"]

        # Reset.
        status, _ = _request(conn, "POST", "/_test/reset", {})
        assert status == 204

        # Old token now invalid; new user sees empty list.
        status, body = _request(
            conn, "GET", "/reservations",
            headers={"Authorization": f"Bearer {token}"})
        assert status == 401
        email = f"u{uuid.uuid4().hex[:8]}@x.io"
        status, body = _request(conn, "POST", "/auth/signup",
                                {"email": email, "password": "secret123",
                                 "display_name": "U"})
        token2 = body["token"]
        status, body = _request(
            conn, "GET", "/reservations",
            headers={"Authorization": f"Bearer {token2}"})
        assert status == 200, body
        assert body == {"reservations": []}
    finally:
        server.shutdown()


if __name__ == "__main__":
    import inspect
    import sys as _sys

    # Discover and call every test_* function in this file.
    tests = [(n, f) for n, f in globals().items()
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    if failures:
        print(f"{failures} failure(s)")
        _sys.exit(1)
    print("all tests passed")
