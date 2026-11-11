"""Server-side administrator accounts and revocable, scope-bound API keys.

The legacy VMO_ADMIN_KEY is retained as a *cryptographic root* for existing OOBE
secrets. HTTP authentication never compares incoming keys to that environment
variable; a one-time DB migration adds a revocable fingerprint of it.
"""
from __future__ import annotations

import getpass
from contextlib import contextmanager
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROUNDS = 600_000
MAX_FAILED_LOGINS = 3
LOGIN_LOCK_SECONDS = 15 * 60


class AdminLoginLocked(Exception):
    def __init__(self, retry_after: int):
        super().__init__('Administrator login is temporarily locked')
        self.retry_after = max(1, retry_after)

USER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{2,47}$')


def fingerprint(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class AdminAuth:
    def __init__(self, path: Path, legacy_admin_key: str = '', legacy_business_key: str = ''):
        self.path = path
        with self.connect() as d:
            d.executescript('''
                CREATE TABLE IF NOT EXISTS vmo_admin_users(
                    username TEXT PRIMARY KEY, password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vmo_admin_sessions(
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    expires_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vmo_api_keys(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
                    revoked_at INTEGER, last_used_at INTEGER);
                CREATE INDEX IF NOT EXISTS idx_vmo_admin_session_expiry
                    ON vmo_admin_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS vmo_security_audit(
                    id TEXT PRIMARY KEY, action TEXT NOT NULL,
                    detail TEXT NOT NULL, at INTEGER NOT NULL);
            ''')
            # Persist lockouts in the same SQLite DB so a process restart cannot
            # bypass the three-failure rule. Safe for existing v1.4.29 databases.
            existing = {r['name'] for r in d.execute('PRAGMA table_info(vmo_admin_users)')}
            if 'failed_login_count' not in existing:
                d.execute('ALTER TABLE vmo_admin_users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0')
            if 'locked_until' not in existing:
                d.execute('ALTER TABLE vmo_admin_users ADD COLUMN locked_until INTEGER NOT NULL DEFAULT 0')
            # Migrate once; do NOT recreate a key on restart after revocation.
            for ident, scope, source in [('legacy-admin', 'admin', legacy_admin_key),
                                         ('legacy-business', 'business', legacy_business_key)]:
                if source and len(source) >= 32:
                    d.execute('''INSERT OR IGNORE INTO vmo_api_keys
                        (id,name,scope,key_hash,created_at,revoked_at,last_used_at)
                        VALUES(?,?,?,?,?,NULL,NULL)''',
                        (ident, 'Migrated ' + scope + ' API key', scope, fingerprint(source), int(time.time())))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=20000')
        try:
            with conn:yield conn
        finally:conn.close()

    @staticmethod
    def hash_password(password: str, salt_hex: str) -> str:
        return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), ROUNDS).hex()

    def has_user(self) -> bool:
        with self.connect() as d:
            return d.execute('SELECT 1 FROM vmo_admin_users LIMIT 1').fetchone() is not None

    def bootstrap(self, username: str, password: str):
        if not USER_RE.fullmatch(username) or not password:
            raise ValueError('Username must be 3-48 characters; password must not be empty')
        salt = secrets.token_hex(24)
        hashed = self.hash_password(password, salt)
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            if d.execute('SELECT 1 FROM vmo_admin_users LIMIT 1').fetchone():
                raise ValueError('Administrator already exists; use the authenticated password-change API')
            d.execute('''INSERT INTO vmo_admin_users
                (username,password_salt,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)''',
                      (username, salt, hashed, int(time.time()), int(time.time())))
            d.execute('INSERT INTO vmo_security_audit VALUES(?,?,?,?)',
                      (str(uuid.uuid4()),'ADMIN_BOOTSTRAP',username,int(time.time())))

    def check_password(self, username: str, password: str) -> bool:
        with self.connect() as d:
            row = d.execute('SELECT password_salt,password_hash FROM vmo_admin_users WHERE username=?',
                            (username,)).fetchone()
        # Always perform password KDF to make account enumeration less useful.
        salt = row['password_salt'] if row else '00'*24
        hashed = self.hash_password(password, salt)
        return bool(row and hmac.compare_digest(hashed, row['password_hash']))

    def login(self, username: str, password: str) -> str | None:
        now = int(time.time())
        # All lockout checks and counter updates are serialized in SQLite.  This
        # prevents parallel requests from bypassing the third failed attempt.
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            row = d.execute('''SELECT password_salt,password_hash,failed_login_count,locked_until
                               FROM vmo_admin_users WHERE username=?''', (username,)).fetchone()
            if row and row['locked_until'] > now:
                raise AdminLoginLocked(row['locked_until'] - now)
            salt = row['password_salt'] if row else '00' * 24
            hashed = self.hash_password(password, salt)
            valid = bool(row and hmac.compare_digest(hashed, row['password_hash']))
            if not valid:
                if row:
                    # Expired locks start a fresh failure counter.
                    count = (row['failed_login_count'] if row['locked_until'] == 0 else 0) + 1
                    locked_until = now + LOGIN_LOCK_SECONDS if count >= MAX_FAILED_LOGINS else 0
                    d.execute('''UPDATE vmo_admin_users SET failed_login_count=?,locked_until=?
                                 WHERE username=?''', (count, locked_until, username))
                    if locked_until:
                        d.execute('INSERT INTO vmo_security_audit VALUES (?,?,?,?)',
                                  (str(uuid.uuid4()), 'ADMIN_LOGIN_LOCKED', username, now))
                        raise_lock = AdminLoginLocked(LOGIN_LOCK_SECONDS)
                    else:
                        raise_lock = None
                else:
                    raise_lock = None
            else:
                d.execute('''UPDATE vmo_admin_users SET failed_login_count=0,locked_until=0
                             WHERE username=?''', (username,))
                token = secrets.token_urlsafe(48)
                d.execute('DELETE FROM vmo_admin_sessions WHERE expires_at<=?', (now,))
                d.execute('INSERT INTO vmo_admin_sessions VALUES(?,?,?)',
                          (fingerprint(token), username, now + 8 * 3600))
                return token
        if raise_lock:
            raise raise_lock
        return None

    def session(self, token: str):
        if not token or len(token) > 512:
            return None
        with self.connect() as d:
            row = d.execute('SELECT username FROM vmo_admin_sessions WHERE token_hash=? AND expires_at>?',
                            (fingerprint(token), int(time.time()))).fetchone()
        return row['username'] if row else None

    def logout(self, token: str):
        if token:
            with self.connect() as d:
                d.execute('DELETE FROM vmo_admin_sessions WHERE token_hash=?', (fingerprint(token),))

    def change_password(self, username: str, current: str, new: str):
        if not new or not self.check_password(username, current):
            raise ValueError('Current password incorrect or new password is empty')
        if hmac.compare_digest(current, new):
            raise ValueError('New password must be different')
        salt = secrets.token_hex(24)
        with self.connect() as d:
            d.execute('UPDATE vmo_admin_users SET password_salt=?,password_hash=?,updated_at=?,failed_login_count=0,locked_until=0 WHERE username=?',
                      (salt,self.hash_password(new,salt),int(time.time()),username))
            d.execute('DELETE FROM vmo_admin_sessions WHERE username=?',(username,))
            d.execute('INSERT INTO vmo_security_audit VALUES(?,?,?,?)',
                      (str(uuid.uuid4()),'PASSWORD_CHANGED',username,int(time.time())))

    def check_api(self, token: str | None, scope: str) -> bool:
        if not token or len(token) > 1024 or scope not in ('admin','business'):
            return False
        digest = fingerprint(token)
        with self.connect() as d:
            row = d.execute('SELECT id FROM vmo_api_keys WHERE key_hash=? AND scope=? AND revoked_at IS NULL',
                            (digest,scope)).fetchone()
            if row:
                d.execute('UPDATE vmo_api_keys SET last_used_at=? WHERE id=?',(int(time.time()),row['id']))
        return row is not None

    def keys(self):
        with self.connect() as d:
            return [dict(row) for row in d.execute('''SELECT id,name,scope,created_at,revoked_at,last_used_at
                                  FROM vmo_api_keys ORDER BY created_at DESC''')]

    def create_key(self, name: str, scope: str):
        if scope not in ('admin','business') or not name.strip() or len(name)>80:
            raise ValueError('Invalid API key name or scope')
        token = 'vmo_' + scope + '_' + secrets.token_urlsafe(40)
        identifier = str(uuid.uuid4())
        with self.connect() as d:
            d.execute('INSERT INTO vmo_api_keys VALUES(?,?,?,?,?,NULL,NULL)',
                      (identifier,name.strip(),scope,fingerprint(token),int(time.time())))
            d.execute('INSERT INTO vmo_security_audit VALUES(?,?,?,?)',
                      (str(uuid.uuid4()),'API_KEY_CREATED',identifier+':'+scope,int(time.time())))
        return {'id':identifier,'token':token,'name':name.strip(),'scope':scope}

    def revoke_key(self, identifier: str):
        with self.connect() as d:
            res = d.execute('UPDATE vmo_api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                            (int(time.time()),identifier))
            if res.rowcount:
                d.execute('INSERT INTO vmo_security_audit VALUES(?,?,?,?)',
                          (str(uuid.uuid4()),'API_KEY_REVOKED',identifier,int(time.time())))
            return res.rowcount > 0


def cli_bootstrap():
    from .config import Settings
    s=Settings.from_env()
    if not s.db_path.exists():
        raise SystemExit('Database not found: start the Web Console once first')
    a=AdminAuth(s.db_path, s.admin_key, os.environ.get('VMO_BUSINESS_KEY',''))
    if a.has_user():
        raise SystemExit('Admin account already initialized; change password in the Web Console')
    username=input('Initial administrator username [admin]: ').strip() or 'admin'
    one=getpass.getpass('New password: ')
    two=getpass.getpass('Repeat password: ')
    if one != two:
        raise SystemExit('Passwords do not match')
    a.bootstrap(username,one)
    print('Administrator account initialized. The old management key is now a revocable admin API key.')


if __name__ == '__main__':
    cli_bootstrap()
