import os
from dataclasses import dataclass, field
from typing import List


def _env_int(name: str, default: int) -> int:
    """Разбор env с фолбэком. Раньше int(os.getenv(...)) стоял прямо в
    дефолте dataclass: заданная, но ПУСТАЯ переменная в Railway роняла
    импорт core.config, а значит и всё приложение — crash-loop без логов."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        import logging
        logging.getLogger("config").error(f"{name}={raw!r} — не число, беру {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        import logging
        logging.getLogger("config").error(f"{name}={raw!r} — не число, беру {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


@dataclass
class Config:
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "").strip()
    BYBIT_SECRET:  str = os.getenv("BYBIT_SECRET",  "").strip()
    NTFY_URL:      str = os.getenv("NTFY_URL",      "").strip()

    # Scanning
    SCAN_INTERVAL_MIN: int   = _env_int("SCAN_INTERVAL_MIN", 4)
    SCAN_BATCH_SIZE:   int   = _env_int("SCAN_BATCH_SIZE", 10)
    SCAN_BATCH_DELAY:  float = _env_float("SCAN_BATCH_DELAY", 0.5)
    TOP_N_PAIRS:       int   = _env_int("TOP_N_PAIRS", 100)

    BLACKLIST: List[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("BLACKLIST", "LUNAUSDT,FTTUSDT").split(",") if s.strip()
        ]
    )

    # Signal thresholds
    MIN_SCORE:              int   = _env_int("MIN_SCORE", 30)
    OI_CHANGE_THRESHOLD:    float = _env_float("OI_CHANGE_THRESHOLD", 2.0)
    VOL_SPIKE_MULT:         float = _env_float("VOL_SPIKE_MULT", 1.5)
    FUNDING_EXTREME:        float = _env_float("FUNDING_EXTREME", 0.03)
    PRICE_CHANGE_MIN:       float = _env_float("PRICE_CHANGE_MIN", 0.3)
    OB_IMBALANCE_THRESHOLD: float = _env_float("OB_IMBALANCE_THRESHOLD", 0.10)
    MIN_VOL_24H:            float = _env_float("MIN_VOL_24H", 2000000)
    SIGNAL_COOLDOWN_MIN:    int   = _env_int("SIGNAL_COOLDOWN_MIN", 60)

    # Gerchik methodology: key levels, MTF, R:R
    # 1.5: пол риска (0.75 ATR) + буфер делают связку "цена у уровня" и
    # "2R до противоположного уровня" почти неразрешимой — нужно 1.5-2.9 ATR
    # чистого хода до ближайшего пивота, что в окне из 20 свечей редкость.
    # Сетка TP остаётся 1R/2R/3R, торговая цель по-прежнему TP2 = 2R.
    MIN_RR:             float = _env_float("MIN_RR", 1.5)
    KEY_LEVEL_LOOKBACK: int   = _env_int("KEY_LEVEL_LOOKBACK", 20)
    KEY_LEVEL_WING:     int   = _env_int("KEY_LEVEL_WING", 2)
    KEY_LEVEL_ATR_MULT: float = _env_float("KEY_LEVEL_ATR_MULT", 1.2)
    # Пивоты ближе этого расстояния к цене — рыночный шум, а не уровень
    LEVEL_NOISE_ATR:    float = _env_float("LEVEL_NOISE_ATR", 0.5)
    # Потолок ширины стопа в ATR — защита от абсурдно широких стопов
    MAX_SL_ATR:         float = _env_float("MAX_SL_ATR", 3.5)
    # Разворот должен входить рядом с сетапом, а не через несколько ATR
    REVERSAL_MAX_DRIFT_ATR: float = _env_float("REVERSAL_MAX_DRIFT_ATR", 1.0)
    REQUIRE_MTF_ALIGN:  bool  = _env_bool("REQUIRE_MTF_ALIGN", True)
    MTF_TREND_LOOKBACK: int   = _env_int("MTF_TREND_LOOKBACK", 6)
    MIN_LISTING_AGE_DAYS: int = _env_int("MIN_LISTING_AGE_DAYS", 14)
    MAX_LAST_CANDLE_ATR:  float = _env_float("MAX_LAST_CANDLE_ATR", 2.0)

    # Signal history
    # 5000 ≈ 8 суток потока сигналов: лимит не должен обрезать семидневную
    # выборку винрейта (500 выбирались за ~5 дней и статистика теряла хвост)
    MAX_SIGNALS_DB:  int = _env_int("MAX_SIGNALS_DB", 5000)
    SIGNAL_TTL_HOURS: int = _env_int("SIGNAL_TTL_HOURS", 24)

    # ── Auto-trading ──────────────────────────────────────────────────────────
    AUTO_TRADE:      bool  = _env_bool("AUTO_TRADE", False)
    RISK_PER_TRADE:  float = _env_float("RISK_PER_TRADE", 1.0)
    MAX_MARGIN_PCT:  float = _env_float("MAX_MARGIN_PCT", 10.0)
    MAX_POSITIONS:   int   = _env_int("MAX_POSITIONS", 3)
    LEVERAGE:        int   = _env_int("LEVERAGE", 5)
    # 45, а не 60: шкала score дискретна, и при ΔOI<5% потолок = 59 даже при
    # идеальных объёме/фандинге/стакане. Порог 60 отбирал ИСКЛЮЧИТЕЛЬНО
    # импульсные разгоны с ΔOI≥6% — ровно тот вход вдогонку, что дал 1W/14L.
    TRADE_MIN_SCORE: int   = _env_int("TRADE_MIN_SCORE", 45)

    # Risk guards
    MAX_SAME_DIRECTION:     int   = _env_int("MAX_SAME_DIRECTION", 2)
    # 6% допускает 2 стопа при риске 3% или 6 стопов при риске 1%
    DAILY_LOSS_LIMIT_PCT:   float = _env_float("DAILY_LOSS_LIMIT_PCT", 6.0)
    ABORT_ON_LEVERAGE_FAIL: bool  = _env_bool("ABORT_ON_LEVERAGE_FAIL", True)


def _clamp(value, lo, hi, name: str):
    """Жёсткое ограничение инвариантов проекта на уровне конфига.
    Раньше потолки (риск 1-3%, плечо ≤5x) проверялись ТОЛЬКО в /api/settings —
    опечатка в переменной окружения Railway (RISK_PER_TRADE=10) проходила
    насквозь до расчёта размера позиции без единой проверки."""
    if value < lo or value > hi:
        import logging
        clamped = max(lo, min(hi, value))
        logging.getLogger("config").error(
            f"{name}={value} вне допустимого диапазона [{lo}, {hi}] — принудительно {clamped}"
        )
        return clamped
    return value


cfg = Config()

# Инварианты CLAUDE.md — нарушить нельзя ни через env, ни через API
cfg.RISK_PER_TRADE       = _clamp(cfg.RISK_PER_TRADE,       0.1, 3.0,  "RISK_PER_TRADE")
cfg.LEVERAGE             = int(_clamp(cfg.LEVERAGE,           1,   5,  "LEVERAGE"))
cfg.MAX_POSITIONS        = int(_clamp(cfg.MAX_POSITIONS,      1,  20,  "MAX_POSITIONS"))
cfg.MAX_MARGIN_PCT       = _clamp(cfg.MAX_MARGIN_PCT,       1.0, 50.0, "MAX_MARGIN_PCT")
cfg.DAILY_LOSS_LIMIT_PCT = _clamp(cfg.DAILY_LOSS_LIMIT_PCT, 1.0, 20.0, "DAILY_LOSS_LIMIT_PCT")
