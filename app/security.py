from cryptography.fernet import Fernet


class TokenCipher:
    def __init__(self, key: bytes | str) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted: str) -> str:
        return self._fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
