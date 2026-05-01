import os
from dataclasses import dataclass, field
from typing import List

@dataclass
class Config:
    TELEGRAM_TOKEN:   str   = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    BINGX_API_KEY:    str   = os.getenv("BINGX_API_KEY", "").strip()
    BINGX_SECRET:     str   = os.getenv("BINGX_SECRET", "").strip()
    MODE:             str   = "auto"
    RISK_PER_TRADE:   float = float(os.getenv("RISK_PER_TRADE", "1.0").strip())
    MAX_DAILY_LOSS:   float = float(os.getenv("MAX_DAILY_LOSS", "10.0").strip())
    MAX_POSITIONS:    int   = int(os.getenv("MAX_POSITIONS", "3").strip())
    MAX_DAILY_TRADES: int   = int(os.getenv("MAX_DAILY_TRADES", "10").strip())
    LEVERAGE:         int   = int(os.getenv("LEVERAGE", "5").strip())
    MIN_RR:           float = float(os.getenv("MIN_RR", "2.0").strip())
    SL_BUFFER_PCT:    float = float(os.getenv("SL_BUFFER_PCT", "0.15").strip())
    VOLUME_MULT:      float = float(os.getenv("VOLUME_MULT", "1.3").strip())
    VOLUME_MA_PERIOD: int   = int(os.getenv("VOLUME_MA_PERIOD", "20").strip())
    MIN_SCORE:        int   = int(os.getenv("MIN_SCORE", "63").strip())
    TREND_EMA_D1:     int   = int(os.getenv("TREND_EMA_D1", "200").strip())
    TREND_EMA_H4:     int   = int(os.getenv("TREND_EMA_H4", "50").strip())
    TREND_EMA_H1:     int   = int(os.getenv("TREND_EMA_H1", "21").strip())
    FUNDING_MAX_LONG:  float = float(os.getenv("FUNDING_MAX_LONG", "0.05").strip())
    FUNDING_MAX_SHORT: float = float(os.getenv("FUNDING_MAX_SHORT", "-0.05").strip())
    WHITELIST: List[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("WHITELIST","").split(",") if s.strip()])
    BLACKLIST: List[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("BLACKLIST","LUNA-USDT,FTT-USDT").split(",") if s.strip()])
    TOP_N_PAIRS:      int   = int(os.getenv("TOP_N_PAIRS", "0").strip())
    MAX_RISK_USDT:    float = float(os.getenv("MAX_RISK_USDT", "20.0").strip())
    TREND_TF:  str = "1d"
    H4_TF:     str = "4h"
    SIGNAL_TF: str = "1h"
    TP1_RR: float = 1.0
    TP2_RR: float = 2.0
    TP3_RR: float = 3.0
    TP2_CLOSE_PCT:        float = 0.60
    PAUSE_AFTER_LOSS_MIN: int   = 30
    CONFIRM_TIMEOUT_SEC:  int   = 300
    SCAN_H1_INTERVAL_MIN: int   = int(os.getenv("SCAN_H1_INTERVAL_MIN", "15").strip())
    SCAN_BATCH_SIZE:      int   = int(os.getenv("SCAN_BATCH_SIZE", "10").strip())
    SCAN_BATCH_DELAY:     float = float(os.getenv("SCAN_BATCH_DELAY", "1.0").strip())
    # Breakeven: move SL when price moves BE_TRIGGER_PCT% from entry in profit direction
    # 0 = use TP1 as trigger (original behaviour)
    BE_TRIGGER_PCT:       float = float(os.getenv("BE_TRIGGER_PCT", "1.0").strip())
    # SL is placed at entry + this % buffer (locks in tiny profit above fees)
    BE_BUFFER_PCT:        float = float(os.getenv("BE_BUFFER_PCT", "0.05").strip())
    # Trailing stop: move SL this % behind the peak price (after breakeven)
    TRAIL_PCT:            float = float(os.getenv("TRAIL_PCT", "1.0").strip())
    # Minimum position notional value in USDT (bot won't open smaller positions)
    MIN_POSITION_USDT:    float = float(os.getenv("MIN_POSITION_USDT", "20.0").strip())
    # Auto-leverage: adjust leverage based on balance tiers (True/False)
    AUTO_LEVERAGE:        bool  = os.getenv("AUTO_LEVERAGE", "true").strip().lower() == "true"
    # Time filter: skip scanning during low-liquidity hours (UTC)
    QUIET_HOURS_START:    int   = int(os.getenv("QUIET_HOURS_START", "0").strip())
    QUIET_HOURS_END:      int   = int(os.getenv("QUIET_HOURS_END", "7").strip())
    # BTC trend filter: skip LONG when BTC drops >2% in 3h, skip SHORT when BTC rises >2%
    BTC_FILTER:           bool  = os.getenv("BTC_FILTER", "true").strip().lower() == "true"
    BTC_FILTER_PCT:       float = float(os.getenv("BTC_FILTER_PCT", "2.0").strip())
    # ADX minimum — below this value market is ranging, skip signal
    ADX_MIN:              float = float(os.getenv("ADX_MIN", "22.0").strip())

cfg = Config()
