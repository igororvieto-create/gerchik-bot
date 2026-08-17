"""Эндпоинты: рантайм-защита инвариантов, токен, контракт с фронтендом.

_clamp в core/config.py выполняется ТОЛЬКО при импорте, а /api/settings
делает setattr мимо него. Значит таблица spec в роуте — ЕДИНСТВЕННАЯ
защита инвариантов «риск <=3%» и «плечо <=5x» в рантайме, и до этого
файла она не проверялась ничем: правка границ прошла бы все тесты и
дала бы плечо 10x с телефона.
"""
import json

import pytest

import api.routes as R
from core.config import cfg


class FakeRequest:
    def __init__(self, token=None, body=None, path="/test"):
        self.headers = {"X-Dashboard-Token": token} if token else {}
        self.query_params = {}
        self.url = type("U", (), {"path": path})()
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def _code(resp):
    return getattr(resp, "status_code", 200)


def _body(resp):
    return json.loads(bytes(resp.body).decode("utf-8"))


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret-token")
    return "s3cret-token"


# ── Инварианты в рантайме ────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("leverage", 10),          # инвариант: плечо <= 5x
    ("leverage", 0),
    ("risk_per_trade", 5.0),   # инвариант: риск <= 3%
    ("risk_per_trade", 0.0),
    ("max_positions", 999),
])
async def test_settings_rejects_invariant_violations(no_token, field, value):
    before = {f: getattr(cfg, f.upper()) for f in
              ("leverage", "risk_per_trade", "max_positions")}
    resp = await R.update_settings(FakeRequest(body={field: value}))
    assert _code(resp) == 400, f"{field}={value} принято"
    for f, old in before.items():
        assert getattr(cfg, f.upper()) == old, f"{f} изменился несмотря на отказ"


async def test_settings_is_atomic(no_token):
    """Частичное сохранение хуже отказа: раньше ранние поля применялись,
    а UI показывал «не сохранено»."""
    saved = cfg.AUTO_TRADE
    cfg.AUTO_TRADE = False
    try:
        resp = await R.update_settings(
            FakeRequest(body={"auto_trade": True, "leverage": 10}))
        assert _code(resp) == 400
        assert cfg.AUTO_TRADE is False, "auto_trade применился при отказе тела"
    finally:
        cfg.AUTO_TRADE = saved


async def test_settings_string_false_does_not_enable_trading(no_token):
    """bool("false") is True — строка "false" ВКЛЮЧАЛА реальную торговлю."""
    saved = cfg.AUTO_TRADE
    cfg.AUTO_TRADE = False
    try:
        resp = await R.update_settings(FakeRequest(body={"auto_trade": "false"}))
        assert _code(resp) == 200
        assert cfg.AUTO_TRADE is False
        bad = await R.update_settings(FakeRequest(body={"auto_trade": "мусор"}))
        assert _code(bad) == 400
        assert cfg.AUTO_TRADE is False
    finally:
        cfg.AUTO_TRADE = saved


async def test_settings_cross_relations_enforced(no_token):
    """Поштучные диапазоны не ловят связки: риск 3% × 20 позиций = 60%
    одновременного риска при дневном лимите 6%."""
    resp = await R.update_settings(
        FakeRequest(body={"risk_per_trade": 3.0, "max_positions": 20}))
    assert _code(resp) == 400
    resp2 = await R.update_settings(
        FakeRequest(body={"min_score": 60, "trade_min_score": 45}))
    assert _code(resp2) == 400, "порог показа выше торгового делает торговый фиктивным"


# ── Токен ────────────────────────────────────────────────────────────────────

async def test_protected_endpoints_require_token(with_token):
    for fn in (R.get_positions, R.get_balance, R.get_stats, R.get_status):
        assert _code(await fn(FakeRequest())) == 401, f"{fn.__name__} открыт без токена"
        assert _code(await fn(FakeRequest(with_token))) != 401


async def test_cyrillic_token_does_not_crash(monkeypatch):
    """hmac.compare_digest на строках бросает TypeError на не-ASCII:
    кириллический токен ронял КАЖДЫЙ защищённый запрос в 500, а фронт
    молча показывал «—»."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "секретный-токен")
    assert _code(await R.get_positions(FakeRequest())) == 401
    assert _code(await R.get_positions(FakeRequest("неверный"))) == 401
    assert _code(await R.get_positions(FakeRequest("секретный-токен"))) == 200


async def test_no_token_configured_keeps_everything_open(no_token):
    """Без DASHBOARD_TOKEN интерфейс обязан работать как раньше."""
    assert _code(await R.get_positions(FakeRequest())) == 200


# ── Контракт с фронтендом ────────────────────────────────────────────────────

async def test_stats_carries_every_field_dashboard_reads(no_token):
    body = _body(await R.get_stats(FakeRequest()))
    for field in ("total_24h", "by_direction", "outcomes_7d", "scan_count",
                  "last_scan_at", "scan_error", "trading_halted",
                  "halt_reason", "daily_realized_pnl"):
        assert field in body, f"дашборд читает {field}, а бэкенд его не шлёт"
    for field in ("win", "loss", "be", "expired", "open", "winrate", "ev_r"):
        assert field in body["outcomes_7d"], f"карточка читает outcomes_7d.{field}"


async def test_outcomes_carries_slices_and_order(no_token):
    body = _body(await R.get_outcomes(FakeRequest()))
    for field in ("summary", "by_score", "by_direction", "by_type",
                  "by_sl_atr", "by_headroom", "by_flow", "_order"):
        assert field in body, f"модалка читает {field}"
    for key in ("by_score", "by_sl_atr", "by_headroom", "by_flow"):
        assert key in body["_order"], f"порядок корзин {key} не задан бэкендом"
