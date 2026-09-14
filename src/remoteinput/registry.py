import hashlib
import hmac
import secrets
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32, maxmem=64 << 20)


class Registry:
    """One durable SQLite database per relay; numeric IDs are never reused."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS targets (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_hash TEXT NOT NULL UNIQUE,
                salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,
                rotation_id TEXT
            )""")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def name(number):
        return f"TARGET-{number:03d}"

    @staticmethod
    def number(name):
        if not isinstance(name, str) or not name.startswith("TARGET-"):
            return -1
        tail = name[7:]
        return int(tail) if tail.isascii() and tail.isdigit() and len(tail) <= 12 else -1

    def register(self, owner, password):
        owner_hash = digest(owner)
        with self.db() as db:
            row = db.execute("SELECT number,generation FROM targets WHERE owner_hash=?", (owner_hash,)).fetchone()
            if row:
                return self.name(row["number"]), row["generation"]
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # The unique index resolves simultaneous first registration / retry safely.
            row = db.execute("SELECT number,generation FROM targets WHERE owner_hash=?", (owner_hash,)).fetchone()
            if not row:
                cursor = db.execute("INSERT INTO targets(owner_hash,salt,password_hash) VALUES(?,?,?)",
                                    (owner_hash, salt, hashed))
                return self.name(cursor.lastrowid), 1
            return self.name(row["number"]), row["generation"]

    def owner(self, name, secret):
        with self.db() as db:
            row = db.execute("SELECT owner_hash,generation FROM targets WHERE number=?", (self.number(name),)).fetchone()
            return row["generation"] if row and hmac.compare_digest(row["owner_hash"], digest(secret)) else None

    def login(self, name, password):
        with self.db() as db:
            row = db.execute("SELECT salt,password_hash,generation FROM targets WHERE number=?",
                             (self.number(name),)).fetchone()
        # Match the cost for unknown IDs; do not reveal whether an ID is registered.
        salt = row["salt"] if row else b"\0" * 16
        hashed = password_hash(password, salt)
        return row["generation"] if row and hmac.compare_digest(hashed, row["password_hash"]) else None

    def generation(self, name):
        with self.db() as db:
            row = db.execute("SELECT generation FROM targets WHERE number=?", (self.number(name),)).fetchone()
            return row[0] if row else None

    def rotate(self, name, owner, password, rotation_id):
        salt = secrets.token_bytes(16)
        hashed = password_hash(password, salt)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM targets WHERE number=?", (self.number(name),)).fetchone()
            if not row or not hmac.compare_digest(row["owner_hash"], digest(owner)):
                raise ValueError("Authentication failed")
            if row["rotation_id"] == rotation_id:
                return row["generation"]
            generation = row["generation"] + 1
            db.execute("UPDATE targets SET salt=?,password_hash=?,generation=?,rotation_id=? WHERE number=?",
                       (salt, hashed, generation, rotation_id, self.number(name)))
            return generation


class RateLimit:
    """Bounded sliding windows. Count all attempts, including concurrent in-flight checks."""

    def __init__(self, attempts=8, seconds=60, capacity=10000, clock=time.monotonic):
        self.attempts, self.seconds, self.capacity, self.clock = attempts, seconds, capacity, clock
        self.buckets = OrderedDict()

    def allow(self, key):
        now = self.clock()
        # Do not evict active buckets: varying IDs must not reset an attacker's limits.
        while self.buckets and self.buckets[next(iter(self.buckets))][-1] <= now - self.seconds:
            self.buckets.popitem(last=False)
        if key not in self.buckets and len(self.buckets) >= self.capacity:
            return False
        bucket = self.buckets.setdefault(key, deque())
        while bucket and bucket[0] <= now - self.seconds:
            bucket.popleft()
        allowed = len(bucket) < self.attempts
        if allowed:
            bucket.append(now)
        self.buckets.move_to_end(key)
        return allowed

