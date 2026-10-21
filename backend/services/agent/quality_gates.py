"""Phase 5：质量门禁 —— artifact 结构校验 / 一致性 / 风险引用 / 持久化验证。

任务书 §5.3：
- 所有必需 artifact 符合结构 schema；
- 策略/创意/分镜/拍摄清单一致（产品信息/时长/人物设备不冲突）；
- 高风险宣传事实必须有事实引用或被明确标记为"待核实/不可作为确定宣传"；
- 保存后重新读取作品，验证 artifact 真实持久化且关联正确；
- 仅当全部 required 门禁通过才 completed，否则 partial/failed 并说明。

门禁只做判定与说明，不改写 artifact（修复由 orchestrator 的有限修复轮做）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

__all__ = [
    "GateIssue",
    "GateResult",
    "QualityReport",
    "StrategyArtifact",
    "CreativeArtifact",
    "StoryboardArtifact",
    "ShootingListArtifact",
    "CopyArtifact",
    "HIGH_RISK_KEYWORDS",
    "validate_artifact",
    "check_consistency",
    "check_fact_grounding",
    "verify_persistence",
    "run_quality_gates",
]


# ---------------------------------------------------------------------------
# 门禁结果模型
# ---------------------------------------------------------------------------

class GateIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gate: str
    severity: Literal["error", "warning"] = "error"
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class GateResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    passed: bool
    required: bool = True
    issues: list[GateIssue] = Field(default_factory=list)


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gates: list[GateResult] = Field(default_factory=list)
    passed: bool = False          # 全部 required 门禁通过
    verdict: Literal["completed", "partial", "failed"] = "failed"
    summary: str = ""


def _issue(gate: str, message: str,
           severity: str = "error",
           detail: Optional[dict[str, Any]] = None) -> GateIssue:
    return GateIssue(gate=gate, severity=severity,  # type: ignore[arg-type]
                     message=message, detail=detail or {})


# ---------------------------------------------------------------------------
# artifact 结构 schema
# ---------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class StrategyArtifact(_Strict):
    goals: list[str] = Field(min_length=1, max_length=10)
    target_audience: str = Field(min_length=1, max_length=500)
    content_pillars: list[str] = Field(min_length=1, max_length=10)
    key_messages: list[str] = Field(min_length=1, max_length=10)


class CreativeDirection(_Strict):
    title: str = Field(min_length=1, max_length=200)
    angle: str = Field(min_length=1, max_length=500)
    selling_points: list[str] = Field(min_length=1, max_length=10)


class CreativeArtifact(_Strict):
    directions: list[CreativeDirection] = Field(min_length=1, max_length=10)
    recommended_index: int = Field(ge=0, le=9)


class StoryboardShot(_Strict):
    shot_no: int = Field(ge=1)
    scene: str = Field(min_length=1, max_length=1000)
    shot_type: str = Field(default="", max_length=100)
    duration_sec: float = Field(ge=0.5, le=600)
    voiceover: str = Field(default="", max_length=2000)
    subtitle: str = Field(default="", max_length=500)
    shoot_notes: str = Field(default="", max_length=1000)


class StoryboardArtifact(_Strict):
    shots: list[StoryboardShot] = Field(min_length=1, max_length=30)


class ShootingItem(_Strict):
    shot_no: int = Field(ge=1)
    scene: str = Field(min_length=1, max_length=1000)
    duration_sec: float = Field(ge=0.5, le=600)
    people_needed: int = Field(default=1, ge=1, le=100)
    devices_needed: list[str] = Field(default_factory=list, max_length=20)
    props: list[str] = Field(default_factory=list, max_length=20)


class ShootingListArtifact(_Strict):
    items: list[ShootingItem] = Field(min_length=1, max_length=30)
    total_duration_sec: float = Field(ge=1)
    crew_size: int = Field(ge=1, le=100)
    devices_available: list[str] = Field(default_factory=list, max_length=20)


class CopyArtifact(_Strict):
    platform: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=20)


_ARTIFACT_MODELS: dict[str, type[_Strict]] = {
    "strategy": StrategyArtifact,
    "creative": CreativeArtifact,
    "storyboard": StoryboardArtifact,
    "shooting_list": ShootingListArtifact,
    "copy": CopyArtifact,
}


def validate_artifact(kind: str, data: Any) -> tuple[Optional[_Strict], list[GateIssue]]:
    """校验单个 artifact 的结构 schema。返回 (对象|None, issues)。"""
    model = _ARTIFACT_MODELS.get(kind)
    if model is None:
        return None, [_issue("schema", f"未知 artifact 类型: {kind}")]
    if not isinstance(data, dict):
        return None, [_issue("schema", f"{kind} 不是 JSON 对象")]
    try:
        return model.model_validate(data), []
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
            for err in e.errors()[:5])
        return None, [_issue("schema", f"{kind} 结构非法: {msgs}")]


# ---------------------------------------------------------------------------
# 一致性检查：产品信息 / 时长 / 人物设备不冲突
# ---------------------------------------------------------------------------

def check_consistency(brief: Any, artifacts: dict[str, Any]) -> list[GateIssue]:
    """策略/创意/分镜/拍摄清单一致性。brief 为 Brief 或等价 dict。"""
    issues: list[GateIssue] = []
    b = brief if isinstance(brief, dict) else brief.model_dump()

    storyboard, s_issues = validate_artifact("storyboard",
                                             artifacts.get("storyboard"))
    shooting, l_issues = validate_artifact("shooting_list",
                                           artifacts.get("shooting_list"))
    issues.extend(s_issues)
    issues.extend(l_issues)

    # 时长一致：分镜总时长 ≈ 目标时长（±30% 容差）；拍摄清单 ≈ 分镜
    target = float(b.get("video_duration_sec") or 0)
    if storyboard is not None and target > 0:
        total = sum(s.duration_sec for s in storyboard.shots)
        if total <= 0:
            issues.append(_issue("consistency", "分镜总时长为 0"))
        elif abs(total - target) / target > 0.3:
            issues.append(_issue(
                "consistency",
                f"分镜总时长 {total:.0f}s 与目标 {target:.0f}s 偏差超过 30%",
                detail={"storyboard_total": total, "target": target}))
    if storyboard is not None and shooting is not None:
        sb_nos = {s.shot_no for s in storyboard.shots}
        li_nos = {i.shot_no for i in shooting.items}
        if sb_nos != li_nos:
            issues.append(_issue(
                "consistency",
                f"拍摄清单镜头编号与分镜不一致（分镜 {sorted(sb_nos)} / 清单 {sorted(li_nos)}）",
                severity="warning"))
        if abs(shooting.total_duration_sec
               - sum(s.duration_sec for s in storyboard.shots)) > 1:
            issues.append(_issue(
                "consistency", "拍摄清单总时长与分镜总时长不一致",
                severity="warning"))

    # 人物设备约束：拍摄清单需求 vs brief 约束
    people = b.get("people_count")
    if shooting is not None and people:
        need = max((i.people_needed for i in shooting.items), default=1)
        if shooting.crew_size > people:
            issues.append(_issue(
                "consistency",
                f"拍摄清单需要 {shooting.crew_size} 人，超过 brief 约束的 {people} 人",
                detail={"crew_size": shooting.crew_size,
                        "people_count": people}))
        if need > people:
            issues.append(_issue(
                "consistency",
                f"有镜头需要 {need} 人同时出镜，超过 brief 约束的 {people} 人"))
    devices = [d for d in (b.get("devices") or []) if d]
    if shooting is not None and devices:
        missing = sorted({d for i in shooting.items for d in i.devices_needed}
                         - set(devices))
        if missing:
            issues.append(_issue(
                "consistency",
                f"拍摄清单需要但 brief 未列出的设备：{'、'.join(missing)}",
                severity="warning", detail={"missing_devices": missing}))

    # 产品名一致：创意/分镜/文案中若出现产品名，应与 brief 一致（防张冠李戴）
    product_name = (b.get("product_name") or "").strip()
    if product_name:
        texts = []
        creative = artifacts.get("creative") or {}
        for d in (creative.get("directions") or []):
            texts.append(f"{d.get('title','')} {d.get('angle','')}")
        if storyboard is not None:
            texts.extend(s.voiceover for s in storyboard.shots)
        copy = artifacts.get("copy") or {}
        texts.append(copy.get("text", ""))
        blob = "\n".join(texts)
        # 若文案里出现了"牌/合作社/基地"类主体名却与产品名完全无关 → 警告
        # （轻量启发式，不做硬拦截）
        if product_name not in blob and any(
                kw in blob for kw in ("牌", "合作社", "基地", "农场")):
            issues.append(_issue(
                "consistency",
                f"文案中未出现产品名“{product_name}”，请确认没有张冠李戴",
                severity="warning"))
    return issues


# ---------------------------------------------------------------------------
# 风险引用检查：高风险宣传必须有事实引用或标"待核实"
# ---------------------------------------------------------------------------

#: 高风险宣传关键词（功效/认证/极限词/产地宣称）
HIGH_RISK_KEYWORDS = [
    "治愈", "包治", "根治", "药效", "疗效", "减肥", "降糖", "降压",
    "血糖", "血压",  # 兜底"降血糖/降血压"（子串"降糖/降压"无法命中"降血糖"）
    "防癌", "抗癌", "有机", "绿色食品", "地理标志", "原产地",
    "第一", "最", "100%", "纯天然", "零添加", "无添加",
    "亩产", "增产", "高产",
]

_UNVERIFIED_MARKERS = ("待核实", "不可作为确定宣传")


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?\n；;]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def check_fact_grounding(brief: Any, artifacts: dict[str, Any],
                         facts: list[dict[str, Any]]) -> list[GateIssue]:
    """高风险宣传事实必须有事实引用或标"待核实/不可作为确定宣传"。

    facts: [{"id","content",...}]（已确认可用事实）。
    """
    issues: list[GateIssue] = []
    b = brief if isinstance(brief, dict) else brief.model_dump()

    texts: list[tuple[str, str]] = []  # (来源, 文本)
    creative = artifacts.get("creative") or {}
    for idx, d in enumerate(creative.get("directions") or []):
        for sp in (d.get("selling_points") or []):
            texts.append((f"创意方向{idx + 1}卖点", str(sp)))
    storyboard = artifacts.get("storyboard") or {}
    for s in (storyboard.get("shots") or []):
        texts.append((f"镜头{s.get('shot_no')}口播", str(s.get("voiceover") or "")))
        texts.append((f"镜头{s.get('shot_no')}字幕", str(s.get("subtitle") or "")))
    copy = artifacts.get("copy") or {}
    if copy.get("text"):
        texts.append(("平台文案", str(copy["text"])))

    fact_contents = [str(f.get("content") or "") for f in (facts or [])]
    verify_items = " ".join(b.get("items_to_verify") or [])

    for source, text in texts:
        for sent in _split_sentences(text):
            hit = next((kw for kw in HIGH_RISK_KEYWORDS if kw in sent), None)
            if not hit:
                continue
            # 已标待核实 → 合规
            if any(m in sent for m in _UNVERIFIED_MARKERS):
                continue
            # 有事实引用（句子含事实原文片段，或事实原文含句子关键片段）→ 合规
            grounded = any(
                (fc and (fc[:12] in sent or sent[:12] in fc))
                for fc in fact_contents if fc)
            if grounded:
                continue
            # brief 待核实项覆盖 → 合规（已声明不作为确定宣传）
            if hit in verify_items or any(
                    kw in verify_items for kw in HIGH_RISK_KEYWORDS
                    if kw in sent):
                continue
            issues.append(_issue(
                "fact_grounding",
                f"{source}含高风险宣称“{hit}”但无事实引用、未标待核实：{sent[:60]}",
                detail={"source": source, "keyword": hit,
                        "sentence": sent[:120]}))
    return issues


# ---------------------------------------------------------------------------
# 持久化验证：保存后重读作品
# ---------------------------------------------------------------------------

def verify_persistence(db: Session, work_id: str, org_id: str,
                       expected_keys: list[str]) -> list[GateIssue]:
    """保存后重读作品，验证 artifact 真实持久化且关联正确。"""
    issues: list[GateIssue] = []
    try:
        from ...repositories.work_repository import WorkRepository
        w = WorkRepository(db).get(work_id, org_id)
    except Exception as e:  # noqa: BLE001
        return [_issue("persistence", f"重读作品失败: {e}")]
    if w is None or getattr(w, "deleted_at", None) is not None:
        return [_issue("persistence", "重读作品失败：作品不存在或已删除")]
    try:
        data = json.loads(w.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return [_issue("persistence", "作品 data_json 损坏，无法解析")]
    if not isinstance(data, dict):
        return [_issue("persistence", "作品 data_json 不是对象")]
    missing = [k for k in expected_keys
               if not isinstance(data.get(k), dict) or not data.get(k)]
    if missing:
        issues.append(_issue(
            "persistence",
            f"作品缺少已生成的 artifact：{', '.join(missing)}",
            detail={"missing": missing}))
    return issues


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------

def run_quality_gates(
    brief: Any,
    artifacts: dict[str, Any],
    facts: Optional[list[dict[str, Any]]] = None,
    *,
    db: Optional[Session] = None,
    work_id: Optional[str] = None,
    org_id: Optional[str] = None,
    required_kinds: Optional[list[str]] = None,
) -> QualityReport:
    """跑全部质量门禁，返回 QualityReport。

    required_kinds：必需 artifact 类型（默认 strategy/creative/storyboard/
    shooting_list/copy 全必需）。仅全部 required 门禁通过 → completed；
    有 required 失败 → failed；仅 warning → completed（带警告说明）。
    """
    kinds = required_kinds or ["strategy", "creative", "storyboard",
                               "shooting_list", "copy"]
    gates: list[GateResult] = []

    # 1. artifact 结构 schema
    for kind in kinds:
        obj, issues = validate_artifact(kind, artifacts.get(kind))
        gates.append(GateResult(
            name=f"schema:{kind}", passed=not issues,
            required=True, issues=issues))

    # 2. 一致性
    cons_issues = check_consistency(brief, artifacts)
    gates.append(GateResult(
        name="consistency", passed=not cons_issues, required=True,
        issues=cons_issues))

    # 3. 风险引用
    ground_issues = check_fact_grounding(brief, artifacts, facts or [])
    gates.append(GateResult(
        name="fact_grounding", passed=not ground_issues, required=True,
        issues=ground_issues))

    # 4. 持久化（给了 work_id/db 才跑）
    if db is not None and work_id and org_id:
        p_issues = verify_persistence(db, work_id, org_id, kinds)
        gates.append(GateResult(
            name="persistence", passed=not p_issues, required=True,
            issues=p_issues))

    required_failed = [g for g in gates if g.required and not g.passed]
    warnings = [i for g in gates for i in g.issues
                if i.severity == "warning"]
    passed = not required_failed
    if passed:
        verdict: Literal["completed", "partial", "failed"] = "completed"
        summary = ("质量门禁全部通过" + (f"（{len(warnings)} 条警告）"
                                        if warnings else ""))
    else:
        verdict = "failed"
        summary = ("质量门禁未通过："
                   + "; ".join(f"{g.name}: {g.issues[0].message}"
                               for g in required_failed if g.issues))
    return QualityReport(gates=gates, passed=passed,
                         verdict=verdict, summary=summary)
