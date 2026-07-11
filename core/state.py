from dataclasses import dataclass, field
from datetime import datetime
from typing import Set, Any, Optional, Dict


@dataclass
class Signal:
    symbol:      str
    signal_type: str
    direction:   str
    score:       int
    price:       float
    oi_change:   float
    vol_ratio:   float
    funding:     float
    ob_bias:     str
    atr_pct:     float
    details:     str
    entry:       float    = 0.0
    sl:          float    = 0.0
    tp1:         float    = 0.0
    tp2:         float    = 0.0
    tp3:         float    = 0.0
    rr:          float    = 0.0
    sl_pct:      float    = 0.0
    ts:          datetime = field(default_factory=datetime.utcnow)

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


@dataclass
class Position:
    symbol:         str
    side:           str       # Buy | Sell
    entry:          float
    sl:             float
    tp1:            float
    tp2:            float
    tp3:            float
    qty:            float
    score:          int
    signal_type:    str
    order_id:       str   = ""
    unrealised_pnl: float = 0.0
    ts:             datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        direction = "LONG" if self.side == "Buy" else "SHORT"
        return {
            "symbol":         self.symbol,
            "side":           self.side,
            "direction":      direction,
            "entry":          self.entry,
            "sl":             self.sl,
            "tp1":            self.tp1,
            "tp2":            self.tp2,
            "tp3":            self.tp3,
            "qty":            self.qty,
            "score":          self.score,
            "signal_type":    self.signal_type,
            "order_id":       self.order_id,
            "unrealised_pnl": round(self.unrealised_pnl, 2),
            "ts":             self.ts.isoformat() + "Z",
        }


class AppState:
    def __init__(self):
        self.ws_clients: Set[Any] = set()
        self.last_scan_at: Optional[datetime] = None
        self.scan_count: int = 0
        self.total_signals: int = 0
        self.positions: Dict[str, Position] = {}
        self.balance: float = 0.0

    def add_ws(self, ws) -> None:
        self.ws_clients.add(ws)

    def remove_ws(self, ws) -> None:
        self.ws_clients.discard(ws)


state = AppState()
