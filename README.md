# Crate Returns

Internal API for tracking reusable delivery crates issued to riders.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python seed.py
python app.py
```

Serves on `http://localhost:5000`.

## Endpoints

```
POST   /crates
GET    /crates?state=in_yard
GET    /crates/<id>
PATCH  /crates/<id>
DELETE /crates/<id>
GET    /crates/<id>/history

POST   /riders
GET    /riders
GET    /riders/deposits
GET    /riders/<id>

POST   /movements/issue     {"crate_id": 1, "rider_id": 4}
POST   /movements/return    {"crate_id": 1, "rider_id": 4}
```

## The rule ops cares about

A crate is always in exactly one of three states: `in_yard`, `with_rider`, or
`retired`.

## Movement rules (fix 1)

Issue and return requests require positive integer `crate_id` and `rider_id`
values identifying existing records. Only yard crates can be issued, and only
the current holder can return a crate. Retired crates cannot move. A successful
request still returns `201` with `{"ok": true}`.

The API checks custody while holding a SQLite write lock, then records the
movement and state change in one transaction. Concurrent claims are checked
one at a time; a duplicate issue or return is rejected rather than recorded
again. A failed write rolls back the transaction.

| Status | Meaning |
| --- | --- |
| `400` | Invalid JSON body or IDs (positive integers up to SQLite's signed 64-bit maximum are required). |
| `404` | Crate or rider does not exist. |
| `409` | Invalid transition, wrong holder, or history requiring reconciliation. |
| `503` | Database lock could not be acquired; retry the movement. |

Existing databases need no schema migration for this fix. Custody is determined
by replaying the crate's entire movement history in ID order. Every issue must
start in the yard and every return must match its holder. Rider references,
timestamps, and the resulting stored state must agree. Timestamps must use
`YYYY-MM-DD HH:MM:SS`, be nondecreasing, and not be in the future; equal timestamps
are allowed because events have second precision and IDs preserve write order.

Contradictory histories return `409` with `"code": "reconciliation_required"`.
This includes seeded examples such as crate 7's two open issues. The service
preserves those records for ops to reconcile; it does not select the latest
claim, repair history automatically, or rebuild the database. The unchanged
`seed.py` remains a destructive demo-data generator, not a repair command.

Fix 1 covers the two movement endpoints. Fix 2 below protects filtering and
PATCH, including retirement. Fix 3 preserves crate records and reserves IDs
referenced by history. The deposit report below uses these custody rules.

## Crate filtering and updates (fix 2)

`GET /crates` lists all crates. An optional `state` filter accepts only
`in_yard`, `with_rider`, or `retired`; an empty or unknown value returns `400`.
Filter values are bound SQL parameters.

`PATCH /crates/<id>` requires a nonempty JSON object. The editable fields are:

| Field | Accepted value |
| --- | --- |
| `code` | A nonblank string, stored as supplied. |
| `size` | `small`, `medium`, or `large`. |
| `state` | `retired`, subject to the custody checks below. |

IDs, deposits, and all other fields are rejected with `400`. Invalid fields or
values reject the entire request, including any valid edits in the same body.
Column names are fixed in the UPDATE statement and values are bound parameters.
Metadata edits preserve the crate's ID, deposit, state, and movement history.
Code uniqueness remains a deferred schema issue from the review.

Retire a crate using:

```http
PATCH /crates/1
Content-Type: application/json

{"state": "retired"}
```

Only a crate verified to be in the yard can become retired. Retirement uses
the same history checks and write lock as movements, so an issue and retirement
cannot both claim a yard crate. A held crate returns `409`; contradictory data
returns `409` with `"code": "reconciliation_required"`. Retrying retirement of
a consistently retired crate succeeds without changing its history. PATCH
cannot set `in_yard` or `with_rider`, or revive a retired crate; use the movement
endpoints for issues and returns.

Metadata edits may accompany retirement and are committed together or rolled
back together. Successful PATCH returns `200` with `{"ok": true}`, missing
crates return `404`, and database lock contention returns `503`. Retirement
changes the stored state; a separate retirement audit event is still a deferred
audit improvement.

## Preserve crate identities (fix 3)

`DELETE /crates/<id>` returns `409` with `"code": "retirement_required"` for
every existing crate, including yard stock and retired crates. It changes
neither the crate nor its history. Missing or out-of-range IDs return `404`.
To remove a crate from service, use the guarded PATCH retirement shown above;
DELETE does not implicitly retire it. Even an unused crate keeps its record
so its identity cannot be reassigned through the API.

Creation returns `201` with the new ID as before. A new ID is allocated above
both the existing crate IDs and all integer crate references in movement
history, including orphaned history from earlier deletions. Allocation and
insertion hold one SQLite write lock so concurrent creates receive different
IDs. For example, if the highest crate ID is 3 but history references deleted
crate 9, the next crate receives ID 10, with no inherited movements.

If the highest reserved ID is SQLite's signed 64-bit maximum, creation returns
`409` with `"code": "id_space_exhausted"` rather than reusing a lower ID.
Database lock contention returns `503`; a failed insert rolls back. No schema
migration is required.

These changes preserve future API records and avoid reconnecting orphaned
history. They cannot reconstruct already-deleted crates, repair histories
already attached to reused IDs, or discover old IDs without surviving records.
Historical reconciliation and database constraints remain deferred findings.

## Outstanding deposits (Part C)

`GET /riders/deposits` returns every rider in ID order, including riders holding
zero crates. Amounts are integer rupees: each currently held crate contributes
₹50. The report uses validated current custody, not total issues minus returns
or the legacy, editable-on-creation `deposit` column. It does not track payments
collected or refunds paid.

For example, with two crates held by rider 1 and none by rider 2:

```json
{
  "currency": "INR",
  "deposit_per_crate_inr": 50,
  "complete": true,
  "riders": [
    {"rider_id": 1, "name": "Rider A", "verified_crates_held": 2, "verified_deposit_inr": 100, "outstanding_deposit_inr": 100},
    {"rider_id": 2, "name": "Rider B", "verified_crates_held": 0, "verified_deposit_inr": 0, "outstanding_deposit_inr": 0}
  ],
  "reconciliation": {"crate_ids": [], "orphan_movement_ids": []}
}
```

Issue increases the current holder's amount by ₹50; return removes it. Reissue
assigns the amount to the new holder. Yard and consistently retired crates
contribute nothing. Completed trips do not accumulate extra liability.

The endpoint reads riders, crates, and movements with three queries inside one
read transaction, so concurrent writes cannot mix old state with new history.
It makes no changes and uses the same history validation as issue/return.
Completeness describes consistency of recorded custody, not independently
verified physical stock or payment receipts.

If any crate's history/state or physical label is unresolved, `complete` is
`false`. All `outstanding_deposit_inr` values become `null`, because unresolved
custody can affect any rider. `verified_crates_held` and `verified_deposit_inr`
remain available as partial results; they are not final balances to collect.
Crates with duplicate labels are all excluded from those verified subtotals,
even when one duplicate is in the yard or retired. Label comparison trims outer
whitespace; blank/non-string labels are also excluded. The stored labels are
never changed by the report.

`reconciliation.crate_ids` lists affected crates, and
`reconciliation.orphan_movement_ids` lists movements referencing missing crates.
The supplied corrupt seed therefore returns an incomplete report with these
IDs, without silently repairing data or assigning the latest claimant.
Successful reports, including incomplete ones, return `200`; a database lock
failure returns `503`. The older `GET /riders` event-balance calculation remains
unchanged; use this report for deposit totals only when `complete` is `true`.

To inspect it in PowerShell while the app is running:

```powershell
Invoke-RestMethod 'http://localhost:5000/riders/deposits' | ConvertTo-Json -Depth 6
```

## Tests

After installing `requirements.txt`, run:

```bash
python -B -m unittest discover -s tests -v
```

On Windows, use `py -3.12` in place of `python` if needed. Tests use temporary
databases and do not run the seeder against your application database.
