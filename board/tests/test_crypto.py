"""
Round-trip tests for the credential cipher.

The cipher is the only thing standing between an attacker with a database
dump and a usable model API key, so a few small tests are worth more than
they look.
"""

import pytest

from board.crypto import decrypt_secret, encrypt_secret


def test_roundtrip_returns_the_original_plaintext():
    pt = "sk-cp-EXAMPLE_abcdefghij1234567890_-OK"
    ct = encrypt_secret(pt)
    assert ct and ct != pt
    assert decrypt_secret(ct) == pt


def test_empty_string_round_trips_to_empty():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_decrypting_a_tampered_token_returns_empty():
    ct = encrypt_secret("sk-cp-real")
    # flip a byte in the middle; HMAC must catch it
    tampered = ct[:20] + ("A" if ct[20] != "A" else "B") + ct[21:]
    assert decrypt_secret(tampered) == ""


def test_two_identical_plaintexts_produce_different_ciphertexts():
    """
    Fernet uses a random IV, so equal inputs must not yield equal outputs.
    Otherwise log lines that include the ciphertext would leak equality of
    credentials across agents.
    """
    a = encrypt_secret("sk-cp-same")
    b = encrypt_secret("sk-cp-same")
    assert a != b


def test_long_plaintext_round_trips():
    pt = "x" * 4096
    assert decrypt_secret(encrypt_secret(pt)) == pt
