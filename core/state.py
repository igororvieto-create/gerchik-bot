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
    # Запас до встречного уровня в R. Читается трейдером: сделка, чей TP2=2R
    # лежит за уровнем-целью, структурно не может выиграть.
    headroom:    float    = 0.0
    # Лента исполненных сделок: направленное «усилие» из VSA. Пока ТОЛЬКО
    # измеряется и пишется в БД — на отбор и скор не влияет, пока не
    # наберётся статистика исходов по этим срезам.
    # None = ленты не было (сбой запроса, TRADE_FLOW_LIMIT=0). Отличать от
    # 0.0 («поток сбалансирован») обязательно: иначе срез по потоку меряет
    # не поток, а долю символов с недоступной лентой.
    flow_delta:    Optional[float] = None  # (покупки-продажи)/оборот по агрессору
    flow_span_min: float = 0.0   # сколько минут покрывает лента
    flow_absorb:   bool  = False # усилие есть, движения нет = поглощение
    sl_pct:      float    = 0.0
    # Замеры без влияния на решение (docs/LITERATURE.md §1 и §3).
    # ob_ratio: числовой перекос стакана. В БД лежала только корзина ob_bias,
    # поэтому проверить, добавляет ли голос стакана что-то к ev_r, было
    # нечем. confidence: доля согласных голосов — ею ограничивается score в
    # _apply_confluence_cap, но на исходах сам кап никогда не проверялся.
    # round_dist_atr: дистанция до круглого числа в ATR (Osler 2003) —
    # механизм, эмпирически объясняющий работу уровней, у нас не используется.
    ob_ratio:       float = 0.0
    confidence:     float = 0.0
    round_dist_atr: Optional[float] = None
    # Метка 4h-свечи, по которой построен сигнал. Нужна для дедупа: один
    # сетап = один сигнал. Раньше навешивалась на объект динамически
    # (sig._candle_ts) — работало только потому, что у dataclass нет
    # __slots__, и молча сломалось бы при их добавлении.
    candle_ts:   int      = 0
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
            "headroom":    round(self.headroom, 2),
            "flow_delta":   round(self.flow_delta, 3) if self.flow_delta is not None else None,
            "flow_span_min": round(self.flow_span_min, 1),
            "flow_absorb":  self.flow_absorb,
            "sl_pct":      round(self.sl_pct, 2),
            "ob_ratio":    round(self.ob_ratio, 3),
            "confidence":  round(self.confidence, 2),
            "round_dist_atr": (round(self.round_dist_atr, 2)
                               if self.round_dist_atr is not None else None),
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
    # Стоп уже перенесён в безубыток. Флаг нужен, чтобы монитор не пытался
    # переносить его повторно каждые 30 секунд и не «откатывал» стоп, уже
    # подтянутый выше безубытка.
    breakeven_done: bool  = False
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
        # Optional[Position]: значение None — это sentinel заброни-
        # рованного слота на время enter_trade. Тип отражает реальность,
        # иначе mypy не сможет ловить настоящие None-разыменования здесь.
        self.positions: Dict[str, Optional[Position]] = {}
        # symbol → (side, started_at) для входов В ПОЛЁТЕ. Слот в positions
        # резервируется значением None, которое стороны не несёт, поэтому
        # MAX_SAME_DIRECTION не видел параллельные входы и пропускал третий
        # однонаправленный вход при лимите 2. Отметка времени нужна монитору:
        # sentinel, переживший свой enter_trade (потерян ответ биржи), обязан
        # быть разрешён, иначе он навсегда занимает слот из MAX_POSITIONS.
        self.pending_entries: Dict[str, tuple] = {}
        self.balance: float = 0.0
        self.client: Any = None  # set by main.py after BybitClient init
        self.last_balance_error: str = ""
        self.last_scan_error: str = ""  # non-empty if the most recent scan failed
        self.signal_seen: Dict[str, datetime] = {}  # symbol → last broadcast time
        # Daily circuit breaker — defined here (not attached lazily) so a read
        # before the first _ensure_daily_state() can never raise AttributeError
        self.daily_pnl_date: Optional[str] = None
        self.daily_realized_pnl: float = 0.0
        self.trading_halted: bool = False

    def add_ws(self, ws) -> None:
        self.ws_clients.add(ws)

    def remove_ws(self, ws) -> None:
        self.ws_clients.discard(ws)


state = AppState()
