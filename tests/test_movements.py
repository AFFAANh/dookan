"""Custody regressions use disposable databases, never the operational database."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import app as service
from seed import SCHEMA


class MovementTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database = str(Path(temporary_directory.name) / "crates.db")
        database_patch = patch.object(service, "DB", self.database)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        config_patch = patch.dict(
            service.app.config,
            TESTING=True,
            PROPAGATE_EXCEPTIONS=False,
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        with self.connection() as database:
            database.executescript(SCHEMA)
            database.executemany(
                "INSERT INTO crates VALUES (?, ?, 'medium', ?, 50)",
                [(1, "CR-1", "in_yard"), (2, "CR-2", "in_yard"),
                 (3, "CR-3", "retired")],
            )
            database.executemany(
                "INSERT INTO riders VALUES (?, ?, ?)",
                [(1, "First rider", "111"), (2, "Second rider", "222"),
                 (3, "Third rider", "333")],
            )
        self.client = service.app.test_client()

    @contextmanager
    def connection(self):
        database = sqlite3.connect(self.database, timeout=1)
        try:
            with database:
                yield database
        finally:
            database.close()

    def snapshot(self):
        with self.connection() as database:
            return {
                table: database.execute(
                    f"SELECT * FROM {table} ORDER BY id"
                ).fetchall()
                for table in ("crates", "riders", "movements")
            }

    def movement(self, kind, crate_id=1, rider_id=1):
        return self.client.post(
            f"/movements/{kind}",
            json={"crate_id": crate_id, "rider_id": rider_id},
        )

    def assert_rejected(self, kind, status, **request_arguments):
        before = self.snapshot()
        response = self.client.post(f"/movements/{kind}", **request_arguments)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertEqual(self.snapshot(), before)
        return response

    def install_history(self, state, events):
        """Represent existing starter data without going through the guarded API."""
        with self.connection() as database:
            database.execute("DELETE FROM movements")
            database.execute("UPDATE crates SET state = ? WHERE id = 1", (state,))
            database.executemany(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (1, ?, ?, ?)",
                events,
            )

    def test_issue_return_and_reissue_transfer_custody(self):
        for kind, rider_id, expected_state in (
            ("issue", 1, "with_rider"),
            ("return", 1, "in_yard"),
            ("issue", 2, "with_rider"),
            ("return", 2, "in_yard"),
        ):
            with self.subTest(kind=kind, rider_id=rider_id):
                before = self.snapshot()
                response = self.movement(kind, rider_id=rider_id)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.get_json(), {"ok": True})
                after = self.snapshot()
                self.assertEqual(after["crates"][0][3], expected_state)
                self.assertEqual(after["crates"][1:], before["crates"][1:])
                self.assertEqual(after["riders"], before["riders"])
                self.assertEqual(after["movements"][:-1], before["movements"])
                self.assertEqual(after["movements"][-1][1:4], (1, rider_id, kind))

    def test_invalid_json_shapes_and_identifiers_are_rejected_without_writes(self):
        invalid_payloads = [None, [], "text", 1, True, {}, {"crate_id": 1},
                            {"rider_id": 1}]
        for field in ("crate_id", "rider_id"):
            for value in (None, True, False, "1", 1.0, [], {}, 0, -1, 2**63):
                payload = {"crate_id": 1, "rider_id": 1}
                payload[field] = value
                invalid_payloads.append(payload)
        for kind in ("issue", "return"):
            for payload in invalid_payloads:
                with self.subTest(kind=kind, payload=payload):
                    # Explicit serialization ensures None is JSON null, not no body.
                    self.assert_rejected(
                        kind, 400, data=json.dumps(payload),
                        content_type="application/json",
                    )
            for body, content_type in (
                ("{", "application/json"),
                ("", "application/json"),
                ('{"crate_id":1,"rider_id":1}', "text/plain"),
            ):
                with self.subTest(kind=kind, body=body, content_type=content_type):
                    self.assert_rejected(kind, 400, data=body, content_type=content_type)

    def test_missing_crates_and_riders_are_rejected_without_writes(self):
        for kind in ("issue", "return"):
            for crate_id, rider_id in ((99, 1), (1, 99), (99, 99)):
                with self.subTest(kind=kind, crate_id=crate_id, rider_id=rider_id):
                    self.assert_rejected(
                        kind, 404, json={"crate_id": crate_id, "rider_id": rider_id}
                    )

    def test_duplicate_issue_and_wrong_rider_return_preserve_current_holder(self):
        self.assertEqual(self.movement("issue").status_code, 201)
        for kind, rider_id in (("issue", 1), ("issue", 2), ("return", 2)):
            with self.subTest(kind=kind, rider_id=rider_id):
                self.assert_rejected(
                    kind, 409, json={"crate_id": 1, "rider_id": rider_id}
                )
        self.assertEqual(self.movement("return").status_code, 201)

    def test_unissued_repeated_and_retired_movements_are_rejected(self):
        self.assert_rejected("return", 409, json={"crate_id": 1, "rider_id": 1})
        self.assertEqual(self.movement("issue").status_code, 201)
        self.assertEqual(self.movement("return").status_code, 201)
        self.assert_rejected("return", 409, json={"crate_id": 1, "rider_id": 1})
        for kind in ("issue", "return"):
            self.assert_rejected(kind, 409, json={"crate_id": 3, "rider_id": 1})

    def test_ambiguous_legacy_history_is_not_silently_repaired(self):
        first = "2000-01-01 08:00:00"
        second = "2000-01-01 09:00:00"
        cases = [
            ("same rider double issue", "with_rider",
             [(1, "issue", first), (1, "issue", second)]),
            ("different riders double issue", "with_rider",
             [(1, "issue", first), (2, "issue", second)]),
            ("unmatched return", "in_yard", [(1, "return", first)]),
            ("wrong rider return", "in_yard",
             [(1, "issue", first), (2, "return", second)]),
            ("missing rider", "with_rider", [(99, "issue", first)]),
            ("missing rider in completed trip", "in_yard",
             [(99, "issue", first), (99, "return", second)]),
            ("unknown movement", "in_yard", [(1, "transfer", first)]),
            ("invalid timestamp", "with_rider", [(1, "issue", "not a timestamp")]),
            ("impossible calendar date", "with_rider",
             [(1, "issue", "2000-02-30 08:00:00")]),
            ("noncanonical timestamp", "with_rider",
             [(1, "issue", "2000-1-1 08:00:00")]),
            ("missing timestamp", "with_rider", [(1, "issue", None)]),
            ("future timestamp", "with_rider", [(1, "issue", "2999-01-01 08:00:00")]),
            ("decreasing timestamps", "in_yard",
             [(1, "issue", second), (1, "return", first)]),
            ("yard with unresolved issue", "in_yard", [(1, "issue", first)]),
            ("with rider without history", "with_rider", []),
            ("with rider after completed trip", "with_rider",
             [(1, "issue", first), (1, "return", second)]),
            ("invalid state", "missing", []),
            ("null state", None, []),
            ("old contradiction followed by plausible latest issue", "with_rider",
             [(1, "return", first), (1, "issue", second)]),
        ]
        for label, state, events in cases:
            self.install_history(state, events)
            for kind in ("issue", "return"):
                with self.subTest(case=label, kind=kind):
                    response = self.assert_rejected(
                        kind, 409, json={"crate_id": 1, "rider_id": 1}
                    )
                    self.assertEqual(response.get_json()["code"], "reconciliation_required")

    def test_consistent_legacy_trips_allow_only_the_current_holder_to_return(self):
        self.install_history("with_rider", [
            (1, "issue", "2000-01-01 08:00:00"),
            (1, "return", "2000-01-01 09:00:00"),
            (2, "issue", "2000-01-01 09:00:00"),
        ])
        self.assert_rejected("return", 409, json={"crate_id": 1, "rider_id": 1})
        self.assertEqual(self.movement("return", rider_id=2).status_code, 201)
        after = self.snapshot()
        self.assertEqual(after["crates"][0][3], "in_yard")
        self.assertEqual(after["movements"][-1][1:4], (1, 2, "return"))
        self.assertEqual(self.movement("issue", rider_id=3).status_code, 201)

    def test_database_write_failures_roll_back_both_changes_and_release_the_lock(self):
        for kind in ("issue", "return"):
            for operation in ("INSERT ON movements", "UPDATE ON crates"):
                with self.subTest(kind=kind, operation=operation):
                    self.install_history("in_yard", [])
                    if kind == "return":
                        self.assertEqual(self.movement("issue").status_code, 201)
                    with self.connection() as database:
                        database.execute(
                            f"CREATE TRIGGER reject_write BEFORE {operation} "
                            "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
                        )
                    with patch.object(service.app.logger, "disabled", True):
                        self.assert_rejected(
                            kind, 500, json={"crate_id": 1, "rider_id": 1}
                        )
                    # An independent writer must acquire the lock immediately.
                    with self.connection() as database:
                        database.execute("DROP TRIGGER reject_write")
                    self.assertEqual(self.movement(kind).status_code, 201)

    def test_busy_database_returns_retryable_error_without_partial_writes(self):
        def short_timeout_connection():
            database = sqlite3.connect(self.database, timeout=0.01)
            database.row_factory = sqlite3.Row
            return database

        for kind in ("issue", "return"):
            with self.subTest(kind=kind):
                self.install_history("in_yard", [])
                if kind == "return":
                    self.assertEqual(self.movement("issue").status_code, 201)
                with self.connection() as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    with patch.object(service, "conn", short_timeout_connection):
                        self.assert_rejected(
                            kind, 503, json={"crate_id": 1, "rider_id": 1}
                        )
                self.assertEqual(self.movement(kind).status_code, 201)

    def concurrent_movements(self, kind, rider_ids):
        barrier = Barrier(len(rider_ids))

        def request_from_rider(rider_id):
            with service.app.test_client() as client:
                barrier.wait(timeout=5)
                response = client.post(
                    f"/movements/{kind}", json={"crate_id": 1, "rider_id": rider_id}
                )
                return rider_id, response.status_code

        with ThreadPoolExecutor(max_workers=len(rider_ids)) as executor:
            return list(executor.map(request_from_rider, rider_ids))

    def test_concurrent_issues_choose_exactly_one_holder(self):
        outcomes = self.concurrent_movements("issue", [1, 2])
        self.assertEqual(sorted(status for _, status in outcomes), [201, 409])
        winner = next(rider_id for rider_id, status in outcomes if status == 201)
        after = self.snapshot()
        self.assertEqual(after["crates"][0][3], "with_rider")
        self.assertEqual(len(after["movements"]), 1)
        self.assertEqual(after["movements"][0][1:4], (1, winner, "issue"))

    def test_concurrent_returns_record_exactly_one_return(self):
        self.assertEqual(self.movement("issue").status_code, 201)
        outcomes = self.concurrent_movements("return", [1, 1])
        self.assertEqual(sorted(status for _, status in outcomes), [201, 409])
        after = self.snapshot()
        self.assertEqual(after["crates"][0][3], "in_yard")
        self.assertEqual([row[3] for row in after["movements"]], ["issue", "return"])


if __name__ == "__main__":
    unittest.main()
