# Tablekeeper stage 2 — requirements ledger

Increment: stage-2. Folder: /workspace/band-work/final-result/stage-2/
Carry-forward: copied from accepted stage-1/ (rev 883364bf) and extended; stage-1/ untouched.
Stage-1 spec remains binding; all stage-1 ledger entries 1–67 still hold unless amended below.

## Numbered requirements (testable statements)

### Screens
1. Routes `/` (search + availability grid), `/signup`, `/login`, `/lookup` return HTML and are reachable by URL; other screens reachable through the UI. Server- or client-side rendering both permitted.
2. Screen routes return HTML; §3.4 JSON convention applies to the API only.

### Competing clients / uncertain outcomes (UI behavior)
3. Out-of-order search responses: if search A starts before B but finishes after, the grid, table labels and booking form describe B; a late response never restores A's results.
4. `409 table_unavailable` on submit shows nonempty `booking-error`, refreshes availability, preserves the form and its inputs, and shows no confirmation for that attempt.
5. A lost booking response (including after commit) shows nonempty `booking-uncertain` text, without `booking-error` or a new confirmation; the unchanged form retries with the same idempotency key and body; a successful retry removes uncertainty/error elements and shows the original reference; a confirmed rejection uses `booking-error`.
6. These rules also apply to combination bookings. No background polling, live updates, cross-tab sync or reload recovery required; server stays authoritative; the browser never manufactures success from cached data.

### Product / visual quality
7. Coherent presentation-ready restaurant product, warm hospitality character, clear visual hierarchy for dates, times, party size and table choices; no raw API data dumps; combined tables read as intentional seating options (not concatenated ids).
8. Consistent visual system (typography, spacing, colour, controls, feedback); primary actions easy to identify; available, unavailable, selected, loading, successful, refused and uncertain states visually distinct.
9. Human-readable restaurant and table labels prominent; technical ids only where useful.
10. Usable at 375 CSS px and desktop widths without horizontal scrolling; visible input labels; apparent keyboard focus; sufficient contrast; considered empty, loading and error states; consistent navigation across routes.

### Auth screens
11. `/signup` inputs `signup-email`, `signup-password`, `signup-display-name`, button `signup-submit`; `/login` inputs `login-email`, `login-password`, button `login-submit`; `auth-error` element present only when there is an error.
12. `current-user` visible on every screen when signed in, text contains display name; `logout-button` present.

### Search & grid `/`
13. `restaurant-select` (option values are restaurant ids), `date-input` (value YYYY-MM-DD), `party-size-input`, `search-button`, `availability-grid`, `no-slots` (shown instead of grid when the day has no slots).
14. Cells `slot-{table_id}-{HH:MM}` per table per slot; `data-available="true"` exactly when table_id is in that slot's available_table_ids for the searched party size.
15. Clicking an available cell opens the booking form for that table and slot; clicking an unavailable cell does nothing.
16. Booking requires sign-in: clicking an available cell while signed out shows `auth-error` or navigates to `/login` (implementer's choice).

### Booking form / confirmation
17. `booking-form` container; `booking-summary` text contains the table label and local start time; `booking-party-size` number input pre-filled from search; `booking-submit`; `booking-error` on failure.
18. Form stays on screen after success; resubmitting unchanged returns the same `confirmation-reference` (idempotent replay), without `booking-error` or another booking; changing a field makes the next submission a new booking request; retries follow §7 (same key + same body for unchanged form).
19. Confirmation: `confirmation` container; `confirmation-reference` text is exactly the reference (no surrounding words); `confirmation-details` contains restaurant name, table label and local start time.

### Lookup `/lookup`
20. `lookup-reference-input`, `lookup-submit`; `reservation-detail` shown when found; `reservation-status` text exactly `confirmed` or `cancelled`; `reservation-cancel-button` cancels and is absent once cancelled; `reservation-error` shown when not found or cancel refused.

### Upgrade continuity (stage-1 export → stage-2 import)
21. The stage-2 service accepts an export produced by the same team's stage-1 service.
22. A browser signed in before the export/import upgrade stays signed in afterwards (tokens survive).
23. A retained booking reference still works through the lookup screen.
24. A booking whose response was lost before export remains retryable after import with the same body and key; the UI recovers the original confirmation.
25. These apply when import completes between browser requests (not during an in-flight request); no page reload or new screen required; form and pending retry identity survive the upgrade.

### Combined tables — model
26. Restaurant fixture gains `combinable`: list of unordered pairs of table ids; pairs only (never 3+); unlisted pairs cannot be combined regardless of sizes; combining is not transitive.
27. Combination capacity = sum of member capacities.
28. Seeded reservations are confirmed unless `status: "cancelled"`; they may hold `table_id` or `table_ids`.

### Combined tables — API
29. `GET /availability` slots gain `available_options`; `available_table_ids` unchanged (singles only).
30. `available_options` lists every single table then every declared pair with capacity ≥ party_size and no overlapping confirmed reservation on any member; singles first in fixture order, then pairs in `combinable` order; `table_ids` within a pair in `combinable` order; each option carries `table_ids` and `capacity`.
31. `POST /reservations` takes `table_ids` (array); `table_id` still accepted as a set of one; sending both → 422 `validation_failed`.
32. Responses always carry `table_ids`; also carry `table_id` iff the set has exactly one member; omitted otherwise.
33. Pair not in `combinable` → 422 `combination_not_allowed`; more than two tables → 422 `combination_not_allowed`; any member table taken overlapping → 409 `table_unavailable`; party_size > summed capacity → 422 `party_exceeds_capacity`; duplicate table id in the set → 422 `validation_failed`.
34. `PATCH /reservations/{reference}` accepts `table_ids` under the same rules; cancelling frees every table in the set.
35. Seeded/other consumers: single-table formats remain supported everywhere (GET /reservations, lookup, moves).

### Combined tables — UI
36. Combination cells `slot-{t_a}+{t_b}-{HH:MM}` (ids in combinable order), shown when a declared pair is available for the searched party size; carry `data-available` like single cells; clickable-when-available opens the booking form.
37. `confirmation-tables` text contains every table label in the reservation; `reservation-tables` on the lookup screen, same.
38. `booking-summary` names every table in the selection; single-table cell testid, confirmation and lookup unchanged from stage 1.

### Moves + concurrency
39. `POST /reservation-moves` accepts `table_ids` per move under the same rules; no table may belong to overlapping resulting bookings (within batch and with unlisted bookings).
40. Browser recovery and original-receipt idempotency requirements apply to combined-table bookings.
41. Concurrent requests produce the same results as some serial order; requirements hold at every read (availability, occupancy invariants) — extends stage-1 req 40 to multi-table sets.

## Ambiguities and decisions

- E1 (single vs pair responses): every reservation record stores a set of table ids; serialization adds `table_id` only for singletons. Stage-1 seeded `table_id` fixtures become singleton sets.
- E2 (both fields sent): `table_id` and `table_ids` together in POST/PATCH/moves → 422 `validation_failed` (spec explicit).
- E3 (combination grid semantics): a pair cell's availability = both members free and pair listed in combinable and summed capacity ≥ searched party size — i.e. the pair is in available_options. Pair cells appear only for declared pairs available at the searched party size.
- E4 (booking from a pair cell): submit POST /reservations with table_ids = [t_a, t_b] and the same idempotency semantics; uncertain/lost-response handling identical.
- E5 (409 refresh): on table_unavailable the UI refreshes availability for the current search; form inputs preserved.
- E6 (out-of-order guard): client tags each search with a sequence number; only the latest response renders; late responses dropped.
- E7 (retry identity): the form keeps its idempotency key and body as long as no field changes; changing any field generates a new key.
- E8 (export compatibility): stage-1 export must import into stage-2 (format_version stays 1, same track; stage-2 import maps stage-1 state forward, e.g. singleton table sets). Stage-2 exports remain opaque and self-compatible.
- E9 (lookup auth): lookup screen uses the API's GET /reservations/{reference} which requires a token — the spec's public-endpoint list is unchanged, so the lookup screen requires the diner to be signed in; the reservation-error element covers not-found. (Stage-1 §8 list governs.)
- E10 (cutoff/cancel semantics for combined bookings): unchanged from stage 1; cancel frees all member tables.
- E11 (visual design): implementer's choice within the stated quality bar; no external assets at runtime — all CSS/JS/fonts served from the image.
- E12 (past slot availability semantics, empty day): `no-slots` shown when the day has no slots at all (closed day or no grid entries), matching stage-1 slots list.

## Work items

- WI-1 (single slice): carry stage-1/ forward to stage-2/, extend the API/model for combinable tables, then build the four screens with the required testids, competing-client handling, upgrade continuity, and visual quality bar. Acceptance: official harness (stage 2) passes on a fresh clone — "claimed stage: 2", all stage 1..2 suites pass — plus Reviewer verification against this ledger and stage-1's.

## Status

- [ ] WI-1 dispatched to Coder; Reviewer briefed in parallel.

Decision E13 (from Reviewer question, 2026-10-02): unknown table id inside table_ids returns 404 not_found (stage-1 req 46 remains binding: unknown table or table of another restaurant -> 404). Check order: (a) every member exists and belongs to the restaurant, else 404 not_found; (b) at most two tables, else 422 combination_not_allowed; (c) duplicate member, else 422 validation_failed; (d) pair must be listed in combinable, else 422 combination_not_allowed; (e) capacity/occupancy as specified.
