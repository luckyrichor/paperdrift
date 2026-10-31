import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT 'any',        -- any / landscape / portrait
  interval_hours REAL NOT NULL DEFAULT 24,
  batch_size INTEGER NOT NULL DEFAULT 30,
  nsfw INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run TEXT,
  last_status TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,                        -- image / gif / video
  page_url TEXT,
  preview_url TEXT,
  full_url TEXT,
  preview_path TEXT,
  local_path TEXT,
  width INTEGER,
  height INTEGER,
  orientation TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  phash TEXT,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',        -- new / liked / rejected
  reason TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(profile_id, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_items_profile_status ON items(profile_id, status);
CREATE TABLE IF NOT EXISTS tag_weights(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  weight REAL NOT NULL,
  PRIMARY KEY(profile_id, tag)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = str(path)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ---------- profiles ----------
    def create_profile(self, name, query, target="any", interval_hours=24, batch_size=30, nsfw=False) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO profiles(name,query,target,interval_hours,batch_size,nsfw,created_at) VALUES(?,?,?,?,?,?,?)",
                (name, query, target, interval_hours, batch_size, int(nsfw), now()),
            )
            return cur.lastrowid

    def get_profile(self, pid: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            return dict(r) if r else None

    def list_profiles(self):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM profiles ORDER BY id").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                counts = c.execute(
                    "SELECT status, COUNT(*) n FROM items WHERE profile_id=? GROUP BY status", (r["id"],)
                ).fetchall()
                d["counts"] = {x["status"]: x["n"] for x in counts}
                out.append(d)
            return out

    def update_profile(self, pid: int, **fields) -> None:
        allowed = {"name", "query", "target", "interval_hours", "batch_size", "nsfw", "enabled", "last_run", "last_status"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE profiles SET {sets} WHERE id=?", (*fields.values(), pid))

    def delete_profile(self, pid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM profiles WHERE id=?", (pid,))

    # ---------- items ----------
    def item_exists(self, pid, source, source_id) -> bool:
        with self.conn() as c:
            return c.execute(
                "SELECT 1 FROM items WHERE profile_id=? AND source=? AND source_id=?", (pid, source, source_id)
            ).fetchone() is not None

    def seen_ids(self, pid) -> set[tuple[str, str]]:
        with self.conn() as c:
            return {(r["source"], r["source_id"]) for r in c.execute(
                "SELECT source, source_id FROM items WHERE profile_id=?", (pid,))}

    def hashes(self, pid) -> list[str]:
        with self.conn() as c:
            return [r["phash"] for r in c.execute(
                "SELECT phash FROM items WHERE profile_id=? AND phash IS NOT NULL", (pid,))]

    def insert_item(self, pid, cand, preview_path, phash, score) -> int:
        with self.conn() as c:
            cur = c.execute(
                """INSERT INTO items(profile_id,source,source_id,kind,page_url,preview_url,full_url,preview_path,
                   width,height,orientation,tags,phash,score,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, cand.source, cand.source_id, cand.kind, cand.page_url, cand.preview_url, cand.full_url,
                 str(preview_path), cand.width, cand.height, cand.orientation, json.dumps(cand.tags, ensure_ascii=False),
                 phash, score, now()),
            )
            return cur.lastrowid

    def get_item(self, iid: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            return _item(r) if r else None

    def list_items(self, pid, status="new", kind=None, orientation=None, limit=50, offset=0):
        q = "SELECT * FROM items WHERE profile_id=? AND status=?"
        args: list = [pid, status]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if orientation:
            q += " AND orientation=?"
            args.append(orientation)
        order = "score DESC, id" if status == "new" else "decided_at DESC"
        q += f" ORDER BY {order} LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self.conn() as c:
            return [_item(r) for r in c.execute(q, args)]

    def set_decision(self, iid: int, status: str, reason: str | None) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE items SET status=?, reason=?, decided_at=? WHERE id=?",
                (status, reason, None if status == "new" else now(), iid),
            )

    def set_local_path(self, iid: int, path: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET local_path=? WHERE id=?", (path, iid))

    # ---------- weights ----------
    def weights(self, pid) -> dict[str, float]:
        with self.conn() as c:
            return {r["tag"]: r["weight"] for r in c.execute(
                "SELECT tag, weight FROM tag_weights WHERE profile_id=?", (pid,))}

    def bump_weights(self, pid, tags: list[str], delta: float) -> None:
        with self.conn() as c:
            for t in tags:
                c.execute(
                    """INSERT INTO tag_weights(profile_id, tag, weight) VALUES(?,?,?)
                       ON CONFLICT(profile_id, tag) DO UPDATE SET weight=weight+excluded.weight""",
                    (pid, t, delta),
                )


def _item(r) -> dict:
    d = dict(r)
    d["tags"] = json.loads(d["tags"])
    return d
