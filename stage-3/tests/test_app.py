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


# ---- stage-2 specific coverage -------------------------------------------


def _combinable_fixture() -> dict:
    weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return {
        "users": [],
        "restaurants": [{
            "id": "rest_combo",
            "name": "Combo Bistro",
            "timezone": "UTC",
            "opening_hours": [{"weekday": wd, "opens": "00:00", "closes": "23:59"}
                              for wd in weekday_codes],
            "tables": [
                {"id": "tbl_a", "label": "A", "capacity": 2},
                {"id": "tbl_b", "label": "B", "capacity": 2},
                {"id": "tbl_c", "label": "C", "capacity": 6},
            ],
            "combinable": [["tbl_a", "tbl_b"]],
            "slot_minutes": 30,
            "reservation_duration_minutes": 60,
            "cancellation_cutoff_minutes": 60,
        }],
        "reservations": [],
    }


def _bootstrap_combo(port: int) -> tuple[str, str]:
    operations.load_fixture(appmod.STATE, _combinable_fixture())
    conn = HTTPConnection("127.0.0.1", port)
    email = f"u{uuid.uuid4().hex[:8]}@x.io"
    status, body = _request(conn, "POST", "/auth/signup",
                            {"email": email, "password": "secret123",
                             "display_name": "U"})
    assert status == 201, body
    token = body["token"]
    conn.close()
    return token, "rest_combo"


def test_combination_not_allowed_when_pair_not_in_combinable() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_c"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 3,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 422, body
        assert body["error"]["code"] == "combination_not_allowed"
    finally:
        server.shutdown()


def test_both_table_id_and_table_ids_is_422() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_id": "tbl_a",
            "table_ids": ["tbl_a", "tbl_b"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 2,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed"
    finally:
        server.shutdown()


def test_missing_both_table_id_and_table_ids_is_422() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 2,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 422, body
    finally:
        server.shutdown()


def test_availability_lists_pair_option_in_combinable_order() -> None:
    server, _host, port = _start_server()
    try:
        _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        url = "/availability?restaurant_id=rest_combo&date=2030-06-04&party_size=4"
        status, body = _request(conn, "GET", url)
        assert status == 200, body
        slot = body["slots"][0]
        opts = slot["available_options"]
        ids = [o["table_ids"] for o in opts]
        # tbl_a + tbl_b is the only combinable pair; tbl_c alone has cap 6.
        assert ["tbl_a", "tbl_b"] in ids or ["tbl_b", "tbl_a"] in ids
        # Combinable order (a, b) is preserved.
        assert ids[ids.index(["tbl_a", "tbl_b"])] == ["tbl_a", "tbl_b"]
    finally:
        server.shutdown()


def test_pair_booking_blocks_overlapping_pair() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, _body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_b"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 4,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 201, _body
        # Second user; party of 2; tries tbl_a alone — must be 409.
        email2 = f"u{uuid.uuid4().hex[:8]}@x.io"
        status, body = _request(conn, "POST", "/auth/signup",
                                {"email": email2, "password": "secret123",
                                 "display_name": "Y"})
        token2 = body["token"]
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_id": "tbl_a",
            "starts_at_local": "2030-06-04T12:30",
            "party_size": 2,
            "name": "Y", "email": email2,
        }, headers={"Authorization": f"Bearer {token2}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 409, body
    finally:
        server.shutdown()


def test_pair_capacity_exceeds_is_422() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_b"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 5,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 422, body
        assert body["error"]["code"] == "party_exceeds_capacity"
    finally:
        server.shutdown()


def test_pair_cancellation_frees_both_tables() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_b"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 4,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 201, body
        ref = body["reference"]
        assert body["table_ids"] == ["tbl_a", "tbl_b"]
        status, _ = _request(conn, "POST", f"/reservations/{ref}/cancel",
                             headers={"Authorization": f"Bearer {token}"})
        assert status == 200
        # Now tbl_a alone should be available at the same slot.
        url = "/availability?restaurant_id=rest_combo&date=2030-06-04&party_size=2"
        status, body = _request(conn, "GET", url)
        assert status == 200, body
        first_slot = body["slots"][0]
        assert "tbl_a" in first_slot["available_table_ids"]
    finally:
        server.shutdown()


def test_reservation_payload_table_id_only_for_singletons() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        # Singleton
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_id": "tbl_c",
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 4,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 201, body
        assert body["table_id"] == "tbl_c"
        assert body["table_ids"] == ["tbl_c"]
        # Pair
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_b"],
            "starts_at_local": "2030-06-04T14:00",
            "party_size": 4,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 201, body
        assert "table_id" not in body
        assert body["table_ids"] == ["tbl_a", "tbl_b"]
    finally:
        server.shutdown()


def test_stage1_export_import_round_trip_to_stage2() -> None:
    """E6: stage-1 export must import into stage-2 with the singleton
    ``table_id`` mapped to ``table_ids``."""
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _default_fixture())
        conn = HTTPConnection("127.0.0.1", port)
        email = f"u{uuid.uuid4().hex[:8]}@x.io"
        status, body = _request(conn, "POST", "/auth/signup",
                                {"email": email, "password": "secret123",
                                 "display_name": "U"})
        token = body["token"]
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": "rest_001",
            "table_id": "tbl_a",
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 2,
            "name": "X", "email": email,
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 201, body
        # Export snapshot then strip table_ids → table_id (stage-1 style).
        status, exported = _request(conn, "GET", "/_test/export")
        assert status == 200
        snap = exported["state"]
        for ref, res in snap["reservations"].items():
            res["table_id"] = res.pop("table_ids")[0]
            # Restaurants drop combinable (stage-1 didn't have it).
            for r in snap["restaurants"].values():
                r.pop("combinable", None)
        # Reset state and import.
        status, _ = _request(conn, "POST", "/_test/reset", {})
        assert status == 204
        status, _ = _request(conn, "POST", "/_test/import", exported)
        assert status == 204
        # Reservations list should now include the imported booking as
        # a singleton list (table_ids mapped forward, table_id present).
        status, body = _request(conn, "GET", "/reservations",
                                headers={"Authorization": f"Bearer {token}"})
        assert status == 200, body
        items = body["reservations"]
        assert len(items) == 1
        assert items[0]["table_ids"] == ["tbl_a"]
        assert items[0].get("table_id") == "tbl_a"
    finally:
        server.shutdown()


def test_html_pages_served() -> None:
    server, _host, port = _start_server()
    try:
        conn = HTTPConnection("127.0.0.1", port)
        for path in ("/", "/signup", "/login", "/lookup"):
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = resp.read()
            assert resp.status == 200, (path, raw[:200])
            assert b"text/html" in (resp.getheader("Content-Type") or "").encode()
        # Static asset
        conn.request("GET", "/static/app.css")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read()
        conn.request("GET", "/static/app.js")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read()
    finally:
        server.shutdown()


def test_combinable_validation_rejects_unknown_table() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, {})
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/_test/reset", {
            "users": [],
            "restaurants": [{
                "id": "rest_bad",
                "name": "Bad",
                "timezone": "UTC",
                "opening_hours": [{"weekday": "mon", "opens": "00:00",
                                   "closes": "23:59"}],
                "tables": [{"id": "tbl_a", "label": "A", "capacity": 2}],
                "combinable": [["tbl_a", "tbl_x"]],
                "slot_minutes": 30,
                "reservation_duration_minutes": 60,
                "cancellation_cutoff_minutes": 60,
            }],
            "reservations": [],
        })
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed"
    finally:
        server.shutdown()


def test_too_many_table_ids_is_combination_not_allowed() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_b", "tbl_c"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 2,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 422, body
        assert body["error"]["code"] == "combination_not_allowed"
    finally:
        server.shutdown()


def test_pair_with_unknown_member_is_not_found() -> None:
    server, _host, port = _start_server()
    try:
        token, rid = _bootstrap_combo(port)
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "POST", "/reservations", {
            "restaurant_id": rid,
            "table_ids": ["tbl_a", "tbl_nope"],
            "starts_at_local": "2030-06-04T12:00",
            "party_size": 2,
            "name": "X", "email": "x@x.io",
        }, headers={"Authorization": f"Bearer {token}",
                   "Idempotency-Key": uuid.uuid4().hex})
        assert status == 404, body
        assert body["error"]["code"] == "not_found"
    finally:
        server.shutdown()


# ---- Stage-3 ledger coverage ----------------------------------------------
# The harness ships a small stage-3 sample; the assertions below lock in the
# finer-grained behaviour called out by ledger entries 17-43 (and decisions
# F1-F14).  Boots a fresh HTTP server for each test.


def _stage3_fixture() -> dict:
    weekday_codes = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return {
        "users": [
            {"id": "u_ada", "email": "ada@example.com",
             "password": "correct horse", "display_name": "Ada"},
            {"id": "u_bob", "email": "bob@example.com",
             "password": "correct horse", "display_name": "Bob"},
            {"id": "u_mgr", "email": "mgr@example.com",
             "password": "correct horse", "display_name": "Mgr"},
        ],
        "restaurants": [{
            "id": "r_a",
            "name": "A",
            "timezone": "Europe/Berlin",
            "opening_hours": [{"weekday": wd, "opens": "18:00",
                               "closes": "23:00"} for wd in weekday_codes],
            "tables": [
                {"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 6},
            ],
            "slot_minutes": 30,
            "reservation_duration_minutes": 90,
            "cancellation_cutoff_minutes": 120,
            "manager_user_ids": ["u_mgr"],
        }],
        "reservations": [],
    }


def _stage3_login(port: int, user_id: str) -> str:
    email = {"u_ada": "ada@example.com", "u_bob": "bob@example.com",
             "u_mgr": "mgr@example.com"}[user_id]
    conn = HTTPConnection("127.0.0.1", port)
    status, body = _request(conn, "POST", "/auth/login",
                            {"email": email, "password": "correct horse"})
    assert status == 200, body
    conn.close()
    return body["token"]


def _stage3_book(port: int, token: str, *, party_size: int = 2,
                 table: str = "t_2",
                 starts: str = "2030-06-04T19:00") -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port)
    status, body = _request(conn, "POST", "/reservations", {
        "restaurant_id": "r_a",
        "table_id": table,
        "starts_at_local": starts,
        "party_size": party_size,
    }, headers={"Authorization": f"Bearer {token}",
                "Idempotency-Key": uuid.uuid4().hex})
    conn.close()
    return status, body


def test_acceptance_terms_recorded_on_create() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        status, body = _stage3_book(port, ada, party_size=2)
        assert status == 201, body
        assert body["revision"] == 1, body
        terms = body["accepted_terms"]
        assert terms["policy_version"] == 0, terms
        assert terms["reservation_duration_minutes"] == 90, terms
        assert terms["slot_minutes"] == 30, terms
    finally:
        server.shutdown()


def test_history_records_create_then_change_then_cancel() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, created = _stage3_book(port, ada)
        ref = created["reference"]
        conn = HTTPConnection("127.0.0.1", port)
        # Patch (within cutoff, no other change): party_size 2 → 3 still fits.
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"party_size": 3},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        assert body["revision"] == 2, body
        # Cancel.
        status, body = _request(conn, "POST",
                                f"/reservations/{ref}/cancel", {},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        assert body["revision"] == 3, body
        # History now has 3 entries.
        status, body = _request(conn, "GET",
                                f"/reservations/{ref}/history", {},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        events = [(e["seq"], e["event"]) for e in body["entries"]]
        assert events == [(1, "created"), (2, "changed"), (3, "cancelled")], \
            body
        # The "created" entry lists every reservation field with from=None.
        # For a singleton booking the field is named "table_id", not "table_ids".
        first = body["entries"][0]
        first_changes = {c["field"]: c for c in first["changes"]}
        for field in ("starts_at_local", "party_size", "table_id"):
            assert field in first_changes, first
            assert first_changes[field]["from"] is None, first
        # The "cancelled" entry carries no change diff (it is a status flip).
        assert body["entries"][2]["changes"] == [], body
    finally:
        server.shutdown()


def test_history_unknown_owner_returns_not_found() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, body = _stage3_book(port, ada)
        ref = body["reference"]
        bob = _stage3_login(port, "u_bob")
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "GET",
                                f"/reservations/{ref}/history", {},
                                headers={"Authorization": f"Bearer {bob}"})
        assert status == 404, body
        # Anon (no Authorization header at all) is also treated as 404.
        status, body = _request(conn, "GET",
                                f"/reservations/{ref}/history", {})
        assert status == 404, body
    finally:
        server.shutdown()


def test_no_op_patch_records_nothing_and_keeps_revision() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, created = _stage3_book(port, ada, party_size=2)
        ref = created["reference"]
        conn = HTTPConnection("127.0.0.1", port)
        # Same payload as the original booking → no-op.
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"party_size": 2, "table_id": "t_2",
                                 "starts_at_local": "2030-06-04T19:00"},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        assert body["revision"] == 1, body
        # History is still just the creation entry.
        status, body = _request(conn, "GET",
                                f"/reservations/{ref}/history", {},
                                headers={"Authorization": f"Bearer {ada}"})
        events = [(e["seq"], e["event"]) for e in body["entries"]]
        assert events == [(1, "created")], body
    finally:
        server.shutdown()


def test_expected_revision_match_then_stale_then_invalid() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, created = _stage3_book(port, ada, party_size=2)
        ref = created["reference"]; rev = created["revision"]
        conn = HTTPConnection("127.0.0.1", port)
        # Match → 200, revision bumps.
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"expected_revision": rev, "party_size": 3},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        assert body["revision"] == rev + 1, body
        # Stale → 409.
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"expected_revision": rev, "party_size": 4},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 409, body
        assert body["error"]["code"] == "stale_revision", body
        # Invalid type → 422.
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"expected_revision": "abc"},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed", body
        # Negative → 422.
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"expected_revision": -1},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed", body
    finally:
        server.shutdown()


def test_decision_endpoint_returns_revision_and_accepted_terms() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, created = _stage3_book(port, ada, party_size=2)
        ref = created["reference"]
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "GET",
                                f"/reservations/{ref}/decision", {},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        assert body["reference"] == ref, body
        assert body["revision"] == 1, body
        assert body["accepted_terms"]["policy_version"] == 0, body
    finally:
        server.shutdown()


def test_explain_lists_every_table_exactly_once() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "GET",
                                "/availability?restaurant_id=r_a&date=2030-06-04"
                                "&party_size=2&explain=true", {})
        assert status == 200, body
        seen: set[str] = set()
        rules_order: list[str] = []
        for slot in body["slots"]:
            for entry in slot["explain"]:
                seen.add(entry["table_id"])
                if not rules_order:
                    rules_order = [r["rule"] for r in entry["rules"]]
        assert seen == {"t_1", "t_2", "t_3"}, seen
        assert rules_order == ["capacity", "no_overlap"], rules_order
        # No explain → stage-1 shape, no "explain" key in slots.
        status, body = _request(conn, "GET",
                                "/availability?restaurant_id=r_a&date=2030-06-04"
                                "&party_size=2", {})
        assert status == 200, body
        assert "explain" not in body["slots"][0], body["slots"][0]
        # Invalid explain value → 422.
        status, body = _request(conn, "GET",
                                "/availability?restaurant_id=r_a&date=2030-06-04"
                                "&party_size=2&explain=yes", {})
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed", body
    finally:
        server.shutdown()


def test_publish_policy_requires_manager() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        mgr = _stage3_login(port, "u_mgr")
        policy = {"effective_from": "2030-06-01",
                  "slot_minutes": 15, "reservation_duration_minutes": 60,
                  "cancellation_cutoff_minutes": 60,
                  "opening_hours": [{"weekday": wd, "opens": "18:00",
                                     "closes": "23:00"}
                                    for wd in ["mon", "tue", "wed", "thu",
                                               "fri", "sat", "sun"]],
                  "capacities": {"t_1": 2, "t_2": 4, "t_3": 6}}
        conn = HTTPConnection("127.0.0.1", port)
        # Ada → 403.
        status, body = _request(conn, "POST", "/restaurants/r_a/policies",
                                policy,
                                headers={"Authorization": f"Bearer {ada}",
                                         "Idempotency-Key":
                                             uuid.uuid4().hex})
        assert status == 403, body
        # Manager → 201 with policy_version=1.
        key1 = uuid.uuid4().hex
        status, body = _request(conn, "POST", "/restaurants/r_a/policies",
                                policy,
                                headers={"Authorization": f"Bearer {mgr}",
                                         "Idempotency-Key": key1})
        assert status == 201, body
        assert body["policy_version"] == 1, body
        # Replay same key + same body → 200.
        status, body = _request(conn, "POST", "/restaurants/r_a/policies",
                                policy,
                                headers={"Authorization": f"Bearer {mgr}",
                                         "Idempotency-Key": key1})
        assert status == 200, body
        # Replay same key + different body → 409 idempotency_key_reuse.
        status, body = _request(conn, "POST", "/restaurants/r_a/policies",
                                {**policy, "slot_minutes": 45},
                                headers={"Authorization": f"Bearer {mgr}",
                                         "Idempotency-Key": key1})
        assert status == 409, body
        assert body["error"]["code"] == "idempotency_key_reuse", body
        # Unknown capacity key → 422.
        status, body = _request(conn, "POST", "/restaurants/r_a/policies",
                                {**policy, "capacities": {"t_9": 2}},
                                headers={"Authorization": f"Bearer {mgr}",
                                         "Idempotency-Key":
                                             uuid.uuid4().hex})
        assert status == 422, body
        # List omits policy 0.
        status, body = _request(conn, "GET",
                                "/restaurants/r_a/policies", {})
        assert status == 200, body
        versions = [p["policy_version"] for p in body["policies"]]
        assert versions == [1], versions
    finally:
        server.shutdown()


def test_series_creates_one_week_apart_and_is_idempotent() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, created = _stage3_book(port, ada, party_size=2)
        anchor_ref = created["reference"]
        conn = HTTPConnection("127.0.0.1", port)
        # Validate count=1 and interval_weeks=0 → 422.
        status, body = _request(conn, "POST", "/series",
                                {"anchor_reference": anchor_ref,
                                 "count": 1, "interval_weeks": 1},
                                headers={"Authorization": f"Bearer {ada}",
                                         "Idempotency-Key":
                                             uuid.uuid4().hex})
        assert status == 422, body
        assert body["error"]["code"] == "validation_failed", body
        status, body = _request(conn, "POST", "/series",
                                {"anchor_reference": anchor_ref,
                                 "count": 3, "interval_weeks": 0},
                                headers={"Authorization": f"Bearer {ada}",
                                         "Idempotency-Key":
                                             uuid.uuid4().hex})
        assert status == 422, body
        # Non-owner → 404.
        bob = _stage3_login(port, "u_bob")
        status, body = _request(conn, "POST", "/series",
                                {"anchor_reference": anchor_ref,
                                 "count": 3, "interval_weeks": 1},
                                headers={"Authorization": f"Bearer {bob}",
                                         "Idempotency-Key":
                                             uuid.uuid4().hex})
        assert status == 404, body
        # Owner → 201.
        key = uuid.uuid4().hex
        status, body = _request(conn, "POST", "/series",
                                {"anchor_reference": anchor_ref,
                                 "count": 3, "interval_weeks": 1},
                                headers={"Authorization": f"Bearer {ada}",
                                         "Idempotency-Key": key})
        assert status == 201, body
        series_id = body["series_id"]
        dates = [o["reservation"]["starts_at_local"]
                 for o in body["occurrences"]]
        assert dates == ["2030-06-04T19:00",
                         "2030-06-11T19:00",
                         "2030-06-18T19:00"], dates
        assert all(o["exception"] is False for o in body["occurrences"]), \
            body
        # Replay same key + same body → 200 (no new occurrences).
        status, body = _request(conn, "POST", "/series",
                                {"anchor_reference": anchor_ref,
                                 "count": 3, "interval_weeks": 1},
                                headers={"Authorization": f"Bearer {ada}",
                                         "Idempotency-Key": key})
        assert status == 200, body
        # Replay same key + different body → 409 idempotency_key_reuse.
        status, body = _request(conn, "POST", "/series",
                                {"anchor_reference": anchor_ref,
                                 "count": 5, "interval_weeks": 1},
                                headers={"Authorization": f"Bearer {ada}",
                                         "Idempotency-Key": key})
        assert status == 409, body
        assert body["error"]["code"] == "idempotency_key_reuse", body
        # GET owner → 200; GET bob → 404; GET anon → 404.
        status, body = _request(conn, "GET", f"/series/{series_id}", {},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        assert len(body["occurrences"]) == 3, body
        status, body = _request(conn, "GET", f"/series/{series_id}", {},
                                headers={"Authorization": f"Bearer {bob}"})
        assert status == 404, body
        status, body = _request(conn, "GET", f"/series/{series_id}", {})
        assert status == 404, body
    finally:
        server.shutdown()


def test_history_records_table_id_field_for_singleton() -> None:
    server, _host, port = _start_server()
    try:
        operations.load_fixture(appmod.STATE, _stage3_fixture())
        ada = _stage3_login(port, "u_ada")
        _status, created = _stage3_book(port, ada, party_size=2,
                                        table="t_1")
        ref = created["reference"]
        conn = HTTPConnection("127.0.0.1", port)
        status, body = _request(conn, "PATCH", f"/reservations/{ref}",
                                {"party_size": 1},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        status, body = _request(conn, "GET",
                                f"/reservations/{ref}/history", {},
                                headers={"Authorization": f"Bearer {ada}"})
        assert status == 200, body
        # The change entry must name party_size only — no table_id mutation.
        change = body["entries"][1]
        assert change["event"] == "changed", body
        fields = [c["field"] for c in change["changes"]]
        assert fields == ["party_size"], fields
    finally:
        server.shutdown()
