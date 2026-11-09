"""打包鲁品开拍数据包：一致性快照 + 表行数 + README。

只读真实库（backend/data/lupin.db），用 VACUUM INTO 生成一致快照，
不修改源库、不做迁移。
"""
import csv
import hashlib
import io
import os
import sqlite3
import zipfile
from datetime import datetime

ROOT = r"D:/Chrome/交接包/鲁品开拍"
SRC_DB = os.path.join(ROOT, "项目代码", "backend", "data", "lupin.db")
OUT_DIR = ROOT
STAMP = datetime.now().strftime("%Y%m%d_%H%M")
SNAP = os.path.join(OUT_DIR, "_snap_lupin.db")
ZIP_PATH = os.path.join(OUT_DIR, f"鲁品开拍_数据包_{STAMP}.zip")

if os.path.exists(SNAP):
    os.remove(SNAP)

src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
src.execute(f"VACUUM INTO '{SNAP}'")
tables = [r[0] for r in src.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
ver = None
try:
    ver = src.execute("SELECT version_num FROM alembic_version").fetchone()
    ver = ver[0] if ver else None
except Exception:
    pass
src.close()

snap = sqlite3.connect(SNAP)
counts = []
for t in tables:
    try:
        n = snap.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except Exception:
        n = -1
    counts.append((t, n))

# 账号概览（只给邮箱/角色/组织，不含任何口令或哈希）
accounts = []
try:
    accounts = snap.execute(
        "SELECT u.email, m.role, o.name FROM users u "
        "LEFT JOIN memberships m ON m.user_id = u.id "
        "LEFT JOIN organizations o ON o.id = m.org_id ORDER BY u.email").fetchall()
except Exception:
    try:
        accounts = snap.execute("SELECT email FROM users ORDER BY email").fetchall()
    except Exception:
        accounts = []
snap.close()

buf = io.StringIO()
w = csv.writer(buf)
w.writerow(["table", "rows"])
for t, n in counts:
    w.writerow([t, n])
tables_csv = buf.getvalue().encode("utf-8-sig")

buf2 = io.StringIO()
w2 = csv.writer(buf2)
w2.writerow(["email", "role", "org"])
for a in accounts:
    w2.writerow(list(a) + [""] * (3 - len(a)))
accounts_csv = buf2.getvalue().encode("utf-8-sig")

nonempty = [(t, n) for t, n in counts if n > 0]
top = sorted(nonempty, key=lambda x: -x[1])[:12]

readme = f"""# 鲁品开拍 · 数据包

导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}（本机时间）
数据库版本（alembic head）：{ver}
来源：backend/data/lupin.db（业务真库，导出为只读快照，源库未被修改）

## 包内文件

| 文件 | 说明 |
|---|---|
| data/lupin.db | SQLite 一致性快照（VACUUM INTO 生成，可直接用 DB Browser 打开） |
| data/tables.csv | 每张表的行数 |
| data/accounts.csv | 账号/角色/组织概览（**不含口令与哈希**） |
| README.md | 本说明 |

## 数据规模（行数 > 0 的表，前 12）

| 表 | 行数 |
|---|---|
""" + "\n".join(f"| {t} | {n} |" for t, n in top) + f"""

全部表数量：{len(tables)}，其中有数据的 {len(nonempty)} 张。完整清单见 data/tables.csv。

## 用法

```bash
sqlite3 data/lupin.db ".tables"
sqlite3 data/lupin.db "select count(*) from works;"
```

## 注意

- 本快照的 alembic head 是 {ver}。若要用最新代码跑起来，先在项目根目录执行
  `alembic upgrade head`（线上部署库已到 `0022_poster_image_usage`）。

- 快照只含数据库，不含对象存储里的图片文件（海报配图以 base64 存在作品数据里，随库带出）。
- 账号表只提供邮箱与角色，任何情况下都不包含密码哈希与密钥。
"""

sha = hashlib.sha256(open(SNAP, "rb").read()).hexdigest()
with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    z.write(SNAP, "data/lupin.db")
    z.writestr("data/tables.csv", tables_csv)
    z.writestr("data/accounts.csv", accounts_csv)
    z.writestr("README.md", readme)

os.remove(SNAP)
size = os.path.getsize(ZIP_PATH)
print("ZIP", ZIP_PATH)
print("SIZE_MB", round(size / 1048576, 2))
print("SHA256_16", sha[:16])
print("TABLES", len(tables), "NONEMPTY", len(nonempty))
print("HEAD", ver)
print("TOP", top[:6])
