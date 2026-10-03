# Stage 3 review plan — tablekeeper

Reviewer: @fahmi24trk/reviewer. Target revision: whatever HANDOFF reports for `stage-3/`.
Folder under review: `stage-3/`. Ledgers: `plans/stage-3/ledger.md` (reqs 1–43, F1–F14),
`plans/stage-2/ledger.md` (reqs 1–41, E1–E13) and `plans/stage-1/ledger.md` (reqs 1–67,
D1–D12) — all binding; specs `stage-1.md`, `stage-2.md`, `stage-3.md` are the contract.

Written in parallel with the Coder, independent of the supplied stage-3 checks. The shipped
checks are a partial smoke signal: they do not exercise policy selection ordering (past
dates, publication vs effective order, same-date supersession ties by greatest version),
immutability/no-version-on-failure, accepted-terms snapshots on amendment (old idempotent
responses keep original revision/terms), no-op amendments, replay records nothing,
`stale_revision` precedence and concurrent amendment races, history exactness, owner-only
404 incl. unauthenticated, decision after cancellation, explain exactness
(every table once, both rules, order consistency with `available_table_ids`,
`policy_version`), invalid explain values, plain-shape invariance, series adoption
(cutoffs, already_in_series, bounds incl. booleans, all-or-nothing first-failing error,
per-occurrence policy/DST/occupancy, anchor unchanged, occurrence 404s, exception/revision
semantics, replay after changes, restaurant revision once), combined-table history forms and
reversed-pair no-op, moves under policies with per-move `expected_revision`, and
stage-1/2 export continuity into stage-3.

## Verification method

1. Clone the handed-off revision into a fresh scratch dir outside the repository.
2. Gate: `docker build` the `stage-3/` folder, start a clean container with
   `--network none --cpus 2 --memory 2g -e PORT=8080`, `/health` 200 within 60 s.
   Build/start/health failure ⇒ REJECT.
3. Official check exactly as briefed against the clone, fresh `--out`:
   `cd /workspace/dark-factory/kickoff && .venv/bin/python -m harness run --track
   tablekeeper --repo <clone> --stage 3 --mode isolated --out
   /workspace/band-work/checks/tablekeeper-s3-<short revision>-<run>`.
   Acceptance: prints `claimed stage: 3` and stages 1..3 pass; `stage 4: fail` is the
   expected overshoot probe.
4. Run the two probe suites below against the built image (internal network / shared netns;
   stage-1 and stage-2 siblings built from the same clone as upgrade sources).
5. Read the full diff from the accepted stage-2 (`52f7051`) to the handed-off revision for
   check-gaming; confirm the shipped `stage_3/` test files and harness are untouched.
6. Clean up containers, images, network and scratch clones.

## Probe suites (independent)

### `probe_stage3_api.py`

- **P1 Policies: publication, immutability, versions.** manager-only (403 non-manager,
  401 no token, 404 unknown restaurant); complete-body validation incl. booleans not
  integers, ranges, exactly-the-table-ids capacities, duplicate weekdays; 201 returns
  supplied policy + `policy_version` starting 1, +1 per publication; invalid → 422 with no
  version/state change; replay → 200 original and allocates no version; failed-key reuse =
  first use; policies immutable; `GET /restaurants/{id}/policies` public, publication order,
  omits policy 0; restaurant detail still original fixture config (reqs 14–20).
- **P2 Policy selection.** greatest `effective_from` ≤ booking's local start date; ties by
  greatest `policy_version`; publication order may differ from effective order; effective
  dates in the past; publication never retroactively edits accepted bookings; a same-date
  new policy governs future decisions (reqs 18–19, F1).
- **P3 Review revisions/terms baseline.** `revision: 1` at creation; `accepted_terms`
  snapshot of the whole selected policy excluding `effective_from`; seeded bookings rev 1 /
  policy 0; old idempotent responses keep original revision+terms (reqs 21–23).
- **P4 Amendments under policies.** real amendment checks accepted cutoff then validates
  all fields against the resulting date's policy, replaces terms+end time, revision +1;
  no-op retains everything and records no history; failed changes nothing; cancel checks
  accepted cutoff, revision +1 once; repeated cancel no-op; `expected_revision` semantics
  (409 `stale_revision` before cutoff/validation, 422 invalid type/range, omission = stage-1)
  and a concurrent one-revision race where at most one real change wins (reqs 24–28).
- **P5 Explain exactness.** invalid values (`false`, `1`, ``) → 422; without `explain` the
  exact stage-1 shape (no new fields); with it every table exactly once in fixture order,
  both rules reported in order, `available` ⟺ both hold, `available:true` set ==
  `available_table_ids` in order, `policy_version` present per table, closed day `[]`, slot
  with no available table still appears (reqs 1–6, F2, F14).
- **P6 History.** owner-only 404 (other user, signed-out) incl. after cancel; seq starts 1,
  +1, total order; created names all three fields from null; changed names only real changes
  in field order; no-op records nothing; cancelled empty changes and terminal; replay records
  nothing; every entry carries resulting revision and complete accepted_terms; old entries
  keep old terms (reqs 7–13).
- **P7 Decision.** `{reference, revision, accepted_terms}` current incl. after cancellation;
  owner-only 404, 404 unauthenticated (req 29).
- **P8 Series adoption.** anchor ownership/confirmed/cutoff (404 / 409
  `reservation_cancelled` / 409 `already_in_series`); bounds incl. booleans → 422; no token
  401; occurrence 0 unchanged; occurrence i date arithmetic at same clock time; per-occurrence
  policy/DST/occupancy; nonexistent local time → `invalid_local_time`; all-or-nothing with
  first-failing-occurrence error; 201 shape, distinct refs, all count in order, ordinary
  lists/histories; `GET /series/{id}` owner-only; PATCH marks exception + series rev +1 once,
  no-op/failure neither; cancel bumps series rev once without exception, repeated cancel no-op;
  cancelling anchor affects no siblings; adoption bumps restaurant rev once; replay returns
  original after changes with no counter movement; unknown fields ignored
  (reqs 30–38, F7–F11).
- **P9 Combined-table history.** pair creation uses `table_ids` from null; pair changes use
  complete `table_ids` lists; table-set order is declared combination order; reversed pair is
  no-op; single-to-single keeps stage-3 `table_id` (reqs 40–41, F6).
- **P10 Moves under policies.** per-move old-cutoff then resulting-date policy; optional
  per-move `expected_revision`; no-op retains terms/history; all-or-nothing occupancy;
  every changed booking +1 revision and one changed history entry; restaurant rev +1 once;
  each affected series +1 once and changed occurrence becomes exception; failed/replay moves
  nothing (reqs 42–43, F11).
- **P11 Upgrade continuity.** stage-1 and stage-2 exports import into stage-3; imported
  reservations adoptable; tokens/retries/references survive (req 39, F12).
- **P12 Stage-1/2 regressions.** health; public endpoints; error matrix; combinable/table_ids
  rules; stage-1 DST edges (spring-forward `invalid_local_time`, fall-back first occurrence +
  absolute duration) carried forward.

### `probe_stage3_ui.py`

Stage 3 adds no new screens (F13). Confirms the stage-2 UI still functions and the grid
reflects published policies (slot grid/duration/capacity from the selected policy), plus
routes/testids, 375 px with no horizontal scroll, and the stage-1→stage-3 signed-in
continuity. Kept small; the API suites carry the stage-3 weight.

## Verdict criteria

ACCEPT only with a real official `--mode isolated` run against the exact named revision that
prints `claimed stage: 3` and passes stages 1..3, the clean-container gate, and both probe
suites passing. Otherwise REJECT with specific ledger numbers and evidence. Reviewer does
not edit product code/build files/Coder tests or fix findings; missing spec content goes to
the Architect as a QUESTION.
