"""Crate query and update regressions run against disposable SQLite databases."""

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


class CrateTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database = str(Path(temporary_directory.name) / "crates.db")
        database_patch = patch.object(service, "DB", self.database)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        config_patch = patch.dict(
            service.app.config, TESTING=True, PROPAGATE_EXCEPTIONS=False
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        with self.connection() as database:
            database.executescript(SCHEMA)
            database.executemany(
                "INSERT INTO crates VALUES (?, ?, 'medium', ?, 50)",
                [(1, "CR-1", "in_yard"), (2, "CR-2", "with_rider"),
                 (3, "CR-3", "retired"), (4, "CR-4", "in_yard")],
            )
            database.executemany(
                "INSERT INTO riders VALUES (?, ?, ?)",
                [(1, "First rider", "111"), (2, "Second rider", "222")],
            )
            database.execute(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (2, 1, 'issue', '2000-01-01 08:00:00')"
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

    def assert_patch_rejected(self, status, crate_id=1, **request_arguments):
        before = self.snapshot()
        response = self.client.patch(f"/crates/{crate_id}", **request_arguments)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertEqual(self.snapshot(), before)
        return response

    def install_history(self, state, events):
        with self.connection() as database:
            database.execute("DELETE FROM movements WHERE crate_id = 1")
            database.execute("UPDATE crates SET state = ? WHERE id = 1", (state,))
            database.executemany(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (1, ?, ?, ?)", events,
            )

    def test_list_all_crates_and_filter_each_valid_state(self):
        before = self.snapshot()
        response = self.client.get("/crates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["id"] for row in response.get_json()}, {1, 2, 3, 4})
        for state, expected_ids in (
            ("in_yard", {1, 4}), ("with_rider", {2}), ("retired", {3})
        ):
            with self.subTest(state=state):
                response = self.client.get("/crates", query_string={"state": state})
                self.assertEqual(response.status_code, 200)
                rows = response.get_json()
                self.assertEqual({row["id"] for row in rows}, expected_ids)
                self.assertTrue(all(row["state"] == state for row in rows))
        self.assertEqual(self.snapshot(), before)

    def test_list_rejects_invalid_states_and_sql_injection_without_leaking_rows(self):
        before = self.snapshot()
        for state in (
            "", "missing", "IN_YARD", " in_yard ",
            "in_yard' OR 1=1 --",
            "' UNION SELECT id, name, phone, 'leaked', 0 FROM riders --",
        ):
            with self.subTest(state=state):
                response = self.client.get("/crates", query_string={"state": state})
                self.assertEqual(response.status_code, 400)
                self.assertIsInstance(response.get_json(), dict)
                self.assertIn("error", response.get_json())
                for row in before["riders"]:
                    self.assertNotIn(row[1], response.get_data(as_text=True))
                for row in before["crates"]:
                    self.assertNotIn(row[1], response.get_data(as_text=True))
        self.assertEqual(self.snapshot(), before)

    def test_patch_rejects_invalid_json_and_values_without_any_writes(self):
        invalid_payloads = [None, [], "text", 1, True, {}]
        for field, values in (
            ("code", (None, True, 1, [], {}, "", " \t\n")),
            ("size", (None, True, 1, [], {}, "", "extra_large", "SMALL", " small ")),
            ("state", (None, True, 1, [], {}, "", "missing", "in_yard", "with_rider")),
        ):
            invalid_payloads.extend({field: value} for value in values)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assert_patch_rejected(
                    400, data=json.dumps(payload), content_type="application/json"
                )
        for body, content_type in (
            ("{", "application/json"), ("", "application/json"),
            ('{"code":"new"}', "text/plain"),
        ):
            with self.subTest(body=body, content_type=content_type):
                self.assert_patch_rejected(400, data=body, content_type=content_type)

    def test_patch_rejects_identity_deposit_and_sql_field_tampering_atomically(self):
        for field, value in (
            ("id", 99), ("deposit", 0), ("unknown", "value"),
            ("state = ? WHERE id != ? --", "retired"),
        ):
            with self.subTest(field=field):
                # A legitimate edit earlier in the object must not be partly applied.
                self.assert_patch_rejected(400, json={"code": "new", field: value})
        self.assert_patch_rejected(400, json={"code": "new", "size": "invalid"})
        self.assert_patch_rejected(400, json={"code": "new", "state": "in_yard"})

    def test_metadata_edits_preserve_custody_history_and_other_records(self):
        literal_code = "  CR-' ; UPDATE crates SET state = 'retired'; --  "
        for crate_id, size in ((1, "small"), (2, "medium"), (3, "large")):
            with self.subTest(crate_id=crate_id, size=size):
                before = self.snapshot()
                response = self.client.patch(
                    f"/crates/{crate_id}", json={"code": literal_code, "size": size}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {"ok": True})
                expected = before["crates"][crate_id - 1]
                before["crates"][crate_id - 1] = (
                    expected[0], literal_code, size, expected[3], expected[4]
                )
                self.assertEqual(self.snapshot(), before)

    def test_patch_missing_and_out_of_range_ids_do_not_write(self):
        for crate_id in (0, 99, 2**63, 10**100):
            with self.subTest(crate_id=crate_id):
                self.assert_patch_rejected(404, crate_id=crate_id, json={"code": "new"})

    def test_retirement_is_atomic_and_idempotent_for_consistent_yard_crates(self):
        before = self.snapshot()
        response = self.client.patch(
            "/crates/1", json={"code": "retired crate", "size": "large", "state": "retired"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        before["crates"][0] = (1, "retired crate", "large", "retired", 50)
        self.assertEqual(self.snapshot(), before)
        response = self.client.patch("/crates/1", json={"state": "retired"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        self.assertEqual(self.snapshot(), before)

    def test_held_retirement_and_manual_custody_changes_are_rejected(self):
        self.assert_patch_rejected(
            409, crate_id=2, json={"code": "new", "state": "retired"}
        )
        for crate_id in (1, 2, 3):
            for state in ("in_yard", "with_rider"):
                with self.subTest(crate_id=crate_id, state=state):
                    self.assert_patch_rejected(400, crate_id=crate_id, json={"state": state})

    def test_completed_legacy_trips_allow_retirement_and_repeated_retirement(self):
        events = [(1, "issue", "2000-01-01 08:00:00"),
                  (1, "return", "2000-01-01 09:00:00")]
        for state in ("in_yard", "retired"):
            with self.subTest(state=state):
                self.install_history(state, events)
                before = self.snapshot()
                response = self.client.patch("/crates/1", json={"state": "retired"})
                self.assertEqual(response.status_code, 200)
                before["crates"][0] = (1, "CR-1", "medium", "retired", 50)
                self.assertEqual(self.snapshot(), before)

    def test_retirement_refuses_contradictory_legacy_history_without_repair(self):
        first = "2000-01-01 08:00:00"
        second = "2000-01-01 09:00:00"
        cases = [
            ("yard with unresolved issue", "in_yard", [(1, "issue", first)]),
            ("retired with unresolved issue", "retired", [(1, "issue", first)]),
            ("with rider without history", "with_rider", []),
            ("unmatched return", "in_yard", [(1, "return", first)]),
            ("wrong rider return", "in_yard",
             [(1, "issue", first), (2, "return", second)]),
            ("duplicate issue", "with_rider",
             [(1, "issue", first), (2, "issue", second)]),
            ("missing rider in completed trip", "retired",
             [(99, "issue", first), (99, "return", second)]),
            ("invalid timestamp", "in_yard", [(1, "issue", "invalid")]),
            ("unknown state", "missing", []),
            ("null state", None, []),
        ]
        for label, state, events in cases:
            with self.subTest(case=label):
                self.install_history(state, events)
                response = self.assert_patch_rejected(
                    409, json={"code": "new", "state": "retired"}
                )
                self.assertEqual(response.get_json()["code"], "reconciliation_required")

    def test_database_failure_rolls_back_metadata_and_retirement_and_releases_lock(self):
        with self.connection() as database:
            database.execute(
                "CREATE TRIGGER reject_retirement BEFORE UPDATE OF state ON crates "
                "WHEN NEW.state = 'retired' "
                "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
            )
        payload = {"code": "new", "size": "large", "state": "retired"}
        with patch.object(service.app.logger, "disabled", True):
            self.assert_patch_rejected(500, json=payload)
        # A new writer can immediately take the lock after the failed request.
        with self.connection() as database:
            database.execute("DROP TRIGGER reject_retirement")
        response = self.client.patch("/crates/1", json=payload)
        self.assertEqual(response.status_code, 200)

    def test_busy_database_returns_retryable_error_without_partial_writes(self):
        def short_timeout_connection():
            database = sqlite3.connect(self.database, timeout=0.01)
            database.row_factory = sqlite3.Row
            return database

        payload = {"code": "new", "state": "retired"}
        with self.connection() as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(service, "conn", short_timeout_connection):
                self.assert_patch_rejected(503, json=payload)
        self.assertEqual(self.client.patch("/crates/1", json=payload).status_code, 200)

    def test_concurrent_retirement_and_issue_choose_exactly_one_outcome(self):
        barrier = Barrier(2)

        def send_request(operation):
            with service.app.test_client() as client:
                barrier.wait(timeout=5)
                if operation == "retire":
                    response = client.patch("/crates/1", json={"state": "retired"})
                else:
                    response = client.post(
                        "/movements/issue", json={"crate_id": 1, "rider_id": 2}
                    )
                return operation, response.status_code

        before = self.snapshot()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = dict(executor.map(send_request, ("retire", "issue")))
        self.assertIn(outcomes, ({"retire": 200, "issue": 409},
                                 {"retire": 409, "issue": 201}))
        after = self.snapshot()
        self.assertEqual(after["crates"][1:], before["crates"][1:])
        self.assertEqual(after["riders"], before["riders"])
        if outcomes["retire"] == 200:
            self.assertEqual(after["crates"][0][3], "retired")
            self.assertEqual(after["movements"], before["movements"])
        else:
            self.assertEqual(after["crates"][0][3], "with_rider")
            self.assertEqual(after["movements"][:-1], before["movements"])
            self.assertEqual(after["movements"][-1][1:4], (1, 2, "issue"))


if __name__ == "__main__":
    unittest.main()
