"""补齐 SQLite 缺失列，并把 alembic 版本戳到 0018_review。

背景：本库历史上是 create_all 建表（LUPIN_DB_AUTO_CREATE=1），
所以 alembic_version 停在 0007_media，但表其实已经建全，
只差 0010/0018 两次迁移新增的 3 个列。直接跑 upgrade 会撞
"table already exists"，因此这里按迁移里的定义精确补列再打戳。

用法：python _fix_schema.py <db 路径> [--stamp]
"""
import sqlite3
import sys

ALTERS = [
    ("facts", "scope",
     "ALTER TABLE facts ADD COLUMN scope VARCHAR(128) NOT NULL DEFAULT ''"),
    ("publish_records", "review_note",
     "ALTER TABLE publish_records ADD COLUMN review_note TEXT NOT NULL DEFAULT ''"),
    ("publish_records", "reviewed_at",
     "ALTER TABLE publish_records ADD COLUMN reviewed_at DATETIME"),
]
HEAD = "0018_review"


def cols(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def main(db, stamp=True):
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, col, sql in ALTERS:
        if table not in tables:
            print("  SKIP  %-18s 表不存在" % table)
            continue
        if col in cols(conn, table):
            print("  HAVE  %-18s %s 已存在" % (table, col))
            continue
        conn.execute(sql)
        print("  ADD   %-18s %s" % (table, col))
    conn.commit()
    if stamp:
        conn.execute("UPDATE alembic_version SET version_num=?", (HEAD,))
        conn.commit()
        print("  STAMP ->", HEAD)
    conn.close()


if __name__ == "__main__":
    path = sys.argv[1]
    main(path, stamp="--no-stamp" not in sys.argv)
