"""
strategy/orderbook_analyzer.py

Анализ стакана ордеров для фильтрации сигналов стратегии.
Метрики: дисбаланс bid/ask, стенки, спред, тонкость книги.

Режимы работы (управляются через config):
  ORDERBOOK_ENABLED=false  — модуль не вызывается (по умолчанию)
  ORDERBOOK_ENABLED=true + ORDERBOOK_LOG_ONLY=true  — логирует, не блокирует
  ORDERBOOK_ENABLED=true + ORDERBOOK_LOG_ONLY=false — активная фильтрация
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Literal, Optional

log = logging.getLogger("orderbook")


# ─────────────────────────────────────────────── config ──

@dataclass
class OrderbookConfig:
    """Пороги для фильтрации сигналов по стакану."""

    # Дисбаланс bid/ask в ±3% от цены: перевес против сигнала > threshold → отказ
    imbalance_threshold: float = 0.15
    imbalance_depth_pct: float = 3.0

    # Стенка = ордер > wall_size_multiplier × медиана топ-5% уровней
    wall_size_multiplier: float = 3.0
    wall_min_size_usdt: float = 50_000

    # Стенка между entry и TP1 ближе чем этот ratio → блокируем
    wall_distance_min_ratio: float = 0.7

    # Тонкий стакан: суммарная глубина ±1% < порога (USDT)
    thin_book_threshold_usdt: float = 100_000
    thin_book_max_leverage: int = 3

    # Максимальный спред в bps (1 bps = 0.01%)
    max_spread_bps: float = 15.0

    # Минимум уровней в стакане для валидного анализа
    min_levels_required: int = 20


# ─────────────────────────────────────────────── data ──

@dataclass
class OrderbookSnapshot:
    """Снимок стакана на момент времени."""
    symbol: str
    timestamp_ms: int
    bids: list[tuple[float, float]]  # (price, qty), по убыванию цены
    asks: list[tuple[float, float]]  # (price, qty), по возрастанию цены

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def is_valid(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_ask > self.best_bid
            and len(self.bids) >= 5
            and len(self.asks) >= 5
        )


@dataclass
class Wall:
    """Крупная стенка в стакане."""
    price: float
    qty: float
    size_usdt: float
    side: Literal["bid", "ask"]


@dataclass
class OrderbookMetrics:
    """Метрики стакана."""
    symbol: str
    mid_price: float
    spread_bps: float

    # Дисбаланс: (bids - asks) / (bids + asks) — положительный = перевес покупателей
    imbalance_1pct: float
    imbalance_3pct: float

    depth_bids_1pct_usdt: float
    depth_asks_1pct_usdt: float
    depth_total_1pct_usdt: float

    nearest_bid_wall: Optional[Wall] = None
    nearest_ask_wall: Optional[Wall] = None
    all_walls: list[Wall] = field(default_factory=list)

    is_thin: bool = False
    is_valid: bool = True

    def summary(self) -> str:
        bid_w = (
            f"{self.nearest_bid_wall.price:.4f}({self.nearest_bid_wall.size_usdt/1000:.0f}k)"
            if self.nearest_bid_wall else "none"
        )
        ask_w = (
            f"{self.nearest_ask_wall.price:.4f}({self.nearest_ask_wall.size_usdt/1000:.0f}k)"
            if self.nearest_ask_wall else "none"
        )
        return (
            f"OB[{self.symbol}] imb3%={self.imbalance_3pct:+.2%} "
            f"spread={self.spread_bps:.1f}bps "
            f"thin={self.is_thin} "
            f"bid_wall={bid_w} ask_wall={ask_w}"
        )


@dataclass
class OrderbookValidation:
    """Результат проверки сигнала по стакану."""
    passed: bool
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_leverage: Optional[int] = None
    metrics: Optional[OrderbookMetrics] = None


# ─────────────────────────────────────────────── metrics ──

def compute_metrics(
    snapshot: OrderbookSnapshot,
    config: OrderbookConfig | None = None,
) -> OrderbookMetrics:
    """Рассчитать все метрики из снимка стакана."""
    if config is None:
        config = OrderbookConfig()

    if not snapshot.is_valid:
        return OrderbookMetrics(
            symbol=snapshot.symbol,
            mid_price=0, spread_bps=0,
            imbalance_1pct=0, imbalance_3pct=0,
            depth_bids_1pct_usdt=0, depth_asks_1pct_usdt=0,
            depth_total_1pct_usdt=0,
            is_valid=False,
        )

    mid = snapshot.mid_price
    if mid is None or snapshot.best_ask is None or snapshot.best_bid is None:
        return OrderbookMetrics(
            symbol=snapshot.symbol,
            mid_price=0, spread_bps=0,
            imbalance_1pct=0, imbalance_3pct=0,
            depth_bids_1pct_usdt=0, depth_asks_1pct_usdt=0,
            depth_total_1pct_usdt=0,
            is_valid=False,
        )
    spread_bps = (snapshot.best_ask - snapshot.best_bid) / mid * 10_000

    bids_1pct = _depth_in_range(snapshot.bids, mid, 1.0, "bid")
    asks_1pct = _depth_in_range(snapshot.asks, mid, 1.0, "ask")
    bids_3pct = _depth_in_range(snapshot.bids, mid, 3.0, "bid")
    asks_3pct = _depth_in_range(snapshot.asks, mid, 3.0, "ask")

    bid_walls = _find_walls(snapshot.bids, mid, "bid", config)
    ask_walls = _find_walls(snapshot.asks, mid, "ask", config)

    return OrderbookMetrics(
        symbol=snapshot.symbol,
        mid_price=mid,
        spread_bps=spread_bps,
        imbalance_1pct=_imbalance(bids_1pct, asks_1pct),
        imbalance_3pct=_imbalance(bids_3pct, asks_3pct),
        depth_bids_1pct_usdt=bids_1pct,
        depth_asks_1pct_usdt=asks_1pct,
        depth_total_1pct_usdt=bids_1pct + asks_1pct,
        nearest_bid_wall=max(bid_walls, key=lambda w: w.price) if bid_walls else None,
        nearest_ask_wall=min(ask_walls, key=lambda w: w.price) if ask_walls else None,
        all_walls=bid_walls + ask_walls,
        is_thin=(bids_1pct + asks_1pct) < config.thin_book_threshold_usdt,
        is_valid=True,
    )


def _depth_in_range(
    orders: list[tuple[float, float]],
    mid_price: float,
    pct_range: float,
    side: Literal["bid", "ask"],
) -> float:
    if side == "bid":
        lo = mid_price * (1 - pct_range / 100)
        return sum(p * q for p, q in orders if p >= lo)
    else:
        hi = mid_price * (1 + pct_range / 100)
        return sum(p * q for p, q in orders if p <= hi)


def _imbalance(bids_usdt: float, asks_usdt: float) -> float:
    total = bids_usdt + asks_usdt
    return 0.0 if total == 0 else (bids_usdt - asks_usdt) / total


def _find_walls(
    orders: list[tuple[float, float]],
    mid_price: float,
    side: Literal["bid", "ask"],
    config: OrderbookConfig,
) -> list[Wall]:
    if len(orders) < config.min_levels_required:
        return []
    sizes = [p * q for p, q in orders]
    top_n = max(5, len(sizes) // 20)
    median_top = statistics.median(sorted(sizes, reverse=True)[:top_n])
    threshold = max(config.wall_size_multiplier * median_top, config.wall_min_size_usdt)
    return [
        Wall(price=p, qty=q, size_usdt=p * q, side=side)
        for p, q in orders
        if p * q >= threshold
    ]


# ─────────────────────────────────────────────── validation ──

def validate_signal(
    side: str,            # "LONG" or "SHORT" from Signal.side
    entry: float,
    sl: float,
    tp1: float,
    leverage: int,
    metrics: OrderbookMetrics,
    config: OrderbookConfig | None = None,
) -> OrderbookValidation:
    """Проверить параметры сигнала по метрикам стакана."""
    if config is None:
        config = OrderbookConfig()

    result = OrderbookValidation(passed=True, metrics=metrics)

    if not metrics.is_valid:
        result.warnings.append("orderbook_invalid_skipping_filter")
        return result

    # 1. Широкий спред
    if metrics.spread_bps > config.max_spread_bps:
        result.passed = False
        result.rejections.append(f"wide_spread_{metrics.spread_bps:.1f}bps")

    # 2. Тонкий стакан + высокое плечо
    if metrics.is_thin:
        if leverage > config.thin_book_max_leverage:
            result.passed = False
            result.rejections.append(
                f"thin_book_lev_{leverage}x_max_{config.thin_book_max_leverage}x"
            )
            result.suggested_leverage = config.thin_book_max_leverage
        else:
            result.warnings.append("thin_book_acceptable_leverage")

    # 3. Дисбаланс против направления
    imb = metrics.imbalance_3pct
    if side == "LONG" and imb < -config.imbalance_threshold:
        result.passed = False
        result.rejections.append(f"imbalance_against_long_{imb:+.2%}")
    elif side == "SHORT" and imb > config.imbalance_threshold:
        result.passed = False
        result.rejections.append(f"imbalance_against_short_{imb:+.2%}")

    # 4. Стенка между entry и TP1 (блокирует движение к цели)
    if side == "LONG":
        wall = metrics.nearest_ask_wall
        if wall and entry < wall.price < tp1:
            d_wall = wall.price - entry
            d_tp = tp1 - entry
            if d_tp > 0 and d_wall < d_tp * config.wall_distance_min_ratio:
                result.passed = False
                result.rejections.append(
                    f"ask_wall_blocks_tp1_at_{wall.price:.4f}_{wall.size_usdt:.0f}usdt"
                )
    else:
        wall = metrics.nearest_bid_wall
        if wall and tp1 < wall.price < entry:
            d_wall = entry - wall.price
            d_tp = entry - tp1
            if d_tp > 0 and d_wall < d_tp * config.wall_distance_min_ratio:
                result.passed = False
                result.rejections.append(
                    f"bid_wall_blocks_tp1_at_{wall.price:.4f}_{wall.size_usdt:.0f}usdt"
                )

    # 5. Стенка за стопом (риск стоп-ханта)
    if side == "LONG":
        wall = metrics.nearest_bid_wall
        if wall and wall.price < sl:
            if (sl - wall.price) / entry < 0.003:
                result.warnings.append("bid_wall_just_below_sl_stop_hunt_risk")
    else:
        wall = metrics.nearest_ask_wall
        if wall and wall.price > sl:
            if (wall.price - sl) / entry < 0.003:
                result.warnings.append("ask_wall_just_above_sl_stop_hunt_risk")

    return result


# ─────────────────────────────────────────────── entry point ──

async def validate_signal_with_orderbook(
    sig,           # Signal from strategy/gerchik.py
    exchange,      # BingXClient instance
    leverage: int,
    config: OrderbookConfig | None = None,
    log_only: bool = True,
) -> OrderbookValidation:
    """Получить стакан и провалидировать сигнал.

    Args:
        sig:      Signal объект (имеет .symbol, .side, .entry, .sl, .tp1)
        exchange: BingXClient с методом get_orderbook()
        leverage: расчётное плечо (из auto-leverage логики)
        config:   пороги (None = дефолты)
        log_only: True = логировать, не блокировать (режим сбора данных)
    """
    if config is None:
        config = OrderbookConfig()

    try:
        raw = await exchange.get_orderbook(sig.symbol, limit=100)
        snapshot = _parse_bingx_orderbook(sig.symbol, raw)
    except Exception as e:
        log.error(f"[{sig.symbol}] Не удалось получить стакан: {e}")
        result = OrderbookValidation(passed=True)
        result.warnings.append(f"orderbook_fetch_error_{type(e).__name__}")
        return result

    metrics = compute_metrics(snapshot, config)
    log.info(metrics.summary())

    validation = validate_signal(
        side=sig.side,
        entry=sig.entry,
        sl=sig.sl,
        tp1=sig.tp1,
        leverage=leverage,
        metrics=metrics,
        config=config,
    )

    if log_only and not validation.passed:
        log.info(
            f"[{sig.symbol}] LOG_ONLY — сигнал не заблокирован, "
            f"был бы отклонён: {validation.rejections}"
        )
        validation.warnings.extend(f"would_reject:{r}" for r in validation.rejections)
        validation.rejections.clear()
        validation.passed = True

    if validation.warnings:
        log.debug(f"[{sig.symbol}] OB предупреждения: {validation.warnings}")

    return validation


def _parse_bingx_orderbook(symbol: str, raw: dict) -> OrderbookSnapshot:
    """Парсинг ответа BingX API /openApi/swap/v2/quote/depth."""
    data = raw.get("data", raw)
    bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
    return OrderbookSnapshot(
        symbol=symbol,
        timestamp_ms=int(data.get("T", 0)),
        bids=bids,
        asks=asks,
    )
