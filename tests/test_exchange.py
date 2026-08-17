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
