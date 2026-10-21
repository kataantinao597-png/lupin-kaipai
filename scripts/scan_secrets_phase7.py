#!/usr/bin/env python3
"""Phase 7 密钥扫描：检查本次新增代码 + git diff 是否有密钥明文泄漏。

检查项：
1. 疑似密钥明文赋值：key/secret/token/password/private_key 等变量被赋予
   字面量长字符串（排除占位符）。
2. 已知密钥前缀：sk- / sk-ant- / xox* / ghp_ / gho_ / AKIA / BEGIN PRIVATE KEY。
3. `.env` 文件引用（代码里读 .env 文件名即违规；`.env.example` 文档提及除外）。
4. 日志/异常里拼接密钥变量（`logger.*(api_key|secret|token)` 模式抽查）。

用法：
    ./venv/bin/python scripts/scan_secrets_phase7.py [--diff-only]

- 默认：扫描 `git diff HEAD` 的新增行 + Phase 7 新增文件全量。
- 退出码 0 = 干净；1 = 发现疑似泄漏（打印文件:行号:原因）。

注意：这是启发式扫描，误报请人工复核；漏报不代表绝对安全。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 疑似密钥赋值：变量名含 key/secret/token/password/passwd/private_key，
# 右侧是长度>=12 的引号字面量。排除常见占位符。
_ASSIGN_RE = re.compile(
    r"""(?i)[\w-]*(api[_-]?key|secret|token|passwd|password|private[_-]?key|
        client[_-]?secret|access[_-]?key)[\w-]*\s*[:=]\s*["']([^"'\\]{12,})["']""",
    re.VERBOSE,
)

# 已知密钥前缀（只要出现字面量即报）
_PREFIX_RES = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"xox[bpras]-[A-Za-z0-9-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

# .env 文件引用（文档里提 .env.example 不算）
_DOTENV_RE = re.compile(r"""["']\.env(?!\.example)["']|\.env\b(?!=\.example)""")

# 日志/异常拼接密钥变量（抽查）
_LOGKEY_RE = re.compile(
    r"""(?i)(logger\.\w+|raise\s+\w*Error|BusinessError)\s*\([^)]*
        \b(api_key|apiKey|secret|token|password)\b""",
    re.VERBOSE,
)

#: 占位/测试值：一律放行
_PLACEHOLDERS = (
    "xxx", "example", "your-", "<", ">", "...", "changeme", "placeholder",
    "dummy", "fake", "test-", "todo", "none", "null", "password_hash",
    "secret-name", "key-name", "env-var",
)

#: 这些路径不扫（文档/示例/依赖/扫描器自身）
_SKIP_DIRS = ("/venv/", "/node_modules/", "/.git/", "/__pycache__/",
              "/docs/", "/screenshots/")
_SKIP_FILES = (".env.example", "scan_secrets_phase7.py")


def _is_placeholder(value: str) -> bool:
    v = value.lower()
    return any(p in v for p in _PLACEHOLDERS) or len(set(v)) < 4


def _should_skip(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith("docs/") or p.startswith("venv/"):
        return True
    if any(d in p for d in _SKIP_DIRS):
        return True
    return any(p.endswith(s) for s in _SKIP_FILES)


def scan_text(path: str, text: str, only_added: set[int] | None = None
              ) -> list[str]:
    findings: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if only_added is not None and i not in only_added:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*", "<!--")):
            # 注释行只做前缀扫描（注释里贴 key 同样违规）
            check = stripped
        else:
            check = line
        m = _ASSIGN_RE.search(check)
        if m and not _is_placeholder(m.group(2)):
            # 排除：os.environ.get("SECRET_NAME") 这类"读环境变量名"
            if "environ" not in check and "getenv" not in check:
                findings.append(
                    f"{path}:{i}: 疑似密钥明文赋值 ({m.group(1)})")
        for rx in _PREFIX_RES:
            if rx.search(check):
                findings.append(f"{path}:{i}: 命中已知密钥前缀")
                break
        if _DOTENV_RE.search(check) and ".env.example" not in check:
            findings.append(f"{path}:{i}: 引用 .env 文件")
        if _LOGKEY_RE.search(check):
            findings.append(f"{path}:{i}: 日志/异常可能拼接密钥变量")
    return findings


def _diff_added_lines() -> dict[str, set[int]]:
    """git diff HEAD：每个文件的新增行号集合。"""
    try:
        out = subprocess.run(
            ["git", "diff", "HEAD", "--unified=0", "--", "."],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            timeout=60).stdout
    except Exception:
        return {}
    added: dict[str, set[int]] = {}
    cur: str | None = None
    new_line = 0
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            added.setdefault(cur, set())
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
        elif cur and line.startswith("+") and not line.startswith("+++"):
            added[cur].add(new_line)
            new_line += 1
        elif cur and not line.startswith("-"):
            new_line += 1
    return added


def _new_files_full_scan() -> list[str]:
    """Phase 7 新增/改动文件全量扫描。"""
    return [
        "backend/core/idempotency.py",
        "backend/api/v1/agent.py",
        "backend/security/rate_limit.py",
        "backend/services/agent/runner.py",
        "backend/models.py",
        "migrations/versions/0021_idempotency_keys.py",
        "tests/test_agent_security_phase7.py",
    ]


def main() -> int:
    diff_only = "--diff-only" in sys.argv
    findings: list[str] = []
    if diff_only:
        added = _diff_added_lines()
        for rel, lines in sorted(added.items()):
            if _should_skip(rel):
                continue
            p = os.path.join(PROJECT_ROOT, rel)
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                findings += scan_text(rel, f.read(), only_added=lines)
    else:
        for rel in _new_files_full_scan():
            if _should_skip(rel):
                continue
            p = os.path.join(PROJECT_ROOT, rel)
            if not os.path.isfile(p):
                print(f"跳过缺失文件：{rel}", file=sys.stderr)
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                findings += scan_text(rel, f.read())
    if findings:
        print(f"发现 {len(findings)} 处疑似密钥泄漏：")
        for f in findings:
            print("  " + f)
        return 1
    print("密钥扫描通过：未发现明文 key/secret/token 与 .env 引用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
