from cryptography.fernet import Fernet


def validate_fernet_key(key: bytes | str) -> bytes:
    if isinstance(key, str):
        normalized = key.encode("utf-8")
    elif isinstance(key, bytes):
        normalized = key
    else:
        raise TypeError("Fernet key must be bytes or str")
    Fernet(normalized)
    return normalized


class TokenCipher:
    def __init__(self, key: bytes | str) -> None:
        self._fernet = Fernet(validate_fernet_key(key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted: str) -> str:
        return self._fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
