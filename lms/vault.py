"""Обратимое шифрование коротких секретов ключом проекта (без внешних зависимостей).

Нужно ровно для одного: учитель выдал ученику пароль и хочет увидеть его снова.
Django хранит только хэш, поэтому выданный пароль сохраняется отдельно — но не
открытым текстом, а зашифрованным ключом, производным от ``SECRET_KEY``.

Схема — encrypt-then-MAC на примитивах стандартной библиотеки:

* ключи шифрования и подписи выводятся из ``SECRET_KEY`` через HMAC-SHA256 с
  разными метками, так что утечка одного не раскрывает другой;
* поток ключа — HMAC-SHA256(enc_key, nonce ‖ счётчик), XOR с открытым текстом
  (PRF в режиме счётчика); nonce — 16 случайных байт на каждое шифрование;
* тег — HMAC-SHA256(mac_key, nonce ‖ шифртекст); проверяется до расшифровки,
  сравнение константное по времени.

Смена ``SECRET_KEY`` делает старые записи нечитаемыми: ``decrypt`` пробует
``SECRET_KEY_FALLBACKS`` и возвращает ``None``, если ничего не подошло —
интерфейс в этом случае предлагает выдать новый пароль.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from django.conf import settings

_PREFIX = "v1:"
_NONCE_SIZE = 16
_TAG_SIZE = 32
_BLOCK = hashlib.sha256().digest_size


def _derive(secret: str, label: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), b"lms.vault:" + label, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _secrets() -> list[str]:
    keys = [settings.SECRET_KEY]
    keys.extend(getattr(settings, "SECRET_KEY_FALLBACKS", None) or [])
    return [key for key in keys if key]


def encrypt(plaintext: str) -> str:
    """Зашифровать строку текущим ``SECRET_KEY``; результат — печатный токен."""
    if plaintext is None:
        raise ValueError("Нечего шифровать")
    secret = _secrets()[0]
    data = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_SIZE)
    stream = _keystream(_derive(secret, b"enc"), nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(_derive(secret, b"mac"), nonce + ciphertext, hashlib.sha256).digest()
    return _PREFIX + base64.urlsafe_b64encode(nonce + ciphertext + tag).decode("ascii")


def decrypt(token: str) -> str | None:
    """Расшифровать токен любым из действующих ключей; ``None``, если не подошёл ни один."""
    if not token or not token.startswith(_PREFIX):
        return None
    try:
        raw = base64.urlsafe_b64decode(token[len(_PREFIX) :].encode("ascii"))
    except (ValueError, TypeError):
        return None
    if len(raw) < _NONCE_SIZE + _TAG_SIZE:
        return None
    nonce, body, tag = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:-_TAG_SIZE], raw[-_TAG_SIZE:]
    for secret in _secrets():
        expected = hmac.new(_derive(secret, b"mac"), nonce + body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, tag):
            continue
        stream = _keystream(_derive(secret, b"enc"), nonce, len(body))
        try:
            return bytes(a ^ b for a, b in zip(body, stream)).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None
