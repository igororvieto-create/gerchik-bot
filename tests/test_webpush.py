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


# ── Сторож: одна тревога на инцидент ───────────────────────────────────────

async def test_watchdog_alerts_once_per_incident_and_once_on_recovery(monkeypatch):
    """Сторож ходит раз в 5 минут. Без дедупликации телефон получал бы
    двенадцать одинаковых тревог в час — такие уведомления отключают первыми,
    вместе с настоящими."""
    from notifications import notify
    from core.state import state
    sent = []

    async def fake_broadcast(title, body, tag="", url="/", urgency="normal"):
        sent.append((title, tag))
        return 1
    monkeypatch.setattr(notify, "broadcast", fake_broadcast)
    monkeypatch.setattr(notify, "_active", {})
    now = datetime(2026, 10, 10, 12, 0)
    monkeypatch.setattr(state, "last_scan_error", "")
    monkeypatch.setattr(state, "last_balance_error", "")
    monkeypatch.setattr(state, "last_scan_at", now)
    monkeypatch.setattr(state, "last_monitor_ok", now - timedelta(minutes=20))

    for k in range(4):
        # скан идёт штатно — меняется только монитор
        monkeypatch.setattr(state, "last_scan_at", now + timedelta(minutes=5 * k))
        await notify.watchdog(now + timedelta(minutes=5 * k))
    assert [t for t, _ in sent] == ["⚠ Gerchik: проблема"], sent

    for m in (20, 25):
        # монитор ожил и работает штатно — отметка свежая на каждой проверке
        monkeypatch.setattr(state, "last_monitor_ok", now + timedelta(minutes=m))
        monkeypatch.setattr(state, "last_scan_at", now + timedelta(minutes=m))
        await notify.watchdog(now + timedelta(minutes=m))
    assert [t for t, _ in sent] == ["⚠ Gerchik: проблема", "✅ Gerchik: восстановлено"]


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
