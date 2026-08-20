"""
Encrypts and decrypts GitHub Personal Access Tokens before storing in DB.
Uses Fernet symmetric encryption — only the server with ENCRYPTION_KEY can decrypt.
"""
import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set in environment variables.")
    return Fernet(key.encode())


def encrypt_token(plain_token: str) -> str:
    """Encrypt a GitHub PAT before saving to the database."""
    f = _get_fernet()
    return f.encrypt(plain_token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a GitHub PAT retrieved from the database."""
    f = _get_fernet()
    return f.decrypt(encrypted_token.encode()).decode()
