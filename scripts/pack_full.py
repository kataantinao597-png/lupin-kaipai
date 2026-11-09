"""打包鲁品开拍「项目完整包」：源码 + deploy 发布副本 + 数据快照 + 文档。

原则：
- 源库只读，用 VACUUM INTO 生成一致性快照（不改源库、不迁移）。
- 凡含密钥的文件（.env / env.production）一律不进包，只保留 .env.example。
- 虚拟环境、依赖目录、缓存、db 备份不进包。
- 产出 MANIFEST.json（逐文件 sha256），便于接收方核对完整性。
"""
import csv
import fnmatch
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime

ROOT = r"D:/Chrome/交接包/鲁品开拍"
SRC = os.path.join(ROOT, "项目代码")
DEPLOY = os.path.join(ROOT, "deploy")
LIVE_DB = os.path.join(SRC, "backend", "data", "lupin.db")

STAMP = datetime.now().strftime("%Y%m%d_%H%M")
PKG = f"鲁品开拍_项目完整包_{STAMP}"
ZIP_PATH = os.path.join(ROOT, PKG + ".zip")
TMP_DB = os.path.join(ROOT, "_pack_snapshot.db")

EXCLUDE_DIRS = {"venv", "node_modules", "__pycache__", ".pytest_cache", ".git",
                ".mypy_cache", "htmlcov", ".idea", ".vscode", ".ruff_cache"}
EXCLUDE_FILES = {".env", "env.production", ".DS_Store", "Thumbs.db", "lupin.db"}
EXCLUDE_GLOBS = ["*.pyc", "*.pyo", "*.log", "*.db-shm", "*.db-wal",
                 "*.db.bak*", "*.sqlite.bak*", "*.zip",
                 # 轻微 #9：私钥/密钥文件绝不能进部署包（原先未覆盖）
                 "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*"]

# 根目录额外收录的少量文件/目录
ROOT_EXTRAS = ["交接说明.md", "验证截图", "第二版独立展示页"]


def sha256_of(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def collect(base, prefix, drop_prefixes=()):
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES or any(fnmatch.fnmatch(fn, g) for g in EXCLUDE_GLOBS):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, base).replace("\\", "/")
            if any(rel.startswith(p) for p in drop_prefixes):
                continue
            out.append((full, f"{prefix}/{rel}"))
    return out


# ---------- 数据快照 ----------
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
con.execute(f"VACUUM INTO '{TMP_DB}'")
try:
    row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    alembic_live = row[0] if row else "?"
except Exception:
    alembic_live = "?"
con.close()

# 快照升级到代码 head：本地库若落后于代码，接手方直接跑会撞缺表。
# 只在快照副本上执行，真库不动；失败则保留原版本并在 README 说明。
alembic_head = alembic_live
alembic_upgrade = "skipped"
try:
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=SRC,
                       env={**os.environ, "DATABASE_URL": f"sqlite:///{TMP_DB}"},
                       capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        alembic_upgrade = "ok"
    else:
        alembic_upgrade = "failed: " + (r.stderr or r.stdout).strip()[-160:]
except Exception as e:
    alembic_upgrade = "error: " + str(e)[:160]

con2 = sqlite3.connect(TMP_DB)
tables = [r[0] for r in con2.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
try:
    row = con2.execute("SELECT version_num FROM alembic_version").fetchone()
    alembic_head = row[0] if row else alembic_live
except Exception:
    pass
con2.close()

snap = sqlite3.connect(TMP_DB)
counts = []
for t in tables:
    try:
        counts.append((t, snap.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]))
    except Exception:
        counts.append((t, -1))
try:
    accounts = snap.execute(
        "SELECT u.email, m.role, o.name FROM users u "
        "LEFT JOIN memberships m ON m.user_id = u.id "
        "LEFT JOIN organizations o ON o.id = m.org_id ORDER BY u.email").fetchall()
except Exception:
    accounts = []
try:
    works = snap.execute("SELECT COUNT(*) FROM works").fetchone()[0]
except Exception:
    works = -1
snap.close()

buf = io.StringIO(); w = csv.writer(buf); w.writerow(["table", "rows"])
for t, n in counts:
    w.writerow([t, n])
tables_csv = buf.getvalue().encode("utf-8-sig")

buf2 = io.StringIO(); w2 = csv.writer(buf2); w2.writerow(["email", "role", "org"])
for a in accounts:
    w2.writerow(list(a) + [""] * (3 - len(a)))
accounts_csv = buf2.getvalue().encode("utf-8-sig")

nonempty = [(t, n) for t, n in counts if n > 0]
top = sorted(nonempty, key=lambda x: -x[1])[:10]

# ---------- 收集文件 ----------
files = []
files += collect(SRC, f"{PKG}/项目代码")
files += collect(DEPLOY, f"{PKG}/deploy", drop_prefixes=("backend/data/",))
for name in ROOT_EXTRAS:
    full = os.path.join(ROOT, name)
    if os.path.isfile(full):
        files.append((full, f"{PKG}/{name}"))
    elif os.path.isdir(full):
        files += collect(full, f"{PKG}/{name}")

gen = {
    "README_打包说明.md": None,   # 稍后填充
    "MANIFEST.json": None,
    "数据/tables.csv": tables_csv,
    "数据/accounts.csv": accounts_csv,
}

# ---------- 版本信息 ----------
def short_git():
    try:
        h = subprocess.run(["git", "-C", SRC, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=20).stdout.strip()
        dirty = subprocess.run(["git", "-C", SRC, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
        n = len([l for l in dirty.splitlines() if l.strip()])
        return h, n
    except Exception:
        return "?", 0


git_head, dirty_n = short_git()
version_txt = "?"
vp = os.path.join(SRC, "VERSION.txt")
if os.path.exists(vp):
    version_txt = open(vp, encoding="utf-8", errors="ignore").read().strip()

# 分区统计
def section_of(arc):
    rel = arc.split(f"{PKG}/", 1)[-1]
    parts = rel.split("/")
    if parts[0] in ("项目代码", "deploy"):
        return f"{parts[0]}/{parts[1]}" if len(parts) > 1 else parts[0]
    return parts[0]

sections = {}
for full, arc in files:
    sections.setdefault(section_of(arc), [0, 0])
    sections[section_of(arc)][0] += 1
    sections[section_of(arc)][1] += os.path.getsize(full)
sec_rows = "\n".join(
    f"| `{k}` | {v[0]} | {v[1] / 1048576:.1f} MB |"
    for k, v in sorted(sections.items()))

# 代码规模
py = sum(1 for f, a in files if a.endswith(".py"))
js = sum(1 for f, a in files if a.endswith(".js"))
html = sum(1 for f, a in files if a.endswith(".html"))
css = sum(1 for f, a in files if a.endswith(".css"))

readme = f"""# 鲁品开拍 · 项目完整包

打包时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}（本机时间）
代码版本：VERSION.txt = `{version_txt}`　git HEAD = `{git_head}`（工作区另有 {dirty_n} 个未提交改动，已随包带出）
数据库版本：源库 `{alembic_live}` → 包内快照 `{alembic_head}`（升级结果：{alembic_upgrade}）
作品数：{works}　表数：{len(tables)}（有数据 {len(nonempty)} 张）

## 一、包内结构

```
{PKG}/
├── README_打包说明.md      ← 本文件
├── MANIFEST.json           ← 逐文件 sha256 清单（校验完整性用）
├── 数据/
│   ├── tables.csv          ← 每张表行数
│   └── accounts.csv        ← 账号/角色/组织概览（不含口令与哈希）
├── 项目代码/               ← 完整源码（后端 + 前端 + 测试 + 迁移 + 文档）
│   └── backend/data/lupin.db   ← 数据快照（可直接跑）
├── deploy/                 ← 线上发布副本（可直接部署，需自填 env.production）
├── 交接说明.md             ← 初版交接说明
├── 验证截图/               ← 历史验证截图
└── 第二版独立展示页/       ← 独立展示页包
```

分区统计（文件数 / 解压后体积）：

| 分区 | 文件数 | 体积 |
|---|---|---|
{sec_rows}

代码文件：Python {py} 个、JavaScript {js} 个、HTML {html} 个、CSS {css} 个。

## 二、快速启动

```bash
cd {PKG}/项目代码
python -m venv venv
# Windows
venv\\Scripts\\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # 然后填入 AI 网关 key 等（本包不含 .env）
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8080
```

打开 http://127.0.0.1:8080 是展示页，http://127.0.0.1:8080/app 是工作台。
数据库默认落在 `backend/data/lupin.db`（包内已带快照，开箱即有数据）。

## 三、数据说明

- `项目代码/backend/data/lupin.db` 是用 `VACUUM INTO` 生成的只读一致性快照，**不是**直接拷贝的运行中库。
- 源库当时停在 `{alembic_live}`，包内快照已用 `alembic upgrade head` 升到 `{alembic_head}`（升级结果：{alembic_upgrade}），开箱即可跑，无需再迁移。
- 库内含真实业务数据与账号（口令以哈希存储），**请勿外发此包**。
- 数据规模（行数前 10 的表）：

| 表 | 行数 |
|---|---|
""" + "\n".join(f"| {t} | {n} |" for t, n in top) + f"""

完整清单见 `数据/tables.csv`。

## 四、刻意未打包的内容

| 未打包 | 原因 |
|---|---|
| `项目代码/.env`、`deploy/env.production` | **含真实密钥（AI key / JWT secret），不进任何交付包** |
| `项目代码/venv`（100 MB） | 虚拟环境，按 requirements.txt 重建 |
| `e2e/node_modules`（25 MB） | 依赖，`npm i` 重建 |
| `__pycache__` / `.pytest_cache` / `*.pyc` | 编译缓存 |
| `backend/data/lupin.db.bak-*`（5 份，各 14 MB） | 历史备份，只保留最新快照 |
| `deploy/backend/data/` | 线上库副本，避免与源码库混淆 |
| `.git`（13 MB） | 版本历史；需要单独补 |
| 根目录 2 个 mp4 + 散落 png 截图（约 30 MB） | 工作素材/过程截图；站点用的图已在 `frontend/assets` 内 |

## 五、完整性校验

```python
import json, hashlib, pathlib
root = pathlib.Path("{PKG}")
man = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
bad = [e["path"] for e in man["files"]
       if hashlib.sha256((root / e["path"]).read_bytes()).hexdigest() != e["sha256"]]
print(f"共 {{len(man['files'])}} 个文件，异常 {{len(bad)}} 个", bad[:5])
```
"""

gen["README_打包说明.md"] = readme.encode("utf-8")

# ---------- 写 zip ----------
manifest_entries = []
written = 0
with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    written += 1
    z.write(TMP_DB, f"{PKG}/项目代码/backend/data/lupin.db")
    manifest_entries.append({
        "path": f"{PKG}/项目代码/backend/data/lupin.db",
        "size": os.path.getsize(TMP_DB),
        "sha256": sha256_of(TMP_DB)})
    for full, arc in files:
        z.write(full, arc)
        manifest_entries.append({"path": arc, "size": os.path.getsize(full), "sha256": sha256_of(full)})
        written += 1
    for name, data in gen.items():
        if data is None:
            continue
        z.writestr(f"{PKG}/{name}", data)
        manifest_entries.append({"path": f"{PKG}/{name}", "size": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()})

manifest = {
    "package": PKG,
    "created_at": datetime.now().isoformat(timespec="seconds"),
    "version_txt": version_txt,
    "git_head": git_head,
    "git_uncommitted": dirty_n,
    "alembic_head": alembic_head,
    "alembic_source": alembic_live,
    "alembic_upgrade": alembic_upgrade,
    "db_snapshot": "项目代码/backend/data/lupin.db (VACUUM INTO)",
    "excluded_secrets": [".env", "env.production"],
    "file_count": len(manifest_entries),
    "total_bytes": sum(e["size"] for e in manifest_entries),
    "files": manifest_entries,
}
# MANIFEST 自身不入清单，重写一次完整包（先算 manifest 再补写）
mbytes = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
with zipfile.ZipFile(ZIP_PATH, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    z.writestr(f"{PKG}/MANIFEST.json", mbytes)

os.remove(TMP_DB)
zip_size = os.path.getsize(ZIP_PATH)
print("ZIP", ZIP_PATH)
print("ZIP_MB", round(zip_size / 1048576, 2))
print("FILES", len(manifest_entries))
print("RAW_MB", round(manifest["total_bytes"] / 1048576, 2))
print("SHA256_16", sha256_of(ZIP_PATH)[:16])
print("HEAD", alembic_head, "WORKS", works, "TABLES", len(tables))
