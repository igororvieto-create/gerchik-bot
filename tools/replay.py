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
        rows = [r for r in self._h["oi"] if r["ts"] < self.now_ms]
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
    fund = [f for f in hist["funding"] if f["ts"] < now_ms]
    rate = fund[-1]["rate"] if fund else 0.0
    oi_rows = [r for r in hist["oi"] if r["ts"] < now_ms]
    oi_val = (oi_rows[-1]["oi"] * price) if oi_rows else 0.0
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


async def replay_symbol(hist: Dict[str, Any], step: int = 1) -> List[Dict]:
    """Прогон одного символа: сигналы + их исходы."""
    out: List[Dict] = []
    k4 = hist["k4"]
    # Разогрев: нужно хотя бы 26 закрытых свечей (kline_4h_limit) плюс запас
    warm = max(26, cfg.KEY_LEVEL_LOOKBACK + 2, cfg.MTF_TREND_LOOKBACK + 3) + 2
    for i in range(warm, len(k4), step):
        now_ms = k4[i]["ts"] + _H4_MS       # момент закрытия свечи i
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
        # прогон упадёт с AttributeError — и это правильно: значит харнесс
        # перестал соответствовать измеряемой стратегии и его надо чинить,
        # а не молча мерить старую.
        sig = await scanner._analyze_symbol(cast(BybitClient, client), ticker)
        if sig is None:
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
        f"\nСимволов: {len(meta.get('symbols', []))}   окно: {meta.get('days')} дн."
        f"\nСигналов с исходом: {len(rows)}"
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
    args = ap.parse_args()

    meta_path = os.path.join(args.hist, "_meta.json")
    if not os.path.isfile(meta_path):
        print(f"Нет {meta_path} — сначала запусти tools/fetch_history.py",
              file=sys.stderr)
        return 1
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    rows: List[Dict] = []
    for sym in meta["symbols"]:
        p = os.path.join(args.hist, f"{sym}.json")
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            hist = json.load(f)
        got = await replay_symbol(hist, step=args.step)
        rows.extend(got)
        print(f"{sym}: {len(got)} исходов", file=sys.stderr)

    print(report(rows, meta))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
