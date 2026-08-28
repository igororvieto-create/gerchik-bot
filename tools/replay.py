"""Офлайн-прогон стратегии по скачанной истории.

Зачем: при текущем темпе (~16 исходов в неделю) до статистически значимой
выборки ~130 исходов — восемь недель. Прогон по истории даёт сотни исходов
за минуты.

ГЛАВНЫЙ ПРИНЦИП: прогон ВЫЗЫВАЕТ БОЕВОЙ КОД ОТБОРА, а не повторяет его.
`_analyze_symbol` вызывается как есть, вместе со всеми гейтами (анти-спайк,
MTF, близость к уровню, дрейф разворота, возраст листинга, MIN_RR).
Переписанная копия логики измеряла бы другую стратегию — и именно так
выглядит самая частая ошибка бэктестов.

ЧЕГО НЕТ В ИСТОРИИ (и потому нет в прогоне):
  * стакан — исторических снапшотов Bybit не отдаёт: в прогоне он
    нейтрален, очков не даёт и в голосовании не участвует;
  * лента сделок — то же, flow_* пустые.
В боевом скоринге эти два фактора дают 14-20 очков из ~64 и участвуют в
выборе направления. Значит прогон меряет ЯДРО (цена, объём, VSA, уровни,
фандинг, OI), а не боевую копию. В отчёте это написано прямо.

ДИСЦИПЛИНА (docs/LITERATURE.md §6): прогон делается ОДИН РАЗ и по его
результату НИЧЕГО не подкручивается. Порог, подобранный на выборке, нельзя
обосновывать той же выборкой — иначе получим переобучение и потеряем право
на любые выводы.

Запуск:
    python3 -m tools.replay --hist data/history
"""
import argparse
import asyncio
import json
import math
import os
import time
import sys
from typing import Any, Dict, List, Optional, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db                      # noqa: E402
from exchange.bybit import BybitClient    # noqa: E402
from core.config import cfg              # noqa: E402
import strategy.scanner as scanner       # noqa: E402
from strategy.evaluator import _judge, _mfe, _MAX_AGE_HOURS  # noqa: E402

_H4_MS = 4 * 3600 * 1000


class ReplayClient:
    """Клиент, отдающий историю СТРОГО до момента `now_ms`.

    Заглядывание в будущее — кардинальная ошибка бэктеста, поэтому отсечка
    здесь структурная: каждый метод фильтрует `ts < now_ms`, и обойти её
    нельзя, не переписав класс. Отдельный тест подкладывает в историю
    заведомо «отравленную» будущую свечу и требует, чтобы результат не
    изменился.
    """

    api_key = ""
    secret = ""

    def __init__(self, hist: Dict[str, Any], now_ms: int):
        self._h = hist
        self.now_ms = now_ms

    # --- вспомогательное -------------------------------------------------

    def _closed(self, key: str) -> List[Dict]:
        """Свечи, ЗАВЕРШЁННЫЕ к моменту now_ms.

        ts у Bybit — время НАЧАЛА свечи, поэтому свеча закрыта, когда
        ts + длительность <= now_ms. Сравнивать только по ts нельзя: свеча,
        начавшаяся минуту назад, ещё содержит будущее.
        """
        dur = {"k4": _H4_MS, "k1": 3600 * 1000, "k15": 15 * 60 * 1000}[key]
        return [k for k in self._h[key] if k["ts"] + dur <= self.now_ms]

    # --- интерфейс, который читает _analyze_symbol -----------------------

    async def get_klines(self, symbol: str, interval: str = "240",
                         limit: int = 25) -> List[Dict]:
        key = {"240": "k4", "60": "k1", "15": "k15"}[interval]
        closed = self._closed(key)
        if not closed:
            return []
        # Боевой клиент отдаёт ПОСЛЕДНЮЮ свечу незакрытой: весь анализ
        # рассчитан на klines[-1] = формирующаяся, klines[-2] = последняя
        # закрытая. Моделируем момент сразу после закрытия: формирующаяся
        # свеча вырожденная — открылась по цене закрытия предыдущей, хай =
        # лоу = открытие, объём ноль. Подставлять сюда настоящую следующую
        # свечу было бы прямым заглядыванием в будущее.
        last = closed[-1]
        forming = {"ts": last["ts"] + {"k4": _H4_MS, "k1": 3600 * 1000,
                                       "k15": 15 * 60 * 1000}[key],
                   "open": last["close"], "high": last["close"],
                   "low": last["close"], "close": last["close"], "volume": 0.0}
        tail = closed[-(limit - 1):] if limit > 1 else []
        return tail + [forming]

    async def get_open_interest(self, symbol: str, interval: str = "4h",
                                limit: int = 12) -> List[Dict]:
        # ГРАНИЦА ВКЛЮЧИТЕЛЬНО, и это НЕ заглядывание в будущее.
        #
        # Ряд OI строится фетчером как «последний замер НЕ ПОЗЖЕ границы»
        # (fetch_history_binance.open_interest), а сами границы совпадают с
        # закрытиями 4h-свечей — то есть с моментом анализа. Запись со
        # ts == now_ms содержит ровно то, что боевой бот читает из
        # ticker.openInterestValue прямо сейчас.
        #
        # Строгое '<' выбрасывало её и подставляло значение ПРЕДЫДУЩЕЙ
        # свечи: замер показал, что запись ровно в now_ms есть у 2159 из
        # 2160 моментов, а корреляция лагового ΔOI с актуальным всего
        # 0.03..0.26 — это разные величины. OI даёт до 30 очков из ~64 и
        # задаёт тип сигнала, так что прогон мерил не ту стратегию.
        rows = [r for r in self._h["oi"] if r["ts"] <= self.now_ms]
        return rows[-limit:]

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        # Исторических снапшотов нет. Пустой стакан даёт ratio 0.0 и
        # bias NEUTRAL — фактор просто не участвует, а не подменяется нулём
        # «книга сбалансирована»: разницу отчёт называет явно.
        return {"bids": [], "asks": []}

    async def get_recent_trades(self, symbol: str, limit: int = 500) -> List[Dict]:
        return []

    async def get_instrument_info(self, symbol: str) -> Dict:
        # Возраст листинга боевая функция считает от ФАКТИЧЕСКОГО «сейчас»
        # (datetime.now). Чтобы не подменять сам гейт — он часть измеряемой
        # стратегии — сдвигаем дату листинга так, чтобы возраст, посчитанный
        # от стенных часов, равнялся возрасту на МОДЕЛИРУЕМЫЙ момент. Сдвиг
        # сокращается точно, гейт исполняется как есть.
        #
        # Опорной точкой обязаны быть именно стенные часы, а не конец
        # истории: с концом истории гейт пропускал листинг возрастом 6 дней
        # при пороге 14 — поймано тестом.
        launch = float(self._h.get("launch_ms") or 0)
        if launch <= 0:
            return {"launchTime": 0}
        age_at_sim = self.now_ms - launch
        return {"launchTime": time.time() * 1000 - age_at_sim}


def build_ticker(hist: Dict[str, Any], now_ms: int) -> Optional[Dict]:
    """Тикер на момент now_ms из тех же полей, что читает _analyze_symbol."""
    dur = _H4_MS
    closed = [k for k in hist["k4"] if k["ts"] + dur <= now_ms]
    if len(closed) < 7:
        return None
    last = closed[-1]
    price = last["close"]
    if price <= 0:
        return None
    # Изменение за 24ч = 6 закрытых 4h-свечей назад
    ref = closed[-7]["close"]
    chg = (price - ref) / ref if ref > 0 else 0.0
    vol24 = sum(k["volume"] for k in closed[-6:])
    # Отсутствие данных НЕ подставляется нулём.
    #
    # Раньше здесь стояло `rate = ... if fund else 0.0`, и когда истории
    # фандинга не хватало, каждый сигнал получал ровно 0.0000%. Это тихо
    # выключало и голос фандинга, и очки за него: замер показал confidence
    # 0.4 у 100% сделок, то есть НИ ОДИН фактор ни разу не подтверждал
    # направление. Прогон мерил не стратегию, а её обрубок — и заметить
    # это по отчёту было нельзя.
    #
    # Теперь момент без данных просто не моделируется.
    fund = [f for f in hist["funding"] if f["ts"] < now_ms]
    if not fund:
        return None
    rate = fund[-1]["rate"]
    # Та же граница, что и в get_open_interest, и по той же причине.
    # Фандинг, наоборот, остаётся строгим: его записи — события в свой
    # момент, а не агрегат «на момент», и запас здесь консервативен.
    oi_rows = [r for r in hist["oi"] if r["ts"] <= now_ms]
    if not oi_rows:
        return None
    oi_val = oi_rows[-1]["oi"] * price
    return {
        "symbol": hist["symbol"],
        "lastPrice": str(price),
        "price24hPcnt": str(chg),
        "fundingRate": str(rate),
        "volume24h": str(vol24),
        "openInterestValue": str(oi_val),
    }


def judge_signal(hist: Dict[str, Any], sig, now_ms: int) -> Optional[tuple]:
    """(outcome, price, mfe_r) по 15м-свечам ПОСЛЕ входа, боевым _judge."""
    window = [k for k in hist["k15"]
              if now_ms <= k["ts"] < now_ms + _MAX_AGE_HOURS * 3600 * 1000]
    if not window:
        return None
    verdict = _judge(sig.direction, sig.sl, sig.tp2, window, entry=sig.entry)
    if verdict:
        return verdict
    # Окно исчерпано и вердикта нет — просрочка, как в боевом оценщике.
    covered_ms = window[-1]["ts"] - now_ms
    if covered_ms >= (_MAX_AGE_HOURS - 4) * 3600 * 1000:
        return ("EXPIRED", window[-1]["close"],
                _mfe(sig.direction, sig.entry, sig.sl, window))
    return None   # истории не хватило — исход НЕ выдумываем


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def half_window(start_ms: int, end_ms: int, half: str) -> tuple:
    """Границы половины выборки, С ЭМБАРГО (López de Prado, purging/embargo).

    Разрез идёт по моменту ВХОДА, а исход тянется до 48ч вперёд: сигнал за
    час до границы судится свечами из закрытой трети. Замер: 23 из 1730
    explore-сигналов (1.33%) так подсматривали holdout. Течёт только в эту
    сторону, но проверочная треть обязана быть неприкосновенной — поэтому у
    explore отрезаем последние 48 часов.
    """
    cut = start_ms + int((end_ms - start_ms) * 2 / 3)
    embargo = _MAX_AGE_HOURS * 3600 * 1000
    if half == "explore":
        return (start_ms, cut - embargo)
    if half == "holdout":
        return (cut, end_ms)
    return (0, 0)


def avg_concurrency(rows: List[Dict]) -> float:
    """Во сколько раз метки перекрываются (López de Prado, §5 LITERATURE).

    Интервал Уилсона считает наблюдения НЕЗАВИСИМЫМИ. У нас это неправда:
    исход тянется 48ч, и сигналы по одному символу внутри этого окна судятся
    во многом одними и теми же свечами. Отчёт по круглым числам заявлял
    точность ±0.6 п.п., хотя честная — вдвое-втрое хуже.

    Считаем среднюю «занятость»: сколько сигналов того же символа делят окно
    с данным (включая его самого). Эффективное n = n / этой величины.
    Межсимвольную корреляцию НЕ учитываем — оценка консервативна в сторону
    заниженного перекрытия, то есть интервал всё ещё слегка оптимистичен.
    """
    span = _MAX_AGE_HOURS * 3600 * 1000
    by_sym: Dict[str, List[int]] = {}
    for r in rows:
        by_sym.setdefault(str(r["symbol"]), []).append(int(r["ts"]))
    tot, cnt = 0, 0
    for ts_list in by_sym.values():
        ts_list.sort()
        lo = hi = 0
        for t in ts_list:
            while ts_list[lo] <= t - span:
                lo += 1
            while hi < len(ts_list) and ts_list[hi] < t + span:
                hi += 1
            tot += hi - lo
            cnt += 1
    return (tot / cnt) if cnt else 1.0


def wilson_overlap(k: int, n: int, rows: List[Dict]) -> tuple:
    """Интервал Уилсона по ЭФФЕКТИВНОМУ размеру выборки."""
    c = avg_concurrency(rows)
    return wilson(int(round(k / c)), max(1, int(round(n / c))))


def _acc(d: Dict, key: str, outcome: str, sl_pct: float) -> None:
    """Накопление корзины — теми же правилами, что в core/db.py.

    Комиссия копится ПОСТРОЧНО и только по решённым исходам: усреднение
    sl_pct занижало бы издержки (неравенство Йенсена), а просрочки в
    знаменатель матожидания не входят.
    """
    slot = d.setdefault(key, {"win": 0, "loss": 0, "be": 0, "expired": 0,
                              "_fee_sum": 0.0, "_fee_n": 0})
    k = outcome.lower()
    if k in slot:
        slot[k] += 1
    if sl_pct and sl_pct > 0 and k in ("win", "loss", "be"):
        slot["_fee_sum"] += db.ROUND_TRIP_FEE_PCT / sl_pct
        slot["_fee_n"] += 1


def _finish(d: Dict) -> None:
    for slot in d.values():
        fee = (slot["_fee_sum"] / slot["_fee_n"]) if slot["_fee_n"] else None
        slot.update(db._ev(slot, fee_r=fee))
        slot.pop("_fee_sum", None)
        slot.pop("_fee_n", None)


# Варианты структуры стратегии. Каждый — гипотеза, сформулированная ДО
# просмотра результата, а не порог, подобранный под него.
VARIANTS: Dict[str, Dict[str, Any]] = {
    # как сейчас в бою: пусто, потому что apply_variant сбрасывает ВСЕ флаги
    # в True перед применением варианта. Без этого сброса пустой словарь
    # ничего не восстанавливал — в одном процессе "nospike" после
    # "nofunding" давал фактически "both", а повторный "baseline"
    # отчитывался чужой конфигурацией: флаги живут в глобальном cfg.
    "baseline":   {},
    # фандинг задаёт сторону ~2/3 сделок, литературной опоры нет (§4)
    "nofunding":  {"FUNDING_VOTE": False},
    # без освобождения VSA бот перестаёт входить против вертикальных свечей
    "nospike":    {"VSA_SPIKE_EXEMPT": False},
    # обе сразу
    "both":       {"FUNDING_VOTE": False, "VSA_SPIKE_EXEMPT": False},
    # ВСЕ три освобождения VSA сняты: разворот проходит те же гейты, что и
    # трендовый вход — анти-спайк, MTF и близость к уровню. Гипотеза
    # цельная: освобождения вводились вместе и защищают инварианты,
    # которые бот сейчас обходит (CLAUDE.md: не торговать спайки).
    "strict":     {"VSA_SPIKE_EXEMPT": False, "VSA_MTF_EXEMPT": False,
                   "VSA_LEVEL_EXEMPT": False},
    # то же плюс снятый голос фандинга
    "strict_nf":  {"VSA_SPIKE_EXEMPT": False, "VSA_MTF_EXEMPT": False,
                   "VSA_LEVEL_EXEMPT": False, "FUNDING_VOTE": False},
}


_SWITCHES = ("FUNDING_VOTE", "VSA_SPIKE_EXEMPT",
             "VSA_MTF_EXEMPT", "VSA_LEVEL_EXEMPT")


def apply_variant(name: str) -> None:
    """Переключатели ставятся ДО прогона и не трогают боевые дефолты.

    Сначала ВСЕ флаги возвращаются к True, потом применяется вариант.
    Без сброса варианты накапливались друг на друга в одном процессе.
    """
    if name not in VARIANTS:
        raise SystemExit(f"неизвестный вариант {name}; есть: {list(VARIANTS)}")
    for k in _SWITCHES:
        setattr(cfg, k, True)
    for k, v in VARIANTS[name].items():
        setattr(cfg, k, v)


async def _make_signal(client, ticker, strategy: str):
    """Боевой отбор или экспериментальный вход по круглым числам.

    Боевой путь вызывается КАК ЕСТЬ. Круглые числа — отдельный модуль в
    tools/, боевого кода он не касается вовсе.
    """
    if strategy == "live":
        return await scanner._analyze_symbol(cast(BybitClient, client), ticker)
    from tools.round_strategy import analyze_round
    return await analyze_round(client, ticker, strategy.split("_", 1)[1])


async def replay_symbol(hist: Dict[str, Any], step: int = 1,
                        lo_ms: int = 0, hi_ms: int = 0,
                        long_only: bool = False,
                        strategy: str = "live") -> List[Dict]:
    """Прогон одного символа: сигналы + их исходы."""
    out: List[Dict] = []
    k4 = hist["k4"]
    # Разогрев: нужно хотя бы 26 закрытых свечей (kline_4h_limit) плюс запас
    warm = max(26, cfg.KEY_LEVEL_LOOKBACK + 2, cfg.MTF_TREND_LOOKBACK + 3) + 2
    for i in range(warm, len(k4), step):
        now_ms = k4[i]["ts"] + _H4_MS       # момент закрытия свечи i
        # Раздел выборки: половина для поиска гипотез, половина закрытая.
        # Проверять идею на тех же данных, где она найдена, значит мерить
        # собственную подгонку (docs/LITERATURE.md §6).
        if lo_ms and now_ms < lo_ms:
            continue
        if hi_ms and now_ms >= hi_ms:
            break
        ticker = build_ticker(hist, now_ms)
        if ticker is None:
            continue
        # Кэш возраста листинга зависит от моделируемого момента — чистим,
        # иначе первая же запись заморозила бы возраст на весь прогон.
        scanner._LISTING_AGE_CACHE.pop(hist["symbol"], None)
        client = ReplayClient(hist, now_ms)
        # cast, а не подавление проверки: ReplayClient намеренно реализует
        # ровно тот срез интерфейса BybitClient, который читает
        # _analyze_symbol. Если боевой анализ начнёт звать новый метод,
        # прогон упадёт с AttributeError — и это правильно.
        sig = await _make_signal(client, ticker, strategy)
        if sig is None:
            continue
        if long_only and sig.direction != "LONG":
            continue
        verdict = judge_signal(hist, sig, now_ms)
        if verdict is None:
            continue
        sl_atr = (sig.sl_pct / sig.atr_pct) if sig.atr_pct > 0 else 0.0
        out.append({
            "symbol": sig.symbol, "ts": now_ms, "score": sig.score,
            "direction": sig.direction, "type": sig.signal_type,
            "sl_pct": sig.sl_pct, "atr_pct": sig.atr_pct, "sl_atr": sl_atr,
            "headroom": sig.headroom, "confidence": sig.confidence,
            "round_pos": sig.round_pos,
            "outcome": verdict[0], "mfe_r": verdict[2],
        })
    return out


def _score_bucket(s: int) -> str:
    if s >= 60: return "60+"
    if s >= 45: return "45-59"
    return "30-44"


def _sl_bucket(r: float) -> str:
    if r < 1.0: return "<1.0 ATR"
    if r < 1.5: return "1.0-1.5"
    if r < 2.5: return "1.5-2.5"
    return ">2.5 ATR"


def _hr_bucket(h: float) -> str:
    if h < 2.0: return "1.5-2.0R (не торгуется)"
    if h < 3.0: return "2.0-3.0R"
    return ">3.0R"


def report(rows: List[Dict], meta: Dict) -> str:
    if not rows:
        return "Сигналов не найдено — проверь окно истории и пороги."
    overall: Dict = {}
    by_score: Dict = {}
    by_dir: Dict = {}
    by_type: Dict = {}
    by_sl: Dict = {}
    by_hr: Dict = {}
    tradable: Dict = {}
    for r in rows:
        _acc(overall, "все", r["outcome"], r["sl_pct"])
        _acc(by_score, _score_bucket(r["score"]), r["outcome"], r["sl_pct"])
        _acc(by_dir, r["direction"], r["outcome"], r["sl_pct"])
        _acc(by_type, r["type"], r["outcome"], r["sl_pct"])
        if r["sl_atr"] > 0:
            _acc(by_sl, _sl_bucket(r["sl_atr"]), r["outcome"], r["sl_pct"])
        if r["headroom"] > 0:
            _acc(by_hr, _hr_bucket(r["headroom"]), r["outcome"], r["sl_pct"])
        # Торгуемая популяция: те же два порога, что читает enter_trade
        if r["score"] >= cfg.TRADE_MIN_SCORE and r["headroom"] >= cfg.MIN_TRADE_HEADROOM_R:
            _acc(tradable, "торгуемые", r["outcome"], r["sl_pct"])
    for d in (overall, by_score, by_dir, by_type, by_sl, by_hr, tradable):
        _finish(d)

    def fmt(name: str, d: Dict) -> str:
        lines = [f"\n{name}"]
        for k, v in sorted(d.items(), key=lambda kv: -(kv[1]["win"] + kv[1]["loss"])):
            dec = v["win"] + v["loss"]
            lo, hi = wilson(v["win"], dec)
            ev = v.get("ev_r")
            lines.append(
                f"  {k:<26} n={dec + v['be'] + v['expired']:<5} "
                f"{v['win']}W/{v['loss']}L/{v['be']}BE/{v['expired']}E  "
                f"ev_r={('%+.3f' % ev) if ev is not None else '—':<8}"
                f"wr={(('%.1f%%' % (v['win'] / dec * 100)) if dec else '—'):<8}"
                f"CI95=[{lo * 100:.0f}%..{hi * 100:.0f}%]")
        return "\n".join(lines)

    head = (
        "=" * 72 +
        "\nПРОГОН СТРАТЕГИИ ПО ИСТОРИИ\n" + "=" * 72 +
        f"\nСтратегия: {meta.get('_strategy', 'live')}"
        f"   вариант: {meta.get('_variant', 'baseline')}"
        f"{'  ТОЛЬКО ЛОНГИ' if meta.get('_long_only') else ''}"
        f"{'  половина: ' + meta['_half'] if meta.get('_half') else ''}"
        f"\nИсточник данных: {meta.get('source', 'не указан')}"
        f"\nСимволов: {len(meta.get('symbols', []))}   окно: {meta.get('days')} дн."
        f"\nСигналов с исходом: {len(rows)}"
        f"   перекрытие меток: x{avg_concurrency(rows):.2f}"
        f" (эффективно ~{int(len(rows) / max(avg_concurrency(rows), 1e-9))})"
        f"\nПороги: MIN_SCORE={cfg.MIN_SCORE} TRADE_MIN_SCORE={cfg.TRADE_MIN_SCORE} "
        f"MIN_RR={cfg.MIN_RR} MIN_TRADE_HEADROOM_R={cfg.MIN_TRADE_HEADROOM_R}"
    )
    tail = (
        "\n\n" + "-" * 72 +
        "\nЧТО ЭТОТ ПРОГОН НЕ ИЗМЕРЯЕТ"
        "\n  * стакан и ленту сделок — исторических данных нет, в боевом"
        "\n    скоринге они дают 14-20 очков из ~64 и голосуют за направление;"
        "\n  * проскальзывание и реальные филлы — вход считается по цене"
        "\n    закрытия 4h-свечи;"
        "\n  * один момент на свечу (сразу после закрытия), а не каждые 4 мин."
        "\nЗначит это измерение ЯДРА стратегии, а не её боевой копии."
        "\n\nCI95 в таблицах посчитаны как для НЕЗАВИСИМЫХ наблюдений. Метки"
        "\nперекрываются (исход тянется 48ч), поэтому честный интервал шире"
        "\nв корень из перекрытия — множитель напечатан в шапке."
        "\n\nДИСЦИПЛИНА: прогон делается ОДИН РАЗ. Подкручивать пороги по его"
        "\nрезультату нельзя — порог, подобранный на выборке, нельзя"
        "\nобосновывать той же выборкой (docs/LITERATURE.md §6)."
    )
    return (head + fmt("ВСЕГО", overall) + fmt("ТОРГУЕМАЯ ПОПУЛЯЦИЯ", tradable) +
            fmt("ПО SCORE", by_score) + fmt("ПО НАПРАВЛЕНИЮ", by_dir) +
            fmt("ПО ТИПУ", by_type) + fmt("ПО ШИРИНЕ СТОПА", by_sl) +
            fmt("ПО ЗАПАСУ ДО ЦЕЛИ", by_hr) + tail)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", default=os.path.join("data", "history"))
    ap.add_argument("--step", type=int, default=1,
                    help="шаг по 4h-свечам (1 = каждая)")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--variant", default="baseline",
                    help=f"структура стратегии: {list(VARIANTS)}")
    ap.add_argument("--long-only", action="store_true",
                    help="отбрасывать шорты (шорты теряют втрое больше)")
    ap.add_argument("--strategy", default="live",
                    choices=["live", "round_fade", "round_break"],
                    help="live = боевой отбор; round_* = вход по круглым числам")
    ap.add_argument("--half", default="", choices=["", "explore", "holdout"],
                    help="explore = первые 2/3 окна, holdout = последняя треть")
    args = ap.parse_args()

    meta_path = os.path.join(args.hist, "_meta.json")
    if not os.path.isfile(meta_path):
        print(f"Нет {meta_path} — сначала запусти tools/fetch_history.py",
              file=sys.stderr)
        return 1
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    apply_variant(args.variant)
    lo_ms = hi_ms = 0
    if args.half:
        lo_ms, hi_ms = half_window(int(meta["start_ms"]), int(meta["end_ms"]),
                                   args.half)

    rows: List[Dict] = []
    for sym in meta["symbols"]:
        p = os.path.join(args.hist, f"{sym}.json")
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            hist = json.load(f)
        got = await replay_symbol(hist, step=args.step, lo_ms=lo_ms,
                                  hi_ms=hi_ms, long_only=args.long_only,
                                  strategy=args.strategy)
        rows.extend(got)
        print(f"{sym}: {len(got)} исходов", file=sys.stderr)

    meta["_strategy"] = args.strategy
    meta["_variant"] = args.variant
    meta["_long_only"] = args.long_only
    meta["_half"] = args.half
    print(report(rows, meta))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
