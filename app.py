"""
Crate Returns - internal API for tracking reusable delivery crates.

Written in a hurry by a previous contractor. It runs, and ops has been using it
for a few weeks.
"""

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime

from flask import Flask, jsonify, request

DB = "crates.db"
DEPOSIT_PER_CRATE_INR = 50

app = Flask(__name__)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def database_is_busy(exc):
    # Python before 3.11 exposes the SQLite message but not its error name.
    error_name = getattr(exc, "sqlite_errorname", "")
    return error_name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")) or str(exc) in (
        "database is locked", "database table is locked", "database schema is locked"
    )


# ---------------------------------------------------------------- crates


@app.post("/crates")
def create_crate():
    data = request.get_json()
    with closing(conn()) as c:
        try:
            with c:
                c.execute("BEGIN IMMEDIATE")
                # Old deletions may have left history whose crate ID must stay reserved.
                highest = c.execute(
                    "SELECT MAX(id) AS highest FROM ("
                    "SELECT 0 AS id UNION ALL SELECT id FROM crates "
                    "UNION ALL SELECT crate_id FROM movements "
                    "WHERE typeof(crate_id) = 'integer')"
                ).fetchone()["highest"]
                if highest == 2**63 - 1:
                    return jsonify({
                        "error": "No higher crate ID is available; manual reconciliation is required",
                        "code": "id_space_exhausted",
                    }), 409
                crate_id = highest + 1
                c.execute(
                    "INSERT INTO crates (id, code, size, state, deposit) VALUES (?, ?, ?, ?, ?)",
                    (crate_id, data["code"], data["size"], "in_yard", data.get("deposit", 50.0)),
                )
        except sqlite3.OperationalError as exc:
            if database_is_busy(exc):
                return jsonify({"error": "Database is busy; retry crate creation"}), 503
            raise
    return jsonify({"id": crate_id}), 201


@app.get("/crates")
def list_crates():
    state = request.args.get("state")
    if state is not None and state not in ("in_yard", "with_rider", "retired"):
        return jsonify({"error": "state must be in_yard, with_rider, or retired"}), 400
    q = "SELECT * FROM crates"
    parameters = ()
    if state is not None:
        q += " WHERE state = ?"
        parameters = (state,)
    with closing(conn()) as c:
        rows = c.execute(q, parameters).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/crates/<int:crate_id>")
def get_crate(crate_id):
    c = conn()
    row = c.execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone()
    return jsonify(dict(row))


@app.patch("/crates/<int:crate_id>")
def update_crate(crate_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "A nonempty JSON object is required"}), 400
    if set(data) - {"code", "size", "state"}:
        return jsonify({"error": "Only code, size, and guarded retirement are editable"}), 400
    if "code" in data and (not isinstance(data["code"], str) or not data["code"].strip()):
        return jsonify({"error": "code must be a nonblank string"}), 400
    if "size" in data and data["size"] not in ("small", "medium", "large"):
        return jsonify({"error": "size must be small, medium, or large"}), 400
    if "state" in data and data["state"] != "retired":
        return jsonify({"error": "state can only be set to retired; use issue/return for custody"}), 400
    if not 0 < crate_id <= 2**63 - 1:
        return jsonify({"error": "Crate not found"}), 404

    with closing(conn()) as c:
        try:
            with c:
                c.execute("BEGIN IMMEDIATE")
                crate = c.execute(
                    "SELECT * FROM crates WHERE id = ?", (crate_id,)
                ).fetchone()
                if crate is None:
                    return jsonify({"error": "Crate not found"}), 404
                if "state" in data:
                    try:
                        current_holder(c, crate, datetime.now().replace(microsecond=0))
                    except CustodyConflict:
                        return jsonify({
                            "error": "Crate state or history needs reconciliation before retirement",
                            "code": "reconciliation_required",
                        }), 409
                    if crate["state"] not in ("in_yard", "retired"):
                        return jsonify({"error": "Only a crate in the yard can be retired"}), 409

                # Column names are fixed; every client-supplied value is a parameter.
                c.execute(
                    "UPDATE crates SET code = ?, size = ?, state = ? WHERE id = ?",
                    (data.get("code", crate["code"]), data.get("size", crate["size"]),
                     data.get("state", crate["state"]), crate_id),
                )
        except sqlite3.OperationalError as exc:
            if database_is_busy(exc):
                return jsonify({"error": "Database is busy; retry the update"}), 503
            raise
    return jsonify({"ok": True})


@app.delete("/crates/<int:crate_id>")
def delete_crate(crate_id):
    if not 0 < crate_id <= 2**63 - 1:
        return jsonify({"error": "Crate not found"}), 404
    with closing(conn()) as c:
        crate = c.execute("SELECT id FROM crates WHERE id = ?", (crate_id,)).fetchone()
    if crate is None:
        return jsonify({"error": "Crate not found"}), 404
    return jsonify({
        "error": "Crate records cannot be deleted; use PATCH with state=retired to retire a yard crate",
        "code": "retirement_required",
    }), 409


@app.get("/crates/<int:crate_id>/history")
def crate_history(crate_id):
    c = conn()
    rows = c.execute(
        "SELECT * FROM movements WHERE crate_id = %d ORDER BY id" % crate_id
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- riders


@app.post("/riders")
def create_rider():
    data = request.get_json()
    c = conn()
    cur = c.execute(
        "INSERT INTO riders (name, phone) VALUES (?, ?)",
        (data["name"], data["phone"]),
    )
    c.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.get("/riders")
def list_riders():
    c = conn()
    riders = c.execute("SELECT * FROM riders").fetchall()
    out = []
    for r in riders:
        issued = c.execute(
            "SELECT COUNT(*) AS n FROM movements WHERE rider_id = ? AND kind = 'issue'",
            (r["id"],),
        ).fetchone()["n"]
        returned = c.execute(
            "SELECT COUNT(*) AS n FROM movements WHERE rider_id = ? AND kind = 'return'",
            (r["id"],),
        ).fetchone()["n"]
        row = dict(r)
        row["crates_held"] = issued - returned
        out.append(row)
    return jsonify(out)


@app.get("/riders/<int:rider_id>")
def get_rider(rider_id):
    c = conn()
    rider = c.execute("SELECT * FROM riders WHERE id = ?", (rider_id,)).fetchone()
    movements = c.execute(
        "SELECT * FROM movements WHERE rider_id = ?", (rider_id,)
    ).fetchall()
    return jsonify(
        {"rider": dict(rider), "movements": [dict(m) for m in movements]}
    )


@app.get("/riders/deposits")
def rider_deposits():
    with closing(conn()) as c:
        try:
            with c:
                # One read snapshot keeps rider, crate and history data consistent.
                c.execute("BEGIN")
                riders = c.execute("SELECT id, name FROM riders ORDER BY id").fetchall()
                crates = c.execute("SELECT id, code, state FROM crates ORDER BY id").fetchall()
                movements = c.execute(
                    "SELECT m.*, r.id AS known_rider FROM movements m "
                    "LEFT JOIN riders r ON r.id = m.rider_id ORDER BY m.id"
                ).fetchall()
                at = datetime.now().replace(microsecond=0)
        except sqlite3.OperationalError as exc:
            if database_is_busy(exc):
                return jsonify({"error": "Database is busy; retry the deposit report"}), 503
            raise

    crate_ids = {crate["id"] for crate in crates}
    histories = defaultdict(list)
    orphan_movement_ids = []
    for movement in movements:
        if movement["crate_id"] not in crate_ids:
            orphan_movement_ids.append(movement["id"])
        else:
            histories[movement["crate_id"]].append(movement)

    # A duplicated physical label cannot establish separate, billable crates.
    codes = Counter(
        crate["code"].strip() for crate in crates if isinstance(crate["code"], str)
    )
    counts = {rider["id"]: 0 for rider in riders}
    invalid_crate_ids = []
    for crate in crates:
        code = crate["code"]
        if not isinstance(code, str) or not code.strip() or codes[code.strip()] != 1:
            invalid_crate_ids.append(crate["id"])
            continue
        try:
            holder = holder_from_history(crate, histories[crate["id"]], at)
        except CustodyConflict:
            invalid_crate_ids.append(crate["id"])
            continue
        if holder is not None:
            counts[holder] += 1

    complete = not (invalid_crate_ids or orphan_movement_ids)
    return jsonify({
        "currency": "INR",
        "deposit_per_crate_inr": DEPOSIT_PER_CRATE_INR,
        "complete": complete,
        "riders": [{
            "rider_id": rider["id"],
            "name": rider["name"],
            "verified_crates_held": counts[rider["id"]],
            "verified_deposit_inr": counts[rider["id"]] * DEPOSIT_PER_CRATE_INR,
            "outstanding_deposit_inr": (
                counts[rider["id"]] * DEPOSIT_PER_CRATE_INR if complete else None
            ),
        } for rider in riders],
        "reconciliation": {
            "crate_ids": invalid_crate_ids,
            "orphan_movement_ids": orphan_movement_ids,
        },
    })


# ------------------------------------------------------------- movements


class CustodyConflict(ValueError):
    """The recorded state and history cannot establish one current holder."""


def current_holder(c, crate, at):
    """Replay a complete, consistent history; never choose the latest claim."""
    movements = c.execute(
        "SELECT m.kind, m.rider_id, m.at, r.id AS known_rider "
        "FROM movements m LEFT JOIN riders r ON r.id = m.rider_id "
        "WHERE m.crate_id = ? ORDER BY m.id",
        (crate["id"],),
    ).fetchall()
    return holder_from_history(crate, movements, at)


def holder_from_history(crate, movements, at):
    """Validate the same custody rules for a single crate or a batched report."""
    if crate["state"] not in ("in_yard", "with_rider", "retired"):
        raise CustodyConflict
    holder = None
    previous_at = None
    for movement in movements:
        try:
            recorded_at = datetime.strptime(movement["at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            raise CustodyConflict from None
        if (
            recorded_at.strftime("%Y-%m-%d %H:%M:%S") != movement["at"]
            or recorded_at > at
            or (previous_at is not None and recorded_at < previous_at)
            or movement["known_rider"] is None
        ):
            raise CustodyConflict
        previous_at = recorded_at

        if movement["kind"] == "issue" and holder is None:
            holder = movement["rider_id"]
        elif movement["kind"] == "return" and holder == movement["rider_id"]:
            holder = None
        else:
            raise CustodyConflict

    expected_state = "with_rider" if holder is not None else "in_yard"
    if crate["state"] != expected_state and not (
        crate["state"] == "retired" and holder is None
    ):
        raise CustodyConflict
    return holder


def record_movement(kind):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or any(
        type(data.get(field)) is not int or not 0 < data[field] <= 2**63 - 1
        for field in ("crate_id", "rider_id")
    ):
        return jsonify({"error": "crate_id and rider_id must be positive integers"}), 400

    with closing(conn()) as c:
        try:
            with c:
                # Lock before reading: competing writers must check the committed result.
                c.execute("BEGIN IMMEDIATE")
                crate = c.execute(
                    "SELECT * FROM crates WHERE id = ?", (data["crate_id"],)
                ).fetchone()
                if crate is None:
                    return jsonify({"error": "Crate not found"}), 404
                rider = c.execute(
                    "SELECT id FROM riders WHERE id = ?", (data["rider_id"],)
                ).fetchone()
                if rider is None:
                    return jsonify({"error": "Rider not found"}), 404

                # Sample time after obtaining the lock, so queued writes stay ordered.
                at = datetime.now().replace(microsecond=0)
                try:
                    holder = current_holder(c, crate, at)
                except CustodyConflict:
                    return jsonify({
                        "error": "Crate state or history needs reconciliation before movement",
                        "code": "reconciliation_required",
                    }), 409

                if kind == "issue":
                    if crate["state"] != "in_yard":
                        return jsonify({"error": "Only a crate in the yard can be issued"}), 409
                    state = "with_rider"
                else:
                    if crate["state"] != "with_rider" or holder != data["rider_id"]:
                        return jsonify({"error": "Only the current holder can return this crate"}), 409
                    state = "in_yard"

                c.execute(
                    "INSERT INTO movements (crate_id, rider_id, kind, at) VALUES (?, ?, ?, ?)",
                    (data["crate_id"], data["rider_id"], kind, at.strftime("%Y-%m-%d %H:%M:%S")),
                )
                c.execute(
                    "UPDATE crates SET state = ? WHERE id = ?", (state, data["crate_id"])
                )
        except sqlite3.OperationalError as exc:
            if database_is_busy(exc):
                return jsonify({"error": "Database is busy; retry the movement"}), 503
            raise
    return jsonify({"ok": True}), 201


@app.post("/movements/issue")
def issue_crate():
    return record_movement("issue")


@app.post("/movements/return")
def return_crate():
    return record_movement("return")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
