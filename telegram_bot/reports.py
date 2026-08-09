"""
Weekly market report + trend charts (premium Pro feature).

Builds a human-readable market summary and (optionally) a PNG chart entirely from
the local serving-layer cache — no live Databricks call, so it renders instantly.
matplotlib is imported lazily and guarded: if it's not installed the chart helpers
return None and the caller falls back to the text-only report.
"""

from __future__ import annotations

import io
import logging

from telegram_bot import serving

logger = logging.getLogger(__name__)


def build_market_report() -> str:
    """Assemble the weekly market report as a Telegram HTML string.

    Sections: headline volume/salary trend, top hiring companies, hottest
    technologies (biggest week-over-week demand jump). Degrades to a "not ready"
    message if the cache hasn't been synced yet.
    """
    if not serving.is_ready():
        return (
            "📊 <b>Market report</b>\n\n"
            "The market data cache isn't ready yet. It refreshes after each daily "
            "pipeline run — try again a bit later."
        )

    lines: list[str] = ["📊 <b>Weekly IT Market Report — Poland</b>\n"]

    # --- Volume + salary trend ---
    trend = serving.market_trend(weeks=8)
    if trend:
        latest = trend[-1]
        first = trend[0]
        vol_now = latest.get("rolling_7d_listings")
        vol_then = first.get("rolling_7d_listings")
        sal_now = latest.get("rolling_7d_avg_salary")
        lines.append("<b>📈 Trend (last 8 weeks)</b>")
        if vol_now is not None and vol_then:
            delta = vol_now - vol_then
            arrow = "🔺" if delta > 0 else ("🔻" if delta < 0 else "▶️")
            lines.append(
                f"{arrow} 7-day listing volume: {vol_then} → {vol_now} "
                f"({'+' if delta >= 0 else ''}{delta})"
            )
        if sal_now:
            lines.append(f"💰 Avg salary (7-day rolling mid): ~{round(sal_now)} PLN")
        lines.append("")

    # --- Top hiring companies ---
    companies = serving.top_hiring_companies(limit=8)
    if companies:
        lines.append("<b>🏢 Top hiring companies</b>")
        for i, c in enumerate(companies, 1):
            lines.append(f"{i}. {c.get('company_name')} — {c.get('listing_count')} listings")
        lines.append("")

    # --- Hot technologies ---
    hot = serving.hot_technologies(limit=8)
    if hot:
        lines.append("<b>🔥 Hottest technologies (week-over-week)</b>")
        for h in hot:
            wow = h.get("wow_change") or 0
            sign = f"+{wow}" if wow >= 0 else str(wow)
            lines.append(
                f"• {h.get('technology_name')}: {h.get('listing_count')} listings ({sign} WoW)"
            )
        lines.append("")

    lines.append("<i>Generated from the latest gold marts.</i>")
    return "\n".join(lines)


def build_trend_chart() -> bytes | None:
    """Render the 8-week listing-volume + salary trend as a PNG. None if unavailable."""
    trend = serving.market_trend(weeks=8)
    if not trend:
        return None
    dates = [r.get("full_date") for r in trend]
    volume = [r.get("rolling_7d_listings") or 0 for r in trend]
    salary = [r.get("rolling_7d_avg_salary") or 0 for r in trend]
    return _render_dual_axis(
        dates,
        volume,
        salary,
        title="Polish IT Market — 8-week trend",
        left_label="7-day listings",
        right_label="Avg salary (PLN)",
    )


def build_tech_demand_chart(tech: str) -> bytes | None:
    """Render weekly demand for a technology as a PNG bar chart. None if unavailable."""
    data = serving.tech_demand_trend(tech, limit=12)
    if not data:
        return None
    weeks = [r.get("week_start") for r in data]
    counts = [r.get("listing_count") or 0 for r in data]
    return _render_bars(
        weeks,
        counts,
        title=f"Weekly demand — {tech}",
        ylabel="Listings",
    )


# --------------------------------------------------------------------------- #
# matplotlib rendering (lazy, guarded)
# --------------------------------------------------------------------------- #
def _matplotlib():
    """Import matplotlib with a non-interactive backend. None if unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception as e:  # ImportError or backend failure
        logger.warning("matplotlib unavailable; charts disabled: %s", e)
        return None


def _render_dual_axis(x, y_left, y_right, *, title, left_label, right_label) -> bytes | None:
    plt = _matplotlib()
    if plt is None:
        return None
    try:
        fig, ax1 = plt.subplots(figsize=(9, 4.5))
        ax1.plot(range(len(x)), y_left, color="#2563eb", marker="o", label=left_label)
        ax1.set_ylabel(left_label, color="#2563eb")
        ax1.tick_params(axis="y", labelcolor="#2563eb")

        ax2 = ax1.twinx()
        ax2.plot(range(len(x)), y_right, color="#16a34a", marker="s", label=right_label)
        ax2.set_ylabel(right_label, color="#16a34a")
        ax2.tick_params(axis="y", labelcolor="#16a34a")

        step = max(1, len(x) // 8)
        ax1.set_xticks(range(0, len(x), step))
        ax1.set_xticklabels([str(x[i]) for i in range(0, len(x), step)], rotation=45, ha="right")
        ax1.set_title(title)
        fig.tight_layout()
        return _fig_to_png(plt, fig)
    except Exception as e:
        logger.warning("chart render failed: %s", e)
        return None


def _render_bars(x, y, *, title, ylabel) -> bytes | None:
    plt = _matplotlib()
    if plt is None:
        return None
    try:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.bar(range(len(x)), y, color="#2563eb")
        step = max(1, len(x) // 12)
        ax.set_xticks(range(0, len(x), step))
        ax.set_xticklabels([str(x[i]) for i in range(0, len(x), step)], rotation=45, ha="right")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        fig.tight_layout()
        return _fig_to_png(plt, fig)
    except Exception as e:
        logger.warning("chart render failed: %s", e)
        return None


def _fig_to_png(plt, fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
