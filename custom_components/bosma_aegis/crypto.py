"""Bosma app-compatible AES helpers. Inputs are never logged or persisted."""
from __future__ import annotations

import base64
import binascii
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Shared protocol constants embedded in the public Bosma Android app, not account credentials.
LOCK_IV = b'5beba2290a7fc515'
SESSION_PADDING = '61622324'


def decode_offline_key(encoded: str, mac_key: str) -> bytes:
    """Decode the account-authorized encrypted lock key returned by getOfflineKey."""
    try:
        wrapping_key = bytes.fromhex(mac_key + SESSION_PADDING)
        if len(wrapping_key) != 16:
            raise ValueError
        ciphertext = base64.b64decode(encoded, validate=True)
        if not ciphertext or len(ciphertext) % 16:
            raise ValueError
        decryptor = Cipher(algorithms.AES(wrapping_key), modes.CBC(LOCK_IV)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        key = unpadder.update(padded) + unpadder.finalize()
        if len(key) != 16:
            raise ValueError
        return key
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("Invalid Bosma offline-key response") from None


def encrypt_challenge(challenge: str, key: bytes) -> str:
    """Return the app's uppercase AES-CBC/PKCS7 challenge response."""
    try:
        if len(key) != 16:
            raise ValueError
        value = bytes.fromhex(challenge)
        if not value or len(value) > 256:
            raise ValueError
        padder = padding.PKCS7(128).padder()
        padded = padder.update(value) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(LOCK_IV)).encryptor()
        return (encryptor.update(padded) + encryptor.finalize()).hex().upper()
    except (ValueError, TypeError):
        raise ValueError("Invalid Bosma control challenge") from None
