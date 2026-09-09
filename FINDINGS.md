# Findings — Part A

Reviewed every supplied file: `app.py`, `seed.py`, `README.md`, and `requirements.txt`. Ranked by damage to custody and recoverability, then operational impact. Findings 1–3 were selected for the three fixes. This review describes the original starter; line references use that version.

1. **Movements do not enforce custody** (`app.py:134–167`). Two issues for one crate both return 201 and record different riders as holding it. Wrong-rider returns make it yard stock and produce negative holdings; repeated returns, retired-crate movements, and nonexistent IDs also succeed. Existing writes share a transaction, but state, ownership, and concurrency guards are absent.

2. **Untrusted query input permits bulk corruption and lifecycle bypass** (`app.py:39–64`). PATCH accepts arbitrary columns, including state and ID; null state leaves a crate outside the three allowed states. The key `state = ? WHERE id != ? --` updates other crates. GET's interpolated state filter accepts UNION queries exposing rider records. Fix scope: parameterized values, explicit editable fields, and guarded lifecycle changes.

3. **Deletion erases identity and can reassign history** (`app.py:67–72`; `seed.py:11–30`). Deleting an issued crate leaves its movements behind. Reproduction: delete highest crate ID 3, create a replacement, and SQLite reuses 3; the replacement inherits the old issue history. Preserve tracked identities and use controlled retirement so loss investigations remain possible.

4. **The schema accepts invalid physical identities and relationships** (`seed.py:10–31`; `app.py:18–21`). There are no unique crate codes, required fields, state/kind checks, foreign keys, or explicit current-holder representation. The seed creates two `CR-0500` labels; separate rows can represent one physical crate in different states. Database constraints and an existing-data migration policy are missing.

5. **Existing demo history is already contradictory** (`seed.py:69–126`). Crate 7 has two unresolved riders; retired crate 12 receives an issue; crate 31 gets an unmatched return. Random generation ignores unresolved custody and inserts future returns immediately. A fresh run produces 1,201 crates and 3,206 movements. Neither insertion order nor latest timestamp safely determines every actual holder.

6. **Rider balances count events, not currently held crates** (`app.py:99–116`). Seeded issues minus returns total 222, versus 114 crates marked `with_rider`; neither establishes verified ownership. Duplicate, unrelated, retired, and deleted movements distort balances. Creation/PATCH also accept arbitrary deposits, including negative values, despite the fixed ₹50 requirement; money uses unconstrained floating-point storage.

7. **Setup can destroy the operational database** (`seed.py:45–50`; `README.md:7–13`). The documented seed command deletes an existing database before rebuilding it, without backup or confirmation. Both scripts use a working-directory-relative path, so launching elsewhere selects another database. There is no nondestructive schema-upgrade workflow.

8. **Invalid requests and missing records give misleading responses** (`app.py:27–36,50–64,87–96,119–128`). Missing keys, wrong JSON shapes, invalid columns, and nonexistent resource reads can raise 500s. Missing-record PATCH/DELETE instead report success. Field types, blank identifiers, supported sizes, and deposit values lack validation; clients cannot reliably distinguish rejection from success.

9. **Deployment and connection handling are fragile** (`app.py:18–21,170–171`). Connections have no explicit close/rollback lifecycle, leaving cleanup and failed-write locks to garbage collection. Startup enables the development debugger. Application authentication and actor attribution are absent; reachability and any external access controls are unknown, so public exposure is not assumed.

10. **Reads scale with history unnecessarily** (`app.py:99–116`; `seed.py:10–31`). Listing 60 riders executes 121 queries, repeatedly counting unindexed movements. Collection and history endpoints are unbounded, increasing latency and response size as operations grow.

11. **Audit timing and regression protection are weak** (`app.py:79,143,162`; `seed.py:77–97`; `requirements.txt:1`). Naive local timestamps and ID-ordered history can disagree with event chronology; retirement and metadata edits have no audit events. No tests protect the invariant, and the open-ended Flask requirement leaves installations unreproducible.

## Decisions and validation

Assume retirement requires a crate in the yard and is terminal. Outstanding deposit means ₹50 per currently held crate, not a payment ledger. Ambiguous legacy custody requires explicit reconciliation; do not silently choose the latest issue or reseed operational data. Lower-ranked issues remain deferred under the three-fix limit.

Verified failures using Flask's test client and disposable SQLite databases outside the repo; also ran the unmodified seeder against a temporary database. No operational database was touched.

Time spent: estimated at 6–8.5 hours for Parts A–C (review, three fixes, feature, tests, and documentation); walkthrough preparation is pending.
