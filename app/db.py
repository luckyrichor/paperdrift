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
  search_terms TEXT,                          -- JSON：{for, wallhaven:[…], danbooru:[…], notes}，由 LLM 把中文描述翻译而来
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
  embedding BLOB,
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',        -- new / liked / rejected
  reason TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(profile_id, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_items_profile_status ON items(profile_id, status);
CREATE TABLE IF NOT EXISTS refs(
  id INTEGER PRIMARY KEY,
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  embedding BLOB,
  created_at TEXT NOT NULL
);
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
            cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
            if "embedding" not in cols:                 # 从 v0.1 升级
                c.execute("ALTER TABLE items ADD COLUMN embedding BLOB")
            if "purged_at" not in cols:
                c.execute("ALTER TABLE items ADD COLUMN purged_at TEXT")
            if "file_size" not in cols:
                c.execute("ALTER TABLE items ADD COLUMN file_size INTEGER")
            if "dl_error" not in cols:
                c.execute("ALTER TABLE items ADD COLUMN dl_error TEXT")
            pcols = {r["name"] for r in c.execute("PRAGMA table_info(profiles)")}
            if "search_terms" not in pcols:
                c.execute("ALTER TABLE profiles ADD COLUMN search_terms TEXT")

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
            return _profile(r) if r else None

    def list_profiles(self):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM profiles ORDER BY id").fetchall()
            out = []
            for r in rows:
                d = _profile(r)
                counts = c.execute(
                    "SELECT status, COUNT(*) n FROM items WHERE profile_id=? GROUP BY status", (r["id"],)
                ).fetchall()
                d["counts"] = {x["status"]: x["n"] for x in counts}
                d["refs"] = c.execute("SELECT COUNT(*) n FROM refs WHERE profile_id=?", (r["id"],)).fetchone()["n"]
                out.append(d)
            return out

    def update_profile(self, pid: int, **fields) -> None:
        allowed = {"name", "query", "target", "interval_hours", "batch_size", "nsfw", "enabled", "last_run", "last_status", "search_terms"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "search_terms" in fields and fields["search_terms"] is not None:
            fields["search_terms"] = json.dumps(fields["search_terms"], ensure_ascii=False)
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE profiles SET {sets} WHERE id=?", (*fields.values(), pid))

    def reset_stale_runs(self) -> None:
        """服务重启时，上次还标着「运行中」的其实已经被打断了。"""
        with self.conn() as c:
            c.execute("UPDATE profiles SET last_status='上次抓取被中断（服务重启），请重新抓取' WHERE last_status LIKE '运行中%'")

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

    def insert_item(self, pid, cand, preview_path, phash, score, embedding=None) -> int:
        with self.conn() as c:
            cur = c.execute(
                """INSERT INTO items(profile_id,source,source_id,kind,page_url,preview_url,full_url,preview_path,
                   width,height,orientation,tags,phash,embedding,score,created_at,file_size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, cand.source, cand.source_id, cand.kind, cand.page_url, cand.preview_url, cand.full_url,
                 str(preview_path), cand.width, cand.height, cand.orientation, json.dumps(cand.tags, ensure_ascii=False),
                 phash, embedding, score, now(), cand.file_size),
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
                "UPDATE items SET status=?, reason=?, decided_at=?, purged_at=NULL WHERE id=?",
                (status, reason, None if status == "new" else now(), iid),
            )

    def list_purgeable(self, cutoff_iso: str, pid: int | None = None) -> list[dict]:
        sql = "SELECT * FROM items WHERE status='rejected' AND purged_at IS NULL AND decided_at <= ?"
        args: list = [cutoff_iso]
        if pid is not None:
            sql += " AND profile_id=?"
            args.append(pid)
        with self.conn() as c:
            return [{**_item(r), "embedding": r["embedding"]} for r in c.execute(sql, args)]

    def mark_purged(self, iid: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET purged_at=?, local_path=NULL, dl_error=NULL WHERE id=?", (now(), iid))

    def set_preview_path(self, iid: int, path: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET preview_path=? WHERE id=?", (path, iid))

    def set_local_path(self, iid: int, path: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET local_path=?, dl_error=NULL WHERE id=?", (path, iid))

    def set_dl_error(self, iid: int, msg: str | None) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET dl_error=? WHERE id=?", (msg, iid))

    # ---------- 向量 / 参考图 ----------
    def add_ref(self, pid, path, embedding) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO refs(profile_id,path,embedding,created_at) VALUES(?,?,?,?)",
                             (pid, str(path), embedding, now())).lastrowid

    def list_refs(self, pid):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT id,path,embedding FROM refs WHERE profile_id=? ORDER BY id", (pid,))]

    def get_ref(self, rid):
        with self.conn() as c:
            r = c.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
            return dict(r) if r else None

    def delete_ref(self, rid) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM refs WHERE id=?", (rid,))

    def set_ref_embedding(self, rid, emb) -> None:
        with self.conn() as c:
            c.execute("UPDATE refs SET embedding=? WHERE id=?", (emb, rid))

    def decided_vectors(self, pid, status):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, preview_path, embedding FROM items WHERE profile_id=? AND status=?", (pid, status))]

    def set_item_embedding(self, iid, emb) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET embedding=? WHERE id=?", (emb, iid))

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


def _profile(r) -> dict:
    d = dict(r)
    try:
        d["search_terms"] = json.loads(d["search_terms"]) if d.get("search_terms") else None
    except ValueError:
        d["search_terms"] = None
    return d


def _item(r) -> dict:
    d = dict(r)
    d["tags"] = json.loads(d["tags"])
    d.pop("embedding", None)          # 二进制向量不进 API 输出
    return d
