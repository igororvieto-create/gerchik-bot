import io
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

log = logging.getLogger("chart")


def generate_chart(klines: dict, symbol: str, signal) -> bytes | None:
    """
    Draw a dark-theme candlestick chart with SL/TP lines.
    Returns PNG bytes, or None on failure.
    """
    try:
        n = min(60, len(klines["close"]))
        opens  = klines["open"][-n:]
        highs  = klines["high"][-n:]
        lows   = klines["low"][-n:]
        closes = klines["close"][-n:]

        fig, ax = plt.subplots(figsize=(10, 5))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#16213e")

        for i in range(n):
            bull = closes[i] >= opens[i]
            color = "#26a69a" if bull else "#ef5350"
            # Wick
            ax.plot([i, i], [lows[i], highs[i]], color=color, linewidth=0.8, zorder=1)
            # Body
            body_lo = min(opens[i], closes[i])
            body_hi = max(opens[i], closes[i])
            height  = max(body_hi - body_lo, (highs[i] - lows[i]) * 0.005)
            rect = mpatches.FancyBboxPatch(
                (i - 0.35, body_lo), 0.7, height,
                boxstyle="square,pad=0",
                linewidth=0,
                facecolor=color,
                zorder=2,
            )
            ax.add_patch(rect)

        # Key price lines
        ax.axhline(signal.entry, color="#ffffff", linewidth=1.2, linestyle="-",
                   alpha=0.9, label=f"Entry {signal.entry:.4f}", zorder=3)
        ax.axhline(signal.sl,   color="#ef5350", linewidth=1.0, linestyle="--",
                   label=f"SL {signal.sl:.4f}", zorder=3)
        ax.axhline(signal.tp1,  color="#ffd54f", linewidth=0.8, linestyle=":",
                   label=f"TP1 {signal.tp1:.4f}", zorder=3)
        ax.axhline(signal.tp2,  color="#aed581", linewidth=0.8, linestyle=":",
                   label=f"TP2 {signal.tp2:.4f}", zorder=3)
        ax.axhline(signal.tp3,  color="#26a69a", linewidth=1.0, linestyle="-",
                   label=f"TP3 {signal.tp3:.4f}", zorder=3)

        ax.set_title(
            f"{symbol}  |  {signal.side}  |  ⭐ {signal.score}/100  |  {signal.pattern}",
            color="white", fontsize=11, pad=8,
        )
        ax.tick_params(colors="#888888", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#333344")
        ax.legend(
            loc="upper left", fontsize=7,
            facecolor="#1a1a2e", labelcolor="white",
            framealpha=0.8, edgecolor="#444",
        )

        plt.tight_layout(pad=1.5)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        log.error(f"generate_chart {symbol}: {e}")
        return None
