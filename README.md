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

This first fix covers the two movement endpoints. The PATCH/lifecycle bypass
and DELETE/identity-loss problems remain pending as fixes 2 and 3 from
`FINDINGS.md`. Retirement creation and the deposit endpoint are not added here.

## Tests

After installing `requirements.txt`, run:

```bash
python -B -m unittest discover -s tests -v
```

On Windows, use `py -3.12` in place of `python` if needed. Tests use temporary
databases and do not run the seeder against your application database.
