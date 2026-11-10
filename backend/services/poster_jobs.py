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
#: O-09：数值与起算口径见 backend/core/timeouts.py（集中常量表），
#: 此处只 re-export；与上游 2×90s=180s 紧配合，勿改数值。
from ..core.timeouts import JOB_TIMEOUT_SECONDS  # noqa: E402

#: O-08：单任务典型耗时区间（秒）——ETA 估算用。上游单通道实测
#: 约 30～90s；ETA 只给区间不给精确秒数（波动大，排队任务还可能
#: 被看门狗判失败）。已开始执行的任务按已耗时折算，上限受看门狗
#: JOB_TIMEOUT_SECONDS 约束。
POSTER_SINGLE_MIN_SECONDS = 30
POSTER_SINGLE_MAX_SECONDS = 90

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
    #: S-06：上游返回 url 时单独存这里（不混进 image_b64，前端按
    #: image_url 分支展示）；b64_json 时 image_b64 有值、此字段为空。
    image_url: Optional[str] = None
    mime: str = "image/png"
    error: Optional[str] = None
    error_code: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: B-09：开始执行时刻（_run 取到任务时写）。看门狗改按它计时——
    #: 单 worker 串行下排队任务不应把排队时间算进执行超时。
    #: 为 None 表示尚未开始执行，看门狗回退 created_at 计时。
    started_at: Optional[float] = None

    def public(self) -> dict:
        """对外字段。出图后一次性带上 base64；失败带错误文案与业务码。

        O-08：另带排队位置 ``position``（前面有几个未完成任务，全局
        FIFO，单 worker 串行）与预计等待区间 ``eta_seconds``
        （``{"min": .., "max": ..}``，只给区间不给精确秒数）。
        终态任务 position=0、eta 为 0。
        """
        with _lock:
            _gc_locked()
            position, eta = _queue_info_locked(self, time.time())
        out = {
            "job_id": self.id,
            "status": self.status,
            "mime": self.mime,
            "error": self.error,
            "error_code": self.error_code,
            "elapsed": round(max(0.0, (self.updated_at if self.status != STATUS_RUNNING
                                        else time.time()) - self.created_at), 1),
            "position": position,
            "eta_seconds": eta,
        }
        if self.status == STATUS_DONE:
            out["image_b64"] = self.image_b64
            if self.image_url:
                out["image_url"] = self.image_url
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
        # B-09：看门狗按“开始执行时间”计时（started_at 已写时），
        # 尚未开始执行的任务仍按 created_at 计——排队过久未启动
        # 本身也是超时（前端可重试），只是不再误杀“刚启动就超时”。
        base = j.started_at if j.started_at is not None else j.created_at
        if (j.status == STATUS_RUNNING
                and now - base > JOB_TIMEOUT_SECONDS):
            j.status = STATUS_FAILED
            j.error = ("图片生成超时（服务器等待超过 %d 秒），请重试"
                       % JOB_TIMEOUT_SECONDS)
            j.error_code = 2006
            j.updated_at = now


def _queue_info_locked(job: PosterJob, now: float) -> tuple[int, dict]:
    """必须持锁调用：O-08 排队位置与预计等待区间。

    position：前面有几个未完成（running）任务。单 worker 串行、
    全局按 created_at 先后执行，故按全局 FIFO 数，不按用户分
    （排在别用户后面的任务同样要等）。
    eta_seconds：{"min": .., "max": ..}。每个未完成任务按单通道
    典型 30～90s 估算；已开始执行的任务按已耗时折算（下限
    max(0, 30-elapsed)；上限 90s 内按首通道折算，超过 90s 后
    受看门狗 200s 约束）。终态任务 position=0、eta 为 0。
    区间是估算：上游挂住时看门狗会判失败、实际等待可能超出上限，
    前端只作“预计”展示，不得当作精确承诺。
    """
    if job.status != STATUS_RUNNING:
        return 0, {"min": 0, "max": 0}

    def _remaining(j: PosterJob) -> tuple[float, float]:
        if j.started_at is not None:
            elapsed = max(0.0, now - j.started_at)
            low = max(0.0, POSTER_SINGLE_MIN_SECONDS - elapsed)
            # 上限分两段：90s 内仍在首通道，按首通道上限（90s）折算；
            # 超过 90s 说明首通道已超时（在走备选或已挂住），上限改受
            # 看门狗 200s 约束。刚启动的任务仍给 [30, 90]，不因“可能
            # 挂到看门狗”就报 200s——否则新任务永远显示 3 分多钟。
            if elapsed < POSTER_SINGLE_MAX_SECONDS:
                high = max(0.0, POSTER_SINGLE_MAX_SECONDS - elapsed)
            else:
                high = max(0.0, JOB_TIMEOUT_SECONDS - elapsed)
            return (low, high)
        return (float(POSTER_SINGLE_MIN_SECONDS),
                float(POSTER_SINGLE_MAX_SECONDS))

    lo = hi = 0.0
    position = 0
    for j in _jobs.values():
        if j.status != STATUS_RUNNING:
            continue
        if j.id == job.id:
            a, b = _remaining(j)
            lo += a
            hi += b
        elif j.created_at < job.created_at:
            position += 1
            a, b = _remaining(j)
            lo += a
            hi += b
    return position, {"min": int(lo), "max": int(hi)}


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
    # B-09：执行前检查任务状态——看门狗已判 failed 的排队任务不再调
    # 上游出图（烧钱且结果会被丢弃）。同时写 started_at，看门狗自此
    # 改按执行时长计时。
    with _lock:
        if job.status != STATUS_RUNNING:
            logging.getLogger(__name__).info(
                "海报任务 %s 执行前已是 %s，跳过上游调用", job.id, job.status)
            return
        job.started_at = time.time()
        job.updated_at = job.started_at
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
        job.image_b64 = b64
        job.image_url = url
        job.updated_at = time.time()


def run(job: PosterJob, work: Callable[[], dict],
        on_success: Optional[Callable[[dict], None]] = None) -> None:
    """把任务丢进后台线程池，立即返回。"""
    _pool.submit(_run, job, work, on_success)


def reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
