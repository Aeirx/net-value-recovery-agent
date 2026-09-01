"""Calendar structure: salary cycles and the financial-year-end squeeze.

Both effects are real and both are why retry *timing* is a decision rather than a delay.
An agent that understands the salary cycle waits for the 1st; one that does not burns its
attempts mid-month when the balance is lowest.

Calibration status (see ``CALIBRATION.md``):

* Salary-date clustering is structurally real but the *shape* here is ``chosen``.
* The financial-year-end spike is ``chosen`` in magnitude — NPCI publicly attributed a
  late-March outage to year-end closing at banks, but no quantified seasonal effect is
  published.
"""

from __future__ import annotations

from datetime import datetime

#: Days of the month on which salary credits cluster in India. [chosen]
SALARY_DAYS: tuple[int, ...] = (1, 7)

#: Multiplier applied to the probability that funds are available, by distance in days
#: from the nearest salary day. Index 0 is the salary day itself. [chosen]
_SALARY_LIFT: tuple[float, ...] = (2.10, 1.95, 1.65, 1.30, 1.05, 0.85, 0.70)

#: Floor for the deep mid-month trough.
_SALARY_TROUGH = 0.55

#: Financial-year-end: banks close books on 31 March and technical declines spike.
_FY_END_MONTH, _FY_END_DAY = 3, 31

#: Multiplier on technical failure rates, by days from 31 March. [chosen]
_FY_END_STRESS: tuple[float, ...] = (3.20, 2.10, 1.45)


def days_to_nearest_salary_day(when: datetime) -> int:
    """Absolute distance in days to the nearest salary credit, wrapping months.

    Wrapping matters: on the 29th the next salary day is two or three days out, not
    twenty-eight days back, and an agent that gets this wrong will schedule its retry into
    the worst possible window.
    """
    day = when.day
    best = 99
    # Previous month's cycle, this month's, and next month's — so month boundaries wrap.
    for offset in (-31, 0, 31):
        for salary_day in SALARY_DAYS:
            best = min(best, abs(day - (salary_day + offset)))
    return best


def salary_factor(when: datetime) -> float:
    """Multiplier on the availability of funds at ``when``.

    Above 1 near payday, below 1 in the mid-month trough.
    """
    distance = days_to_nearest_salary_day(when)
    if distance < len(_SALARY_LIFT):
        return _SALARY_LIFT[distance]
    return _SALARY_TROUGH


def fy_end_stress(when: datetime) -> float:
    """Multiplier on technical failure rates around financial-year-end closing."""
    if when.month != _FY_END_MONTH:
        return 1.0
    distance = abs(when.day - _FY_END_DAY)
    if distance < len(_FY_END_STRESS):
        return _FY_END_STRESS[distance]
    return 1.0


def is_fy_end_window(when: datetime) -> bool:
    return fy_end_stress(when) > 1.0


def describe(when: datetime) -> str:
    """Compact human-readable calendar context, used in the evidence view the agent sees.

    The agent is told the *observable* calendar position — anyone can look at a date — but
    never the recovery probability it implies.
    """
    distance = days_to_nearest_salary_day(when)
    if distance == 0:
        timing = "salary credit day"
    elif distance <= 2:
        timing = f"{distance}d from salary credit"
    elif distance >= 5:
        timing = "mid-cycle trough"
    else:
        timing = f"{distance}d from salary credit"
    if is_fy_end_window(when):
        timing += ", financial year-end window"
    return timing
