"""Identity and history retention regressions use disposable SQLite databases."""

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


class IdentityTests(unittest.TestCase):
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
            database.executemany(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (?, ?, ?, ?)",
                [(1, 1, "issue", "2000-01-01 08:00:00"),
                 (1, 1, "return", "2000-01-01 09:00:00"),
                 (2, 1, "issue", "2000-01-01 08:00:00"),
                 (3, 2, "issue", "2000-01-01 08:00:00"),
                 (3, 2, "return", "2000-01-01 09:00:00"),
                 # A legacy yard crate still has an unresolved issue.
                 (4, 2, "issue", "2000-01-01 08:00:00")],
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

    def create(self, code="NEW"):
        return self.client.post("/crates", json={"code": code, "size": "medium"})

    def assert_delete_rejected(self, crate_id, status=409):
        before = self.snapshot()
        response = self.client.delete(f"/crates/{crate_id}")
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertEqual(self.snapshot(), before)
        if status == 409:
            body = response.get_json()
            self.assertEqual(body["code"], "retirement_required")
            self.assertIn("PATCH", body["error"])
            self.assertIn("retir", body["error"].lower())
        return response

    def test_delete_repeatedly_preserves_yard_held_retired_and_corrupt_crates(self):
        # Even a no-op UPDATE or a rolled-back DELETE must not be attempted.
        with self.connection() as database:
            for operation in ("UPDATE", "DELETE"):
                database.execute(
                    f"CREATE TRIGGER reject_{operation.lower()} BEFORE {operation} "
                    "ON crates BEGIN SELECT RAISE(ABORT, 'unexpected write'); END"
                )
        for crate_id in (1, 2, 3, 4):
            with self.subTest(crate_id=crate_id):
                self.assert_delete_rejected(crate_id)
                self.assert_delete_rejected(crate_id)

    def test_delete_preserves_records_with_invalid_or_null_state(self):
        for state in ("unknown", None):
            with self.subTest(state=state):
                with self.connection() as database:
                    database.execute("UPDATE crates SET state = ? WHERE id = 4", (state,))
                self.assert_delete_rejected(4)

    def test_delete_missing_and_out_of_range_ids_returns_not_found_without_writes(self):
        for crate_id in (0, 99, 2**63, 10**100):
            with self.subTest(crate_id=crate_id):
                self.assert_delete_rejected(crate_id, 404)

    def test_explicit_retirement_then_delete_keeps_record_and_completed_history(self):
        before = self.snapshot()
        response = self.client.patch("/crates/1", json={"state": "retired"})
        self.assertEqual(response.status_code, 200)
        before["crates"][0] = (1, "CR-1", "medium", "retired", 50)
        self.assertEqual(self.snapshot(), before)
        self.assert_delete_rejected(1)
        self.assertEqual(self.snapshot(), before)

    def test_rejected_delete_of_highest_issued_crate_cannot_transfer_its_history(self):
        response = self.create("OLD-HIGHEST")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {"id": 5})
        response = self.client.post(
            "/movements/issue", json={"crate_id": 5, "rider_id": 2}
        )
        self.assertEqual(response.status_code, 201)
        before = self.snapshot()
        self.assert_delete_rejected(5)
        response = self.create("NEW-HIGHEST")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {"id": 6})
        after = self.snapshot()
        self.assertEqual(after["crates"][:-1], before["crates"])
        self.assertEqual(after["crates"][-1], (6, "NEW-HIGHEST", "medium", "in_yard", 50))
        self.assertEqual(after["movements"], before["movements"])
        self.assertEqual(after["riders"], before["riders"])
        self.assertEqual(after["movements"][-1][1:4], (5, 2, "issue"))
        self.assertFalse(any(row[1] == 6 for row in after["movements"]))

    def test_creation_reserves_orphan_ids_even_without_existing_crates(self):
        for keep_crates in (False, True):
            for reference in (99, "99", 99.0):
                with self.subTest(keep_crates=keep_crates, reference=reference):
                    with self.connection() as database:
                        database.execute("DELETE FROM movements")
                        database.execute("DELETE FROM crates")
                        if keep_crates:
                            database.execute(
                                "INSERT INTO crates VALUES (4, 'OLD', 'small', 'in_yard', 50)"
                            )
                        database.execute(
                            "INSERT INTO movements (crate_id, rider_id, kind, at) "
                            "VALUES (?, 1, 'issue', '2000-01-01 08:00:00')", (reference,)
                        )
                        # INTEGER affinity preserves valid numeric text/integral reals.
                        self.assertEqual(
                            database.execute(
                                "SELECT crate_id, typeof(crate_id) FROM movements"
                            ).fetchone(), (99, "integer")
                        )
                    before = self.snapshot()
                    response = self.create()
                    self.assertEqual(response.status_code, 201)
                    self.assertEqual(response.get_json(), {"id": 100})
                    before["crates"].append((100, "NEW", "medium", "in_yard", 50))
                    self.assertEqual(self.snapshot(), before)

    def test_creation_starts_at_one_with_empty_or_nonpositive_identity_history(self):
        for negative_history in (False, True):
            with self.subTest(negative_history=negative_history):
                with self.connection() as database:
                    database.execute("DELETE FROM movements")
                    database.execute("DELETE FROM crates")
                    if negative_history:
                        database.execute(
                            "INSERT INTO crates VALUES (-2, 'LEGACY', 'small', 'retired', 50)"
                        )
                        database.execute(
                            "INSERT INTO movements (crate_id, rider_id, kind, at) "
                            "VALUES (-1, 1, 'issue', '2000-01-01 08:00:00')"
                        )
                before = self.snapshot()
                response = self.create()
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.get_json(), {"id": 1})
                before["crates"].append((1, "NEW", "medium", "in_yard", 50))
                self.assertEqual(self.snapshot(), before)

    def test_exhausted_crate_or_orphan_identity_space_does_not_reuse_random_id(self):
        maximum_id = 2**63 - 1
        for source in ("crates", "movements"):
            with self.subTest(source=source):
                with self.connection() as database:
                    database.execute("DELETE FROM crates WHERE id = ?", (maximum_id,))
                    database.execute("DELETE FROM movements WHERE crate_id = ?", (maximum_id,))
                    if source == "crates":
                        database.execute(
                            "INSERT INTO crates VALUES (?, 'LAST', 'small', 'retired', 50)",
                            (maximum_id,),
                        )
                    else:
                        database.execute(
                            "INSERT INTO movements (crate_id, rider_id, kind, at) "
                            "VALUES (?, 1, 'issue', '2000-01-01 08:00:00')", (maximum_id,)
                        )
                before = self.snapshot()
                for _ in range(2):
                    response = self.create()
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.get_json()["code"], "id_space_exhausted")
                    self.assertEqual(self.snapshot(), before)
                # Exhaustion must also release the writer lock.
                with self.connection() as database:
                    database.execute("BEGIN IMMEDIATE")

    def test_creation_failure_rolls_back_and_releases_lock(self):
        with self.connection() as database:
            database.execute(
                "CREATE TRIGGER reject_insert BEFORE INSERT ON crates "
                "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
            )
        before = self.snapshot()
        with patch.object(service.app.logger, "disabled", True):
            response = self.create()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.snapshot(), before)
        with self.connection() as database:
            database.execute("DROP TRIGGER reject_insert")
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {"id": 5})

    def test_creation_lock_contention_is_retryable_without_partial_writes(self):
        def short_timeout_connection():
            database = sqlite3.connect(self.database, timeout=0.01)
            database.row_factory = sqlite3.Row
            return database

        before = self.snapshot()
        with self.connection() as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(service, "conn", short_timeout_connection):
                response = self.create()
            self.assertEqual(response.status_code, 503)
            self.assertIn("error", response.get_json())
            self.assertEqual(self.snapshot(), before)
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {"id": 5})

    def test_concurrent_creates_get_distinct_ids_above_orphan_history(self):
        with self.connection() as database:
            database.execute(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (99, 1, 'issue', '2000-01-01 08:00:00')"
            )
        before = self.snapshot()
        barrier = Barrier(2)

        def send_request(code):
            with service.app.test_client() as client:
                barrier.wait(timeout=5)
                response = client.post("/crates", json={"code": code, "size": "medium"})
                return code, response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(send_request, ("FIRST", "SECOND")))
        self.assertEqual([status for _, status, _ in outcomes], [201, 201])
        self.assertEqual(sorted(body["id"] for _, _, body in outcomes), [100, 101])
        before["crates"].extend(sorted(
            (body["id"], code, "medium", "in_yard", 50) for code, _, body in outcomes
        ))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
