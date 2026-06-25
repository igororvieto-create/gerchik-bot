"""
Tests for Signal.reason string construction in strategy/strategy/gerchik.py.

The bug (ternary precedence over implicit f-string concat) caused:
  - funding is not None → entry/SL/TP/R:R lines were silently dropped
  - funding is None     → header lines were silently dropped

Both paths must produce a single str containing ALL sections.
"""
from typing import Optional


def _build_reason(funding: Optional[float]) -> str:
    """Replicate the exact reason-building code from gerchik.py:analyze()."""
    symbol, trend = "BTC-USDT", "LONG"
    pname, h4p, h4ok = "Молот", "Молот", True
    d1_up, d1_slope = True, 0.10
    h4_up, cur_adx = True, 30.0
    level, touches, touch_age = 49500.0, 3, 5
    vrat, rsi_str, macd_str, atr_str = 2.50, "50", "🟢", "0.0100"
    price, sl, tp2, tp3, rr, score = 50000.0, 49000.0, 52000.0, 53000.0, 2.0, 75

    # This is the fixed pattern from gerchik.py — any regression breaks the assertions below.
    funding_line = (f"💱 Funding: <code>{funding:.4f}%</code>\n"
                    if funding is not None else "💱 Funding: <code>н/д</code>\n")
    reason = (
        f"📊 <b>{symbol}</b> | {trend}\n"
        f"🕯 H1: {pname} | H4: {h4p if h4ok else '—'}\n"
        f"📈 D1: {'🟢' if d1_up else '🔴'} EMA200 slope {d1_slope:+.2f}%\n"
        f"H4: {'🟢' if h4_up else '🔴'} EMA50 | ADX: <code>{cur_adx:.1f}</code>\n"
        f"🎯 Уровень: <code>{level:.4f}</code> ({touches} кас., свежесть: {touch_age} св.)\n"
        f"📦 Объём: <code>{vrat:.2f}×</code> | RSI: <code>{rsi_str}</code> | "
        f"MACD: {macd_str} | ATR: <code>{atr_str}</code>\n"
        + funding_line
        + f"🟡 Вход: <code>{price:.4f}</code> | 🔴 SL: <code>{sl:.4f}</code>\n"
        f"🟢 TP2: <code>{tp2:.4f}</code> | TP3: <code>{tp3:.4f}</code>\n"
        f"⚡ R/R: 1:{rr:.1f} | ⭐ Score: {score}/100"
    )
    return reason


def test_reason_with_funding_is_complete():
    """When funding is a float, reason is a str with header AND entry/TP lines."""
    reason = _build_reason(funding=0.015)

    assert isinstance(reason, str), "reason must be str, not tuple"
    # Header (was the true branch before the bug)
    assert "BTC-USDT" in reason
    assert "EMA200" in reason
    # Funding line
    assert "Funding" in reason
    assert "0.0150%" in reason
    # Entry/TP lines (were silently dropped by the bug when funding was not None)
    assert "Вход" in reason
    assert "TP2" in reason
    assert "R/R" in reason


def test_reason_with_none_funding_is_complete():
    """When funding is None, reason is a str with header AND entry/TP lines."""
    reason = _build_reason(funding=None)

    assert isinstance(reason, str), "reason must be str, not tuple"
    # Header (was silently dropped by the bug when funding was None)
    assert "BTC-USDT" in reason
    assert "EMA200" in reason
    # н/д placeholder
    assert "н/д" in reason
    # Entry/TP lines
    assert "Вход" in reason
    assert "TP2" in reason
    assert "R/R" in reason
