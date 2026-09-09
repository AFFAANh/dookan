"""Deposit reports use disposable databases, including inconsistent legacy data."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import patch

import app as service
from seed import SCHEMA


class DepositTests(unittest.TestCase):
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
                "INSERT INTO riders VALUES (?, ?, ?)",
                [(7, "Rider with no crates", "777"), (2, "Second rider", "222"),
                 (1, "First rider", "111")],
            )
            database.executemany(
                "INSERT INTO crates VALUES (?, ?, 'medium', ?, ?)",
                [(1, "CR-1", "with_rider", 50), (2, "CR-2", "with_rider", 500),
                 (3, "CR-3", "with_rider", -10), (4, "CR-4", "in_yard", None),
                 (5, "CR-5", "retired", "invalid legacy deposit")],
            )
            database.executemany(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (?, ?, ?, ?)",
                [(1, 1, "issue", "2000-01-01 08:00:00"),
                 (2, 2, "issue", "2000-01-01 08:00:00"),
                 (2, 2, "return", "2000-01-01 09:00:00"),
                 (2, 1, "issue", "2000-01-01 10:00:00"),
                 (3, 2, "issue", "2000-01-01 08:00:00"),
                 (5, 2, "issue", "2000-01-01 08:00:00"),
                 (5, 2, "return", "2000-01-01 09:00:00")],
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

    def report(self):
        response = self.client.get("/riders/deposits")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def assert_totals(self, body, counts, complete=True, crate_ids=(), orphan_ids=()):
        self.assertEqual(body["currency"], "INR")
        self.assertEqual(body["deposit_per_crate_inr"], 50)
        self.assertIs(body["complete"], complete)
        self.assertEqual(body["reconciliation"], {
            "crate_ids": list(crate_ids), "orphan_movement_ids": list(orphan_ids)
        })
        self.assertEqual([rider["rider_id"] for rider in body["riders"]], sorted(counts))
        for rider in body["riders"]:
            count = counts[rider["rider_id"]]
            self.assertEqual(rider["verified_crates_held"], count)
            self.assertEqual(rider["verified_deposit_inr"], count * 50)
            self.assertEqual(
                rider["outstanding_deposit_inr"], count * 50 if complete else None
            )

    def replace_history(self, state, events):
        with self.connection() as database:
            database.execute("DELETE FROM movements WHERE crate_id = 1")
            database.execute("UPDATE crates SET state = ? WHERE id = 1", (state,))
            database.executemany(
                "INSERT INTO movements (crate_id, rider_id, kind, at) "
                "VALUES (1, ?, ?, ?)", events,
            )

    def test_clean_report_counts_current_custody_and_includes_zero_balance_riders(self):
        body = self.report()
        self.assert_totals(body, {1: 2, 2: 1, 7: 0})
        self.assertEqual([rider["name"] for rider in body["riders"]], [
            "First rider", "Second rider", "Rider with no crates"
        ])

    def test_empty_database_has_a_complete_empty_report(self):
        with self.connection() as database:
            for table in ("movements", "crates", "riders"):
                database.execute(f"DELETE FROM {table}")
        self.assert_totals(self.report(), {})

    def test_return_then_reissue_transfers_the_deposit_to_the_new_holder(self):
        self.assert_totals(self.report(), {1: 2, 2: 1, 7: 0})
        for kind, rider_id, counts in (
            ("return", 1, {1: 1, 2: 1, 7: 0}),
            ("issue", 7, {1: 1, 2: 1, 7: 1}),
            ("return", 7, {1: 1, 2: 1, 7: 0}),
        ):
            with self.subTest(kind=kind, rider_id=rider_id):
                response = self.client.post(
                    f"/movements/{kind}", json={"crate_id": 1, "rider_id": rider_id}
                )
                self.assertEqual(response.status_code, 201)
                self.assert_totals(self.report(), counts)

    def test_deposit_is_fixed_at_fifty_regardless_of_legacy_column_values(self):
        for legacy_value in (0, -999, 123.75, None, "not money"):
            with self.subTest(deposit=legacy_value):
                with self.connection() as database:
                    database.execute("UPDATE crates SET deposit = ?", (legacy_value,))
                self.assert_totals(self.report(), {1: 2, 2: 1, 7: 0})

    def test_duplicate_trimmed_codes_are_excluded_across_all_crate_states(self):
        for other_state in ("in_yard", "retired", "with_rider"):
            with self.subTest(other_state=other_state):
                with self.connection() as database:
                    database.execute("DELETE FROM movements WHERE crate_id = 4")
                    database.execute(
                        "UPDATE crates SET code = ?, state = ? WHERE id = 4",
                        (" \tCR-1\n", other_state),
                    )
                    if other_state == "with_rider":
                        database.execute(
                            "INSERT INTO movements (crate_id, rider_id, kind, at) "
                            "VALUES (4, 2, 'issue', '2000-01-01 08:00:00')"
                        )
                before = self.snapshot()
                self.assert_totals(
                    self.report(), {1: 1, 2: 1, 7: 0}, False, crate_ids=(1, 4)
                )
                self.assertEqual(self.snapshot(), before)

    def test_duplicate_yard_and_retired_codes_still_make_exact_balances_unavailable(self):
        with self.connection() as database:
            database.execute("UPDATE crates SET code = 'DUPLICATE' WHERE id IN (4, 5)")
        self.assert_totals(
            self.report(), {1: 2, 2: 1, 7: 0}, False, crate_ids=(4, 5)
        )

    def test_unique_code_whitespace_is_accepted_without_rewriting_the_code(self):
        with self.connection() as database:
            database.execute("UPDATE crates SET code = '  CR-1  ' WHERE id = 1")
        before = self.snapshot()
        self.assert_totals(self.report(), {1: 2, 2: 1, 7: 0})
        self.assertEqual(self.snapshot(), before)

    def test_blank_null_and_nontext_codes_require_reconciliation(self):
        for code in ("", " \t\n", None, sqlite3.Binary(b"CR-1")):
            with self.subTest(code=code):
                with self.connection() as database:
                    database.execute("UPDATE crates SET code = ? WHERE id = 1", (code,))
                self.assert_totals(
                    self.report(), {1: 1, 2: 1, 7: 0}, False, crate_ids=(1,)
                )

    def test_corrupt_custody_is_excluded_while_unrelated_verified_balances_survive(self):
        first = "2000-01-01 08:00:00"
        second = "2000-01-01 09:00:00"
        cases = [
            ("double issue", "with_rider", [(1, "issue", first), (2, "issue", second)]),
            ("wrong return", "in_yard", [(1, "issue", first), (2, "return", second)]),
            ("unmatched return before plausible issue", "with_rider",
             [(1, "return", first), (1, "issue", second)]),
            ("missing holder", "with_rider", []),
            ("yard while issued", "in_yard", [(1, "issue", first)]),
            ("retired while issued", "retired", [(1, "issue", first)]),
            ("unknown state", "lost", []),
            ("null state", None, []),
            ("missing rider", "with_rider", [(999, "issue", first)]),
            ("missing rider on completed trip", "in_yard",
             [(999, "issue", first), (999, "return", second)]),
            ("unknown movement", "in_yard", [(1, "transfer", first)]),
            ("invalid time", "with_rider", [(1, "issue", "invalid")]),
            ("null time", "with_rider", [(1, "issue", None)]),
            ("future time", "with_rider", [(1, "issue", "2999-01-01 08:00:00")]),
            ("noncanonical time", "with_rider", [(1, "issue", "2000-1-1 08:00:00")]),
            ("time running backwards", "in_yard",
             [(1, "issue", second), (1, "return", first)]),
        ]
        for label, state, events in cases:
            with self.subTest(case=label):
                self.replace_history(state, events)
                before = self.snapshot()
                self.assert_totals(
                    self.report(), {1: 1, 2: 1, 7: 0}, False, crate_ids=(1,)
                )
                self.assertEqual(self.snapshot(), before)

    def test_orphan_movements_are_reported_by_sorted_movement_id_without_guessing_debt(self):
        with self.connection() as database:
            database.executemany(
                "INSERT INTO movements VALUES (?, ?, ?, 'issue', '2000-01-01 08:00:00')",
                [(20, 999, 1), (10, None, 2), (30, "unknown crate", 999)],
            )
        before = self.snapshot()
        self.assert_totals(
            self.report(), {1: 2, 2: 1, 7: 0}, False, orphan_ids=(10, 20, 30)
        )
        self.assertEqual(self.snapshot(), before)

    def test_multiple_conflicts_are_all_reported_in_order(self):
        with self.connection() as database:
            database.execute("UPDATE crates SET code = NULL WHERE id = 3")
            database.execute("UPDATE crates SET state = 'lost' WHERE id = 1")
            database.execute(
                "INSERT INTO movements VALUES (99, 999, 2, 'issue', '2000-01-01 08:00:00')"
            )
        self.assert_totals(
            self.report(), {1: 1, 2: 0, 7: 0}, False, crate_ids=(1, 3), orphan_ids=(99,)
        )

    def test_report_uses_three_selects_in_one_read_transaction_and_never_writes(self):
        statements = []
        select_transactions = []

        class ObservedConnection(sqlite3.Connection):
            def execute(database, sql, parameters=()):
                if sql.lstrip().upper().startswith("SELECT"):
                    select_transactions.append(database.in_transaction)
                return super().execute(sql, parameters)

        def read_only_connection():
            database = sqlite3.connect(self.database, factory=ObservedConnection)
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA query_only = ON")
            database.set_trace_callback(statements.append)
            return database

        before = self.snapshot()
        with patch.object(service, "conn", read_only_connection):
            self.assert_totals(self.report(), {1: 2, 2: 1, 7: 0})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(select_transactions, [True, True, True])
        self.assertEqual(statements[0].strip().upper(), "BEGIN")
        self.assertEqual(
            len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]), 3
        )

    def test_report_keeps_one_snapshot_when_a_writer_commits_between_its_reads(self):
        with self.connection() as database:
            self.assertEqual(database.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        first_read_started = Event()
        writer_finished = Event()

        class PausedConnection(sqlite3.Connection):
            has_paused = False

            def execute(database, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.lstrip().upper().startswith("SELECT") and not database.has_paused:
                    database.has_paused = True
                    first_read_started.set()
                    if not writer_finished.wait(timeout=10):
                        raise RuntimeError("Concurrent writer did not finish")
                return result

        def paused_connection():
            database = sqlite3.connect(self.database, factory=PausedConnection)
            database.row_factory = sqlite3.Row
            return database

        def read_report():
            with service.app.test_client() as client:
                response = client.get("/riders/deposits")
                return response.status_code, response.get_json()

        with patch.object(service, "conn", paused_connection):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(read_report)
                try:
                    self.assertTrue(first_read_started.wait(timeout=5))
                    # This commit must not change any table in the ongoing report.
                    with self.connection() as writer:
                        writer.execute("BEGIN IMMEDIATE")
                        writer.execute("UPDATE crates SET state = 'in_yard' WHERE id = 1")
                        writer.execute(
                            "INSERT INTO movements (crate_id, rider_id, kind, at) "
                            "VALUES (1, 1, 'return', '2000-01-01 09:00:00')"
                        )
                        writer.execute("INSERT INTO riders VALUES (99, 'New rider', '999')")
                        writer.execute(
                            "INSERT INTO crates VALUES (99, 'CR-99', 'small', 'with_rider', 50)"
                        )
                        writer.execute(
                            "INSERT INTO movements (crate_id, rider_id, kind, at) "
                            "VALUES (99, 99, 'issue', '2000-01-01 09:00:00')"
                        )
                finally:
                    writer_finished.set()
                status, body = future.result(timeout=10)
        self.assertEqual(status, 200)
        self.assert_totals(body, {1: 2, 2: 1, 7: 0})
        self.assert_totals(self.report(), {1: 1, 2: 1, 7: 0, 99: 1})

    def test_locked_database_returns_a_retryable_error_and_preserves_all_data(self):
        def short_timeout_connection():
            database = sqlite3.connect(self.database, timeout=0.01)
            database.row_factory = sqlite3.Row
            return database

        before = self.snapshot()
        with self.connection() as writer:
            writer.execute("BEGIN EXCLUSIVE")
            with patch.object(service, "conn", short_timeout_connection):
                response = self.client.get("/riders/deposits")
            self.assertEqual(response.status_code, 503)
            self.assertIn("error", response.get_json())
        self.assertEqual(self.snapshot(), before)
        self.assert_totals(self.report(), {1: 2, 2: 1, 7: 0})


if __name__ == "__main__":
    unittest.main()
