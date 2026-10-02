# Tablekeeper stage 1 — requirements ledger

Increment: stage-1. Folder: /workspace/band-work/final-result/stage-1/
Base revision: b75c47f (factory scaffolding, no product code).

## Numbered requirements (testable statements)

### Delivery / runtime
1. The service is delivered as source + Dockerfile + RUN.md in stage-1/; the Dockerfile builds and the container starts with `-e PORT=<port>` and a port mapping, no manual setup, no compose required.
2. Listens on 0.0.0.0, `PORT` env var, default 8080.
3. `GET /health` returns 200 `{"status":"ok"}` once the store can serve; within 60 s of start.
4. Operates within 2 vCPU / 2 GiB; handles up to 50 concurrent requests; per-request timeout 5 s (10 s for `POST /_test/reset`); no outbound network at runtime; all runtime deps baked into the image.
5. State is ephemeral (may be in-memory or on container-local disk); must survive concurrent requests but not container restarts.

### Conventions / errors
6. All request/response bodies are `application/json; charset=utf-8`.
7. Response timestamps are RFC 3339 with explicit offset (e.g. `2026-09-24T19:00:00+02:00`).
8. Unknown request-body fields and unknown query parameters are ignored, never errors.
9. IDs (everywhere, including reset fixtures) are opaque strings ≤ 64 characters.
10. Every 4xx/5xx response has body `{"error":{"code":...,"message":...}}` with the specified status and code.
11. `malformed_request` (400) only for unparseable bodies or fields of the wrong JSON type; invalid values/formats of correct type are 422 `validation_failed` (unless an endpoint specifies another code).
12. Integer query params must be plain decimal digits: `1e9`, `4.0`, `+4` are 422.
13. `Idempotency-Key` values outside 1..255 chars are 422 `validation_failed`.
14. No 5xx under any request, including concurrent load.

### Model / fixtures
15. `POST /_test/reset` replaces ALL state with the fixture; returns 204; repeatable; unauthenticated; enabled in the delivered image.
16. After reset 204, subsequent requests see only the fixture.
17. Fixture shape honored: users (id, email, password, display_name), restaurants (id, name, timezone, slot_minutes, reservation_duration_minutes, cancellation_cutoff_minutes, opening_hours per weekday, tables with id/label/capacity), reservations seeded with POST fields + id/reference/user_id.
18. Seeded users can log in immediately with the given password.
19. Bookings are not rejected merely for starting in the past; cutoff rules still apply.
20. `opening_hours` weekday ∈ {mon,tue,wed,thu,fri,sat,sun}; opens/closes local HH:MM; closes > opens on the same local day; missing day = closed.

### Auth
21. `POST /auth/signup` {email,password,display_name} → 201 {user_id,display_name,token}; email taken → 409 `email_taken`; password <8 chars → 422; malformed email → 422.
22. `POST /auth/login` {email,password} → 200 {user_id,display_name,token}; wrong credentials → 401 `unauthenticated`.
23. All other endpoints require `Authorization: Bearer <token>` except /health, /_test/reset, /_test/export, /_test/import, /auth/*, GET /restaurants, GET /restaurants/{id}, GET /availability.
24. Missing/malformed/unknown token → 401 `unauthenticated`; valid token touching another user's resource → 403 `forbidden` (where specified) or 404 `not_found` (reservations, which must not leak existence).
25. Tokens never expire; multiple concurrent tokens per account allowed.
26. Passwords stored with a password-hashing function (bcrypt/scrypt/Argon2 or equivalent); never plaintext.

### Idempotency (POST /reservations, POST /reservation-moves)
27. Missing/empty Idempotency-Key → 400 `missing_idempotency_key`.
28. Key is scoped per authenticated user.
29. Replay = same user, same key, same method+path+body (JSON-value equality); same key different body → 409 `idempotency_key_reuse`, resolved after JSON parse + auth but before field validation/resource checks.
30. First use → 201; replay → 200 with byte-for-JSON-value-identical original response body.
31. Key reuse after a 4xx failure counts as first use.
32. Concurrent identical requests with an unused key: exactly one 201, others 200 with same body; effect occurs once.
33. Replay after the resource changed/cancelled still returns the original response and makes no state changes.

### API
34. `GET /restaurants` (public) → {restaurants:[{id,name,timezone}...]}.
35. `GET /restaurants/{id}` (public) → restaurant with slot_minutes, reservation_duration_minutes, cancellation_cutoff_minutes, opening_hours, tables, fixture shape; unknown → 404.
36. `GET /availability` (public): restaurant_id, date, party_size all required (missing → 422); date is local calendar date; returns restaurant_id, date, timezone, slots.
37. Slots: every slot_minutes step from opens with slot+duration ≤ closes; each slot has starts_at_local (YYYY-MM-DDTHH:MM), starts_at (RFC3339 w/ offset), available_table_ids (capacity ≥ party_size, no overlapping confirmed reservation, fixture order); empty list still shown; closed day → slots: [].
38. `POST /reservations` (auth + idempotency) → 201 with reservation_id, reference, restaurant_id, table_id, party_size, status "confirmed", starts_at_local, starts_at, ends_at, created_at.
39. `reference` is 6–12 chars of A-Z0-9, globally unique, immutable.
40. Overlap on the same table (half-open [start, start+duration)) → 409 `table_unavailable`, including under concurrency (exactly one of two simultaneous conflicting requests succeeds).
41. `starts_at_local` not on the slot grid (from opens, step slot_minutes) → 422 `not_on_slot_grid`.
42. Slot outside opening hours or reservation ends after closes → 422 `outside_opening_hours`.
43. party_size > table capacity → 422 `party_exceeds_capacity`.
44. party_size < 1 or non-integer → 422 `validation_failed`.
45. Nonexistent local time (DST spring-forward gap) → 422 `invalid_local_time`.
46. Unknown restaurant / unknown table / table of another restaurant → 404 `not_found`.
47. `GET /reservations` → caller's reservations, starts_at descending, confirmed + cancelled, {reservations:[...]}; empty → {"reservations":[]}.
48. `GET /reservations/{reference}` → one reservation; 404 if not the caller's.
49. `POST /reservations/{reference}/cancel` → 200 {reference,status:"cancelled",...}; frees the table immediately (slot reappears in availability); double cancel → 200 current state; within/after cutoff → 409 `cutoff_passed`; not caller's → 404.
50. `PATCH /reservations/{reference}` accepts any subset of table_id, starts_at_local, party_size; no idempotency key; validation identical to POST; cutoff measured against current start (409 `cutoff_passed`); cancelled → 409 `reservation_cancelled`; success releases old and reserves new atomically; failure leaves original unchanged; reference/reservation_id survive.
51. Retries and rejected requests create no duplicate or partial bookings.

### DST / time
52. Berlin spring forward 2026-03-29 02:00→03:00: 02:00–02:59 local never in availability, booking → 422 `invalid_local_time`.
53. Berlin fall back 2026-10-25 03:00→02:00: repeated hour resolves to FIRST occurrence; slot appears once; second occurrence not bookable.
54. New York transitions 2026-03-08 and 2026-11-01 handled per the same rules.
55. `reservation_duration_minutes` is absolute time: a 90-min booking starting 01:30 on fall-back night ends at local 02:00 (90 real minutes).
56. Offsets follow IANA rules for the zone and date.

### Export / import
57. `GET /_test/export` (unauthenticated) → 200 {track:"tablekeeper", format_version:1, state:<opaque>}; atomic read-only snapshot.
58. `POST /_test/import` takes that object, atomically replaces state, → 204; repeatable; accepts this service's unchanged export.
59. Invalid JSON import → §5 errors; wrong track/version/invalid state/missing fields → 422 `validation_failed` with no destination change.
60. Import preserves accounts + hashed-password login, bearer tokens, fixture config, reservations, references, completed idempotent request bodies and original responses; identities/statuses/timestamps not regenerated; failed keys remain reusable; receipts/references/tokens/retries valid after import.
61. Import removes all previous destination data; reset clears everything including imported state.

### Batch moves
62. `POST /reservation-moves` (auth + idempotency): {moves:[1..8 objects, distinct references]}; invalid shape/duplicates → 422 `validation_failed`.
63. All bookings must be caller's and same restaurant: unknown/other owner's reference → 404 `not_found`; different restaurants → 422 `validation_failed`; no token → 401.
64. Per-item fields table_id/starts_at_local/party_size; omitted retain current; unknown ignored; identity/owner/created_at unchanged.
65. Cancelled booking in batch → 409 `reservation_cancelled`; each booking's existing cutoff applies; non-occupancy errors use ordinary amendment codes in input order, cutoff errors preceding other changes for that booking; resulting overlap (among batch or with unlisted) → 409 `table_unavailable`; unchanged listed bookings retain occupancy.
66. All-or-nothing: occupancy, records and retry keys commit together or not at all.
67. Success → 201 {reservations:[...]} in input order including unchanged items; replay → 200 original response even after later amendments/cancellations; export/import preserves batch receipts and bookings.

## Ambiguities and decisions

- D1 (storage): single-process in-memory or embedded store (e.g. SQLite in WAL mode or in-memory structures with a global lock/transaction per write). Must make conflicting concurrent creates atomic — a single-writer lock around availability check + insert is acceptable within the limits.
- D2 (auth on reservations visibility): spec explicitly says 404 (not 403) for other people's reservations (req 24, 48, 49, 63); 403 `forbidden` remains in the shared table for cases where a resource is visible to authenticated callers but not theirs — no stage-1 endpoint is specified to use it, so we never return 403 in stage 1.
- D3 (party_size wrong types): spec carves out invalid party_size values (including strings and booleans) as 422 `validation_failed` (not 400), even though they are "wrong JSON type".
- D4 (query integer rule): applies to integer query parameters such as party_size on /availability.
- D5 (ordering of checks): auth → idempotency resolution → field validation → 404 resource checks → rule checks (grid, opening hours, capacity, local time) → availability/conflict. The spec fixes auth before idempotency and idempotency before field validation; 404 resource checks come after field validation per §7 wording ("current-resource checks").
- D6 (past bookings): allowed; "now" for cutoff comparisons can be real wall-clock time; seeded reservations in the past are accepted as-is.
- D7 (reference generation): random 8-char A-Z0-9 with uniqueness check, retry on collision.
- D8 (availability with past dates): still computed; no requirement to hide past slots.
- D9 (import/export state format): implementation-defined; export must include hashed passwords, tokens, reservations, idempotency receipts including batch receipts.
- D10 (cutoff on PATCH/moves): measured against the CURRENT start time of the booking before changes (spec: "measured against the current start time").
- D11 (DST grid): the slot grid is wall-clock from opens in local time; a slot whose local time doesn't exist (spring-forward gap) never appears and cannot be booked; fall-back first occurrence is chosen (fold=0 in Python terms).
- D12 (language): Coder's choice per spec §2; anything containerized and HTTP is valid.

## Work items

- WI-1 (single slice): implement the complete stage-1 service in /workspace/band-work/final-result/stage-1/ per this ledger — Dockerfile, RUN.md, source, Coder's own tests. Acceptance: official harness check passes on a fresh clone ("claimed stage: 1", all stage 1 suites pass), plus the Coder's tests and Reviewer verification against this ledger.

## Status

- [ ] WI-1 dispatched to Coder; Reviewer briefed in parallel.
