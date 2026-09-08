"""
Crate Returns - internal API for tracking reusable delivery crates.

Written in a hurry by a previous contractor. It runs, and ops has been using it
for a few weeks.
"""

import sqlite3
from datetime import datetime

from flask import Flask, jsonify, request

DB = "crates.db"

app = Flask(__name__)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


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
    c = conn()
    q = "SELECT * FROM crates"
    if state:
        q += " WHERE state = '%s'" % state
    rows = c.execute(q).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/crates/<int:crate_id>")
def get_crate(crate_id):
    c = conn()
    row = c.execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone()
    return jsonify(dict(row))


@app.patch("/crates/<int:crate_id>")
def update_crate(crate_id):
    data = request.get_json()
    c = conn()
    for field, value in data.items():
        c.execute("UPDATE crates SET %s = ? WHERE id = ?" % field, (value, crate_id))
    c.commit()
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


@app.post("/movements/issue")
def issue_crate():
    data = request.get_json()
    c = conn()
    c.execute(
        "INSERT INTO movements (crate_id, rider_id, kind, at) VALUES (?, ?, 'issue', ?)",
        (
            data["crate_id"],
            data["rider_id"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    c.execute(
        "UPDATE crates SET state = 'with_rider' WHERE id = ?", (data["crate_id"],)
    )
    c.commit()
    return jsonify({"ok": True}), 201


@app.post("/movements/return")
def return_crate():
    data = request.get_json()
    c = conn()
    c.execute(
        "INSERT INTO movements (crate_id, rider_id, kind, at) VALUES (?, ?, 'return', ?)",
        (
            data["crate_id"],
            data["rider_id"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    c.execute("UPDATE crates SET state = 'in_yard' WHERE id = ?", (data["crate_id"],))
    c.commit()
    return jsonify({"ok": True}), 201


if __name__ == "__main__":
    app.run(debug=True, port=5000)
