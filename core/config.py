import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "").strip()
    BYBIT_SECRET:  str = os.getenv("BYBIT_SECRET",  "").strip()
    NTFY_URL:      str = os.getenv("NTFY_URL",      "").strip()

    # Scanning
    SCAN_INTERVAL_MIN: int   = int(os.getenv("SCAN_INTERVAL_MIN", "4").strip())
    SCAN_BATCH_SIZE:   int   = int(os.getenv("SCAN_BATCH_SIZE",   "10").strip())
    SCAN_BATCH_DELAY:  float = float(os.getenv("SCAN_BATCH_DELAY", "0.5").strip())
    TOP_N_PAIRS:       int   = int(os.getenv("TOP_N_PAIRS",        "100").strip())

    BLACKLIST: List[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("BLACKLIST", "LUNAUSDT,FTTUSDT").split(",") if s.strip()
        ]
    )

    # Signal thresholds
    MIN_SCORE:              int   = int(os.getenv("MIN_SCORE",              "30").strip())
    OI_CHANGE_THRESHOLD:    float = float(os.getenv("OI_CHANGE_THRESHOLD",  "2.0").strip())
    VOL_SPIKE_MULT:         float = float(os.getenv("VOL_SPIKE_MULT",       "1.5").strip())
    FUNDING_EXTREME:        float = float(os.getenv("FUNDING_EXTREME",      "0.03").strip())
    PRICE_CHANGE_MIN:       float = float(os.getenv("PRICE_CHANGE_MIN",     "0.3").strip())
    OB_IMBALANCE_THRESHOLD: float = float(os.getenv("OB_IMBALANCE_THRESHOLD", "0.10").strip())
    MIN_VOL_24H:            float = float(os.getenv("MIN_VOL_24H",          "2000000").strip())
    SIGNAL_COOLDOWN_MIN:    int   = int(os.getenv("SIGNAL_COOLDOWN_MIN",    "60").strip())

    # Gerchik methodology: key levels, MTF, R:R
    # 1.5: пол риска (0.75 ATR) + буфер делают связку "цена у уровня" и
    # "2R до противоположного уровня" почти неразрешимой — нужно 1.5-2.9 ATR
    # чистого хода до ближайшего пивота, что в окне из 20 свечей редкость.
    # Сетка TP остаётся 1R/2R/3R, торговая цель по-прежнему TP2 = 2R.
    MIN_RR:             float = float(os.getenv("MIN_RR",             "1.5").strip())
    KEY_LEVEL_LOOKBACK: int   = int(os.getenv("KEY_LEVEL_LOOKBACK",   "20").strip())
    KEY_LEVEL_WING:     int   = int(os.getenv("KEY_LEVEL_WING",       "2").strip())
    KEY_LEVEL_ATR_MULT: float = float(os.getenv("KEY_LEVEL_ATR_MULT", "1.2").strip())
    # Пивоты ближе этого расстояния к цене — рыночный шум, а не уровень
    LEVEL_NOISE_ATR:    float = float(os.getenv("LEVEL_NOISE_ATR",    "0.5").strip())
    # Потолок ширины стопа в ATR — защита от абсурдно широких стопов
    MAX_SL_ATR:         float = float(os.getenv("MAX_SL_ATR",         "3.5").strip())
    REQUIRE_MTF_ALIGN:  bool  = os.getenv("REQUIRE_MTF_ALIGN", "true").strip().lower() == "true"
    MTF_TREND_LOOKBACK: int   = int(os.getenv("MTF_TREND_LOOKBACK",   "6").strip())
    MIN_LISTING_AGE_DAYS: int = int(os.getenv("MIN_LISTING_AGE_DAYS", "14").strip())
    MAX_LAST_CANDLE_ATR:  float = float(os.getenv("MAX_LAST_CANDLE_ATR", "2.0").strip())

    # Signal history
    # 5000 ≈ 8 суток потока сигналов: лимит не должен обрезать семидневную
    # выборку винрейта (500 выбирались за ~5 дней и статистика теряла хвост)
    MAX_SIGNALS_DB:  int = int(os.getenv("MAX_SIGNALS_DB",  "5000").strip())
    SIGNAL_TTL_HOURS: int = int(os.getenv("SIGNAL_TTL_HOURS", "24").strip())

    # ── Auto-trading ──────────────────────────────────────────────────────────
    AUTO_TRADE:      bool  = os.getenv("AUTO_TRADE", "false").strip().lower() == "true"
    RISK_PER_TRADE:  float = float(os.getenv("RISK_PER_TRADE",  "1.0").strip())
    MAX_MARGIN_PCT:  float = float(os.getenv("MAX_MARGIN_PCT",  "10.0").strip())
    MAX_POSITIONS:   int   = int(os.getenv("MAX_POSITIONS",     "3").strip())
    LEVERAGE:        int   = int(os.getenv("LEVERAGE",          "5").strip())
    # 45, а не 60: шкала score дискретна, и при ΔOI<5% потолок = 59 даже при
    # идеальных объёме/фандинге/стакане. Порог 60 отбирал ИСКЛЮЧИТЕЛЬНО
    # импульсные разгоны с ΔOI≥6% — ровно тот вход вдогонку, что дал 1W/14L.
    TRADE_MIN_SCORE: int   = int(os.getenv("TRADE_MIN_SCORE",   "45").strip())

    # Risk guards
    MAX_SAME_DIRECTION:     int   = int(os.getenv("MAX_SAME_DIRECTION",     "2").strip())
    # 6% допускает 2 стопа при риске 3% или 6 стопов при риске 1%
    DAILY_LOSS_LIMIT_PCT:   float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "6.0").strip())
    ABORT_ON_LEVERAGE_FAIL: bool  = os.getenv("ABORT_ON_LEVERAGE_FAIL", "true").strip().lower() == "true"


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
