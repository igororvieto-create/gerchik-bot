"""Уведомления на телефон: рассылка всем подпискам приложения + сторож.

Что приходит:
  * тревоги о здоровье бота — скан или монитор позиций остановились,
    потеряна связь с биржей — и отдельное «восстановлено»;
  * сильные сигналы (score >= 60) — из run_scan_and_broadcast;
  * итог недели бумажной стратегии — из lowvol_paper.

Тревога отправляется ОДИН раз за инцидент, а не на каждой проверке: сторож
ходит раз в 5 минут, и иначе телефон получал бы двенадцать одинаковых
сообщений в час — такие отключают первыми, вместе с настоящими.
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

import aiohttp

from core import db
from core.config import cfg
from core.state import state
from notifications import webpush

log = logging.getLogger("notify")

# Монитор позиций ходит каждые 30 с, скан — раз в SCAN_INTERVAL_MIN.
MONITOR_STALE_MIN = 5
SCAN_STALE_FACTOR = 3

_active: Dict[str, str] = {}      # ключ тревоги -> текст, пока инцидент длится


def enabled() -> bool:
    return webpush.load_private_key(cfg.VAPID_PRIVATE_KEY) is not None


async def broadcast(title: str, body: str, tag: str = "gerchik",
                    url: str = "/", urgency: str = "normal") -> int:
    """Отправить всем подпискам. Возвращает число доставленных."""
    priv = webpush.load_private_key(cfg.VAPID_PRIVATE_KEY)
    if priv is None:
        return 0
    subs = await db.push_subs()
    if not subs:
        return 0
    msg = {"title": title, "body": body, "tag": tag, "url": url}
    sent = 0
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[webpush.send(session, s, msg, priv, cfg.VAPID_SUBJECT, urgency=urgency)
              for s in subs])
    for s, (status, err) in zip(subs, results):
        if status in (404, 410):
            # подписка отозвана (приложение удалено, разрешение снято)
            await db.push_sub_remove(s["endpoint"])
        elif 200 <= status < 300:
            sent += 1
        else:
            log.warning(f"push {status}: {err}")
    return sent


def _problems(now: datetime) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if state.last_scan_error:
        out["scan_error"] = f"Скан падает: {state.last_scan_error[:120]}"
    scan_limit = cfg.SCAN_INTERVAL_MIN * SCAN_STALE_FACTOR
    if state.last_scan_at and (now - state.last_scan_at).total_seconds() / 60 > scan_limit:
        mins = int((now - state.last_scan_at).total_seconds() // 60)
        out["scan_stale"] = f"Скан не проходил {mins} мин"
    last_mon = getattr(state, "last_monitor_ok", None)
    if last_mon and (now - last_mon).total_seconds() / 60 > MONITOR_STALE_MIN:
        mins = int((now - last_mon).total_seconds() // 60)
        out["monitor_stale"] = (f"Монитор позиций не работал {mins} мин — "
                                f"стопы открытых позиций не проверяются")
    bybit_err = getattr(state, "last_balance_error", "") or ""
    if "timeout" in bybit_err.lower() or "403" in bybit_err:
        out["bybit"] = f"Нет связи с биржей: {bybit_err[:120]}"
    return out


async def watchdog(now: Optional[datetime] = None) -> None:
    """Проверка здоровья бота. Шлёт тревогу при НАЧАЛЕ инцидента и
    «восстановлено» при его конце — и ничего между."""
    now = now or datetime.utcnow()
    problems = _problems(now)
    for key, text in problems.items():
        if key not in _active:
            _active[key] = text
            await broadcast("⚠ Gerchik: проблема", text, tag=f"alert-{key}",
                            urgency="high")
    for key in list(_active):
        if key not in problems:
            text = _active.pop(key)
            await broadcast("✅ Gerchik: восстановлено", text, tag=f"alert-{key}")
