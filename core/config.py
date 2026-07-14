import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "").strip()
    BYBIT_SECRET:  str = os.getenv("BYBIT_SECRET",  "").strip()
    NTFY_URL:      str = os.getenv("NTFY_URL",      "").strip()
    NTFY_TOPIC:    str = os.getenv("NTFY_TOPIC",    "bybit-scanner").strip()

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
    MIN_SCORE:              int   = int(os.getenv("MIN_SCORE",              "40").strip())
    OI_CHANGE_THRESHOLD:    float = float(os.getenv("OI_CHANGE_THRESHOLD",  "2.0").strip())
    VOL_SPIKE_MULT:         float = float(os.getenv("VOL_SPIKE_MULT",       "1.5").strip())
    FUNDING_EXTREME:        float = float(os.getenv("FUNDING_EXTREME",      "0.03").strip())
    PRICE_CHANGE_MIN:       float = float(os.getenv("PRICE_CHANGE_MIN",     "0.3").strip())
    OB_IMBALANCE_THRESHOLD: float = float(os.getenv("OB_IMBALANCE_THRESHOLD", "0.10").strip())
    MIN_VOL_24H:            float = float(os.getenv("MIN_VOL_24H",          "2000000").strip())
    SIGNAL_COOLDOWN_MIN:    int   = int(os.getenv("SIGNAL_COOLDOWN_MIN",    "20").strip())

    # Gerchik methodology: key levels, MTF, R:R
    MIN_RR:             float = float(os.getenv("MIN_RR",             "2.0").strip())
    KEY_LEVEL_LOOKBACK: int   = int(os.getenv("KEY_LEVEL_LOOKBACK",   "20").strip())
    KEY_LEVEL_WING:     int   = int(os.getenv("KEY_LEVEL_WING",       "2").strip())
    KEY_LEVEL_ATR_MULT: float = float(os.getenv("KEY_LEVEL_ATR_MULT", "1.5").strip())
    REQUIRE_MTF_ALIGN:  bool  = os.getenv("REQUIRE_MTF_ALIGN", "true").strip().lower() == "true"
    MTF_TREND_LOOKBACK: int   = int(os.getenv("MTF_TREND_LOOKBACK",   "6").strip())
    MIN_LISTING_AGE_DAYS: int = int(os.getenv("MIN_LISTING_AGE_DAYS", "14").strip())

    # Signal history
    MAX_SIGNALS_DB:  int = int(os.getenv("MAX_SIGNALS_DB",  "500").strip())
    SIGNAL_TTL_HOURS: int = int(os.getenv("SIGNAL_TTL_HOURS", "24").strip())

    # ── Auto-trading ──────────────────────────────────────────────────────────
    AUTO_TRADE:      bool  = os.getenv("AUTO_TRADE", "false").strip().lower() == "true"
    RISK_PER_TRADE:  float = float(os.getenv("RISK_PER_TRADE",  "1.0").strip())
    MAX_MARGIN_PCT:  float = float(os.getenv("MAX_MARGIN_PCT",  "10.0").strip())
    MAX_POSITIONS:   int   = int(os.getenv("MAX_POSITIONS",     "3").strip())
    LEVERAGE:        int   = int(os.getenv("LEVERAGE",          "5").strip())
    TRADE_MIN_SCORE: int   = int(os.getenv("TRADE_MIN_SCORE",   "60").strip())

    # Risk guards
    MAX_SAME_DIRECTION:     int   = int(os.getenv("MAX_SAME_DIRECTION",     "2").strip())
    DAILY_LOSS_LIMIT_PCT:   float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0").strip())
    ABORT_ON_LEVERAGE_FAIL: bool  = os.getenv("ABORT_ON_LEVERAGE_FAIL", "true").strip().lower() == "true"


cfg = Config()
