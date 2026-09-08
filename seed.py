"""Rebuild crates.db and fill it with roughly a month of ops data."""

import os
import random
import sqlite3
from datetime import datetime, timedelta

DB = "crates.db"

SCHEMA = """
CREATE TABLE crates (
    id INTEGER PRIMARY KEY,
    code TEXT,
    size TEXT,
    state TEXT,
    deposit REAL
);

CREATE TABLE riders (
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT
);

CREATE TABLE movements (
    id INTEGER PRIMARY KEY,
    crate_id INTEGER,
    rider_id INTEGER,
    kind TEXT,
    at TEXT
);
"""

FIRST = [
    "Imran", "Suresh", "Pawan", "Rakesh", "Devendra", "Sunil", "Manoj",
    "Vikas", "Naveen", "Arif", "Santosh", "Dinesh", "Firoz", "Lokesh",
]
LAST = [
    "Kumar", "Yadav", "Sharma", "Patil", "Reddy", "Shaikh", "Verma",
    "Gowda", "Das", "Nair",
]
SIZES = ["small", "medium", "large"]


def main():
    if os.path.exists(DB):
        os.remove(DB)

    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    random.seed(7)

    for i in range(1, 1201):
        c.execute(
            "INSERT INTO crates (id, code, size, state, deposit) VALUES (?, ?, ?, ?, ?)",
            (i, "CR-%04d" % i, random.choice(SIZES), "in_yard", 50.0),
        )

    for i in range(1, 61):
        c.execute(
            "INSERT INTO riders (id, name, phone) VALUES (?, ?, ?)",
            (
                i,
                "%s %s" % (random.choice(FIRST), random.choice(LAST)),
                "9%09d" % random.randrange(10**9),
            ),
        )

    # A month of issues and returns.
    start = datetime(2026, 8, 5, 7, 30)
    movement_id = 0
    for day in range(30):
        for _ in range(random.randint(40, 70)):
            movement_id += 1
            crate_id = random.randint(1, 1200)
            rider_id = random.randint(1, 60)
            out = start + timedelta(days=day, minutes=random.randint(0, 300))
            c.execute(
                "INSERT INTO movements (id, crate_id, rider_id, kind, at) VALUES (?, ?, ?, 'issue', ?)",
                (movement_id, crate_id, rider_id, out.strftime("%Y-%m-%d %H:%M:%S")),
            )
            if random.random() < 0.88:
                movement_id += 1
                back = out + timedelta(hours=random.randint(4, 30))
                c.execute(
                    "INSERT INTO movements (id, crate_id, rider_id, kind, at) VALUES (?, ?, ?, 'return', ?)",
                    (movement_id, crate_id, rider_id, back.strftime("%Y-%m-%d %H:%M:%S")),
                )
                c.execute("UPDATE crates SET state = 'in_yard' WHERE id = ?", (crate_id,))
            else:
                c.execute(
                    "UPDATE crates SET state = 'with_rider' WHERE id = ?", (crate_id,)
                )

    # Retire the crates ops reported as cracked.
    for crate_id in (12, 88, 341, 902):
        c.execute("UPDATE crates SET state = 'retired' WHERE id = ?", (crate_id,))

    # Ops asked for these to be entered manually last week.
    movement_id += 1
    c.execute(
        "INSERT INTO movements (id, crate_id, rider_id, kind, at) VALUES (?, 7, 14, 'issue', '2026-09-02 08:10:00')",
        (movement_id,),
    )
    movement_id += 1
    c.execute(
        "INSERT INTO movements (id, crate_id, rider_id, kind, at) VALUES (?, 7, 39, 'issue', '2026-09-02 09:45:00')",
        (movement_id,),
    )
    c.execute("UPDATE crates SET state = 'with_rider' WHERE id = 7")

    movement_id += 1
    c.execute(
        "INSERT INTO movements (id, crate_id, rider_id, kind, at) VALUES (?, 12, 22, 'issue', '2026-09-03 07:55:00')",
        (movement_id,),
    )

    movement_id += 1
    c.execute(
        "INSERT INTO movements (id, crate_id, rider_id, kind, at) VALUES (?, 31, 51, 'return', '2026-09-04 18:20:00')",
        (movement_id,),
    )

    c.execute(
        "INSERT INTO crates (code, size, state, deposit) VALUES ('CR-0500', 'medium', 'in_yard', 50.0)"
    )

    c.commit()

    crates = c.execute("SELECT COUNT(*) FROM crates").fetchone()[0]
    riders = c.execute("SELECT COUNT(*) FROM riders").fetchone()[0]
    movements = c.execute("SELECT COUNT(*) FROM movements").fetchone()[0]
    print("seeded %d crates, %d riders, %d movements" % (crates, riders, movements))


if __name__ == "__main__":
    main()
