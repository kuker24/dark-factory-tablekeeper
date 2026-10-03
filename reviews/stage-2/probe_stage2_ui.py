#!/usr/bin/env python3
"""Independent stage-2 UI probes for tablekeeper (Reviewer).

Drives headless Chromium via Playwright against a live stage-2 service. Written from
plans/stage-2/ledger.md (reqs 1-41) and the stage-2 specification, not the supplied checks.

    probe_stage2_ui.py --base-url http://127.0.0.1:8080
    probe_stage2_ui.py --base-url http://127.0.0.1:8080 --previous-base-url http://127.0.0.1:8082

Covers: routes/testids, grid data-available exactness, unavailable click, signed-out booking,
booking/confirmation, idempotent resubmit vs changed field, lookup/cancel, combination cells,
out-of-order search (req 3), 409 form preservation (req 4), lost-response uncertain/retry
(req 5), and the visual/quality bar at 375 px (reqs 7-10).

Prints one line per probe: PASS/FAIL/SKIP name (detail). Exit 1 if any probe fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from zoneinfo import ZoneInfo

import httpx
from playwright.sync_api import sync_playwright

TIMEOUT = 5000
RESET_TIMEOUT = 10.0
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REF = re.compile(r"^[A-Z0-9]{6,12}$")

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob"}

RESULTS: list[tuple[str, str, str]] = []


class Skip(Exception):
    pass


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append(("PASS", name, detail or ""))
    except Skip as exc:
        RESULTS.append(("SKIP", name, str(exc)))
    except AssertionError as exc:
        RESULTS.append(("FAIL", name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------- fixtures
def all_week(opens="18:00", closes="23:00"):
    return [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS]


def restaurant(rid="r_anker", *, name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, reservation_duration_minutes=90,
               cancellation_cutoff_minutes=120, opening_hours=None, tables=None,
               combinable=None):
    r = {"id": rid, "name": name, "timezone": timezone, "slot_minutes": slot_minutes,
         "reservation_duration_minutes": reservation_duration_minutes,
         "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
         "opening_hours": all_week() if opening_hours is None else opening_hours,
         "tables": tables if tables is not None else [
             {"id": "t_1", "label": "1", "capacity": 2},
             {"id": "t_2", "label": "2", "capacity": 4},
             {"id": "t_3", "label": "3", "capacity": 6}]}
    if combinable is not None:
        r["combinable"] = combinable
    return r


def fixture(*, users=None, restaurants=None, reservations=None):
    return {"users": [ADA, BOB] if users is None else users,
            "restaurants": [restaurant()] if restaurants is None else restaurants,
            "reservations": reservations or []}


def booking_date(timezone="Europe/Berlin", lead=7):
    return (dt.datetime.now(ZoneInfo(timezone)).date() + dt.timedelta(days=lead)).isoformat()


def local(date, hhmm="19:00"):
    return f"{date}T{hhmm}"


def reset(base, body=None):
    r = httpx.post(f"{base}/_test/reset", json=body if body is not None else fixture(),
                   timeout=RESET_TIMEOUT)
    assert r.status_code == 204, f"reset -> {r.status_code} {r.text[:200]}"


def api_login(base, email=ADA["email"], password=ADA["password"]):
    r = httpx.post(f"{base}/auth/login", json={"email": email, "password": password}, timeout=5)
    assert r.status_code == 200, f"login -> {r.status_code} {r.text[:200]}"
    return r.json()["token"]


def sel(name):
    return f"[data-testid='{name}']"


def q(page, name):
    return page.query_selector(sel(name))


def text(page, name):
    el = page.text_content(sel(name))
    return (el or "").strip()


# ---------------------------------------------------------------- UI helpers
def sign_up(page, base, email="new@example.com", password="correct horse", name="New"):
    page.goto("/signup")
    page.fill(sel("signup-email"), email)
    page.fill(sel("signup-password"), password)
    page.fill(sel("signup-display-name"), name)
    page.click(sel("signup-submit"))
    page.wait_for_selector(sel("current-user"))


def log_in(page, email=ADA["email"], password=ADA["password"]):
    page.goto("/login")
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), password)
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def search(page, date=None, party_size=4, restaurant_id="r_anker"):
    page.goto("/")
    page.select_option(sel("restaurant-select"), restaurant_id)
    page.fill(sel("date-input"), date or booking_date())
    page.fill(sel("party-size-input"), str(party_size))
    page.click(sel("search-button"))
    page.wait_for_selector(f"{sel('availability-grid')}, {sel('no-slots')}")


# ---------------------------------------------------------------- probes
def p_routes_and_auth(run):
    page, base = run["page"], run["base"]
    reset(base, fixture())
    for route, anchor in (("/", "search-button"), ("/signup", "signup-submit"),
                          ("/login", "login-submit"), ("/lookup", "lookup-submit")):
        page.goto(route)
        page.wait_for_selector(sel(anchor), state="attached")
        assert "text/html" in page.evaluate("document.contentType").lower()
    # auth-error absent without an error
    page.goto("/login")
    assert q(page, "auth-error") is None, "auth-error present with no error"
    # wrong password: wait for auth-error, no sign-in
    page.fill(sel("login-email"), ADA["email"])
    page.fill(sel("login-password"), "wrong password")
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("auth-error"))
    assert text(page, "auth-error"), "auth-error empty"
    # good login: current-user text contains display name
    log_in(page)
    assert ADA["display_name"] in text(page, "current-user")
    # current-user on every required route when signed in (client-side session must persist)
    for route in ("/", "/lookup"):
        page.goto(route)
        page.wait_for_selector(sel("current-user"))
    # logout
    page.click(sel("logout-button"))
    page.wait_for_selector(sel("current-user"), state="detached")
    return "routes + auth testids + current-user/logout"


def p_grid_cells_and_data_available(run):
    page, base = run["page"], run["base"]
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    date = booking_date()
    search(page, date=date, party_size=4)
    avail = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 4}, timeout=5).json()
    slots = [s for s in avail["slots"] if s["available_table_ids"]]
    assert slots, "no available slots to check"
    # every table cell present for every slot, data-available exact
    for s in avail["slots"]:
        hh = s["starts_at_local"][-5:]
        for t in ("t_1", "t_2", "t_3"):
            cell = q(page, f"slot-{t}-{hh}")
            assert cell is not None, f"missing cell slot-{t}-{hh}"
            want = "true" if t in s["available_table_ids"] else "false"
            got = cell.get_attribute("data-available")
            assert got == want, f"slot-{t}-{hh} data-available={got} want {want}"
    return "per-table cells + data-available exactness"


def p_combination_cells(run):
    page, base = run["page"], run["base"]
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"], ["t_2", "t_3"]])]))
    date = booking_date()
    search(page, date=date, party_size=6)
    avail = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 6}, timeout=5).json()
    # find a slot where t_1+t_2 is an option
    target = None
    for s in avail["slots"]:
        opts = [o["table_ids"] for o in s.get("available_options", [])]
        if ["t_1", "t_2"] in opts:
            target = s
            break
    assert target, "no slot offered t_1+t_2 for party 6"
    hh = target["starts_at_local"][-5:]
    cell = q(page, f"slot-t_1+t_2-{hh}")
    assert cell is not None, f"missing combination cell slot-t_1+t_2-{hh}"
    assert cell.get_attribute("data-available") == "true", cell.get_attribute("data-available")
    # ids in combinable order: t_2+t_1 must not be a testid when only [t_1,t_2] declared
    assert q(page, f"slot-t_2+t_1-{hh}") is None, "pair cell ids not in combinable order"
    return "combination cell testid + data-available + order"


def p_unavailable_click_and_signed_out(run):
    page, base = run["page"], run["base"]
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    page.goto("/")
    search(page, party_size=6)
    # signed out: click available pair cell -> auth-error or login
    page.click(sel("slot-t_1+t_2-19:00"))
    page.wait_for_selector(f"{sel('auth-error')}, {sel('login-submit')}")
    # signed in: unavailable cell does nothing
    log_in(page, email=BOB["email"])
    search(page, party_size=6)
    # force-click an unavailable single (t_1 cap 2)
    cell = q(page, "slot-t_1-19:00")
    assert cell is not None
    page.click(sel("slot-t_1-19:00"), force=True)
    page.wait_for_timeout(300)
    assert q(page, "booking-form") is None, "unavailable cell opened the form"
    return "signed-out gating + unavailable click inert"


def p_booking_confirmation_and_replay(run):
    page, base = run["page"], run["base"]
    reset(base, fixture())
    log_in(page)
    search(page, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    summary = text(page, "booking-summary")
    assert "19:00" in summary and "2" in summary, f"summary={summary!r}"
    assert page.input_value(sel("booking-party-size")) == "4", "party size not pre-filled"
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    ref = text(page, "confirmation-reference")
    assert REF.fullmatch(ref), f"reference not exact: {ref!r}"
    details = text(page, "confirmation-details")
    for want in ("Zum Anker", "19:00", "2"):
        assert want in details, f"{want!r} missing from details {details!r}"
    # resubmit unchanged -> same reference, no error, one booking
    page.click(sel("booking-submit"))
    page.wait_for_timeout(500)
    assert q(page, "booking-error") is None, "replay produced booking-error"
    assert text(page, "confirmation-reference") == ref, "replay reference changed"
    rows = httpx.get(f"{base}/reservations", headers={"Authorization": f"Bearer {api_login(base)}"},
                     timeout=5).json()["reservations"]
    assert len([r for r in rows if r["status"] == "confirmed"]) == 1, f"booked {len(rows)} times"
    return "confirmation exactness + idempotent resubmit"


def p_changed_field_is_new_booking(run):
    page, base = run["page"], run["base"]
    reset(base, fixture())
    log_in(page)
    search(page, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    page.fill(sel("booking-party-size"), "2")
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-error"))
    return "changed field is a new (refused) booking"


def p_lookup_and_cancel(run):
    page, base = run["page"], run["base"]
    reset(base, fixture())
    log_in(page)
    search(page, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    ref = text(page, "confirmation-reference")
    page.goto("/lookup")
    page.fill(sel("lookup-reference-input"), ref)
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-detail"))
    assert text(page, "reservation-status") == "confirmed", text(page, "reservation-status")
    page.click(sel("reservation-cancel-button"))
    page.wait_for_selector(sel("reservation-cancel-button"), state="detached")
    assert text(page, "reservation-status") == "cancelled", text(page, "reservation-status")
    # not found
    page.fill(sel("lookup-reference-input"), "ZZZZZZ")
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-error"))
    assert q(page, "reservation-detail") is None, "detail shown for unknown reference"
    return "lookup found/cancel/not-found"


def p_confirmation_tables_labels(run):
    page, base = run["page"], run["base"]
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    log_in(page)
    search(page, party_size=6)
    page.click(sel("slot-t_1+t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    summary = text(page, "booking-summary")
    # every table label named in the summary
    for label in ("1", "2"):
        assert label in summary, f"label {label!r} missing from booking-summary {summary!r}"
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    conf = text(page, "confirmation-tables")
    for label in ("1", "2"):
        assert label in conf, f"label {label!r} missing from confirmation-tables {conf!r}"
    ref = text(page, "confirmation-reference")
    page.goto("/lookup")
    page.fill(sel("lookup-reference-input"), ref)
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-detail"))
    rt = text(page, "reservation-tables")
    for label in ("1", "2"):
        assert label in rt, f"label {label!r} missing from reservation-tables {rt!r}"
    return "combined summary/confirmation/lookup labels"


def p_out_of_order_search(run):
    """Req 3: A starts first, finishes last; B must win and A never restore."""
    page, base = run["page"], run["base"]
    reset(base, fixture(restaurants=[
        restaurant("r_anker", name="Anker", tables=[
            {"id": "t_2", "label": "2", "capacity": 4}], combinable=[]),
        restaurant("r_other", name="Other", tables=[
            {"id": "t_x", "label": "X", "capacity": 4}], combinable=[])]))
    date = booking_date()
    # intercept: delay the Anker availability request by ~1.2s, let Other through fast
    calls = {"n": 0}

    def route_handler(route):
        url = route.request.url
        if "/availability" in url and "r_anker" in url:
            calls["n"] += 1
            page.wait_for_timeout(1200)
        route.continue_()

    page.route("**/availability**", route_handler)
    page.goto("/")
    page.select_option(sel("restaurant-select"), "r_anker")
    page.fill(sel("date-input"), date)
    page.fill(sel("party-size-input"), "4")
    page.click(sel("search-button"))
    # immediately switch to other restaurant and search (B)
    page.wait_for_timeout(100)
    page.select_option(sel("restaurant-select"), "r_other")
    page.click(sel("search-button"))
    page.wait_for_timeout(2000)
    # B should be rendered: X cell present, and no t_2 cell from A
    assert q(page, "slot-t_x-19:00") is not None, "B results not rendered"
    assert q(page, "slot-t_2-19:00") is None, "late A response restored A's grid"
    page.unroute("**/availability**")
    return "late response does not restore A"


def p_409_preserves_form_and_refreshes(run):
    """Req 4: external takeover -> booking-error, no confirmation, inputs preserved, refresh."""
    page, base = run["page"], run["base"]
    reset(base, fixture())
    log_in(page)
    search(page, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    # take the table over the API as Bob
    tok = api_login(base, BOB["email"])
    r = httpx.post(f"{base}/reservations", json={
        "restaurant_id": "r_anker", "table_id": "t_2",
        "starts_at_local": local(booking_date(), "19:00"), "party_size": 4},
        headers={"Authorization": f"Bearer {tok}", "Idempotency-Key": "thief-key-1"}, timeout=5)
    assert r.status_code == 201, r.text
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-error"))
    assert text(page, "booking-error"), "booking-error empty"
    assert q(page, "confirmation") is None, "confirmation shown for refused attempt"
    assert q(page, "booking-form") is not None, "form disappeared on 409"
    # inputs preserved
    assert page.input_value(sel("booking-party-size")) == "4", "party size not preserved"
    # availability refreshed: t_2 19:00 now unavailable somewhere on the page
    page.wait_for_timeout(500)
    cells = page.query_selector_all("[data-testid^='slot-t_2-19:00']")
    if cells:
        assert cells[0].get_attribute("data-available") == "false", "availability not refreshed"
    return "409 shows error, keeps form, refreshes availability"


def p_lost_response_uncertain_then_retry(run):
    """Req 5: response dropped after commit -> uncertain; retry same key/body recovers ref."""
    page, base = run["page"], run["base"]
    reset(base, fixture())
    log_in(page)
    search(page, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))

    # Drop the initial POST *and* the client's automatic same-key retry, so the
    # uncertain banner is observable before any successful recovery. The first
    # request is fetched-then-aborted so the server still commits the booking.
    state = {"n": 0, "keys": []}

    def route_handler(route):
        req = route.request
        if req.method == "POST" and req.url.rstrip("/").endswith("/reservations"):
            state["n"] += 1
            state["keys"].append(req.headers.get("idempotency-key"))
            if state["n"] <= 2:
                # Let the server commit the booking, then drop the response.
                try:
                    httpx.post(
                        base.rstrip("/") + "/reservations",
                        json=route.request.post_data_json,
                        headers={
                            "Authorization": route.request.headers.get("authorization", ""),
                            "Idempotency-Key": route.request.headers.get("idempotency-key", ""),
                        },
                        timeout=5,
                    )
                except Exception:
                    pass
                route.abort()
            else:
                route.continue_()
        else:
            route.continue_()

    page.route("**/reservations", route_handler)
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-uncertain"))
    assert text(page, "booking-uncertain"), "booking-uncertain empty"
    assert q(page, "booking-error") is None, "booking-error shown for uncertain outcome"
    assert q(page, "confirmation") is None, "confirmation shown despite lost response"
    assert q(page, "booking-form") is not None, "form disappeared after lost response"
    # The unchanged form retries with the same idempotency key and body.
    page.unroute("**/reservations")
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    ref = text(page, "confirmation-reference")
    assert REF.fullmatch(ref), f"reference not exact: {ref!r}"
    assert q(page, "booking-uncertain") is None, "uncertainty not cleared after success"
    assert q(page, "booking-error") is None, "error present after successful retry"
    page.unroute("**/reservations")
    return "lost response -> uncertain -> same-key retry recovers reference"


def p_upgrade_ui_signed_in(run):
    """Req 22-25 UI: a signed-in browser stays signed in across import; ref survives."""
    page, base = run["page"], run["base"]
    prev = run.get("previous_base")
    if not prev:
        raise Skip("no --previous-base-url (build stage-1/ and pass its URL)")
    # sign in on stage-2
    reset(base, fixture())
    log_in(page)
    # produce a stage-1 export and import it into stage-2 between browser requests
    reset(prev, fixture())
    snap = httpx.get(f"{prev}/_test/export", timeout=RESET_TIMEOUT).json()
    r = httpx.post(f"{base}/_test/import", json=snap, timeout=RESET_TIMEOUT)
    assert r.status_code == 204, r.text
    # navigate within the app: still signed in
    page.goto("/")
    page.wait_for_selector(sel("current-user"))
    assert ADA["display_name"] in text(page, "current-user"), "session lost after import"
    return "signed-in browser survives import"


def p_visual_quality_375(run):
    """Req 7-10: 375px no horizontal scroll, labels, focus, contrast, hierarchy."""
    page, base = run["page"], run["base"]
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]])]))
    page.set_viewport_size({"width": 375, "height": 800})
    log_in(page)
    for route in ("/", "/lookup"):
        page.goto(route)
        page.wait_for_timeout(150)
        sw = page.evaluate("document.documentElement.scrollWidth")
        cw = page.evaluate("document.documentElement.clientWidth")
        assert sw <= cw + 2, f"{route} horizontal scroll: scrollWidth={sw} clientWidth={cw}"
    # inputs have labels (visible text or aria-label/label-for)
    search(page, party_size=4)
    for name in ("date-input", "party-size-input"):
        el = q(page, name)
        lab = page.evaluate(
            """(id) => {
              const e = document.querySelector(`[data-testid='${id}']`);
              if (!e) return false;
              if (e.getAttribute('aria-label') || e.getAttribute('aria-labelledby')) return true;
              if (e.id && document.querySelector(`label[for='${e.id}']`)) return true;
              let p = e.closest('label'); if (p) return true;
              return false;
            }""", name)
        assert lab, f"{name} has no associated label"
    # keyboard focus is visible: focus the search button and compare outline/box-shadow
    page.focus(sel("search-button"))
    foc = page.evaluate(
        """() => { const e = document.activeElement; const s = getComputedStyle(e);
                   return {outline: s.outlineStyle + ' ' + s.outlineWidth,
                           shadow: s.boxShadow, border: s.borderColor}; }""")
    assert not (foc["outline"].endswith("0px") and foc["shadow"] in ("none", "")), \
        f"no visible focus indication: {foc}"
    # no raw JSON dump in the visible grid text
    body = page.inner_text("body")
    assert "{" not in body and "available_table_ids" not in body, "raw API data in the UI"
    # desktop width also no horizontal scroll
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto("/")
    page.wait_for_timeout(150)
    assert page.evaluate("document.documentElement.scrollWidth") <= \
        page.evaluate("document.documentElement.clientWidth") + 2, "desktop horizontal scroll"
    return "375+desktop no h-scroll; labels; focus; no raw dumps"


PROBES = [
    ("U1 routes + auth testids", p_routes_and_auth),
    ("U2 grid cells + data-available", p_grid_cells_and_data_available),
    ("U3 combination cells", p_combination_cells),
    ("U4 unavailable/signed-out click", p_unavailable_click_and_signed_out),
    ("U5 booking/confirmation/replay", p_booking_confirmation_and_replay),
    ("U6 changed field new booking", p_changed_field_is_new_booking),
    ("U7 lookup/cancel/not-found", p_lookup_and_cancel),
    ("U8 combined labels", p_confirmation_tables_labels),
    ("U9 out-of-order search", p_out_of_order_search),
    ("U10 409 preserves form", p_409_preserves_form_and_refreshes),
    ("U11 lost response -> retry", p_lost_response_uncertain_then_retry),
    ("U12 signed-in survives import", p_upgrade_ui_signed_in),
    ("U13 visual quality 375px", p_visual_quality_375),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--previous-base-url", default=None)
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")
    prev = args.previous_base_url.rstrip("/") if args.previous_base_url else None

    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            channel="chromium",
            args=[f"--unsafely-treat-insecure-origin-as-secure={base}"])
        for name, fn in PROBES:
            context = browser.new_context(base_url=base)
            context.set_default_timeout(TIMEOUT)
            page = context.new_page()
            run = {"page": page, "base": base, "previous_base": prev}
            check(name, lambda fn=fn, run=run: fn(run))
            context.close()
        browser.close()

    width = max(len(n) for _, n, _ in RESULTS)
    failed = 0
    for status, name, detail in RESULTS:
        print(f"{status} {name.ljust(width)} :: {detail}")
        if status == "FAIL":
            failed += 1
    print(f"\n{sum(1 for s, _, _ in RESULTS if s == 'PASS')}/{len(RESULTS)} passed ({failed} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
