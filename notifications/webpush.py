"""Web Push — уведомления прямо в установленное приложение на телефоне.

Своя реализация вместо pywebpush: его зависимость http-ece распространяется
только исходниками и не собирается в нашем окружении — сборка образа на
Railway упала бы на ней. Протокол небольшой и полностью описан:
  * RFC 8291 — шифрование содержимого (aes128gcm, ECDH P-256 + HKDF);
  * RFC 8292 — VAPID: подпись запроса ключом сервера (JWT ES256).
Нужна только библиотека cryptography, у которой есть готовые сборки.

Ключ сервера — переменная окружения VAPID_PRIVATE_KEY: 32 байта закрытого
скаляра P-256 в base64url. Публичный ключ выводится из него и отдаётся
браузеру при подписке.
"""
import base64
import json
import logging
import os
import struct
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("webpush")

RECORD_SIZE = 4096


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pub_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def generate_private_key() -> str:
    """Новый ключ VAPID в формате переменной окружения."""
    k = ec.generate_private_key(ec.SECP256R1())
    return b64u(k.private_numbers().private_value.to_bytes(32, "big"))


def load_private_key(raw: str) -> Optional[ec.EllipticCurvePrivateKey]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        d = int.from_bytes(b64u_dec(raw), "big")
        return ec.derive_private_key(d, ec.SECP256R1())
    except Exception as e:
        log.error(f"VAPID_PRIVATE_KEY не читается — {e}")
        return None


def public_key_b64(priv: ec.EllipticCurvePrivateKey) -> str:
    return b64u(_pub_bytes(priv.public_key()))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt,
                info=info).derive(ikm)


def encrypt(payload: bytes, p256dh_b64: str, auth_b64: str,
            _salt: Optional[bytes] = None,
            _as_key: Optional[ec.EllipticCurvePrivateKey] = None) -> bytes:
    """Тело запроса по RFC 8291 (aes128gcm, одна запись).

    _salt и _as_key — только для тестов: в работе оба случайны на каждое
    сообщение, повтор соли с тем же ключом ломает шифрование."""
    ua_pub = b64u_dec(p256dh_b64)
    auth = b64u_dec(auth_b64)
    if len(ua_pub) != 65 or len(auth) != 16:
        raise ValueError("ключи подписки неверной длины")
    as_key = _as_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = _pub_bytes(as_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = _salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) + 1 + 16 > RECORD_SIZE:
        raise ValueError("сообщение длиннее одной записи")
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!I", RECORD_SIZE) + bytes([len(as_pub)]) + as_pub
    return header + ct


def vapid_header(endpoint: str, priv: ec.EllipticCurvePrivateKey,
                 subject: str, now: Optional[float] = None) -> str:
    """Authorization по RFC 8292: JWT ES256 на источник адреса подписки."""
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}",
              "exp": int((now or time.time()) + 12 * 3600),
              "sub": subject}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"},
                           separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={head}.{body}.{b64u(sig)}, k={public_key_b64(priv)}"


async def send(session: aiohttp.ClientSession, sub: Dict, message: Dict,
               priv: ec.EllipticCurvePrivateKey, subject: str,
               ttl: int = 24 * 3600, urgency: str = "normal") -> Tuple[int, str]:
    """Отправить одно уведомление. Возвращает (HTTP-код, текст ошибки).
    404/410 — подписка больше не существует, её надо удалить."""
    body = encrypt(json.dumps(message, ensure_ascii=False).encode(),
                   sub["p256dh"], sub["auth"])
    headers = {"Authorization": vapid_header(sub["endpoint"], priv, subject),
               "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream",
               "TTL": str(ttl), "Urgency": urgency}
    try:
        async with session.post(sub["endpoint"], data=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            text = "" if r.status < 300 else (await r.text())[:200]
            return r.status, text
    except Exception as e:
        return 0, str(e)
