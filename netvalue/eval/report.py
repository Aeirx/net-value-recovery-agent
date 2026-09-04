"""Rendering results: markdown to stdout, a PNG, and one server-rendered HTML page.

The layout is opinionated about one thing. **Gross recovered sits immediately beside net
value**, so the reader can check what was traded for what without hunting.

The project predicted the agent would recover *less* and net *more*. It does not: it nets
more while recovering marginally more too, by spending far less on customer goodwill. The
labels here deliberately do not assert the prediction — ``thesis_line`` reads the actual
numbers and says which of the three possible outcomes occurred, including the one where the
thesis fails. A report that can only describe the hoped-for result is not a measurement.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from netvalue.eval.bootstrap import Interval
from netvalue.eval.metrics import PolicyMetrics


def _inr(value: float) -> str:
    return f"{value:,.0f}"


def metrics_table(rows: Sequence[PolicyMetrics]) -> str:
    header = (
        "| Policy | Net value ₹ | Gross recovered ₹ | Recovery rate | Attempts | "
        "Contacts | Attempt cost ₹ | Annoyance cost ₹ | Abandoned but recoverable |"
    )
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines = [header, sep]
    for m in rows:
        lines.append(
            f"| `{m.policy}` | **{_inr(m.net_value_inr)}** | {_inr(m.gross_recovered_inr)} "
            f"| {m.recovery_rate:.1%} | {m.total_attempts:,} | {m.total_contacts:,} "
            f"| {_inr(m.attempt_cost_inr)} | {_inr(m.annoyance_cost_inr)} "
            f"| {m.abandoned_but_recoverable:,} |"
        )
    return "\n".join(lines)


def comparison_table(
    baseline: str, comparisons: Sequence[tuple[str, Interval, float]]
) -> str:
    lines = [
        f"Paired net-value delta against `{baseline}`, "
        "bootstrap 95% CI over transaction clusters:",
        "",
        "| Policy | Δ net value ₹ | 95% CI | Paired win rate | Sign resolved |",
        "|---|---:|---|---:|:---:|",
    ]
    for name, interval, wins in comparisons:
        resolved = "yes" if interval.excludes_zero else "**no**"
        lines.append(
            f"| `{name}` | {interval.mean:+,.0f} | "
            f"[{interval.low:+,.0f}, {interval.high:+,.0f}] | {wins:.1%} | {resolved} |"
        )
    return "\n".join(lines)


def thesis_line(agent: PolicyMetrics, ceiling: PolicyMetrics) -> str:
    """The one sentence the whole submission exists to be able to write."""
    gross_gap = ceiling.gross_recovered_inr - agent.gross_recovered_inr
    net_gap = agent.net_value_inr - ceiling.net_value_inr
    if gross_gap > 0 and net_gap > 0:
        return (
            f"`{agent.policy}` recovered ₹{_inr(gross_gap)} LESS than `{ceiling.policy}` "
            f"and produced ₹{_inr(net_gap)} MORE net value."
        )
    if net_gap <= 0:
        return (
            f"`{agent.policy}` did NOT beat `{ceiling.policy}` on net value "
            f"(₹{_inr(net_gap)}). The thesis does not hold on this run."
        )
    return (
        f"`{agent.policy}` beat `{ceiling.policy}` on net value by ₹{_inr(net_gap)} "
        f"while recovering ₹{_inr(-gross_gap)} MORE, not less — so the predicted trade "
        f"(recover less, net more) is not what happened. It won on both axes instead, by "
        f"spending ₹{_inr(ceiling.annoyance_cost_inr - agent.annoyance_cost_inr)} less on "
        f"customer goodwill. Report that, not the prediction."
    )


def plot_net_vs_gross(rows: Sequence[PolicyMetrics], path: str | Path) -> Path | None:
    """Net value against gross recovered. Returns None if matplotlib is unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        return None

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    labels = [m.policy for m in rows]
    net = [m.net_value_inr for m in rows]
    gross = [m.gross_recovered_inr for m in rows]

    fig, ax = plt.subplots(figsize=(9, 5.2))
    x = range(len(labels))
    width = 0.38
    ax.bar([i - width / 2 for i in x], gross, width, label="Gross recovered",
           color="#9aa7bd")
    ax.bar([i + width / 2 for i in x], net, width, label="Net value", color="#2440c4")
    ax.axhline(0, color="#333", linewidth=0.8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=12, ha="right")
    ax.set_ylabel("₹")
    ax.set_title("Gross recovered against net value, by policy")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def write_html(
    rows: Sequence[PolicyMetrics],
    comparisons: Sequence[tuple[str, Interval, float]],
    baseline: str,
    path: str | Path,
    *,
    config_hash: str,
    n_replications: int,
    image: Path | None = None,
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    def td(v: str, cls: str = "") -> str:
        return f'<td class="{cls}">{html.escape(v)}</td>'

    body_rows = "\n".join(
        "<tr>"
        + td(m.policy, "mono")
        + td(_inr(m.net_value_inr), "num strong")
        + td(_inr(m.gross_recovered_inr), "num")
        + td(f"{m.recovery_rate:.1%}", "num")
        + td(f"{m.total_attempts:,}", "num")
        + td(f"{m.total_contacts:,}", "num")
        + td(_inr(m.annoyance_cost_inr), "num")
        + td(f"{m.abandoned_but_recoverable:,}", "num")
        + "</tr>"
        for m in rows
    )
    cmp_rows = "\n".join(
        "<tr>"
        + td(name, "mono")
        + td(f"{iv.mean:+,.0f}", "num strong")
        + td(f"[{iv.low:+,.0f}, {iv.high:+,.0f}]", "num")
        + td(f"{wins:.1%}", "num")
        + td("yes" if iv.excludes_zero else "no", "num")
        + "</tr>"
        for name, iv, wins in comparisons
    )
    img = (
        f'<img src="{html.escape(image.name)}" alt="Gross against net value by policy">'
        if image
        else ""
    )

    out.write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<title>Net-value recovery — baselines</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.6 system-ui, sans-serif; margin: 0; padding: 32px;
         max-width: 1100px; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
  .meta {{ font-family: ui-monospace, monospace; font-size: 12px; opacity: .7;
           margin-bottom: 28px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 28px; font-size: 14px; }}
  th, td {{ padding: 8px 12px; border-bottom: 1px solid #8883; text-align: left; }}
  th {{ font-size: 11px; letter-spacing: .08em; text-transform: uppercase; opacity: .65; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums;
          font-family: ui-monospace, monospace; }}
  .mono {{ font-family: ui-monospace, monospace; }}
  .strong {{ font-weight: 600; }}
  img {{ max-width: 100%; height: auto; }}
  .note {{ border-left: 3px solid #2440c4; padding: 8px 14px; background: #2440c410;
           margin: 0 0 28px; }}
</style>
<h1>Baselines — net value against the success-rate ceiling</h1>
<p class="meta">config {html.escape(config_hash[:16])} &middot; {n_replications} replications
&middot; generated {datetime.now().isoformat(timespec='seconds')}</p>
<div class="note">Gross recovered sits beside net value so the trade is visible rather
than asserted. Read the two columns together: the interesting policies are the ones that
move them in different directions.</div>
<table>
<thead><tr><th>Policy</th><th class="num">Net value</th><th class="num">Gross recovered</th>
<th class="num">Recovery</th><th class="num">Attempts</th><th class="num">Contacts</th>
<th class="num">Annoyance</th><th class="num">Abandoned but recoverable</th></tr></thead>
<tbody>{body_rows}</tbody></table>
<h2>Paired deltas against <code>{html.escape(baseline)}</code></h2>
<table>
<thead><tr><th>Policy</th><th class="num">&Delta; net</th><th class="num">95% CI</th>
<th class="num">Win rate</th><th class="num">Sign resolved</th></tr></thead>
<tbody>{cmp_rows}</tbody></table>
{img}
""",
        encoding="utf-8",
    )
    return out
