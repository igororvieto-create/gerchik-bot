import os
from dataclasses import dataclass, field
from typing import List

@dataclass
class Config:
    TELEGRAM_TOKEN:   str   = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")
    BINGX_API_KEY:    str   = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET:     str   = os.getenv("BINGX_SECRET", "")
    MODE:             str   = os.getenv("BOT_MODE", "auto")
    RISK_PER_TRADE:   float = float(os.getenv("RISK_PER_TRADE", "1.0"))
    MAX_DAILY_LOSS:   float = float(os.getenv("MAX_DAILY_LOSS", "2.0"))
    MAX_POSITIONS:    int   = int(os.getenv("MAX_POSITIONS", "5"))
    MAX_DAILY_TRADES: int   = int(os.getenv("MAX_DAILY_TRADES", "10"))
    LEVERAGE:         int   = int(os.getenv("LEVERAGE", "5"))
    MIN_RR:           float = float(os.getenv("MIN_RR", "2.0"))
    SL_BUFFER_PCT:    float = float(os.getenv("SL_BUFFER_PCT", "0.15"))
    VOLUME_MULT:      float = float(os.getenv("VOLUME_MULT", "1.5"))
    VOLUME_MA_PERIOD: int   = int(os.getenv("VOLUME_MA_PERIOD", "20"))
    MIN_SCORE:        int   = int(os.getenv("MIN_SCORE", "65"))
    TREND_EMA_D1:     int   = int(os.getenv("TREND_EMA_D1", "200"))
    TREND_EMA_H4:     int   = int(os.getenv("TREND_EMA_H4", "50"))
    TREND_EMA_H1:     int   = int(os.getenv("TREND_EMA_H1", "21"))
    FUNDING_MAX_LONG:  float = float(os.getenv("FUNDING_MAX_LONG", "0.05"))
    FUNDING_MAX_SHORT: float = float(os.getenv("FUNDING_MAX_SHORT", "-0.05"))
    WHITELIST: List[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("WHITELIST","").split(",") if s.strip()])
    BLACKLIST: List[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("BLACKLIST","LUNA-USDT,FTT-USDT").split(",") if s.strip()])
    TOP_N_PAIRS:      int   = int(os.getenv("TOP_N_PAIRS", "0"))
    TREND_TF:  str = "1D"
    H4_TF:     str = "4h"
    SIGNAL_TF: str = "1h"
    TP1_RR: float = 1.0
    TP2_RR: float = 2.0
    TP3_RR: float = 3.0
    TP2_CLOSE_PCT:        float = 0.60
    PAUSE_AFTER_LOSS_MIN: int   = 30
    CONFIRM_TIMEOUT_SEC:  int   = 300
    SCAN_H1_INTERVAL_MIN: int   = int(os.getenv("SCAN_H1_INTERVAL_MIN", "5"))
    SCAN_BATCH_SIZE:      int   = int(os.getenv("SCAN_BATCH_SIZE", "10"))
    SCAN_BATCH_DELAY:     float = float(os.getenv("SCAN_BATCH_DELAY", "1.0"))

cfg = Config()
