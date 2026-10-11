"""Уведомления в приложение на телефоне (Web Push)."""
import json
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from notifications import webpush as wp


def _rfc_key(b64: str):
    return ec.derive_private_key(int.from_bytes(wp.b64u_dec(b64), "big"), ec.SECP256R1())


def test_encryption_matches_rfc8291_appendix_a_bit_for_bit():
    """Эталон из самого стандарта: ключи, соль и ожидаемое тело заданы.
    Совпадение байт в байт — единственная проверка, которой можно верить:
    ошибка в шифровании не видна до тех пор, пока телефон молча не
    отбросит сообщение."""
    as_key = _rfc_key("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    body = wp.encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        _salt=wp.b64u_dec("DGv6ra1nlYgDCS1FRnbzlw"), _as_key=as_key)
    assert wp.b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
        "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt"
        "2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_each_message_uses_a_fresh_salt_and_key():
    """Повтор соли с тем же ключом ломает шифрование AES-GCM."""
    ua = ec.generate_private_key(ec.SECP256R1())
    p256 = wp.b64u(ua.public_key().public_bytes(
        wp.serialization.Encoding.X962, wp.serialization.PublicFormat.UncompressedPoint))
    auth = wp.b64u(b"0123456789abcdef")
    a, b = wp.encrypt(b"x", p256, auth), wp.encrypt(b"x", p256, auth)
    assert a[:16] != b[:16] and a[21:86] != b[21:86]


def test_vapid_header_is_a_valid_es256_jwt_for_the_endpoint_origin():
    priv = ec.generate_private_key(ec.SECP256R1())
    h = wp.vapid_header("https://fcm.googleapis.com/fcm/send/abc", priv,
                        "mailto:x@y.z", now=1_000_000)
    t = h.split("t=")[1].split(",")[0]
    k = h.split("k=")[1]
    head, body, sig = t.split(".")
    claims = json.loads(wp.b64u_dec(body))
    assert claims == {"aud": "https://fcm.googleapis.com",
                      "exp": 1_000_000 + 12 * 3600, "sub": "mailto:x@y.z"}
    raw = wp.b64u_dec(sig)
    assert len(raw) == 64, "подпись не в формате r||s (64 байта)"
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"),
                               int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.b64u_dec(k))
    pub.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


def test_private_key_roundtrip_through_env_format():
    raw = wp.generate_private_key()
    k = wp.load_private_key(raw)
    assert k is not None and wp.load_private_key("") is None
    assert wp.load_private_key("не base64") is None


# ── Сторож ─────────────────────────────────────────────────────────────────

@pytest.fixture
def dog(monkeypatch):
    """Сторож с подменённой рассылкой и здоровым ботом в момент now."""
    from notifications import notify
    from core.state import state
    sent = []
    deliver = {"ok": True}

    async def fake_broadcast(title, body, tag="", url="/", urgency="normal",
                             only_endpoint=None):
        sent.append((title, tag))
        return 1 if deliver["ok"] else 0
    monkeypatch.setattr(notify, "broadcast", fake_broadcast)
    for name in ("_active", "_seen", "_clear"):
        monkeypatch.setattr(notify, name, {})
    now = datetime(2026, 10, 10, 12, 0)
    monkeypatch.setattr(state, "started_at", now - timedelta(hours=5))
    monkeypatch.setattr(state, "last_scan_error", "")
    monkeypatch.setattr(state, "last_monitor_error", "")
    monkeypatch.setattr(state, "client", None)

    def healthy(t):
        monkeypatch.setattr(state, "last_scan_at", t)
        monkeypatch.setattr(state, "last_monitor_ok", t)
        monkeypatch.setattr(state, "last_positions_ok", t)
    return notify, state, sent, deliver, now, healthy, monkeypatch


async def test_watchdog_alerts_once_per_incident_and_once_on_recovery(dog):
    """Тревога — одна на инцидент, после двух проверок подряд; «восстановлено»
    — одно, тоже после двух. Без этого телефон получал бы двенадцать
    одинаковых сообщений в час — такие отключают первыми."""
    notify, state, sent, _, now, healthy, mp = dog
    for k in range(5):
        t = now + timedelta(minutes=5 * k)
        healthy(t)
        mp.setattr(state, "last_monitor_ok", now - timedelta(minutes=20))
        await notify.watchdog(t)
    assert [x for x, _ in sent] == ["⚠ Gerchik: проблема"], sent
    for m in (30, 35, 40):
        healthy(now + timedelta(minutes=m))
        await notify.watchdog(now + timedelta(minutes=m))
    assert [x for x, _ in sent] == ["⚠ Gerchik: проблема", "✅ Gerchik: восстановлено"]


async def test_flapping_error_does_not_spam(dog):
    """Пограничная ошибка скана то есть, то нет: прежний сторож слал
    сообщение на каждой проверке (6 проверок — 6 сообщений)."""
    notify, state, sent, _, now, healthy, mp = dog
    for k in range(6):
        t = now + timedelta(minutes=5 * k)
        healthy(t)
        mp.setattr(state, "last_scan_error", "данные не получены" if k % 2 == 0 else "")
        await notify.watchdog(t)
    assert sent == [], f"мигающая ошибка дала {len(sent)} сообщений"


async def test_undelivered_alert_is_retried(dog):
    """Тревога считается отправленной только после ДОСТАВКИ. Сбой сети
    бьёт одновременно по бирже и push-сервису — и прежний сторож терял
    именно эту тревогу, помечая её отправленной."""
    notify, state, sent, deliver, now, healthy, mp = dog
    deliver["ok"] = False
    for k in range(3):
        t = now + timedelta(minutes=5 * k)
        healthy(t)
        mp.setattr(state, "last_monitor_ok", now - timedelta(minutes=30))
        await notify.watchdog(t)
    assert len(sent) == 2 and not notify._active, "недоставленная тревога помечена отправленной"
    deliver["ok"] = True
    await notify.watchdog(now + timedelta(minutes=15))
    assert "monitor_stale" in notify._active


async def test_private_api_outage_is_detected_while_scan_works(dog):
    """Скан идёт (публичный API жив), монитор отрабатывает, но биржа не
    отдаёт позиции — стопы не проверяются. Прежний сторож этого не видел:
    монитор отмечал «успех» и при пропущенной проверке."""
    notify, state, sent, _, now, healthy, mp = dog
    mp.setattr(state, "client", object())
    mp.setattr(notify.cfg, "BYBIT_API_KEY", "k")
    for k in range(2):
        t = now + timedelta(minutes=5 * k)
        healthy(t)
        mp.setattr(state, "last_positions_ok", now - timedelta(minutes=30))
        await notify.watchdog(t)
    assert [tag for _, tag in sent] == ["alert-positions"], sent


async def test_monitor_that_never_ran_after_restart_is_detected(dog):
    """После деплоя с монитором, падающим с первого тика, отметка успеха
    пуста навсегда — прежний сторож при пустой отметке молчал."""
    notify, state, sent, _, now, healthy, mp = dog
    mp.setattr(state, "started_at", now - timedelta(minutes=20))
    for k in range(2):
        t = now + timedelta(minutes=5 * k)
        healthy(t)
        mp.setattr(state, "last_monitor_ok", None)
        mp.setattr(state, "last_monitor_error", "boom")
        await notify.watchdog(t)
    assert [tag for _, tag in sent] == ["alert-monitor_stale"], sent


async def test_broken_subscription_does_not_silence_the_others(tmp_path, monkeypatch):
    """Ключ не на кривой ронял шифрование, исключение вылетало из gather — и
    не доставлялось НИКОМУ, а битая подписка оставалась навсегда."""
    from notifications import notify
    monkeypatch.setattr(notify.db, "DB_PATH", str(tmp_path / "b.db"))
    await notify.db.init_db()
    good = wp.b64u(ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        wp.serialization.Encoding.X962, wp.serialization.PublicFormat.UncompressedPoint))
    await notify.db.push_sub_add("https://fcm.googleapis.com/bad",
                                 wp.b64u(b"\x04" + b"\x01" * 64), wp.b64u(b"0" * 16))
    await notify.db.push_sub_add("https://fcm.googleapis.com/good", good, wp.b64u(b"0" * 16))
    monkeypatch.setattr(notify.cfg, "VAPID_PRIVATE_KEY", wp.generate_private_key())
    delivered = []

    class Resp:
        status = 201
        async def text(self): return ""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    def fake_post(self, url, **kw):
        delivered.append(url)
        assert kw.get("allow_redirects") is False, "переход по редиректам не отключён"
        return Resp()
    monkeypatch.setattr(notify.aiohttp.ClientSession, "post", fake_post)
    assert await notify.broadcast("t", "b") == 1
    assert delivered == ["https://fcm.googleapis.com/good"]
    assert [s["endpoint"] for s in await notify.db.push_subs()] == \
        ["https://fcm.googleapis.com/good"], "битая подписка не удалена"


async def test_broadcast_drops_subscriptions_the_push_service_revoked(tmp_path, monkeypatch):
    """410 от push-сервиса = приложение удалено или разрешение снято.
    Подписку надо удалить, иначе каждый раз шлём в пустоту."""
    from notifications import notify
    import core.db as d
    monkeypatch.setattr(notify.db, "DB_PATH", str(tmp_path / "p.db"))
    await notify.db.init_db()
    ua = ec.generate_private_key(ec.SECP256R1())
    p256 = wp.b64u(ua.public_key().public_bytes(
        wp.serialization.Encoding.X962, wp.serialization.PublicFormat.UncompressedPoint))
    await notify.db.push_sub_add("https://push.example/a", p256, wp.b64u(b"0" * 16))
    await notify.db.push_sub_add("https://push.example/b", p256, wp.b64u(b"0" * 16))
    monkeypatch.setattr(notify.cfg, "VAPID_PRIVATE_KEY", wp.generate_private_key())

    async def fake_send(session, sub, msg, priv, subject, ttl=0, urgency=""):
        return (410, "gone") if sub["endpoint"].endswith("/a") else (201, "")
    monkeypatch.setattr(notify.webpush, "send", fake_send)
    assert await notify.broadcast("t", "b") == 1
    assert [s["endpoint"] for s in await notify.db.push_subs()] == ["https://push.example/b"]
