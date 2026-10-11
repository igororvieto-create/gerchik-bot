"""Уведомления на телефон: рассылка всем подпискам приложения + сторож.

Что приходит:
  * тревоги о здоровье бота — скан или монитор позиций остановились,
    биржа перестала отдавать позиции (стопы не проверяются) — и отдельное
    «восстановлено»;
  * сильные сигналы (score >= 60) — из run_scan_and_broadcast;
  * итог недели бумажной стратегии — из lowvol_paper.

Правила сторожа (ревью 2026-10-11):
  * тревога считается отправленной только когда её ДОСТАВИЛИ хотя бы одной
    подписке — иначе сбой сети, бьющий одновременно по бирже и push-сервису,
    съедал бы самую нужную тревогу;
  * проблема должна держаться две проверки подряд, чтобы стать тревогой, и
    отсутствовать две подряд, чтобы стать «восстановлено» — иначе
    пограничная ошибка скана давала сообщение на каждой проверке;
  * «не работал с момента старта» — тоже проблема: после рестарта поля
    пусты, и прежняя проверка молчала ровно после деплоя.
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Set

import aiohttp

from core import db
from core.config import cfg
from core.state import state
from notifications import webpush

log = logging.getLogger("notify")

MONITOR_STALE_MIN = 5          # монитор ходит каждые 30 с
POSITIONS_STALE_MIN = 5        # биржа отдаёт позиции каждый тик монитора
SCAN_STALE_FACTOR = 3          # скан — раз в SCAN_INTERVAL_MIN
CONFIRM_CHECKS = 2             # гистерезис: столько проверок подряд

_active: Dict[str, str] = {}   # тревога доставлена, инцидент длится
_seen: Dict[str, int] = {}     # подряд проверок с проблемой
_clear: Dict[str, int] = {}    # подряд проверок без проблемы (для active)
_bg: Set[asyncio.Task] = set()


def enabled() -> bool:
    return webpush.load_private_key(cfg.VAPID_PRIVATE_KEY) is not None


async def broadcast(title: str, body: str, tag: str = "gerchik",
                    url: str = "/", urgency: str = "normal",
                    only_endpoint: Optional[str] = None) -> int:
    """Отправить всем подпискам (или одной). Возвращает число доставленных.

    Отказ одной подписки не мешает остальным: исключение при шифровании
    (ключ не на кривой) раньше вылетало из gather и отменяло рассылку всем."""
    priv = webpush.load_private_key(cfg.VAPID_PRIVATE_KEY)
    if priv is None:
        return 0
    subs = await db.push_subs()
    if only_endpoint is not None:
        subs = [s for s in subs if s["endpoint"] == only_endpoint]
    if not subs:
        return 0
    msg = {"title": title, "body": body, "tag": tag, "url": url}
    sent = 0
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[webpush.send(session, s, msg, priv, cfg.VAPID_SUBJECT, urgency=urgency)
              for s in subs], return_exceptions=True)
    for s, res in zip(subs, results):
        if isinstance(res, BaseException):
            log.warning(f"push: подписка с негодными ключами удалена — {res}")
            await db.push_sub_remove(s["endpoint"])
            continue
        status, err = res
        if status in (404, 410):
            # подписка отозвана (приложение удалено, разрешение снято)
            await db.push_sub_remove(s["endpoint"])
        elif 200 <= status < 300:
            sent += 1
        else:
            log.warning(f"push {status}: {err}")
    return sent


def broadcast_bg(title: str, body: str, tag: str = "gerchik", **kw) -> None:
    """Рассылка в фоне — для мест, которые нельзя задерживать (цикл скана:
    зависший push-сервис держал бы вход по следующему сигналу до 15 с)."""
    async def run():
        try:
            await broadcast(title, body, tag=tag, **kw)
        except Exception as e:
            log.warning(f"push в фоне: {e}")
    t = asyncio.create_task(run())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def _mins(now: datetime, then: Optional[datetime]) -> Optional[float]:
    return None if then is None else (now - then).total_seconds() / 60


def _problems(now: datetime) -> Dict[str, str]:
    out: Dict[str, str] = {}
    up = _mins(now, state.started_at) or 0.0
    if state.last_scan_error:
        out["scan_error"] = f"Скан падает: {state.last_scan_error[:120]}"
    scan_limit = cfg.SCAN_INTERVAL_MIN * SCAN_STALE_FACTOR
    since_scan = _mins(now, state.last_scan_at)
    if (since_scan if since_scan is not None else up) > scan_limit:
        out["scan_stale"] = (f"Скан не проходил {int(since_scan)} мин" if since_scan
                             is not None else f"Скан не прошёл ни разу за {int(up)} мин после старта")
    since_mon = _mins(now, state.last_monitor_ok)
    if (since_mon if since_mon is not None else up) > MONITOR_STALE_MIN:
        err = f": {state.last_monitor_error[:100]}" if state.last_monitor_error else ""
        out["monitor_stale"] = ("Монитор позиций не работает — стопы открытых "
                                f"позиций не проверяются{err}")
    elif state.client is not None and cfg.BYBIT_API_KEY:
        # Монитор отработал, но позиции с биржи НЕ прочитаны: приватный API
        # недоступен (ключ, IP, гео-блок). Скан при этом идёт — публичный API
        # работает, — и раньше эту аварию не видел никто.
        since_pos = _mins(now, getattr(state, "last_positions_ok", None))
        if (since_pos if since_pos is not None else up) > POSITIONS_STALE_MIN:
            out["positions"] = ("Биржа не отдаёт позиции — стопы не проверяются "
                                "(приватный API: ключ, IP или блокировка)")
    return out


async def watchdog(now: Optional[datetime] = None) -> None:
    """Проверка здоровья бота, раз в 5 минут."""
    now = now or datetime.utcnow()
    problems = _problems(now)
    for key in list(_seen):
        if key not in problems:
            _seen.pop(key, None)
    for key, text in problems.items():
        _clear.pop(key, None)
        _seen[key] = _seen.get(key, 0) + 1
        if key in _active or _seen[key] < CONFIRM_CHECKS:
            continue
        if await broadcast("⚠ Gerchik: проблема", text, tag=f"alert-{key}",
                           urgency="high") > 0:
            _active[key] = text        # только ДОСТАВЛЕННАЯ тревога
    for key in list(_active):
        if key in problems:
            continue
        _clear[key] = _clear.get(key, 0) + 1
        if _clear[key] < CONFIRM_CHECKS:
            continue
        if await broadcast("✅ Gerchik: восстановлено", _active[key],
                           tag=f"alert-{key}") > 0:
            _active.pop(key, None)
            _clear.pop(key, None)
