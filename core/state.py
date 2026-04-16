from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

@dataclass
class Position:
    symbol:      str
    side:        str
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    tp3:         float
    qty:         float
    risk_usdt:   float
    order_id:    str      = ""
    sl_order_id: str      = ""
    tp_order_id: str      = ""
    be_moved:    bool     = False
    tp2_hit:     bool     = False
    trail_price: float    = 0.0   # peak price tracked for trailing stop
    opened_at:   datetime = field(default_factory=datetime.utcnow)
    pattern:     str      = ""
    tf:          str      = "H1+H4"
    rr:          float    = 0.0
    score:       int      = 0

@dataclass
class DayStats:
    date:         date     = field(default_factory=date.today)
    trades:       int      = 0
    wins:         int      = 0
    losses:       int      = 0
    pnl_usdt:     float    = 0.0
    loss_streak:  int      = 0
    paused_until: Optional[datetime] = None

@dataclass
class BotState:
    positions: Dict[str, Position] = field(default_factory=dict)
    pending:   Dict[str, dict]     = field(default_factory=dict)
    day:       DayStats            = field(default_factory=DayStats)
    pairs:     List[str]           = field(default_factory=list)
    paused:    bool                = False
    total_pnl: float               = 0.0
    current_balance: float         = 0.0

    def reset_day(self):
        if self.day.date != date.today():
            self.day = DayStats()

    @property
    def is_paused(self):
        if self.paused:
            return True
        if self.day.paused_until and datetime.utcnow() < self.day.paused_until:
            return True
        return False

    def can_trade(self, max_daily_loss, max_positions, max_daily_trades):
        self.reset_day()
        if self.is_paused:
            return False, "бот на паузе"
        if len(self.positions) >= max_positions:
            return False, f"макс. позиций {max_positions}"
        if self.day.trades >= max_daily_trades:
            return False, f"макс. сделок {max_daily_trades}"
        if self.current_balance > 0:
            loss_pct = abs(min(self.day.pnl_usdt, 0)) / self.current_balance * 100
            if loss_pct >= max_daily_loss:
                return False, f"дневной лимит {max_daily_loss}%"
        if self.day.loss_streak >= 2:
            return False, "2 стопа подряд"
        return True, "ok"

state = BotState()
