import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.security import TokenCipher


def test_encrypt_hides_plaintext_and_decrypt_round_trips():
    cipher = TokenCipher(Fernet.generate_key())
    plaintext = "refresh-token-secret"

    encrypted = cipher.encrypt(plaintext)

    assert plaintext not in encrypted
    assert cipher.decrypt(encrypted) == plaintext


def test_invalid_key_raises_without_leaking_token_material():
    plaintext = "refresh-token-secret"
    encrypted = TokenCipher(Fernet.generate_key()).encrypt(plaintext)

    with pytest.raises(InvalidToken) as raised:
        TokenCipher(Fernet.generate_key()).decrypt(encrypted)

    assert plaintext not in str(raised.value)
    assert encrypted not in str(raised.value)
