from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from synode.infrastructure.config import Settings


class SecretCipher:
    def __init__(self, settings: Settings):
        if not settings.secrets_key or not settings.secrets_key.strip():
            raise RuntimeError("SYNODE_SECRETS_KEY is required for DB secrets")
        digest = hashlib.sha256(settings.secrets_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        if not value.strip():
            raise ValueError("secret value is required")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_value: str) -> str:
        try:
            return self._fernet.decrypt(encrypted_value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("secret cannot be decrypted with SYNODE_SECRETS_KEY") from exc
