"""Unit tests for durable atomic Scout evaluation spend ledger."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import sqlite3
import threading

import pytest

from app.scout.ledger import (
    SpendCapExceeded,
    SpendLedger,
    compute_cost_usd,
)
from app.storage.db import migrate, open_database


@pytest.fixture
def ledger_db(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    conn = open_database(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()
    return db_path


def test_migration_011_creates_ledger_tables(ledger_db):
    with sqlite3.connect(ledger_db) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert {"scout_eval_experiments", "scout_eval_reservations"} <= tables
    assert "011_scout_eval_ledger.sql" in applied


def test_zero_cap_allows_free_and_rejects_paid(ledger_db):
    ledger = SpendLedger(ledger_db)
    ledger.ensure_experiment("exp-zero", hard_spend_cap_usd=Decimal("0.00"), config_hash="a" * 64)

    res = ledger.reserve(
        experiment_id="exp-zero",
        profile_name="ling",
        model_id="inclusionai/ling-3.2-flash:free",
        example_id="ex-1",
        attempt_key="ex-1:ling:0",
        worst_case_usd=Decimal("0"),
    )
    assert res.status == "pending"
    assert res.reserved_usd == Decimal("0")

    reconciled = ledger.reconcile(
        res.reservation_id,
        actual_usd=Decimal("0"),
        input_tokens=120,
        output_tokens=45,
        resolved_model="inclusionai/ling-3.2-flash:free",
    )
    assert reconciled.status == "reconciled"
    assert reconciled.charged_usd == Decimal("0")

    with pytest.raises(SpendCapExceeded):
        ledger.reserve(
            experiment_id="exp-zero",
            profile_name="paid",
            model_id="example/paid-1",
            example_id="ex-2",
            attempt_key="ex-2:paid:0",
            worst_case_usd=Decimal("0.000001"),
        )


def test_exact_cap_succeeds_and_one_unit_overrun_rejected(ledger_db):
    ledger = SpendLedger(ledger_db)
    # 1000 input @ $0.15/1M + 1000 output @ $0.60/1M = $0.000750
    exact_cost = compute_cost_usd(
        input_tokens=1000,
        output_tokens=1000,
        input_usd_per_million_tokens=Decimal("0.15"),
        output_usd_per_million_tokens=Decimal("0.60"),
    )
    assert exact_cost == Decimal("0.00075")

    ledger.ensure_experiment("exp-exact", hard_spend_cap_usd=exact_cost, config_hash="b" * 64)
    res = ledger.reserve(
        experiment_id="exp-exact",
        profile_name="paid",
        model_id="example/paid-1",
        example_id="ex-1",
        attempt_key="ex-1:paid:0",
        worst_case_usd=exact_cost,
    )
    assert res.status == "pending"

    # Even 1 pico-USD (10^-12 USD) over exact cap must fail atomically.
    with pytest.raises(SpendCapExceeded):
        ledger.reserve(
            experiment_id="exp-exact",
            profile_name="paid",
            model_id="example/paid-1",
            example_id="ex-2",
            attempt_key="ex-2:paid:0",
            worst_case_usd=Decimal("0.000000000001"),
        )


def test_ambiguous_retained_and_release_only_pre_send(ledger_db):
    ledger = SpendLedger(ledger_db)
    ledger.ensure_experiment("exp-states", hard_spend_cap_usd=Decimal("0.0010"), config_hash="c" * 64)

    r1 = ledger.reserve(
        experiment_id="exp-states",
        profile_name="paid",
        model_id="example/paid-1",
        example_id="ex-1",
        attempt_key="ex-1:paid:0",
        worst_case_usd=Decimal("0.0008"),
    )
    # Timeout/indeterminate failure keeps the reservation as 'ambiguous'.
    ambiguous = ledger.mark_ambiguous(r1.reservation_id, failure_disposition="timeout")
    assert ambiguous.status == "ambiguous"

    # Ambiguous spend remains encumbered against the cap.
    with pytest.raises(SpendCapExceeded):
        ledger.reserve(
            experiment_id="exp-states",
            profile_name="paid",
            model_id="example/paid-1",
            example_id="ex-2",
            attempt_key="ex-2:paid:0",
            worst_case_usd=Decimal("0.0003"),
        )

    # Pre-send rejection releases its reservation cleanly.
    r2 = ledger.reserve(
        experiment_id="exp-states",
        profile_name="paid",
        model_id="example/paid-1",
        example_id="ex-2",
        attempt_key="ex-2:paid:1",
        worst_case_usd=Decimal("0.0002"),
    )
    released = ledger.release(r2.reservation_id, failure_disposition="endpoint_unavailable")
    assert released.status == "released"

    # Released budget is available again, while ambiguous remains encumbered.
    r3 = ledger.reserve(
        experiment_id="exp-states",
        profile_name="paid",
        model_id="example/paid-1",
        example_id="ex-3",
        attempt_key="ex-3:paid:0",
        worst_case_usd=Decimal("0.0002"),
    )
    ledger.reconcile(
        r3.reservation_id,
        actual_usd=Decimal("0.00015"),
        input_tokens=200,
        output_tokens=200,
        resolved_model="example/paid-1",
    )

    summary = ledger.summary("exp-states")
    assert summary.ambiguous_reservations == 1
    assert summary.released_reservations == 1
    assert summary.reconciled_reservations == 1
    assert summary.pending_reservations == 0
    assert summary.ambiguous_reserved_usd == Decimal("0.0008")
    assert summary.reconciled_cost_usd == Decimal("0.00015")
    assert summary.remaining_budget_usd == Decimal("0.00005")


def test_no_ledger_reset_across_profile_switches_retries_or_resumes(ledger_db):
    first = SpendLedger(ledger_db)
    first.ensure_experiment("exp-shared", hard_spend_cap_usd=Decimal("0.0010"), config_hash="1" * 64)
    r1 = first.reserve(
        experiment_id="exp-shared",
        profile_name="profile_a",
        model_id="example/model-a",
        example_id="ex-1",
        attempt_key="ex-1:profile_a:0",
        worst_case_usd=Decimal("0.0006"),
    )
    first.reconcile(
        r1.reservation_id,
        actual_usd=Decimal("0.0006"),
        input_tokens=500,
        output_tokens=500,
        resolved_model="example/model-a",
    )

    # Re-open ledger (resume) and switch to profile_b under the same experiment_id.
    resumed = SpendLedger(ledger_db)
    resumed.ensure_experiment("exp-shared", hard_spend_cap_usd=Decimal("0.0010"), config_hash="2" * 64)

    # Cannot raise the cap on an existing experiment.
    with pytest.raises(ValueError, match="spend_cap_cannot_increase"):
        resumed.ensure_experiment("exp-shared", hard_spend_cap_usd=Decimal("0.0050"), config_hash="3" * 64)

    # Retry / second profile shares remaining $0.0004 budget.
    with pytest.raises(SpendCapExceeded):
        resumed.reserve(
            experiment_id="exp-shared",
            profile_name="profile_b",
            model_id="example/model-b",
            example_id="ex-1",
            attempt_key="ex-1:profile_b:0",
            worst_case_usd=Decimal("0.0005"),
        )

    r2 = resumed.reserve(
        experiment_id="exp-shared",
        profile_name="profile_b",
        model_id="example/model-b",
        example_id="ex-1",
        attempt_key="ex-1:profile_b:1",
        worst_case_usd=Decimal("0.0004"),
    )
    assert r2.status == "pending"
    summary = resumed.summary("exp-shared")
    assert summary.reconciled_cost_usd == Decimal("0.0006")
    assert summary.pending_reserved_usd == Decimal("0.0004")
    assert summary.remaining_budget_usd == Decimal("0")


def test_concurrent_threaded_reservations_enforce_cap_atomically(ledger_db):
    ledger = SpendLedger(ledger_db)
    ledger.ensure_experiment("exp-threads", hard_spend_cap_usd=Decimal("0.0010"), config_hash="d" * 64)

    workers = 10
    barrier = threading.Barrier(workers)

    def attempt(idx: int) -> bool:
        thread_ledger = SpendLedger(ledger_db)
        barrier.wait()
        try:
            thread_ledger.reserve(
                experiment_id="exp-threads",
                profile_name="paid",
                model_id="example/paid-1",
                example_id=f"ex-{idx}",
                attempt_key=f"ex-{idx}:paid:0",
                worst_case_usd=Decimal("0.0003"),
            )
            return True
        except SpendCapExceeded:
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(attempt, range(workers)))

    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 7
    summary = ledger.summary("exp-threads")
    assert summary.pending_reservations == 3
    assert summary.pending_reserved_usd == Decimal("0.0009")
    assert summary.remaining_budget_usd == Decimal("0.0001")
