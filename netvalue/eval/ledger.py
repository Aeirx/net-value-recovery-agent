"""Double-entry ledger for every rupee the system moves.

Net value is the headline number of the whole submission, so it is not computed by adding
up variables at the end and hoping. Every movement is posted, and the reported figure is
the sum of the postings. ``tests/test_ledger.py`` asserts that the two agree exactly.

The discipline matters because the thesis is a *subtraction*: recovering less money while
producing more net value. If the cost side were quietly under-counted, the result would be
flattering and wrong, and nothing in the output would look unusual.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class PostingKind(StrEnum):
    """Credits are positive, debits negative. Net value is their sum."""

    RECOVERED_VALUE = "recovered_value"
    ATTEMPT_COST = "attempt_cost"
    ANNOYANCE_COST = "annoyance_cost"

    @property
    def is_credit(self) -> bool:
        return self is PostingKind.RECOVERED_VALUE


@dataclass(frozen=True, slots=True)
class Posting:
    transaction_id: str
    kind: PostingKind
    #: Signed. Credits positive, debits negative.
    amount_inr: float
    at: datetime
    note: str = ""


@dataclass(slots=True)
class Ledger:
    postings: list[Posting] = field(default_factory=list)

    def credit(
        self, transaction_id: str, kind: PostingKind, amount: float, at: datetime, note: str = ""
    ) -> None:
        if not kind.is_credit:
            raise ValueError(f"{kind} is a debit category")
        if amount < 0:
            raise ValueError("credit amounts are positive")
        self.postings.append(Posting(transaction_id, kind, amount, at, note))

    def debit(
        self, transaction_id: str, kind: PostingKind, amount: float, at: datetime, note: str = ""
    ) -> None:
        if kind.is_credit:
            raise ValueError(f"{kind} is a credit category")
        if amount < 0:
            raise ValueError("pass debit amounts as positive magnitudes")
        self.postings.append(Posting(transaction_id, kind, -amount, at, note))

    # ---------------------------------------------------------------- aggregates

    def total(self, kind: PostingKind) -> float:
        """Signed total for one category."""
        return math.fsum(p.amount_inr for p in self.postings if p.kind is kind)

    def magnitude(self, kind: PostingKind) -> float:
        """Unsigned total, for reporting costs as positive numbers."""
        return abs(self.total(kind))

    def net_value(self) -> float:
        return math.fsum(p.amount_inr for p in self.postings)

    def gross_recovered(self) -> float:
        return self.total(PostingKind.RECOVERED_VALUE)

    def for_transaction(self, transaction_id: str) -> list[Posting]:
        return [p for p in self.postings if p.transaction_id == transaction_id]

    def extend(self, other: Ledger) -> None:
        self.postings.extend(other.postings)

    # ---------------------------------------------------------------- invariant

    def check_conservation(self, tolerance: float = 1e-6) -> None:
        """Recovered minus costs must equal the reported net value, exactly.

        Raises rather than returning a flag: a ledger that does not balance is not a
        degraded result, it is an invalid one, and every number derived from it is void.
        """
        recovered = self.total(PostingKind.RECOVERED_VALUE)
        attempts = self.total(PostingKind.ATTEMPT_COST)
        annoyance = self.total(PostingKind.ANNOYANCE_COST)
        expected = recovered + attempts + annoyance
        actual = self.net_value()
        if abs(expected - actual) > tolerance:
            raise AssertionError(
                f"ledger does not balance: components {expected:.6f} vs total {actual:.6f}"
            )
        for posting in self.postings:
            if posting.kind.is_credit and posting.amount_inr < 0:
                raise AssertionError(f"credit posted as negative: {posting}")
            if not posting.kind.is_credit and posting.amount_inr > 0:
                raise AssertionError(f"debit posted as positive: {posting}")


def merge(ledgers: Iterable[Ledger]) -> Ledger:
    out = Ledger()
    for ledger in ledgers:
        out.extend(ledger)
    return out
