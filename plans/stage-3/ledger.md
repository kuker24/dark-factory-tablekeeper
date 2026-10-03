# Tablekeeper stage 3 — requirements ledger

Increment: stage-3. Folder: /workspace/band-work/final-result/stage-3/
Carry-forward: copied from accepted stage-2/ (rev 52f7051) and extended; stage-2/ untouched.
Stage-1 and stage-2 specs remain binding (stage-1 ledger 1–67, stage-2 ledger 1–41, decisions D, E1–E13) unless amended below.

## Numbered requirements (testable statements)

### Availability explanations
1. `explain` query param is optional; only accepted value is `true`; any other value (`false`, `1`, empty string) → 422 `validation_failed`.
2. Without `explain=true` the availability response keeps stage-1's exact shape — no explanation fields.
3. With `explain=true`, every slot carries `explain`: every table of the restaurant exactly once, fixture order; both rules (`capacity`, `no_overlap`) reported for every table in that order, never omitted; `available` true exactly when both hold; tables with `available:true` are exactly `available_table_ids` in the same order.
4. Closed day still `"slots": []`; a slot with no available table still appears with full explain.
5. Each explain entry carries the table's `policy_version` (the selected policy for that date).
6. Availability/booking decisions use the selected policy (not the fixture restaurant detail); published policies can change slot values.

### History
7. `GET /reservations/{reference}/history`: owner-only; anyone else (signed in or not) gets 404 `not_found`, same as §8; cancelled reservations still have history; returns 404 even without authentication (overrides the general 401 rule).
8. Entries oldest first, `seq` starting at 1, increasing by exactly 1 (total order even for same-second writes).
9. `created` entry names all three fields (table_id/table_ids, starts_at_local, party_size) each with `"from": null`.
10. `changed` names only fields that actually changed, in order table_id, starts_at_local, party_size; a PATCH setting a field to its existing value succeeds and records no entry at all.
11. `cancelled` carries empty `changes`; nothing follows it.
12. Replaying an idempotent POST /reservations records nothing.
13. Every history entry carries the reservation's resulting `revision` and complete `accepted_terms`; old entries never acquire newer terms.

### Policies
14. Restaurant fixture may declare `manager_user_ids` (default []); only these users may publish policies; unknown restaurant → 404; authenticated non-manager → 403 `forbidden`; no token → 401. Managers do not gain access to other diners' private lookup/history.
15. `POST /restaurants/{id}/policies` requires an idempotency key with stage-1 replay rules (replay → 200 original; same key different body → 409; failed-key reuse = first use).
16. Policy body is complete (not a patch): `effective_from` (actual YYYY-MM-DD), `slot_minutes` and `reservation_duration_minutes` (integers 1..1440), `cancellation_cutoff_minutes` (integer 0..10080, booleans not integers), `opening_hours` (stage-1 rules, no duplicate weekdays), `capacities` (exactly the restaurant's table ids, integers 1..100). All fields required. Invalid → 422 `validation_failed`, no version or state change. Unknown fields ignored. Table ids, labels, timezone, combinations cannot be changed by a policy.
17. Returns 201 with the supplied policy plus `policy_version` (integer, starts at 1, +1 per restaurant); failed writes and replays allocate no version; policies immutable.
18. Policy 0 = original fixture rules, applies before any published policy. Publication order may differ from effective order. Selection: for a booking's local start date, greatest `effective_from` not later than that date; ties → greatest `policy_version`.
19. A new same-date policy supersedes the old for future decisions without changing accepted reservations. Effective dates may be past; publication never retroactively edits a booking.
20. `GET /restaurants/{id}/policies` is public, returns `{"policies":[...]}` in publication order, omitting policy 0. The ordinary restaurant detail keeps returning the original fixture configuration.

### Accepted terms / revisions
21. Every reservation response gains `revision` (1 at creation) and `accepted_terms`: a snapshot of the entire selected policy excluding `effective_from` (policy_version, slot_minutes, reservation_duration_minutes, cancellation_cutoff_minutes, opening_hours, capacities).
22. Seeded bookings start at revision 1 under policy 0. Responses to old idempotency keys remain the original response, including original revision and terms.
23. A policy publication does not change existing bookings, their end times or their history.
24. Cancel checks the accepted cutoff against the current start.
25. A real amendment (time, tables, party size) checks the old accepted cutoff first, then validates all resulting fields against the policy applicable to the resulting start date; atomically replaces accepted terms and end time; increments revision once.
26. A no-op amendment retains terms, end time and revision and records no history; still requires a confirmed, editable booking.
27. Failed amendments change nothing. Cancel increments revision once; repeated cancel does not.
28. `PATCH` optionally accepts `expected_revision`: positive integer ≠ current revision → 409 `stale_revision` before cutoff/validation; invalid type/range → 422; omission retains stage-1 semantics. Two concurrent amendments with one revision: at most one real change succeeds. Unrelated unknown fields ignored.
29. `GET /reservations/{reference}/decision` returns `{"reference", "revision", "accepted_terms"}` for the current booking including after cancellation; owner-only 404 rule as history; 404 without authentication.

### Recurring reservations
30. `POST /series` adopts an existing reservation as occurrence 0; idempotency key required; body {anchor_reference, count (int 2..12 incl. anchor), interval_weeks (int 1..4)}; invalid values incl. booleans → 422; no token → 401.
31. Anchor must be caller's, confirmed, satisfy its accepted cancellation cutoff. Unknown/other owner's anchor → 404; cancelled → 409 `reservation_cancelled`; already adopted → 409 `already_in_series`.
32. Occurrence 0 is the anchor itself, unchanged (reference, identity, revision, terms, history, timestamps, original idempotent response).
33. Occurrence i starts at anchor local date + i × interval_weeks × 7 days, same local clock time; each occurrence independently selects its date's policy (duration, capacity) and obeys opening, DST and occupancy rules; nonexistent local time rejects the whole adoption with `invalid_local_time`; repeated times use first-occurrence rule; occurrences use anchor's party size and table selection.
34. All-or-nothing: no partial series, reservations, histories, counters or idempotency claims survive failure; the first failing occurrence in index order determines the ordinary booking error.
35. Returns 201: {series_id (opaque), revision: 1, interval_weeks, occurrences: all count in index order}, each occurrence {index, reference (distinct), exception: false, reservation: ordinary response shape}. Occurrences appear in ordinary reservation lists, occupy tables, have ordinary histories.
36. `GET /series/{series_id}` returns this shape with current reservation states; only the owner may read (another user or no token → 404).
37. A real individual PATCH permanently marks that occurrence `exception: true` and increments the series revision once; no-op or failure changes neither. Cancellation increments the series revision once, retaining the cancelled occurrence, without marking an exception; repeated cancel does nothing. Cancelling the anchor does not cancel siblings. Ordinary cutoff and revision checks still apply.
38. Adoption increments the restaurant revision once for the whole operation. Replays return the original series response even after later changes, changing no counter. Series creation is a new idempotent write path. Unknown fields ignored.
39. Stage-3 service accepts exports from the same team's stage-1 or stage-2 service; adoption works on imported reservations; existing confirmation links, sessions and original booking retries remain valid.

### Combined-table history
40. Accepted terms apply to combinations; capacity = sum of the selected policy's capacities.
41. History: single-to-single operations retain stage-3 `table_id` fields; pair creation uses `table_ids` (from null to the pair); pair changes use `table_ids` (complete before/after lists) instead of `table_id`; table-set order is the declared combination order; a reversed input pair names the same set and is not an amendment on its own. Policy selection, revision and replay rules unchanged.

### Collective moves under policies/agreements
42. Each real change in POST /reservation-moves uses individual PATCH semantics (old accepted cutoff, then resulting date's policy); per-move `expected_revision` optional, follows PATCH validation/stale rules; no-op retains terms and history.
43. All resulting bookings must satisfy amendment and occupancy rules; failure leaves every booking unchanged; every changed booking gains one revision and one changed history entry; restaurant revision +1 once for the batch; each affected series revision +1 once, each changed series occurrence becomes a permanent exception; a failed batch or replay changes no revisions, histories or exception flags.

## Ambiguities and decisions

- F1 (policy selection date): the booking's **local start date** (restaurant timezone) selects the policy; the same applies to availability for a given date and to each series occurrence by its own date.
- F2 (`explain` without policies): explain uses the selected policy (policy 0 when none published) and reports its `policy_version`.
- F3 (rest-of-day availability grid semantics): slot grid, duration and capacities come from the selected policy for the requested date; `available_table_ids` ordering stays fixture table order.
- F4 (cancellation cutoff for amendments/moves/series anchor): the **accepted** cutoff from the booking's stored terms, against the current start — not the policy-of-resulting-date cutoff.
- F5 (history `at` timestamps): RFC 3339 with explicit offset (UTC or restaurant-local both legal per §3.4); use UTC consistently.
- F6 (`table_ids` in created/changed history entries): field name is `table_ids` when the before or after set is a pair (size 2), `table_id` for single-to-single; a change from a pair to a single is a real change recorded with `table_ids` (complete lists).
- F7 (series occurrence references): fresh distinct references, 6–12 chars A-Z0-9, immutable; index stable.
- F8 (restaurant revision): a per-restaurant counter (starting 0 or 1) incremented once per policy publication, per real amendment/cancel/batch of changes, and once per series adoption; the spec only constrains relative increments, not the start value — start at 0, first real event makes it 1. Not exposed by any stage-3 endpoint except via policy_version; it is internal bookkeeping used to satisfy the "increments once" rules.
- F9 (anchor cutoff check): the anchor must satisfy its accepted cutoff at adoption time (i.e., adoption is allowed only before the cutoff).
- F10 (PATCH on an occurrence in a series): ordinary PATCH semantics + series exception/revision effects; `expected_revision` refers to the occurrence's reservation revision.
- F11 (moves + series): a batch move that really changes a series occurrence marks it an exception and bumps that series revision once, even if multiple occurrences of the same series change in one batch (series revision +1 once per series per batch).
- F12 (upgrade continuity): stage-1/2 exports import into stage-3 (map missing revision/terms to revision 1 under policy 0 with the fixture's terms snapshot); tokens, retries and references survive.
- F13 (no new screens): none required; stage-2 UI unchanged.
- F14 (invalid `explain` vs other query validation): `explain` follows the same precedence as other query validation (before 404 restaurant check? No — stage-1 order: field validation precedes resource checks; so invalid `explain` on an unknown restaurant gives 422).

## Work items

- WI-1 (single slice): carry stage-2/ forward to stage-3/, add policies + accepted terms/revisions + explain, reservation history/decision, recurring series, combined-table history and moves-under-policies. Acceptance: official harness stage 3 passes on a fresh clone ("claimed stage: 3", stage 1..3 suites pass), plus Reviewer verification against all three ledgers.

## Status

- [ ] WI-1 dispatched to Coder; Reviewer briefed in parallel.
