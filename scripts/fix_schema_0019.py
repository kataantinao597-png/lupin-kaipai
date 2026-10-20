"""给存量 SQLite 库补 ai_calls.org_id 并把 alembic 版本戳到 0019_ai_call_org。

背景：本库是 create_all 建的，alembic_version 落后，直接 upgrade 会撞
"table/column already exists"。这里按 0019 迁移的定义补列 + 建索引再打戳。

用法：python scripts/fix_schema_0019.py <db 路径> [--no-stamp]
"""
import sqlite3
import sys

HEAD = "0019_ai_call_org"
COL = "org_id"
TABLE = "ai_calls"
INDEX = "ix_aicall_org"


def cols(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def main(db, stamp=True):
    conn = sqlite3.connect(db)
    if not has_table(conn, TABLE):
        print("  SKIP  %s 表不存在" % TABLE)
        conn.close()
        return
    if COL in cols(conn, TABLE):
        print("  HAVE  %-18s %s 已存在" % (TABLE, COL))
    else:
        conn.execute("ALTER TABLE %s ADD COLUMN %s VARCHAR(32)" % (TABLE, COL))
        print("  ADD   %-18s %s" % (TABLE, COL))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS %s ON %s(%s)" % (INDEX, TABLE, COL))
    print("  IDX   %s" % INDEX)
    conn.commit()
    if stamp:
        conn.execute("UPDATE alembic_version SET version_num=?", (HEAD,))
        conn.commit()
        print("  STAMP ->", HEAD)
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1], stamp="--no-stamp" not in sys.argv)
