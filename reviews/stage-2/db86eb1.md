# Stage 2 review — tablekeeper — REJECT

- Revision: `db86eb11cc617d63c8de2dec50e1c64f6dd36d18` (`db86eb1`)
- Work item: tablekeeper stage 2 (combined-tables booking UI)
- Reviewer: @fahmi24trk/reviewer
- Clone: `/tmp/opencode/s2-review/repo` (fresh clone outside the repo), checked out at `db86eb1`.

## Gate

```
docker build ./stage-2   -> ok
docker build ./stage-1   -> ok (upgrade source)
run --network none --cpus 2 --memory 2g -e PORT=8080
/health -> 200 {"status":"ok"} within 2 s
```

Note: under `--network none` Docker publishes no host port; the official harness
(`--mode isolated`, internal network) and the probes reach the service over its own
network namespace, which is the intended "no outbound at run time" condition.

## Official check

```
cd /workspace/dark-factory/kickoff && .venv/bin/python -m harness run \
  --track tablekeeper --repo /tmp/opencode/s2-review/repo --stage 2 --mode isolated \
  --out /workspace/band-work/checks/tablekeeper-s2-db86eb1-1
```

```
  stage 1: pass   (log .../stage-1.log)
  stage 2: fail   (log .../stage-2.log)
  stage 3: fail   (log .../stage-3.log; the expected overshoot probe)
highest contiguous stage: 1
claimed stage: 2 on the shipped checks
```

Stage-1: 120/120. Stage-2 shipped checks: **13 failed, 12 passed**. All 13 failures are
UI checks; the failures are consistent with the two root defects below (the shipped
`test_sample.py::test_booking_flow_reaches_a_confirmation` log line reads
`locator resolved to hidden <form ... data-testid="booking-form">`).

## Own probes

`reviews/stage-2/probe_stage2_api.py` — 9/10 pass; 1 fail.
`reviews/stage-2/probe_stage2_ui.py` — 5/13 pass; 8 fail.

Both run in a browser-equipped container on the same internal network as the service;
API probes also build this repo's `stage-1/` as an authentic upgrade source. After the
Architect's E13 decision the API probe additionally asserts the E13 check order; the
post-E13 run is 8/10 (F4 in S2, F5 in S4).

## Findings (all bar ACCEPT)

### F1 — Booking form never becomes visible; parent `<section id="book">` stays hidden (UI, blocking)

- Evidence: after clicking an available cell, `[data-testid='booking-form']` still has a
  hidden **ancestor**. `stage-2/static/app.js:242-243` sets only `form.hidden = false`,
  while `stage-2/app.py:99` wraps the form in `<section id="book" hidden>` and nothing ever
  clears `#book`'s `hidden` attribute. DOM probe: `#book hasAttribute('hidden') == True`,
  `is_visible('[data-testid=booking-form]') == False`.
- Consequence: the shipped stage-2 sample test (`test_booking_flow_reaches_a_confirmation`)
  and UI checks U4, U5, U6, U7, U8, U10, U11 all time out waiting for the form to become
  visible, so the entire booking/confirmation/idempotency/409/lost-response surface is
  unreachable in the browser.
- Requirements: stage-2 req 17 (form testids and booking flow), req 18 (resubmit/one
  booking), req 19 (confirmation contents), req 4/U10 (409 preserves form), req 5/U11
  (uncertain/retry), req 37/U8 (`confirmation-tables`).

### F2 — Grid renders no cell for an unavailable table (UI, blocking)

- Evidence: `renderGrid` (`stage-2/static/app.js:213-236`) only creates cells for
  `slot.available_table_ids` and for pair `available_options`; tables that are not
  available get no cell at all. With the default fixture and `party_size=4`, the server
  reports `available_table_ids == ['t_2','t_3']` for every slot, yet DOM probe finds no
  `[data-testid='slot-t_1-18:00']` element ("missing cell slot-t_1-18:00"), and the grid
  contains only `slot-t_2-*`, `slot-t_3-*`, and `slot-t_1+t_2-*` cells.
- Consequence: unavailable tables are invisible, so `data-available="false"` is never
  emitted; the shipped check `test_cells_carry_data_available` times out and U2 fails.
- Requirements: stage-2 req 13 (one cell per table per slot carrying `data-available`),
  E3.

### F3 — Signed-out booking click is a no-op (UI, blocking)

- Evidence: after a signed-out user clicks an available pair cell, the page URL stays `/`,
  `[data-testid='auth-error']` is absent, `[data-testid='login-submit']` is absent, and the
  form is not shown. Direct DOM probe: `auth-error: False`, `login-submit: False`.
- Consequence: U4 fails; the spec-mandated sign-in prompt is missing.
- Requirements: stage-2 req 16 (clicking an available cell while signed out shows
  `auth-error` or navigates to `/login`).

### F4 — More-than-two `table_ids` returns the wrong error code (API)

- Evidence: `POST /reservations` with `table_ids=["t_1","t_2","t_3"]` (and four ids) returns
  `422 validation_failed` `"'table_ids' accepts at most two tables in this stage"`
  (`stage-2/validation.py:254-256`) instead of the specified `422 combination_not_allowed`.
- Consequence: API probe S2 fails; spec table explicitly maps "More than two tables" to
  `combination_not_allowed`.
- Requirements: stage-2 req 33, spec stage-2 §"More than two tables".

### F5 — Unknown member inside `table_ids` returns the wrong code and wrong precedence (API)

- Architect decision E13 (`plans/stage-2/ledger.md:97`, commit `f071690`) settles the
  Reviewer's earlier question: unknown table id (or a table of another restaurant) is
  `404 not_found`, and the required order is (a) membership 404, (b) at most two →
  `combination_not_allowed`, (c) duplicate → `validation_failed`, (d) combinable →
  `combination_not_allowed`, (e) capacity/occupancy.
- Evidence on `db86eb1`: `table_ids=["t_1","t_nope"]` → `422 combination_not_allowed`
  `"pair is not declared combinable"` because the pair-combinable check runs before
  membership validation (`stage-2/operations.py:609-616`). Must be `404 not_found`.
- Consequence: API probe S4 fails; E13(a) violated.
- Requirements: E13(a), stage-1 req 46 (still binding).

### F4 (E13 restatement)

- E13(b) confirms the required code: `>2` ids must be `422 combination_not_allowed`;
  `db86eb1` returns `422 validation_failed` for 3 and 4 ids.

## Passed surfaces (for the record)

API: availability `available_options` ordering/order-of-pairs/capacity/occupancy filtering
(S3), `table_ids` response shape and singleton `table_id` (S4), PATCH to a set and cancel
freeing members (S5), seeded `table_ids` + default status (S6), moves with `table_ids` +
replay + atomic conflict (S7), concurrent pair bookings exactly-one-winner (S8), stage-1
error matrix intact (S9), and stage-1→stage-2 upgrade continuity incl. token/ref/retry (S10).

UI: routes/auth testids/`current-user`/logout (U1), combination cell testids + order +
`data-available` (U3), out-of-order search latest-wins (U9), signed-in session survives
import (U12), and the 375 px / desktop visual bar with labels, visible focus, and no raw
data dumps (U13).

## Verdict

REJECT. Three blocking UI defects (F1, F2, F3) and two API check-order/error-code defects
(F4, F5), each independently reproduced against the named revision. The official stage-2
check fails with 13 failing UI checks against `db86eb1`.
