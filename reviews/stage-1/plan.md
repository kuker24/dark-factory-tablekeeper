# Stage 1 review plan — tablekeeper

Reviewer: @fahmi24trk/reviewer. Target revision: whatever HANDOFF reports.
Folder under review: `stage-1/`. Ledger: `plans/stage-1/ledger.md` (reqs 1–67, D1–D12).

This plan is written from the ledger and the full specification, in parallel with the
Coder, and is independent of the supplied checks. The supplied checks (preview suite,
marked "shipped checks") are only a smoke signal: they never exercise most of §7
concurrency semantics, §9 DST arithmetic on both sides of both transitions, §10
import fidelity, §11 batch atomicity/error precedence, the full error matrix, ID length
limits, unknown-field tolerance, or clean-container start.

## Verification method

1. Clone the handed-off revision into a fresh scratch dir outside the repository.
2. Gate: `docker build` the `stage-1/` folder, start a clean container with
   `--network none --cpus 2 --memory 2g -e PORT=8080 -p <port>:8080`, wait for
   `/health` within 60 s. Build/start/health failure => REJECT.
3. Official check exactly as briefed against the clone, fresh `--out`:
   `cd /workspace/dark-factory/kickoff && .venv/bin/python -m harness run --track tablekeeper --repo <clone> --stage 1 --mode isolated --out /workspace/band-work/checks/tablekeeper-s1-<short>-<n>`
   Quote summary lines. Acceptance: `claimed stage: 1` and every stage 1 suite passes.
   A `stage 2: fail` overshoot-probe line is expected.
4. Run `probe_stage1.py` against the container's published port (host mode is enough
   for behavior probes; the isolated run already proves no-network start).
5. Read the diff for check-gaming, hard-coded expectations, out-of-folder changes,
   secrets, later-increment features.
6. Write `reviews/stage-1/<shortrev>.md`, commit under my identity, send VERDICT.

## Probe coverage (one probe per ledger entry, plus the untested edges)

### Delivery / runtime
- **P1** (req 1–3, 15): health 200 `{"status":"ok"}`; reset 204; reset is unauthenticated.
- **P2** (req 2): default port absent handled by harness; probe uses given port.
- **P3** (req 4, 14): 50 concurrent `GET /availability` all 2xx, no 5xx, each <5 s.
- **P4** (req 15, 16, 61, 83): repeated resets; reset replaces all state; reset after
  import clears imported state.

### Conventions / errors
- **P5** (req 6, 10): error bodies `{"error":{"code","message"}}`; success JSON utf-8.
- **P6** (req 7): every response timestamp RFC3339 with explicit offset.
- **P7** (req 8): unknown body fields and unknown query params ignored (create,
  availability, moves).
- **P8** (req 9): IDs >64 chars in reset -> 422; IDs exactly 64 accepted; reservation
  reference/length limits (6–12 A-Z0-9).
- **P9** (req 11, 13): wrong JSON type -> 400 malformed; bad values -> 422; Idempotency-Key
  length 0/256 -> per §7 (empty -> 400 missing_idempotency_key; 256 -> 422).
- **P10** (req 12): query integers `1e9`, `4.0`, `+4`, ` 4`, `-1`, `abc` -> 422 on
  `/availability`.
- **P11** (req 14): no 5xx under malformed/null/oversized bodies.

### Auth / model
- **P12** (req 18, 21, 22): seeded login; signup 201; duplicate 409 email_taken; short
  password 422; malformed email 422; login wrong 401.
- **P13** (req 23, 24): all protected endpoints 401 without/with-bad token; public
  endpoints work with no token; another user's reservation -> 404 not 403.
- **P14** (req 25): two logins/signups give distinct tokens both usable; token reused.
- **P15** (req 26): hashed password — inspect export state for plaintext password.
- **P16** (req 17, 19, 20): fixture shape honored; past-dated seeded booking accepted;
  closed day -> slots [].

### Idempotency (§7)
- **P17** (req 27): missing/empty key -> 400 missing_idempotency_key.
- **P18** (req 28): same key different users independent.
- **P19** (req 29): same key different body -> 409; key order/whitespace ignored
  (same JSON value counts as replay).
- **P20** (req 29): same key different path (reservations vs moves) not a replay.
- **P21** (req 30, 33): first 201, replay 200 identical body; replay after cancel still
  200 original and no state change.
- **P22** (req 31): key reused after a 4xx failure is a first use.
- **P23** (req 29, D5): key with a different invalid body -> 409 reuse, not 422.
- **P24** (req 32): 20 concurrent identical requests, one key -> exactly one 201,
  nineteen 200, same body, one booking effect.

### API
- **P25** (req 34, 35): restaurants list public; detail shape public; unknown -> 404.
- **P26** (req 36, 37): availability params required; slot grid from opens; full
  `YYYY-MM-DDTHH:MM`; capacity filter fixture order; empty list slot shown.
- **P27** (req 38, 39): create shape; reference regex; uniqueness; immutable across patch.
- **P28** (req 40): overlapping interval (not just equal slot) -> 409; adjacent
  non-overlap OK; concurrency on one table -> exactly one 201.
- **P29** (req 41–44): not_on_slot_grid; outside_opening_hours incl. end-after-close;
  party_exceeds_capacity; party_size <1 / non-integer -> validation_failed.
- **P30** (req 45, 46): DST gap -> invalid_local_time; unknown restaurant/table/foreign
  table -> 404.
- **P31** (req 47, 48): list desc by starts_at, includes cancelled; empty list shape;
  by-reference 404 for others.
- **P32** (req 49): cancel 200 frees table immediately; double cancel 200; cutoff 409;
  foreign 404.
- **P33** (req 50): PATCH subset; no key needed; failed patch leaves original intact;
  cancelled -> 409; reference/id survive; success releases old+reserves new atomically.
- **P34** (req 51): rejected request creates no reservation (list unchanged).

### DST / time (§9)
- **P35** (req 52, 56): Berlin spring 2026-03-29: 02:00–02:59 absent from availability;
  booking 02:30 -> invalid_local_time.
- **P36** (req 53, 56): Berlin fall 2026-10-25: repeated hour appears exactly once;
  booking 02:00 -> `starts_at` ends `+02:00` (first occurrence).
- **P37** (req 54, 56): NY spring 2026-03-08 gap absent + invalid_local_time; NY fall
  2026-11-01 repeated hour once, first occurrence (`-04:00`).
- **P38** (req 55): 90-min booking at 01:30 on Berlin fall night ends local 02:00
  (+01:00), i.e. 90 real minutes, not wall clock.
- **P39** (req 56): same absolute instant from Berlin 18:00 CET and NY 12:00 EST.

### Export / import (§10)
- **P40** (req 57): export 200, `track`/`format_version`/`state`; unauthenticated;
  snapshot atomic (write after export does not change it).
- **P41** (req 58): import unchanged export -> 204; repeatable, idempotent, no dupes.
- **P42** (req 59): invalid JSON -> §5; wrong track/version/missing fields -> 422 with
  destination unchanged.
- **P43** (req 60): after source export -> import into a *different* state (reset to a
  fresh fixture), old tokens, hashed-password login, reservations, references, completed
  idempotent receipts and original responses all survive; failed keys reusable;
  identities/statuses/timestamps unchanged.
- **P44** (req 60, 61): import removes destination credentials/data; reset clears
  imported state.

### Batch moves (§11)
- **P45** (req 62): shape/duplicates/length >8/<1 -> 422; requires auth (401) and key.
- **P46** (req 63): foreign reference -> 404; mixed restaurants -> 422.
- **P47** (req 64, 67): omitted fields retain; identity/owner/created_at unchanged;
  success 201 `{reservations:[...]}` in input order incl. unchanged.
- **P48** (req 65): cancelled -> 409 reservation_cancelled; cutoff error precedes other
  changes for that booking; non-occupancy errors in input order; resulting overlap
  (batch + unlisted) -> 409 table_unavailable.
- **P49** (req 66): all-or-nothing — a failing second move leaves first booking's
  occupancy and records untouched; retry key not consumed on failure.
- **P50** (req 67): replay returns original 200 even after later amendment/cancel;
  export/import preserves batch receipts and resulting bookings.
- **P51** (req 40, §11): concurrent identical batch with one key -> one 201, one effect.

## Artifacts
- `probe_stage1.py` — stdlib+httpx runnable probe suite (`--base-url`), one printed
  line per probe, exit non-zero on any failed probe.
- `fixtures` built relative to "today" exactly as the spec forbids fixed dates.
