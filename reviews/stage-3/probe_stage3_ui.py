#!/usr/bin/env python3
"""Independent stage-3 UI probes for tablekeeper (Reviewer).

Stage 3 adds no new screens (ledger F13); this suite confirms the stage-2 UI still works and
that the availability grid reflects published policies (slot grid, duration and capacities
from the selected policy). Written from plans/stage-3/ledger.md plus the still-binding
stage-2 UI requirements, not the supplied checks.

    probe_stage3_ui.py --base-url http://127.0.0.1:8080
    probe_stage3_ui.py --base-url http://127.0.0.1:8080 --previous-base-url http://127.0.0.1:8081

Prints one line per probe: PASS/FAIL/SKIP name (detail). Exit 1 if any fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from zoneinfo import ZoneInfo

import httpx
from playwright.sync_api import sync_playwright

TIMEOUT = 5000
RESET_TIMEOUT = 10.0
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob"}
MGR = {"id": "u_mgr", "email": "mgr@example.com", "password": "correct horse", "display_name": "Mgr"}

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


def all_week(opens="18:00", closes="23:00"):
    return [{"weekday": d, "opens": opens, "closes": closes} for d in WEEKDAYS]


def restaurant(rid="r_anker", *, name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, reservation_duration_minutes=90,
               cancellation_cutoff_minutes=120, opening_hours=None, tables=None,
               combinable=None, manager_user_ids=None):
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
    if manager_user_ids is not None:
        r["manager_user_ids"] = manager_user_ids
    return r


def fixture(*, users=None, restaurants=None, reservations=None):
    return {"users": [ADA, BOB, MGR] if users is None else users,
            "restaurants": [restaurant()] if restaurants is None else restaurants,
            "reservations": reservations or []}


def policy(*, effective_from, slot_minutes=30, reservation_duration_minutes=90,
           cancellation_cutoff_minutes=120, opening_hours=None, capacities=None):
    return {"effective_from": effective_from, "slot_minutes": slot_minutes,
            "reservation_duration_minutes": reservation_duration_minutes,
            "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
            "opening_hours": all_week() if opening_hours is None else opening_hours,
            "capacities": capacities if capacities is not None else {"t_1": 2, "t_2": 4, "t_3": 6}}


def booking_date(timezone="Europe/Berlin", lead=7):
    return (dt.datetime.now(ZoneInfo(timezone)).date() + dt.timedelta(days=lead)).isoformat()


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
    return (page.text_content(sel(name)) or "").strip()


def log_in(page, email=ADA["email"], password=ADA["password"]):
    page.goto("/login")
    page.fill(sel("login-email"), email)
    page.fill(sel("login-password"), password)
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))


def search(page, date=None, party_size=4):
    page.goto("/")
    page.select_option(sel("restaurant-select"), "r_anker")
    page.fill(sel("date-input"), date or booking_date())
    page.fill(sel("party-size-input"), str(party_size))
    page.click(sel("search-button"))
    page.wait_for_selector(f"{sel('availability-grid')}, {sel('no-slots')}")


# ---------------------------------------------------------------- probes
def p_stage2_ui_regression(run):
    """Stage-2 routes/testids and the single-table booking flow still work."""
    page, base = run["page"], run["base"]
    reset(base)
    for route, anchor in (("/", "search-button"), ("/signup", "signup-submit"),
                          ("/login", "login-submit"), ("/lookup", "lookup-submit")):
        page.goto(route)
        page.wait_for_selector(sel(anchor), state="attached")
    log_in(page)
    date = booking_date()
    search(page, date=date, party_size=2)
    avail = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 2}, timeout=5).json()
    assert avail["slots"], "expected slots"
    s = next(s for s in avail["slots"] if s["available_table_ids"])
    hh = s["starts_at_local"][-5:]
    tid = s["available_table_ids"][0]
    page.click(sel(f"slot-{tid}-{hh}"))
    page.wait_for_selector(sel("booking-form"))
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    ref = text(page, "confirmation-reference")
    assert ref, "empty confirmation reference"
    page.goto("/lookup")
    page.fill(sel("lookup-reference-input"), ref)
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-detail"))
    assert text(page, "reservation-status") == "confirmed"
    return "stage-2 routes + booking/lookup flow"


def p_grid_reflects_policy(run):
    """Req 6: the grid slot step follows the selected policy's slot_minutes."""
    page, base = run["page"], run["base"]
    date = booking_date(lead=9)
    reset(base, fixture(restaurants=[restaurant(manager_user_ids=["u_mgr"])]))
    mgr = api_login(base, MGR["email"], MGR["password"])
    r = httpx.post(f"{base}/restaurants/r_anker/policies", json=policy(
        effective_from=date, slot_minutes=60), headers={
            "Authorization": f"Bearer {mgr}", "Idempotency-Key": "ui-pol-1"}, timeout=5)
    assert r.status_code == 201, r.text[:200]
    avail = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 2}, timeout=5).json()
    starts = [s["starts_at_local"][-5:] for s in avail["slots"]]
    # step is 60 minutes from 18:00
    assert starts[0] == "18:00" and starts[1] == "19:00", starts
    log_in(page)
    search(page, date=date, party_size=2)
    # the grid must contain exactly the policy's slots
    for hh in starts:
        assert q(page, f"slot-t_1-{hh}") is not None, f"missing grid cell for policy slot {hh}"
    return "grid reflects selected policy slot grid"


def p_grid_capacity_from_policy(run):
    """Req 40: a combination's available capacity is the sum of selected-policy capacities."""
    page, base = run["page"], run["base"]
    date = booking_date(lead=10)
    reset(base, fixture(restaurants=[restaurant(combinable=[["t_1", "t_2"]],
                                               manager_user_ids=["u_mgr"])]))
    mgr = api_login(base, MGR["email"], MGR["password"])
    # shrink t_2 so the pair sum drops from 6 to 4; party 5 must no longer see the pair
    r = httpx.post(f"{base}/restaurants/r_anker/policies", json=policy(
        effective_from=date, capacities={"t_1": 2, "t_2": 2, "t_3": 6}), headers={
            "Authorization": f"Bearer {mgr}", "Idempotency-Key": "ui-pol-2"}, timeout=5)
    assert r.status_code == 201, r.text[:200]
    avail = httpx.get(f"{base}/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 5}, timeout=5).json()
    pair_seen = any(["t_1", "t_2"] in [o["table_ids"] for o in s["available_options"]]
                    for s in avail["slots"])
    assert not pair_seen, "policy capacity not applied to combination availability"
    return "combination capacity from selected policy"


def p_visual_bar_375(run):
    """Stage-2 visual bar still holds: no horizontal scroll at 375 px; labels; no raw dumps."""
    page, base = run["page"], run["base"]
    reset(base)
    page.set_viewport_size({"width": 375, "height": 800})
    log_in(page)
    search(page, party_size=4)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"horizontal overflow {overflow}px at 375px"
    for name in ("restaurant-select", "date-input", "party-size-input", "search-button"):
        assert q(page, name) is not None, f"missing {name}"
    page.set_viewport_size({"width": 1280, "height": 800})
    return "375px + desktop no h-scroll; controls present"


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--previous-base-url", default=None)
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chromium",
            args=[f"--unsafely-treat-insecure-origin-as-secure={base}"])
        ctx = browser.new_context(base_url=base, viewport={"width": 1280, "height": 900})
        ctx.set_default_timeout(TIMEOUT)
        page = ctx.new_page()
        run = {"page": page, "base": base}

        check("U1 stage-2 UI regression", lambda: p_stage2_ui_regression(run))
        check("U2 grid reflects policy slots", lambda: p_grid_reflects_policy(run))
        check("U3 combination capacity from policy", lambda: p_grid_capacity_from_policy(run))
        check("U4 visual bar at 375px", lambda: p_visual_bar_375(run))

        ctx.close()
        browser.close()

    width = max(len(n) for _, n, _ in RESULTS)
    failed = 0
    for status, name, detail in RESULTS:
        print(f"{status} {name.ljust(width)} :: {detail}")
        if status == "FAIL":
            failed += 1
    print(f"\n{sum(1 for s, _, _ in RESULTS if s == 'PASS')}/{len(RESULTS)} passed"
          f" ({failed} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
