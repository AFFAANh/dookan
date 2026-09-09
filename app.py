"""
Crate Returns - internal API for tracking reusable delivery crates.

Written in a hurry by a previous contractor. It runs, and ops has been using it
for a few weeks.
"""

import sqlite3
from contextlib import closing
from datetime import datetime

from flask import Flask, jsonify, request

DB = "crates.db"

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
    c = conn()
    cur = c.execute(
        "INSERT INTO crates (code, size, state, deposit) VALUES (?, ?, ?, ?)",
        (data["code"], data["size"], "in_yard", data.get("deposit", 50.0)),
    )
    c.commit()
    return jsonify({"id": cur.lastrowid}), 201


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
    c = conn()
    c.execute("DELETE FROM crates WHERE id = ?", (crate_id,))
    c.commit()
    return jsonify({"ok": True})


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


# ------------------------------------------------------------- movements


class CustodyConflict(ValueError):
    """The recorded state and history cannot establish one current holder."""


def current_holder(c, crate, at):
    """Replay a complete, consistent history; never choose the latest claim."""
    if crate["state"] not in ("in_yard", "with_rider", "retired"):
        raise CustodyConflict

    movements = c.execute(
        "SELECT m.kind, m.rider_id, m.at, r.id AS known_rider "
        "FROM movements m LEFT JOIN riders r ON r.id = m.rider_id "
        "WHERE m.crate_id = ? ORDER BY m.id",
        (crate["id"],),
    ).fetchall()
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
