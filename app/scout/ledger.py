"""Durable, atomic SQLite spend ledger for Scout OpenRouter evaluations."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
import sqlite3
import time
from typing import Iterator, Literal
from uuid import uuid4

from pydantic import Field

from app.contracts.common import Nonempty, StrictModel
from app.scout.eval_config import NonNegativeDecimal
from app.scout.run import TokenBudgetExceeded
from app.storage.db import open_database

PICO_PER_USD = 10**12
_PICO_SCALE = Decimal(PICO_PER_USD)


class SpendCapExceeded(TokenBudgetExceeded):
    """Atomic ledger reservation rejected because the experiment spend cap would be exceeded."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def usd_to_pico_ceil(amount: Decimal) -> int:
    """Convert a nonnegative finite USD Decimal to integer pico-USD, rounding up."""
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise ValueError("amount must be a finite nonnegative Decimal")
    return int((amount * _PICO_SCALE).to_integral_value(rounding=ROUND_CEILING))


def usd_to_pico_floor(amount: Decimal) -> int:
    """Convert a nonnegative finite USD Decimal to integer pico-USD, rounding down."""
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise ValueError("amount must be a finite nonnegative Decimal")
    return int((amount * _PICO_SCALE).to_integral_value(rounding=ROUND_FLOOR))


def pico_to_usd(pico: int) -> Decimal:
    """Convert integer pico-USD to an exact Decimal without scientific notation."""
    if type(pico) is not int or pico < 0:
        raise ValueError("pico must be a nonnegative int")
    if pico == 0:
        return Decimal("0")
    formatted = f"{Decimal(pico) / _PICO_SCALE:.12f}".rstrip("0").rstrip(".")
    return Decimal(formatted or "0")


def compute_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_usd_per_million_tokens: Decimal,
    output_usd_per_million_tokens: Decimal,
) -> Decimal:
    """Compute exact USD cost from token counts and USD-per-million-token rates."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be nonnegative")
    raw = (
        Decimal(input_tokens) * input_usd_per_million_tokens
        + Decimal(output_tokens) * output_usd_per_million_tokens
    ) / Decimal(1_000_000)
    return pico_to_usd(usd_to_pico_ceil(raw))


ReservationStatus = Literal["pending", "reconciled", "ambiguous", "released"]


class ReservationRecord(StrictModel):
    """One persisted reservation row in the durable experiment spend ledger."""

    reservation_id: Nonempty
    experiment_id: Nonempty
    profile_name: Nonempty
    model_id: Nonempty
    example_id: Nonempty
    attempt_key: Nonempty
    status: ReservationStatus
    reserved_usd: NonNegativeDecimal
    charged_usd: NonNegativeDecimal | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    resolved_model: Nonempty | None = None
    failure_disposition: Nonempty | None = None
    created_at: Nonempty
    updated_at: Nonempty


class LedgerSummary(StrictModel):
    """Aggregate spend and reservation state for one experiment ledger."""

    experiment_id: Nonempty
    hard_spend_cap_usd: NonNegativeDecimal
    pending_reservations: int = Field(ge=0)
    reconciled_reservations: int = Field(ge=0)
    ambiguous_reservations: int = Field(ge=0)
    released_reservations: int = Field(ge=0)
    pending_reserved_usd: NonNegativeDecimal
    ambiguous_reserved_usd: NonNegativeDecimal
    reconciled_cost_usd: NonNegativeDecimal
    encumbered_usd: NonNegativeDecimal
    remaining_budget_usd: NonNegativeDecimal


def _row_to_record(row: tuple) -> ReservationRecord:
    (
        reservation_id,
        experiment_id,
        profile_name,
        model_id,
        example_id,
        attempt_key,
        status,
        reserved_pico,
        charged_pico,
        input_tokens,
        output_tokens,
        resolved_model,
        failure_disposition,
        created_at,
        updated_at,
    ) = row
    return ReservationRecord(
        reservation_id=reservation_id,
        experiment_id=experiment_id,
        profile_name=profile_name,
        model_id=model_id,
        example_id=example_id,
        attempt_key=attempt_key,
        status=status,
        reserved_usd=pico_to_usd(reserved_pico),
        charged_usd=pico_to_usd(charged_pico) if charged_pico is not None else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        resolved_model=resolved_model,
        failure_disposition=failure_disposition,
        created_at=created_at,
        updated_at=updated_at,
    )


_SELECT_RESERVATION = (
    "SELECT reservation_id, experiment_id, profile_name, model_id, example_id, attempt_key, "
    "status, reserved_pico, charged_pico, input_tokens, output_tokens, resolved_model, "
    "failure_disposition, created_at, updated_at FROM scout_eval_reservations"
)


class SpendLedger:
    """Atomic SQLite spend ledger using BEGIN IMMEDIATE across threads, profiles, and resumes."""

    def __init__(self, target: Path | sqlite3.Connection) -> None:
        if isinstance(target, Path):
            self._db_path: Path | None = target
            self._shared_conn: sqlite3.Connection | None = None
        elif isinstance(target, sqlite3.Connection):
            self._db_path = None
            self._shared_conn = target
        else:
            raise TypeError("SpendLedger target must be a Path or sqlite3.Connection")

    @contextmanager
    def _immediate_tx(self) -> Iterator[sqlite3.Connection]:
        if self._shared_conn is not None:
            conn = self._shared_conn
            if conn.in_transaction:
                raise ValueError("ledger operation requires an idle connection")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return

        assert self._db_path is not None
        deadline = time.monotonic() + 10.0
        while True:
            conn: sqlite3.Connection | None = None
            try:
                conn = open_database(self._db_path)
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as error:
                if conn is not None:
                    conn.close()
                if ("locked" in str(error).lower() or "busy" in str(error).lower()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                raise
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _read_conn(self) -> Iterator[sqlite3.Connection]:
        if self._shared_conn is not None:
            yield self._shared_conn
            return
        assert self._db_path is not None
        conn = open_database(self._db_path)
        try:
            yield conn
        finally:
            conn.close()

    def ensure_experiment(
        self,
        experiment_id: str,
        *,
        hard_spend_cap_usd: Decimal,
        config_hash: str,
    ) -> None:
        """Register an experiment cap without ever resetting or increasing an existing cap."""
        if not experiment_id or not experiment_id.strip():
            raise ValueError("experiment_id must be nonempty")
        cap_pico = usd_to_pico_floor(hard_spend_cap_usd)
        cap_str = str(pico_to_usd(cap_pico))
        now = _now_iso()
        with self._immediate_tx() as conn:
            row = conn.execute(
                "SELECT hard_spend_cap_pico FROM scout_eval_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO scout_eval_experiments("
                    "experiment_id, hard_spend_cap_pico, hard_spend_cap_usd, config_hash, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (experiment_id, cap_pico, cap_str, config_hash, now, now),
                )
            else:
                existing_cap_pico = row[0]
                if cap_pico > existing_cap_pico:
                    raise ValueError("spend_cap_cannot_increase")
                conn.execute(
                    "UPDATE scout_eval_experiments SET hard_spend_cap_pico = ?, hard_spend_cap_usd = ?, "
                    "config_hash = ?, updated_at = ? WHERE experiment_id = ?",
                    (cap_pico, cap_str, config_hash, now, experiment_id),
                )

    def reserve(
        self,
        *,
        experiment_id: str,
        profile_name: str,
        model_id: str,
        example_id: str,
        attempt_key: str,
        worst_case_usd: Decimal,
    ) -> ReservationRecord:
        """Atomically reserve worst-case spend for one attempt against the experiment cap."""
        reserve_pico = usd_to_pico_ceil(worst_case_usd)
        reserve_str = str(pico_to_usd(reserve_pico))
        reservation_id = str(uuid4())
        now = _now_iso()
        with self._immediate_tx() as conn:
            exp_row = conn.execute(
                "SELECT hard_spend_cap_pico FROM scout_eval_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if exp_row is None:
                raise ValueError(f"unknown_experiment: {experiment_id}")
            cap_pico = exp_row[0]

            encumbered_row = conn.execute(
                "SELECT COALESCE(SUM("
                "CASE WHEN status = 'reconciled' THEN charged_pico "
                "WHEN status IN ('pending', 'ambiguous') THEN reserved_pico "
                "ELSE 0 END"
                "), 0) FROM scout_eval_reservations WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            encumbered_pico = encumbered_row[0]
            if encumbered_pico + reserve_pico > cap_pico:
                raise SpendCapExceeded("experiment_spend_cap_exceeded")

            conn.execute(
                "INSERT INTO scout_eval_reservations("
                "reservation_id, experiment_id, profile_name, model_id, example_id, attempt_key, "
                "status, reserved_pico, charged_pico, reserved_usd, charged_usd, "
                "input_tokens, output_tokens, resolved_model, failure_disposition, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    reservation_id,
                    experiment_id,
                    profile_name,
                    model_id,
                    example_id,
                    attempt_key,
                    reserve_pico,
                    reserve_str,
                    now,
                    now,
                ),
            )
            inserted = conn.execute(
                f"{_SELECT_RESERVATION} WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            assert inserted is not None
            return _row_to_record(inserted)

    def reconcile(
        self,
        reservation_id: str,
        *,
        actual_usd: Decimal,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        resolved_model: str | None = None,
        failure_disposition: str | None = None,
    ) -> ReservationRecord:
        """Finalize a completed call reservation with reported or rate-derived charges."""
        charged_pico = usd_to_pico_ceil(actual_usd)
        charged_str = str(pico_to_usd(charged_pico))
        now = _now_iso()
        with self._immediate_tx() as conn:
            row = conn.execute(
                "SELECT status FROM scout_eval_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown_reservation: {reservation_id}")
            if row[0] != "pending":
                raise ValueError(f"reservation_not_pending: {row[0]}")
            conn.execute(
                "UPDATE scout_eval_reservations SET status = 'reconciled', charged_pico = ?, charged_usd = ?, "
                "input_tokens = ?, output_tokens = ?, resolved_model = ?, failure_disposition = ?, updated_at = ? "
                "WHERE reservation_id = ?",
                (
                    charged_pico,
                    charged_str,
                    input_tokens,
                    output_tokens,
                    resolved_model,
                    failure_disposition,
                    now,
                    reservation_id,
                ),
            )
            updated = conn.execute(
                f"{_SELECT_RESERVATION} WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            assert updated is not None
            return _row_to_record(updated)

    def mark_ambiguous(self, reservation_id: str, *, failure_disposition: str) -> ReservationRecord:
        """Retain encumbered budget as ambiguous on timeout or indeterminate remote failure."""
        now = _now_iso()
        with self._immediate_tx() as conn:
            row = conn.execute(
                "SELECT status FROM scout_eval_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown_reservation: {reservation_id}")
            if row[0] != "pending":
                raise ValueError(f"reservation_not_pending: {row[0]}")
            conn.execute(
                "UPDATE scout_eval_reservations SET status = 'ambiguous', failure_disposition = ?, updated_at = ? "
                "WHERE reservation_id = ?",
                (failure_disposition, now, reservation_id),
            )
            updated = conn.execute(
                f"{_SELECT_RESERVATION} WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            assert updated is not None
            return _row_to_record(updated)

    def release(self, reservation_id: str, *, failure_disposition: str) -> ReservationRecord:
        """Release a reservation ONLY on confirmed pre-send non-billable rejection."""
        now = _now_iso()
        with self._immediate_tx() as conn:
            row = conn.execute(
                "SELECT status FROM scout_eval_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown_reservation: {reservation_id}")
            if row[0] != "pending":
                raise ValueError(f"reservation_not_pending: {row[0]}")
            conn.execute(
                "UPDATE scout_eval_reservations SET status = 'released', failure_disposition = ?, updated_at = ? "
                "WHERE reservation_id = ?",
                (failure_disposition, now, reservation_id),
            )
            updated = conn.execute(
                f"{_SELECT_RESERVATION} WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            assert updated is not None
            return _row_to_record(updated)

    def summary(self, experiment_id: str) -> LedgerSummary:
        """Return durable aggregate spend and reservation counts for an experiment."""
        with self._read_conn() as conn:
            exp_row = conn.execute(
                "SELECT hard_spend_cap_pico FROM scout_eval_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if exp_row is None:
                raise ValueError(f"unknown_experiment: {experiment_id}")
            cap_pico = exp_row[0]
            agg = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status = 'reconciled' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status = 'ambiguous' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status = 'released' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status = 'pending' THEN reserved_pico ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status = 'ambiguous' THEN reserved_pico ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status = 'reconciled' THEN charged_pico ELSE 0 END), 0) "
                "FROM scout_eval_reservations WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        assert agg is not None
        (
            pending_count,
            reconciled_count,
            ambiguous_count,
            released_count,
            pending_pico,
            ambiguous_pico,
            reconciled_pico,
        ) = agg
        encumbered_pico = pending_pico + ambiguous_pico + reconciled_pico
        remaining_pico = max(0, cap_pico - encumbered_pico)
        return LedgerSummary(
            experiment_id=experiment_id,
            hard_spend_cap_usd=pico_to_usd(cap_pico),
            pending_reservations=pending_count,
            reconciled_reservations=reconciled_count,
            ambiguous_reservations=ambiguous_count,
            released_reservations=released_count,
            pending_reserved_usd=pico_to_usd(pending_pico),
            ambiguous_reserved_usd=pico_to_usd(ambiguous_pico),
            reconciled_cost_usd=pico_to_usd(reconciled_pico),
            encumbered_usd=pico_to_usd(encumbered_pico),
            remaining_budget_usd=pico_to_usd(remaining_pico),
        )
