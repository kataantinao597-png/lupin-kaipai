"""海报生图异步任务：绕开网关 60s 硬超时。

背景（2026-10-22 实测）：线上网关对单请求硬超时约 60s。生图上游耗时
gpt-image-2 约 30s、2.5 系约 45s，偶发超过 60s 即被网关切成 504 HTML，
浏览器看到的是"生成失败"，而非"key 没配"。

因此把"等图"从 HTTP 请求里挪出去：
- POST /poster/image/jobs 立刻返回 202 + job_id（毫秒级，永不触网关超时）
- 后台线程跑真实生图（上限 REQUEST_TIMEOUT=120s）
- 前端每 2s 轮询 GET /poster/image/jobs/{id}

实现约束：
- 单进程 uvicorn（start.sh 没有 --workers），故用进程内字典存任务；
- 出图线程池只开 1 个：实测同 key 并发时上游会把后面的请求挂住
  （2026-10-22 线上两个任务卡在 running 十几分钟），串行排队更可靠；
  重启后未完成任务丢失 → 前端拿到 404 提示重试即可，不影响已保存的图片。
- 只存 base64（前端要写进作品数据），TTL 15 分钟，最多 40 个任务，防内存膨胀。
- 额度仍只在"成功出图"后记一条用量（与同步接口一致）。
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

JOB_TTL_SECONDS = 900
MAX_JOBS = 40
MAX_WORKERS = 1  # 串行出图：上游对同 key 并发不友好，并发会把后来的请求挂住
#: S1 修复：每用户同时在途（running）任务上限。额度 TOCTOU 的配套防线：
#: 提交时在途任务计入已用额度，且在途超 3 个直接 429，防止并发 burst
#: 把后台线程池打爆（DoS 面）与绕过每日限额。
MAX_INFLIGHT_PER_USER = 3
#: 任务看门狗：超过这个时长还停在 running，就判失败并释放等待
#: （上游偶发挂住时，不能让前端永远等一个不动的任务）
JOB_TIMEOUT_SECONDS = 200

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class PosterJob:
    id: str
    user_id: str
    org_id: str
    status: str = STATUS_RUNNING
    image_b64: Optional[str] = None
    mime: str = "image/png"
    error: Optional[str] = None
    error_code: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        """对外字段。出图后一次性带上 base64；失败带错误文案与业务码。"""
        out = {
            "job_id": self.id,
            "status": self.status,
            "mime": self.mime,
            "error": self.error,
            "error_code": self.error_code,
            "elapsed": round(max(0.0, (self.updated_at if self.status != STATUS_RUNNING
                                        else time.time()) - self.created_at), 1),
        }
        if self.status == STATUS_DONE:
            out["image_b64"] = self.image_b64
        return out


_lock = threading.Lock()
_jobs: dict[str, PosterJob] = {}
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                           thread_name_prefix="poster-img")


def _mark_expired_locked(now: float) -> None:
    """必须持锁调用：看门狗——running 超 JOB_TIMEOUT_SECONDS 即判失败。

    三处（count_running/get_job/_gc_locked）共用同一口径，禁止各写一份
    判定条件漂移。只做 running → failed 单向转移，失败态是终态，不存在
    “判失败后又被算回来”的路径（_finish 只允许 running → done/failed）。
    """
    for j in _jobs.values():
        if (j.status == STATUS_RUNNING
                and now - j.created_at > JOB_TIMEOUT_SECONDS):
            j.status = STATUS_FAILED
            j.error = ("图片生成超时（服务器等待超过 %d 秒），请重试"
                       % JOB_TIMEOUT_SECONDS)
            j.error_code = 2006
            j.updated_at = now


def _gc_locked() -> None:
    """必须持锁调用：清过期任务，再按创建时间淘汰超量的。

    M-1 修复：淘汰永不碰 running 任务。原先超 40 个按 created_at 淘汰
    最旧的，不分状态——被淘汰的 running 任务后台线程仍出图记账，但字典
    中已无此 id，前端轮询得 404 提示重试 → 重复扣费 + 丢图。现先跑
    看门狗（超 200s 的 running 先判失败），再淘汰：过期（TTL）与超量
    都只淘汰非 running；running 任务即使超过 TTL 也不淘汰（其线程仍
    持有该对象引用，淘汰后状态不可见、记账不可控）。TTL 按 updated_at
    （状态变更时刻）而非 created_at 计：刚判失败的任务从失败时刻起保留
    满 TTL，前端必能看到失败文案，不受 GC 触发频率影响。
    """
    now = time.time()
    _mark_expired_locked(now)
    # DeepSeek 复查指出：看门狗刚判失败的任务若按 created_at 算 TTL，
    # 挂起 >900s 的任务同轮就被删，前端轮询得 404 而非失败文案——失败
    # 不可见、用户无故重试。且 create/get/count 每次都触发 GC，“本轮宽
    # 限”撑不过下一次创建（毫秒级），不够。故 TTL 改按 updated_at 计：
    # 失败/完成态从状态变更时刻起保留满 TTL（900s），与 GC 触发频率无关；
    # 超量淘汰按 updated_at 最旧优先，刚判失败（updated_at=now）排最后，
    # 只有全部非 running 都比它新时才可能被淘汰（极端突发才发生）。
    for key in [k for k, v in _jobs.items()
                if v.status != STATUS_RUNNING
                and now - v.updated_at > JOB_TTL_SECONDS]:
        _jobs.pop(key, None)
    overflow = sum(1 for v in _jobs.values()
                   if v.status != STATUS_RUNNING) - MAX_JOBS
    if overflow > 0:
        victims = sorted(
            (k for k, v in _jobs.items() if v.status != STATUS_RUNNING),
            key=lambda k: _jobs[k].updated_at)[:overflow]
        for key in victims:
            _jobs.pop(key, None)


def create_job(user_id: str, org_id: str) -> PosterJob:
    job = PosterJob(id=uuid.uuid4().hex, user_id=user_id, org_id=org_id)
    with _lock:
        _gc_locked()
        _jobs[job.id] = job
    return job


def count_running_for_user(user_id: str) -> int:
    """该用户当前 running 任务数（S1：提交时计入已用额度，防并发绕过）。

    任务失败/成功后即离 running 态，在途计数自动回落，无需显式释放。
    看门狗同步适用：running 超过 JOB_TIMEOUT_SECONDS 的任务在此就地判
    失败（与 get_job 同口径），避免挂住的上游让在途计数虚高到 TTL。
    """
    with _lock:
        _gc_locked()
        return sum(1 for j in _jobs.values()
                   if j.user_id == user_id and j.status == STATUS_RUNNING)


def get_job(job_id: str, user_id: Optional[str] = None) -> Optional[PosterJob]:
    """取任务；传 user_id 时做归属校验（别人的任务等同不存在）。

    看门狗：running 且已超过 JOB_TIMEOUT_SECONDS 的任务就地判失败，
    避免上游挂住时前端一直等到自己的轮询上限。
    """
    with _lock:
        _gc_locked()
        job = _jobs.get(job_id)
        if job is None:
            return None
        if user_id is not None and job.user_id != user_id:
            return None
        return job


def _finish(job: PosterJob, **kw) -> None:
    """M-3 修复：只允许 running → done/failed 单向转移。

    看门狗已判 failed 的任务，后台线程迟到写回 done 会造成“失败文案 +
    成功状态”分裂与重复记账窗口；迟到写回一律忽略（失败态是终态）。
    必须持锁调用方注意：本函数自己持锁，勿在已持锁处调用。
    """
    with _lock:
        if job.status != STATUS_RUNNING:
            return
        for k, v in kw.items():
            setattr(job, k, v)
        job.updated_at = time.time()


def _run(job: PosterJob, work: Callable[[], dict],
         on_success: Optional[Callable[[dict], None]] = None) -> None:
    import logging
    try:
        first = work()
    except Exception as e:  # BusinessError / 任意异常都收敛成 failed
        code = getattr(e, "code", 0) or 0
        msg = str(e) or type(e).__name__
        _finish(job, status=STATUS_FAILED, error=msg, error_code=code)
        return
    b64 = first.get("b64_json") if isinstance(first, dict) else None
    url = first.get("url") if isinstance(first, dict) else None
    if not b64 and not url:
        _finish(job, status=STATUS_FAILED, error="图片服务未返回图片数据",
                error_code=0)
        return
    # M-2/M-3 修复：出图成功后的“状态检查 + 记账 + 置 done”必须在同一
    # 临界区内完成。原先先检查（锁外）再记账（异常吞掉仍置 done）存在
    # 两个洞：①看门狗可在检查与记账之间把任务判 failed，记账照常提交
    # 后 _finish 又因已失败而忽略 → 用户被扣费却拿到失败文案，重试再扣
    # 一次；②记账异常被 pass 后仍置 done → 白送生图。
    # 现：持锁检查 running，记账在锁内做（单条 INSERT，毫秒级；看门狗
    # 同样持锁，故记账期间看门狗无法插队），记账失败则置 failed 且不给
    # 图（失败不扣额度：INSERT 未提交即无扣费，用户重试是新任务新记账，
    # 旧图丢弃，不产生免费图）。取舍说明：锁内做一次 DB 写会短暂阻塞
    # 轮询/get_job（毫秒级），换取记账与状态的原子性；单 worker + 串行
    # 线程池下该阻塞可忽略。
    with _lock:
        if job.status != STATUS_RUNNING:
            # 看门狗已判失败：不记账、不翻回 done，失败态即最终态
            return
        if on_success is not None:
            try:
                on_success(first)
            except Exception:
                # 记账失败：记日志（含 job_id 供对账）+ 置 failed，不给图
                logging.getLogger(__name__).warning(
                    "海报生图记账失败 job=%s user=%s，已置失败不给图",
                    job.id, job.user_id, exc_info=True)
                job.status = STATUS_FAILED
                job.error = "记账失败，请重试（本次未扣额度）"
                job.error_code = 0
                job.updated_at = time.time()
                return
        job.status = STATUS_DONE
        job.image_b64 = b64 or url
        job.updated_at = time.time()


def run(job: PosterJob, work: Callable[[], dict],
        on_success: Optional[Callable[[dict], None]] = None) -> None:
    """把任务丢进后台线程池，立即返回。"""
    _pool.submit(_run, job, work, on_success)


def reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
