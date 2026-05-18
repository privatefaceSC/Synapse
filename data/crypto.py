# Шифрование текста сообщений (Fernet). В ПРОДЕ обязательно задать
# SKILLWOOD_ENCRYPTION_KEY, иначе используется публичный dev-ключ.
import os

import sqlalchemy.types as types
from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"
# Dev-ключ. В проде ставь SKILLWOOD_ENCRYPTION_KEY=<свой fernet-ключ>.
# Сгенерировать: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
_DEV_KEY = "qkrCQOcCWmDg4ULLcQc6_J6Zi8hO_VSm3X_zXJL7vMc="


def _key() -> bytes:
    return (os.environ.get("SKILLWOOD_ENCRYPTION_KEY") or _DEV_KEY).encode("ascii")


def _cipher() -> Fernet:
    return Fernet(_key())


def encrypt(plaintext):
    if plaintext is None:
        return None
    token = _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value):
    """Расшифровать значение.

    - None - None.
    - Строки без префикса `enc:v1:` возвращаем как есть (обратная совместимость
      со старыми незашифрованными данными).
    - Битый шифротекст / чужой ключ - возвращаем как есть, чтобы не валить
      рендер UI на одном плохом сообщении.
    """
    if value is None or not isinstance(value, str):
        return value
    if not value.startswith(_PREFIX):
        return value
    try:
        token = value[len(_PREFIX):].encode("ascii")
    except UnicodeEncodeError:
        # Шифротекст должен быть чистым ASCII (base64). Если нет — это
        # не fernet-токен, возвращаем как есть.
        return value
    try:
        return _cipher().decrypt(token).decode("utf-8")
    except InvalidToken:
        return value


def encrypt_bytes(data: bytes) -> bytes:
    return _cipher().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    return _cipher().decrypt(token)


class EncryptedText(types.TypeDecorator):
    impl = types.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
