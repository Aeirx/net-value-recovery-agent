"""Bank and acquirer health as a function of absolute time.

This is where the global clock earns its keep. Because health is time-indexed and shared,
an outage is a property of *the world* rather than of a transaction: every transaction on
that bank during that window sees it, which is what makes "wait for the outage to clear" a
real strategy and a correlated multi-bank outage a real regime.

Calibration status: the per-bank technical-decline spread is **chosen**, not sourced. The
NPCI BD/TD page returns HTTP 403 to automated fetches (``CALIBRATION.md`` row 38). The
*existence* of a large spread is well established; these specific multipliers are invented
and are labelled as such.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from netvalue.world import rng
from netvalue.world.calendar import fy_end_stress
from netvalue.world.config import Rail, WorldConfig


@dataclass(frozen=True, slots=True)
class Bank:
    bank_id: str
    name: str
    share: float
    #: Relative technical-decline propensity. 1.0 is the roster average. [chosen]
    td_multiplier: float


#: Eight issuers/PSPs with a deliberately wide reliability spread. [chosen]
BANKS: tuple[Bank, ...] = (
    Bank("BK_HDFC", "HDFC", 0.19, 0.45),
    Bank("BK_ICIC", "ICICI", 0.16, 0.70),
    Bank("BK_AXIS", "Axis", 0.13, 0.95),
    Bank("BK_SBIN", "SBI", 0.22, 2.10),
    Bank("BK_KOTK", "Kotak", 0.09, 0.80),
    Bank("BK_PYTM", "Paytm Payments Bank", 0.08, 1.75),
    Bank("BK_YESB", "Yes Bank", 0.07, 1.40),
    Bank("BK_IDFC", "IDFC First", 0.06, 1.10),
)

BANK_BY_ID: dict[str, Bank] = {b.bank_id: b for b in BANKS}

#: The two acquirer routes available on the card rail. UPI Autopay has no equivalent:
#: a merchant cannot switch acquirer for a mandate debit, which is why
#: ``switch_route_and_retry`` is card-only.
ROUTES: tuple[str, ...] = ("RT_ALPHA", "RT_BETA")

#: Expected independent outages per bank across a 30-day window, at multiplier 1.0. [chosen]
_OUTAGES_PER_MONTH = 1.4
_OUTAGE_MIN_HOURS, _OUTAGE_MAX_HOURS = 2.0, 6.0

#: Route degradation is more frequent but less total than a full outage. [chosen]
_DEGRADATIONS_PER_MONTH = 2.2
_DEGRADE_MIN_HOURS, _DEGRADE_MAX_HOURS = 3.0, 10.0


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open interval [start, end) during which a resource is unhealthy."""

    target: str
    start: datetime
    end: datetime
    correlated: bool = False

    def covers(self, when: datetime) -> bool:
        return self.start <= when < self.end

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


class HealthTimeline:
    """Time-indexed unhealthy windows, queryable by target and instant."""

    def __init__(self, windows: Sequence[Window]) -> None:
        self._by_target: dict[str, list[Window]] = {}
        for w in sorted(windows, key=lambda x: (x.target, x.start)):
            self._by_target.setdefault(w.target, []).append(w)
        self._starts: dict[str, list[datetime]] = {
            t: [w.start for w in ws] for t, ws in self._by_target.items()
        }

    def window_at(self, target: str, when: datetime) -> Window | None:
        windows = self._by_target.get(target)
        if not windows:
            return None
        # Rightmost window whose start is <= when; only that one can cover it.
        idx = bisect_right(self._starts[target], when) - 1
        if idx < 0:
            return None
        candidate = windows[idx]
        return candidate if candidate.covers(when) else None

    def is_unhealthy(self, target: str, when: datetime) -> bool:
        return self.window_at(target, when) is not None

    def hours_until_healthy(self, target: str, when: datetime) -> float:
        """0.0 if already healthy. Used by the *world*, never handed to the agent —
        knowing exactly when an outage ends is precisely the ground truth the agent has to
        infer."""
        window = self.window_at(target, when)
        return 0.0 if window is None else (window.end - when).total_seconds() / 3600.0

    def all_windows(self) -> list[Window]:
        return [w for ws in self._by_target.values() for w in ws]


def _sample_windows(
    cfg: WorldConfig,
    target: str,
    expected_per_month: float,
    min_hours: float,
    max_hours: float,
    kind: str,
) -> list[Window]:
    span_days = (cfg.clock.end - cfg.clock.start).total_seconds() / 86400.0
    expected = expected_per_month * span_days / 30.0
    gen = rng.stream(cfg.seed, "outage-count", kind, target)
    count = int(gen.poisson(expected))

    windows: list[Window] = []
    for i in range(count):
        offset_h = rng.uniform(
            cfg.seed, 0.0, span_days * 24.0, "outage-start", kind, target, i
        )
        start = cfg.clock.start + timedelta(hours=offset_h)
        # Year-end closing concentrates failures; bias duration upward inside that window.
        duration = rng.uniform(
            cfg.seed, min_hours, max_hours, "outage-dur", kind, target, i
        ) * min(fy_end_stress(start), 2.0)
        end = min(start + timedelta(hours=duration), cfg.clock.end)
        if end > start:
            windows.append(Window(target=target, start=start, end=end))
    return windows


def _correlated_outage(cfg: WorldConfig) -> list[Window]:
    """The regime that exists in config B and not in config A.

    Several banks fail at once. It is the failure mode a per-transaction world cannot
    represent at all, and the reason the clock is global: an agent that has learned
    "switch bank, or wait a couple of hours" has learned a rule that does not hold here.
    """
    n_banks = cfg.regime.correlated_outage_banks
    if n_banks <= 0 or cfg.regime.correlated_outage_hours <= 0.0:
        return []

    span_days = (cfg.clock.end - cfg.clock.start).total_seconds() / 86400.0
    gen = rng.stream(cfg.seed, "correlated-outage")
    # Weight bank selection toward the less reliable end of the roster, as a real
    # correlated event would concentrate on shared infrastructure.
    weights = [b.td_multiplier for b in BANKS]
    total = sum(weights)
    chosen_idx = gen.choice(
        len(BANKS), size=min(n_banks, len(BANKS)), replace=False,
        p=[w / total for w in weights],
    )
    offset_h = rng.uniform(cfg.seed, 0.0, span_days * 24.0 * 0.8, "correlated-start")
    start = cfg.clock.start + timedelta(hours=offset_h)
    end = min(start + timedelta(hours=cfg.regime.correlated_outage_hours), cfg.clock.end)

    return [
        Window(target=BANKS[int(i)].bank_id, start=start, end=end, correlated=True)
        for i in chosen_idx
    ]


@dataclass(frozen=True, slots=True)
class WorldHealth:
    """Everything about the world that varies with time and is hidden from the agent."""

    banks: HealthTimeline
    routes: HealthTimeline

    def bank_is_out(self, bank_id: str, when: datetime) -> bool:
        return self.banks.is_unhealthy(bank_id, when)

    def route_is_degraded(self, route: str, when: datetime) -> bool:
        return self.routes.is_unhealthy(route, when)

    def other_route(self, route: str) -> str:
        return ROUTES[1] if route == ROUTES[0] else ROUTES[0]


def build_world_health(cfg: WorldConfig) -> WorldHealth:
    bank_windows: list[Window] = []
    for bank in BANKS:
        bank_windows.extend(
            _sample_windows(
                cfg,
                bank.bank_id,
                _OUTAGES_PER_MONTH * bank.td_multiplier,
                _OUTAGE_MIN_HOURS,
                _OUTAGE_MAX_HOURS,
                "bank",
            )
        )
    bank_windows.extend(_correlated_outage(cfg))

    route_windows: list[Window] = []
    for route in ROUTES:
        route_windows.extend(
            _sample_windows(
                cfg, route, _DEGRADATIONS_PER_MONTH, _DEGRADE_MIN_HOURS,
                _DEGRADE_MAX_HOURS, "route",
            )
        )

    return WorldHealth(
        banks=HealthTimeline(bank_windows), routes=HealthTimeline(route_windows)
    )


def sample_bank(cfg: WorldConfig, transaction_id: str) -> Bank:
    return rng.choice(
        cfg.seed, list(BANKS), [b.share for b in BANKS], "bank-pick", transaction_id
    )


def sample_route(cfg: WorldConfig, transaction_id: str, rail: Rail) -> str | None:
    """Card mandates route through an acquirer. UPI Autopay does not."""
    if rail is not Rail.CARD_MANDATE:
        return None
    return rng.choice(cfg.seed, list(ROUTES), [0.5, 0.5], "route-pick", transaction_id)
