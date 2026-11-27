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
                role TEXT NOT NULL DEFAULT 'operator' CHECK (role IN ('operator', 'bc', 'label_print', 'admin', 'pjt_label_print')),
                active BOOLEAN NOT NULL DEFAULT TRUE, session_version INTEGER NOT NULL DEFAULT 0, pjt_code TEXT
            )"""
            with self.sqlite:
                self.sqlite.execute("BEGIN")
                self.sqlite.execute(schema)
                existing = self.sqlite.execute("SELECT sql FROM sqlite_master WHERE name='auth_users'").fetchone()[0]
                if "pjt_label_print" not in existing:
                    columns = {row[1] for row in self.sqlite.execute("PRAGMA table_info(auth_users)")}
                    fields = {
                        "role": "'operator'", "active": "TRUE", "session_version": "0", "pjt_code": "NULL",
                    }
                    values = ", ".join(field if field in columns else default for field, default in fields.items())
                    self.sqlite.execute("ALTER TABLE auth_users RENAME TO auth_users_previous")
                    self.sqlite.execute(schema)
                    self.sqlite.execute(
                        "INSERT INTO auth_users "
                        "(email, password_hash, access, role, active, session_version, pjt_code) "
                        f"SELECT email, password_hash, access, {values} FROM auth_users_previous"
                    )
                    self.sqlite.execute("DROP TABLE auth_users_previous")
                self.sqlite.executemany("INSERT OR IGNORE INTO auth_users (email, password_hash, access, role) VALUES (?, ?, ?, ?)", users)
        else:
            async with self.store._pool.acquire() as conn:
                await conn.executemany("INSERT INTO cv.auth_users (email, password_hash, access, role) VALUES ($1, $2, $3, $4) ON CONFLICT (email) DO NOTHING", users)

    async def get(self, email):
        if self.sqlite is not None:
            row = self.sqlite.execute("SELECT email, password_hash, access, role, active, session_version, pjt_code FROM auth_users WHERE email = ?", (email,)).fetchone()
            return dict(zip(("email", "password_hash", "access", "role", "active", "session_version", "pjt_code"), row, strict=True)) if row else None
        async with self.store._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email, password_hash, access, role, active, session_version, pjt_code FROM cv.auth_users WHERE email = $1", email)
            return dict(row) if row else None

    async def list_users(self):
        if self.sqlite is not None:
            rows = self.sqlite.execute("SELECT email, access, role, active, pjt_code FROM auth_users ORDER BY email").fetchall()
            return [{"email": email, "access": access, "role": "label_print" if role == "pjt_label_print" else role, "active": bool(active), **({"pjt_code": pjt_code} if pjt_code else {})}
                    for email, access, role, active, pjt_code in rows]
        async with self.store._pool.acquire() as conn:
            return [{key: value for key, value in row.items() if key != "pjt_code" or value is not None}
                    for row in await conn.fetch(
                "SELECT email, access, CASE WHEN role='pjt_label_print' THEN 'label_print' ELSE role END AS role, active, pjt_code FROM cv.auth_users ORDER BY email"
            )]

    async def list_pjt_codes(self, access):
        if self.sqlite is not None:
            return [row[0] for row in self.sqlite.execute(
                "SELECT DISTINCT pjt_code FROM auth_users WHERE access=? AND role='pjt_label_print' "
                "AND pjt_code IS NOT NULL ORDER BY pjt_code", (access,)
            )]
        async with self.store._pool.acquire() as conn:
            return [row["pjt_code"] for row in await conn.fetch(
                "SELECT DISTINCT pjt_code FROM cv.auth_users WHERE access=$1 AND role='pjt_label_print' "
                "AND pjt_code IS NOT NULL ORDER BY pjt_code", access
            )]

    async def create_user(self, email, access, role, password, pjt_code=None):
        role = "pjt_label_print" if role == "label_print" and pjt_code else role
        hashed = await asyncio.to_thread(hash_password, password)
        if self.sqlite is not None:
            with self.sqlite:
                result = self.sqlite.execute(
                    "INSERT OR IGNORE INTO auth_users (email, password_hash, access, role, pjt_code) VALUES (?, ?, ?, ?, ?)",
                    (email, hashed, access, role, pjt_code),
                )
            return result.rowcount == 1
        async with self.store._pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO cv.auth_users (email, password_hash, access, role, pjt_code) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (email) DO NOTHING RETURNING email", email, hashed, access, role, pjt_code,
            ) is not None

    async def update_user(self, email, access, role, active, password, pjt_code=None):
        role = "pjt_label_print" if role == "label_print" and pjt_code else role
        hashed = await asyncio.to_thread(hash_password, password) if password is not None else None
        if self.sqlite is not None:
            with self.sqlite:
                result = self.sqlite.execute(
                    "UPDATE auth_users SET access=?, role=?, active=?, password_hash=COALESCE(?, password_hash), "
                    "pjt_code=?, session_version=session_version+1 WHERE email=? AND role <> 'admin'",
                    (access, role, active, hashed, pjt_code, email),
                )
            return result.rowcount == 1
        async with self.store._pool.acquire() as conn:
            return await conn.fetchval(
                "UPDATE cv.auth_users SET access=$1, role=$2, active=$3, password_hash=COALESCE($4, password_hash), "
                "pjt_code=$5, session_version=session_version+1 WHERE email=$6 AND role <> 'admin' RETURNING email",
                access, role, active, hashed, pjt_code, email,
            ) is not None

    def close(self):
        if self.sqlite is not None:
            self.sqlite.close()
