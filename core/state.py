from dataclasses import dataclass, field
from datetime import datetime
from typing import Set, Any, Optional


@dataclass
class Signal:
    symbol:      str
    signal_type: str       # ACCUMULATION | DISTRIBUTION | SQUEEZE | VOLUME_SPIKE | FUNDING_EXTREME | MOMENTUM
    direction:   str       # LONG | SHORT | NEUTRAL
    score:       int
    price:       float
    oi_change:   float     # % change over last 4h
    vol_ratio:   float     # current vol / avg vol
    funding:     float
    ob_bias:     str       # BUY | SELL | NEUTRAL
    atr_pct:     float     # ATR as % of price
    details:     str
    # Entry levels (ATR-based suggestions)
    entry:       float     = 0.0
    sl:          float     = 0.0
    tp1:         float     = 0.0
    tp2:         float     = 0.0
    tp3:         float     = 0.0
    rr:          float     = 0.0
    sl_pct:      float     = 0.0   # SL distance from entry as %
    ts:          datetime  = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "symbol":      self.symbol,
            "signal_type": self.signal_type,
            "direction":   self.direction,
            "score":       self.score,
            "price":       self.price,
            "oi_change":   round(self.oi_change, 2),
            "vol_ratio":   round(self.vol_ratio, 2),
            "funding":     round(self.funding, 4),
            "ob_bias":     self.ob_bias,
            "atr_pct":     round(self.atr_pct, 3),
            "details":     self.details,
            "entry":       self.entry,
            "sl":          self.sl,
            "tp1":         self.tp1,
            "tp2":         self.tp2,
            "tp3":         self.tp3,
            "rr":          round(self.rr, 2),
            "sl_pct":      round(self.sl_pct, 2),
            "ts":          self.ts.isoformat() + "Z",
        }


class AppState:
    def __init__(self):
        self.ws_clients: Set[Any] = set()
        self.last_scan_at: Optional[datetime] = None
        self.scan_count: int = 0
        self.total_signals: int = 0

    def add_ws(self, ws) -> None:
        self.ws_clients.add(ws)

    def remove_ws(self, ws) -> None:
        self.ws_clients.discard(ws)


state = AppState()
