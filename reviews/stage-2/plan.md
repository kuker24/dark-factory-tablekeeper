# Stage 2 review plan — tablekeeper

Reviewer: @fahmi24trk/reviewer. Target revision: whatever HANDOFF reports for `stage-2/`.
Folder under review: `stage-2/`. Ledgers: `plans/stage-2/ledger.md` (reqs 1–41, E1–E12) and
`plans/stage-1/ledger.md` (reqs 1–67, all still binding). Both specs are the contract.

Written in parallel with the Coder, independent of the supplied stage-2 checks. The shipped
`stage_2/test_ui.py` and `stage_2/test_sample.py` are a partial smoke signal; they never
exercise out-of-order search handling, the lost-response uncertain/retry path, the 409 form
preservation, stage-1→stage-2 upgrade continuity, combinable non-transitivity, pairs-only
enforcement, `available_options` ordering, `table_id`/`table_ids` response-shape rules,
multi-table moves and concurrency, or the visual/quality bar at 375 px.

## Verification method

1. Clone the handed-off revision into a fresh scratch dir outside the repository.
2. Gate: `docker build` the `stage-2/` folder, start a clean container with
   `--network none --cpus 2 --memory 2g -e PORT=8080 -p <port>:8080`, `/health` 200 within
   60 s. Build/start/health failure ⇒ REJECT.
3. Official check exactly as briefed against the clone, fresh `--out`:
   `cd /workspace/dark-factory/kickoff && .venv/bin/python -m harness run --track tablekeeper
   --repo <clone> --stage 2 --mode isolated --out /workspace/band-work/checks/tablekeeper-s2-<short>-<n>`
   Acceptance: prints `claimed stage: 2` and every stage 1..2 suite passes. `stage 3: fail`
   is the expected overshoot probe, not a defect. `--repo ... --stage 2` also builds
   `stage-1/` from the same clone as the upgrade source (`cli.py:check_stage`), so the
   shipped upgrade smoke check runs against the team's real stage-1.
4. Independent API probes: `reviews/stage-2/probe_stage2_api.py` against the running service
   (stage-2 build), plus a second copy of the service built from this repo's `stage-1/` to
   produce an authentic stage-1 export for the upgrade probes.
5. Independent UI probes: `reviews/stage-2/probe_stage2_ui.py` drives headless Chromium via
   Playwright (installed in the kickoff venv, channel `chromium`) against the stage-2 service.
6. Read the full `d76146c..revision` diff for check-gaming (testids only where required, no
   hardcoding of the shipped fixtures, server remains authoritative).

Reviewer must not edit product code/build files/Coder tests or fix findings; missing spec
content goes to Architect as QUESTION. Verdict format per the seat brief. Clean up
containers/images/scratch at the end.

## Requirement → probe map

### API / model (probe_stage2_api.py)
- Req 26–28, E1: `combinable` pairs only — 3+ rejected/ignored; unlisted pair cannot combine;
  non-transitivity ([t_1,t_2]+[t_2,t_3] must not make [t_1,t_3] bookable); capacity = sum;
  seeded reservations may carry `table_id` or `table_ids`, default confirmed unless cancelled.
- Req 29–30: `available_options` — singles first in fixture order, then declared pairs in
  `combinable` order, each with `capacity >= party_size` and both members free; pair
  `table_ids` in `combinable` order; `available_table_ids` stays singles-only.
- Req 31–33, E2: `POST /reservations` with `table_ids`; `table_id` still means a set of one;
  both together → 422 `validation_failed`; response always carries `table_ids`, carries
  `table_id` iff singleton; unlisted pair / >2 tables → 422 `combination_not_allowed`;
  overlap → 409 `table_unavailable`; party_size > summed capacity → 422
  `party_exceeds_capacity`; duplicate id → 422 `validation_failed`.
- Req 34: `PATCH` accepts `table_ids` under the same rules; cancel frees every member.
- Req 35, E1: single-table formats still work everywhere (create, GET, lookup, moves).
- Req 39: `POST /reservation-moves` accepts `table_ids` per move; no table in overlapping
  resulting bookings (within the batch and with unlisted bookings); atomicity.
- Req 41: concurrency on multi-table sets — e.g. many concurrent bookings of the same pair
  produce exactly one 201 and the rest 409; availability/occupancy invariants at every read.
- Stage-1 reqs still binding: full error matrix, DST arithmetic, idempotency path scoping,
  cutoff, export/import fidelity (now with combination sets).

### Upgrade continuity (probe_stage2_api.py + UI probe)
- Req 21–25, E8: build this repo's `stage-1/` as an authentic source; export its state; import
  into stage-2; assert signed-in token survives (`GET /reservations` 200 with same token);
  retained reference resolves; a lost-response booking retried with same body+key returns the
  original confirmation; stage-2 export re-imports unchanged.

### UI (probe_stage2_ui.py)
- Req 1, 11–12: routes return HTML; signup/login testids; `auth-error` only when an error;
  `current-user` on every screen when signed in; logout.
- Req 13–16: grid/testid contract, `data-available` exactness against `GET /availability`,
  unavailable click does nothing, signed-out booking shows `auth-error` or `/login`.
- Req 17–19: booking form testids; summary carries label+time; party-size pre-filled;
  confirmation-reference exact; details carry restaurant+label+time; form stays after success.
- Req 18, E7: unchanged resubmit replays same reference, no error, one booking; changed field
  is a new request.
- Req 20, E9: lookup testids; status exact; cancel removes button without reload; not-found.
- Req 36–38, E3/E4: combination cells `slot-{t_a}+{t_b}-{HH:MM}` in `combinable` order,
  `data-available` exact, clickable-when-available; `booking-summary` names every table;
  `confirmation-tables` and `reservation-tables` list every label.
- Req 3, E6: out-of-order search — delay a late A response and assert B's grid/labels/form
  win and A never restores. (Inject via route interception in Playwright.)
- Req 4, E5: open form, take the table over the API, submit → `booking-error` nonempty, no
  confirmation, inputs preserved, availability refreshed (cell now unavailable).
- Req 5: lost-response semantics — intercept the POST so the response is dropped after the
  server commits, assert `booking-uncertain` nonempty with no `booking-error`/confirmation,
  unchanged form retries with the same key+body and shows the original reference; a confirmed
  rejection shows `booking-error`.
- Req 7–10: product/visual bar — 375 px and desktop no horizontal scroll; visible labels;
  visible keyboard focus; contrast; empty/loading/error states; primary action identifiable;
  human-readable labels; no raw API dumps. Judged with computed measurements (viewport
  scrollWidth, focus styles, label association, colour contrast where feasible) plus DOM
  inspection, not taste alone.

## Evidence to collect

- Gate log, official-check command + quoted summary + counts, probe pass/fail lines.
- For each UI failure: the testid list present/absent and the relevant DOM/attribute values.
- Diff review notes (any testid hardcoding or fixture-specific shortcuts).

## Verdict rule

ACCEPT only on a real official-check run against the named revision with `claimed stage: 2`,
all stage 1–2 suites passing, the gate passing, and no probe finding that breaks a ledger
requirement. Otherwise REJECT with specific ledger numbers and reproducible evidence.
