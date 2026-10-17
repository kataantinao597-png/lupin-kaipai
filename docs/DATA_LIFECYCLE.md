# 数据生命周期策略（093）

## 策略表

| 数据类型 | 保留策略 | 执行方式 | 说明 |
|---|---|---|---|
| 回收站作品 | 30 天后硬删除 | `scripts/cleanup.py --apply` 每日 | 含关联的版本快照；硬删除不可恢复 |
| 数据库备份 | 30 天 | `scripts/backup.py` 每次备份后自动清理 | 按文件 mtime 判断 |
| 应用日志 | 90 天 | `scripts/cleanup.py --apply` 每日 | `backend/logs/*.log` |
| 长期未改草稿 | 90 天未修改 → 提醒 | `scripts/cleanup.py` 只报告、不删除 | 由人工确认是否保留/归档 |
| 作品版本快照 | 每个作品最多 50 个 | 写入新快照时自动删最旧 | 见 `_snapshot_work` |

## 清理脚本

```bash
# 只报告，不执行（默认，安全）
python scripts/cleanup.py --dry-run

# 真正执行（建议 cron 每日凌晨 3 点）
# 0 3 * * * cd /path/to/lupin-kaipai && venv/bin/python scripts/cleanup.py --apply
python scripts/cleanup.py --apply
```

参数：

- `--trash-days N`：回收站保留天数（默认 30）
- `--backup-days N`：备份保留天数（默认 30）
- `--log-days N`：日志保留天数（默认 90）
- `--stale-days N`：草稿提醒阈值（默认 90）
- `--backup-dir / --log-dir`：覆盖默认目录

## 设计原则

1. **删数据只删可再生的**：备份、日志可再生；回收站作品 30 天内可恢复，过期才硬删。
2. **草稿不自动删**：90 天未改只提醒，由人工决定——误删用户心血的代价远大于多存一点。
3. **先报告后执行**：`cleanup.py` 默认 dry-run，cron 用 `--apply` 前应先在测试环境跑一周观察输出。
4. **硬删除是单向门**：执行前确保备份策略正常（RPO≤24h 内一定有可用备份）。
