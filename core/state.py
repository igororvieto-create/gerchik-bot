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
    # round_pos — ниже, отдельным комментарием.
    ob_ratio:       float = 0.0
    confidence:     float = 0.0
    # round_pos: положение между круглыми числами, -1..+1 (знак = круглое
    # число ниже/выше цены). Заменил round_dist_atr, который мерил не
    # близость к круглому, а ведущую цифру цены — разбор в
    # scanner._round_number_pos.
    round_pos:      Optional[float] = None
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
            "round_pos":   (round(self.round_pos, 3)
                            if self.round_pos is not None else None),
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
    # ИСХОДНЫЙ размер позиции. qty уменьшается при частичном закрытии и
    # обнуляется риск-гардом, поэтому опираться на него в учёте закрытий
    # нельзя: ограничитель «суммируем записи, пока не закрыт объём Q» брал
    # Q равным ОСТАТКУ и обрывался на первой же частичной записи, теряя
    # PnL финального выхода; а при qty == 0 ограничитель отключался вовсе
    # и втягивал в сумму ЧУЖИЕ сделки по тому же символу.
    qty_opened:     float = 0.0
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
        # Результат ПОСЛЕДНЕГО скана. Дашборд писал в строку «Скан #N ·
        # найдено: X» две разные величины из двух источников: heartbeat по
        # WS слал число этого скана, а HTTP-фолбэк подставлял total_24h.
        # Пустой скан при живом фолбэке выглядел продуктивным — на экране
        # стояло суточное число под подписью про конкретный скан.
        self.last_scan_found: int = 0
        # Время старта процесса. Нужно, чтобы отличить «база сохранилась»
        # от «база стёрлась»: если самый старый сигнал СТАРШЕ процесса,
        # значит база пережила рестарт — это факт, а не догадка о пути.
        self.started_at: datetime = datetime.utcnow()
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
        # Монитор — единственное, что досылает и удерживает стоп у живых
        # позиций. У его задачи не было обработки исключений: падение
        # повторялось каждые 30 секунд, позиции оставались без присмотра, а
        # на дашборде не менялось НИЧЕГО — счётчик сканов рос, пульс горел
        # зелёным. Отмечаем время последнего УСПЕШНОГО прохода, чтобы
        # «работает» отличалось от «крутится вхолостую».
        self.last_monitor_ok: Optional[datetime] = None
        self.last_monitor_error: str = ""
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
