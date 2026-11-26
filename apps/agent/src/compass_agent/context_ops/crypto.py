"""Authenticated, domain-separated snapshot encryption; redact before calling."""

import base64
import gzip
import hashlib
import json

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SnapshotCipher:
    def __init__(self, master_secret: str):
        if not master_secret:
            raise ValueError("snapshot_key_unavailable")
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"compass-context-snapshot-v1",
        ).derive(master_secret.encode())
        self.fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt_json(self, payload: dict) -> tuple[bytes, str]:
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("snapshot_too_large")
        return self.fernet.encrypt(gzip.compress(raw, mtime=0)), hashlib.sha256(raw).hexdigest()

    def decrypt_json(self, ciphertext: bytes, expected_digest: str) -> dict:
        raw = gzip.decompress(self.fernet.decrypt(ciphertext))
        if len(raw) > 4 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError("snapshot_corrupt")
        return json.loads(raw)
