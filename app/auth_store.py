"""Persist browser accounts independently of the parcel store implementation."""

import asyncio
import hashlib
import hmac
import secrets
import sqlite3


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if algorithm != "scrypt" or (n, r, p) != ("16384", "8", "1"):
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


class AuthStore:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.sqlite = None

    async def initialize(self):
        users = [(f"{access}@paratekno.com", await asyncio.to_thread(hash_password, self.settings.bootstrap_password), access, "operator")
                 for access in ("cas", "bgd")]
        for access in ("bgd", "cas"):
            password = getattr(self.settings, f"bc_{access}_password")
            if password:
                hashed = await asyncio.to_thread(hash_password, password)
                users.append((f"bc-{access}@paratekno.com", hashed, access, "bc"))
        if self.settings.dedup.store == "sqlite":
            self.sqlite = sqlite3.connect(self.settings.sqlite_path, check_same_thread=False)
            self.sqlite.execute("CREATE TABLE IF NOT EXISTS auth_users (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, access TEXT NOT NULL CHECK (access IN ('cas', 'bgd')))")
            if "role" not in {row[1] for row in self.sqlite.execute("PRAGMA table_info(auth_users)")}:
                self.sqlite.execute("ALTER TABLE auth_users ADD COLUMN role TEXT NOT NULL DEFAULT 'operator' CHECK (role IN ('operator', 'bc'))")
            self.sqlite.executemany("INSERT OR IGNORE INTO auth_users (email, password_hash, access, role) VALUES (?, ?, ?, ?)", users)
            self.sqlite.commit()
        else:
            async with self.store._pool.acquire() as conn:
                await conn.executemany("INSERT INTO cv.auth_users (email, password_hash, access, role) VALUES ($1, $2, $3, $4) ON CONFLICT (email) DO NOTHING", users)

    async def get(self, email):
        if self.sqlite is not None:
            row = self.sqlite.execute("SELECT email, password_hash, access, role FROM auth_users WHERE email = ?", (email,)).fetchone()
            return dict(zip(("email", "password_hash", "access", "role"), row, strict=True)) if row else None
        async with self.store._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email, password_hash, access, role FROM cv.auth_users WHERE email = $1", email)
            return dict(row) if row else None

    def close(self):
        if self.sqlite is not None:
            self.sqlite.close()
