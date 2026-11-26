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
        for role, prefix in (("bc", "bc"), ("label_print", "label-print")):
            for access in ("bgd", "cas"):
                password = getattr(self.settings, f"{role}_{access}_password")
                if password:
                    hashed = await asyncio.to_thread(hash_password, password)
                    users.append((f"{prefix}-{access}@paratekno.com", hashed, access, role))
        if self.settings.admin_email and self.settings.admin_password:
            hashed = await asyncio.to_thread(hash_password, self.settings.admin_password)
            users.append((self.settings.admin_email.strip().lower(), hashed, "bgd", "admin"))
        if self.settings.dedup.store == "sqlite":
            self.sqlite = sqlite3.connect(self.settings.sqlite_path, check_same_thread=False)
            schema = """CREATE TABLE IF NOT EXISTS auth_users (
                email TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                access TEXT NOT NULL CHECK (access IN ('cas', 'bgd')),
                role TEXT NOT NULL DEFAULT 'operator' CHECK (role IN ('operator', 'bc', 'label_print', 'admin')),
                active BOOLEAN NOT NULL DEFAULT TRUE, session_version INTEGER NOT NULL DEFAULT 0
            )"""
            with self.sqlite:
                self.sqlite.execute("BEGIN")
                self.sqlite.execute(schema)
                existing = self.sqlite.execute("SELECT sql FROM sqlite_master WHERE name='auth_users'").fetchone()[0]
                if "session_version" not in existing:
                    columns = {row[1] for row in self.sqlite.execute("PRAGMA table_info(auth_users)")}
                    role = "role" if "role" in columns else "'operator'"
                    self.sqlite.execute("ALTER TABLE auth_users RENAME TO auth_users_previous")
                    self.sqlite.execute(schema)
                    self.sqlite.execute(
                        f"INSERT INTO auth_users (email, password_hash, access, role) SELECT email, password_hash, access, {role} FROM auth_users_previous"
                    )
                    self.sqlite.execute("DROP TABLE auth_users_previous")
                self.sqlite.executemany("INSERT OR IGNORE INTO auth_users (email, password_hash, access, role) VALUES (?, ?, ?, ?)", users)
        else:
            async with self.store._pool.acquire() as conn:
                await conn.executemany("INSERT INTO cv.auth_users (email, password_hash, access, role) VALUES ($1, $2, $3, $4) ON CONFLICT (email) DO NOTHING", users)

    async def get(self, email):
        if self.sqlite is not None:
            row = self.sqlite.execute("SELECT email, password_hash, access, role, active, session_version FROM auth_users WHERE email = ?", (email,)).fetchone()
            return dict(zip(("email", "password_hash", "access", "role", "active", "session_version"), row, strict=True)) if row else None
        async with self.store._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email, password_hash, access, role, active, session_version FROM cv.auth_users WHERE email = $1", email)
            return dict(row) if row else None

    async def list_users(self):
        if self.sqlite is not None:
            rows = self.sqlite.execute("SELECT email, access, role, active FROM auth_users ORDER BY email").fetchall()
            return [{"email": email, "access": access, "role": role, "active": bool(active)}
                    for email, access, role, active in rows]
        async with self.store._pool.acquire() as conn:
            return [dict(row) for row in await conn.fetch(
                "SELECT email, access, role, active FROM cv.auth_users ORDER BY email"
            )]

    async def create_user(self, email, access, role, password):
        hashed = await asyncio.to_thread(hash_password, password)
        if self.sqlite is not None:
            with self.sqlite:
                result = self.sqlite.execute(
                    "INSERT OR IGNORE INTO auth_users (email, password_hash, access, role) VALUES (?, ?, ?, ?)",
                    (email, hashed, access, role),
                )
            return result.rowcount == 1
        async with self.store._pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO cv.auth_users (email, password_hash, access, role) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (email) DO NOTHING RETURNING email", email, hashed, access, role,
            ) is not None

    async def update_user(self, email, access, role, active, password):
        hashed = await asyncio.to_thread(hash_password, password) if password is not None else None
        if self.sqlite is not None:
            with self.sqlite:
                result = self.sqlite.execute(
                    "UPDATE auth_users SET access=?, role=?, active=?, password_hash=COALESCE(?, password_hash), "
                    "session_version=session_version+1 WHERE email=? AND role <> 'admin'",
                    (access, role, active, hashed, email),
                )
            return result.rowcount == 1
        async with self.store._pool.acquire() as conn:
            return await conn.fetchval(
                "UPDATE cv.auth_users SET access=$1, role=$2, active=$3, password_hash=COALESCE($4, password_hash), "
                "session_version=session_version+1 WHERE email=$5 AND role <> 'admin' RETURNING email",
                access, role, active, hashed, email,
            ) is not None

    def close(self):
        if self.sqlite is not None:
            self.sqlite.close()
