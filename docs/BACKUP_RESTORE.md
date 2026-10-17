# 备份与恢复演练（090）

## 备份策略

- `scripts/backup.py` 每日执行一次（cron：`0 2 * * *`，凌晨 2 点）。
- PostgreSQL：`pg_dump --format=custom` 生成 `.dump`。
- SQLite：`sqlite3` 备份 API 安全拷贝生成 `.db`（防 WAL 中途写入）。
- 备份文件：`backend/backups/lupin_YYYYMMDD_HHMMSS.{db,dump}`。
- 保留 30 天，超期自动删除（`prune_old_backups`）。
- 备份失败写日志并返回非零退出码，cron/CI 可据此告警。

## 恢复演练步骤（每季度至少一次）

### 1. 准备

```bash
# 选一个最近的备份
ls -lt backend/backups/ | head -5
```

### 2. 恢复到隔离环境（不要直接覆盖生产）

SQLite：
```bash
python scripts/restore.py \
  --backup backend/backups/lupin_20261018_020000.db \
  --target /tmp/lupin_restore_drill.db \
  --yes
```

PostgreSQL：
```bash
# 先建空库
createdb lupin_drill
python scripts/restore.py \
  --backup backend/backups/lupin_20261018_020000.dump \
  --target postgresql://user:pass@localhost:5432/lupin_drill \
  --yes
```

### 3. 行数校验

`restore.py` 恢复后自动比对关键表行数（`users / organizations / memberships / works / work_versions`）。
不一致则返回非零退出码，演练视为失败。

也可单独校验（不恢复）：

```bash
python scripts/restore.py --verify-only \
  --backup backend/backups/lupin_20261018_020000.db \
  --target /tmp/lupin_restore_drill.db
```

### 4. 业务冒烟

用恢复出的库启动应用，抽查：

1. 登录正常；
2. 打开 1–2 个作品，分镜/事实/拍摄状态完整；
3. 新建作品可保存（写操作正常）。

### 5. 记录

演练结果记入运维日志：日期、备份文件、恢复耗时、行数校验结果、冒烟结论。
失败项必须有跟进人和修复期限。

## 生产恢复（真实故障时）

1. 先按上述步骤恢复到隔离库并验证通过；
2. 停止应用写入（维护模式）；
3. 对当前生产库再做一次紧急备份（防止恢复后发现问题无法回退）；
4. 用验证过的备份恢复生产库；
5. 重启应用，跑冒烟测试；
6. 恢复服务，记录 RTO 实际耗时（见 docs/SLO.md）。
