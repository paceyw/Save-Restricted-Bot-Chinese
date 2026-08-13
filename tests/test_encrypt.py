import base64
import binascii
import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

os.environ.setdefault("MASTER_KEY", "phase0-master-key")
os.environ.setdefault("IV_KEY", "phase0-iv-key")

from utils.encrypt import dcs, dyk, ecs


def _legacy_encrypt(plaintext):
    key = dyk()
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    ciphertext = encryptor.update(plaintext.encode()) + encryptor.finalize()
    return base64.b64encode(nonce + encryptor.tag + ciphertext).decode()


def test_ecs_dcs_roundtrip():
    plaintext = "session-token-中文"

    assert dcs(ecs(plaintext)) == plaintext


def test_ecs_uses_a_fresh_salt_for_each_ciphertext():
    first = ecs("same plaintext")
    second = ecs("same plaintext")

    assert first != second
    assert base64.b64decode(first)[:16] != base64.b64decode(second)[:16]


def test_dcs_reads_legacy_ciphertext():
    plaintext = "legacy"

    assert dcs(_legacy_encrypt(plaintext)) == plaintext


def test_corrupted_new_ciphertext_does_not_return_garbage():
    encoded = bytearray(base64.b64decode(ecs("must not decrypt")))
    encoded[-1] ^= 1

    with pytest.raises(InvalidTag):
        dcs(base64.b64encode(encoded).decode())


def test_dcs_rejects_plaintext_that_is_not_base64():
    with pytest.raises(binascii.Error):
        dcs("plain bot token")
