import pytest
"""Клиент биржи: перебор прокси, последний рубеж, сериализация.

Прокси заведены из-за гео-блока Bybit на IP Railway. Если перебор не
доходит до прямого соединения, get_positions() возвращает None, монитор
выходит со «skipping close check», и НЕПРЕРЫВНАЯ проверка наличия стопа —
единственная страховка для всех веток «не удалось подтвердить» в
enter_trade — прекращается. То есть рецидивирующий баг №1.
"""
import pytest

from exchange.bybit import BybitClient, _MAX_PROXY_TRIES, _fmt_price


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, body

    async def text(self):
        return self._b

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Прокси мертвы (или отдают заданный статус), прямое соединение живо."""
    def __init__(self, proxy_status=None):
        self.attempts = []
        self.proxy_status = proxy_status

    def request(self, method, url, **kw):
        proxy = kw.get("proxy")
        self.attempts.append(proxy)
        if proxy is not None:
            if self.proxy_status is None:
                raise ConnectionError("прокси мёртв")
            return _Resp(self.proxy_status, "blocked")
        return _Resp(200, '{"retCode":0,"result":{}}')


def _client_with(n_proxies, session, monkeypatch):
    monkeypatch.setenv("BYBIT_PROXY", "")
    monkeypatch.setenv("BYBIT_PROXY_2", "")
    c = BybitClient("k", "s", extra_proxies=[f"http://p{i}" for i in range(n_proxies)])
    c._session = session

    async def _get_session():
        return session
    c._get_session = _get_session
    return c


@pytest.mark.parametrize("n_proxies", [1, 4, 25])
async def test_direct_connection_tried_within_single_request(n_proxies, monkeypatch):
    """Прямое соединение стоит ПОСЛЕДНИМ в списке, а перебор ограничен
    потолком. Без явной попытки прямого оно оказывалось недостижимым в
    пределах запроса: при 25 прокси до него доходило лишь к седьмому —
    шесть отказов подряд, для монитора это три минуты без проверки стопов."""
    sess = _Session()
    c = _client_with(n_proxies, sess, monkeypatch)
    status, _ = await c._raw_request("GET", "https://x/y", lambda: {})
    assert status == 200
    assert None in sess.attempts, "прямое соединение не опробовано"


async def test_proxy_retry_budget_is_capped(monkeypatch):
    """Без потолка один запрос перебирал до 28 прокси × таймаут 3с × 3
    попытки = 250 секунд, и всё это время монитор держал _MONITORING=True."""
    sess = _Session()
    c = _client_with(25, sess, monkeypatch)
    await c._raw_request("GET", "https://x/y", lambda: {})
    # не больше потолка прокси + одна попытка прямого
    assert len(sess.attempts) <= _MAX_PROXY_TRIES + 1


async def test_http_error_triggers_failover(monkeypatch):
    """Гео-блок отдаёт HTTP 403 с телом, а не исключение. Раньше failover
    срабатывал ТОЛЬКО на исключении транспорта, и рабочее прямое
    соединение не пробовалось никогда."""
    sess = _Session(proxy_status=403)
    c = _client_with(3, sess, monkeypatch)
    status, _ = await c._raw_request("GET", "https://x/y", lambda: {})
    assert status == 200, "403 от прокси не вызвал переключения"
    assert None in sess.attempts


async def test_signature_is_rebuilt_on_every_attempt(monkeypatch):
    """RECV_WINDOW = 5 секунд: медленный ответ прокси означал, что ретрай
    уходит с протухшим timestamp и получает retCode=10002 — для
    set_trading_stop это выглядело как «биржа отвергла стоп»."""
    sess = _Session()
    c = _client_with(3, sess, monkeypatch)
    calls = {"n": 0}

    def sign():
        calls["n"] += 1
        return {"X-BAPI-TIMESTAMP": str(calls["n"])}

    await c._raw_request("GET", "https://x/y", sign)
    assert calls["n"] == len(sess.attempts), "подпись переиспользована между попытками"


def test_price_serialization_survives_round_trip():
    for v in (0.00001234, 7.0565e-05, 0.13665, 104.35, 1e-8):
        s = _fmt_price(v)
        assert "e" not in s.lower()
        assert float(s) == pytest.approx(v, rel=1e-9)


# ── Слой, ставящий и проверяющий СТОП ──────────────────────────────────────
#
# Покрытие этих двух функций было 2/14 и 5/21 строк. Именно здесь живёт
# рецидивирующий баг №1 («SL ставился как чарт-маркер и не долетал до
# биржи»), и на счёте, где ни одна реальная сделка не исполнялась, они
# проверены только тестами.

async def test_set_trading_stop_sends_the_stop_and_reports_truthfully():
    """Возврат True обязан означать «биржа приняла», а не «запрос ушёл».
    retCode 34040 — «уже установлено ровно так» — это тоже успех."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        sent = {}

        async def ok(path, body):
            sent["path"] = path
            sent["body"] = dict(body)
            return {"retCode": 0}
        c._post = ok
        assert await c.set_trading_stop("XUSDT", sl=1.234, tp=2.345) is True
        assert sent["path"] == "/v5/position/trading-stop"
        b = sent["body"]
        assert b["symbol"] == "XUSDT" and b["category"] == "linear"
        assert "stopLoss" in b and "takeProfit" in b, "стоп или цель не отправлены"
        # Стоп по МАРКЕ, а не по последней цене: последняя дёргается на
        # тонком рынке и выбивает стоп там, где марка не сдвинулась.
        assert b["slTriggerBy"] == "MarkPrice"

        async def already(path, body):
            return {"retCode": 34040, "retMsg": "not modified"}
        c._post = already
        assert await c.set_trading_stop("XUSDT", sl=1.0) is True, \
            "«уже установлено ровно так» — это успех, а не отказ"

        async def refused(path, body):
            return {"retCode": 10001, "retMsg": "params error"}
        c._post = refused
        assert await c.set_trading_stop("XUSDT", sl=1.0) is False, \
            "отказ биржи выдан за успех — позиция осталась бы без стопа"

        async def dead(path, body):
            return {}
        c._post = dead
        assert await c.set_trading_stop("XUSDT", sl=1.0) is False, \
            "пустой ответ выдан за успех"
    finally:
        await c.close()


async def test_set_trading_stop_refuses_a_call_with_nothing_to_set():
    """Вызов без SL и TP отправлял бы пустое тело и получал бы retCode 0 —
    то есть «успех», после которого стопа на бирже нет."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        called = []

        async def spy(path, body):
            called.append(body)
            return {"retCode": 0}
        c._post = spy
        assert await c.set_trading_stop("XUSDT", sl=0.0, tp=0.0) is False
        assert not called, "запрос отправлен, хотя ставить нечего"
    finally:
        await c.close()


async def test_get_positions_distinguishes_failure_from_no_positions():
    """None = биржа не ответила, [] = позиций нет. Путать нельзя: пустой
    список означал бы «своих позиций нет», и монитор перестал бы досылать
    стопы живым позициям."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        async def dead(path, params=None, auth=False):
            return {}
        c._get = dead
        assert await c.get_positions() is None, "отказ выдан за «позиций нет»"

        async def err(path, params=None, auth=False):
            return {"retCode": 10006, "retMsg": "rate limit"}
        c._get = err
        assert await c.get_positions() is None, "rate-limit выдан за «позиций нет»"

        async def empty(path, params=None, auth=False):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        c._get = empty
        assert await c.get_positions() == [], "пустой ответ обязан быть []"

        async def one(path, params=None, auth=False):
            return {"retCode": 0, "result": {
                "list": [{"symbol": "AUSDT", "size": "1"},
                         {"symbol": "BUSDT", "size": "0"}],
                "nextPageCursor": ""}}
        c._get = one
        got = await c.get_positions()
        assert [p["symbol"] for p in got] == ["AUSDT"], \
            "закрытая позиция (size=0) попала в список живых"
    finally:
        await c.close()


async def test_get_positions_cannot_loop_forever_on_a_stuck_cursor():
    """Неизменный курсор от биржи давал бесконечный цикл: monitor_positions
    висел с флагом «идёт», и НЕПРЕРЫВНАЯ проверка стопов — единственная
    страховка для всех веток «не удалось подтвердить» — прекращалась
    навсегда."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        calls = {"n": 0}

        async def stuck(path, params=None, auth=False):
            calls["n"] += 1
            if calls["n"] > 50:
                raise AssertionError("цикл не завершился — курсор не сдвинулся")
            return {"retCode": 0, "result": {
                "list": [{"symbol": f"S{calls['n']}USDT", "size": "1"}],
                "nextPageCursor": "SAME"}}
        c._get = stuck
        got = await c.get_positions()
        assert calls["n"] <= 3, f"лишние страницы при залипшем курсоре: {calls['n']}"
        assert got, "позиции первой страницы потеряны"
    finally:
        await c.close()


async def test_place_order_always_attaches_a_stop():
    """Рецидивирующий баг №1 в его исходной форме: ордер уходил без стопа
    либо со стопом «как чарт-маркер». Инвариант CLAUDE.md — SL обязателен
    ПЕРЕД входом, значит он обязан быть в теле самого ордера, а не
    досылаться отдельным запросом, который может не дойти."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        sent = {}

        async def spy(path, body):
            sent["path"] = path
            sent["body"] = dict(body)
            return {"retCode": 0, "result": {"orderId": "1"}}
        c._post = spy
        await c.place_order("XUSDT", "Buy", 1.5, sl=0.9, tp=1.2)
        b = sent["body"]
        assert sent["path"] == "/v5/order/create"
        assert b.get("stopLoss"), "ордер уходит БЕЗ стопа"
        assert float(b["stopLoss"]) == pytest.approx(0.9)
        assert float(b["takeProfit"]) == pytest.approx(1.2)
        # стоп по МАРКЕ: последняя цена дёргается на тонком рынке и выбивает
        # стоп там, где марка не сдвинулась
        assert b["slTriggerBy"] == "MarkPrice"
        assert b["orderType"] == "Market" and b["timeInForce"] == "IOC"
        # объём без хвостовых нулей — биржа отвергает "1.50000000" у части пар
        assert b["qty"] == "1.5", f"объём отправлен как {b['qty']!r}"
    finally:
        await c.close()


async def test_place_order_uses_a_fresh_idempotency_key_per_order():
    """orderLinkId стабилен ВНУТРИ одного вызова (повторы _post не должны
    открыть вторую позицию), но обязан отличаться МЕЖДУ вызовами — иначе
    второй честный вход был бы отвергнут как дубликат."""
    from exchange.bybit import BybitClient
    c = BybitClient()
    try:
        ids = []

        async def spy(path, body):
            ids.append(body["orderLinkId"])
            return {"retCode": 0}
        c._post = spy
        await c.place_order("XUSDT", "Buy", 1.0, sl=0.9, tp=1.1)
        await c.place_order("XUSDT", "Buy", 1.0, sl=0.9, tp=1.1)
        assert len(set(ids)) == 2, "два разных входа получили один orderLinkId"
        assert all(i.startswith("gb-") for i in ids)
    finally:
        await c.close()
