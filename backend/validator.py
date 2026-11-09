"""鲁品开拍 - 分镜业务规则校验（由实验 validate.py 移植，逻辑一致）。"""
from typing import Any, List, Tuple
from .schemas import SHOT_TYPES, Storyboard

# 单人手机拍摄下不可用的拍摄方式关键词
FORBIDDEN_SOLO_PHONE = ["航拍", "摇臂", "多机位", "轨道", "无人机", "斯坦尼康", "大摇臂", "稳定器", "滑轨"]

# 可行性规则：需要多人配合的关键词
NEEDS_MULTI_PEOPLE = ["双人", "多人", "群演", "演员", "助手协助", "摄影师", "跟拍人员"]

# 可行性规则：需要特殊场地的关键词
NEEDS_SPECIAL_LOCATION = ["摄影棚", "影棚", "演播室", "专业厨房", "实验室"]

# 抽象运镜/动作词：出现时必须配具体做法（量化数字或方法词），否则记 warning
ABSTRACT_ACTION_WORDS = ["缓慢", "稍微", "适当", "轻轻", "慢慢"]
# 具体做法的标记：数字单位或方法关键词，出现任一即视为"有具体做法"
CONCRETE_MARKERS = ["秒", "cm", "厘米", "米", "度", "支架", "双手", "单手",
                    "焦点", "对焦", "匀速", "固定", "抵住", "步骤", "第", "先", "再", "然后"]


def _is_v2_storyboard(sb: "Storyboard") -> bool:
    """是否为 v2 分镜：任一镜头填充了 v2 专属字段即视为 v2。"""
    for s in sb.shots:
        if (s.final_duration_seconds is not None
                or s.record_duration_seconds is not None
                or s.shoot_steps
                or s.acceptance_criteria):
            return True
    return False


def _normalize_storyboard_for_validation(sb: Any) -> Any:
    """把简化分镜格式归一化为 validator 完整 schema（共享入口）。

    原在 services/agent/adapters.py，2026-10-28 校验入口统一时迁到这里：
    导出/可行性/单镜重生成与 Agent 清单路共用同一归一化，同一份分镜
    不再在不同入口下得出不同结论。填充的默认值（中景/固定机位/
    手机1人等）只是占位，不代表已核实，导出时用户仍可改。

    背景：``backend.validator.Storyboard`` 要求 product_summary /
    target_audience / creative_direction / target_duration_seconds / shoot /
    shot_number / duration_seconds 等完整字段，而工具链产出的是简化格式
    （shot_no / duration_sec / scene）。直接校验会误报"结构非法"。
    本函数只做字段映射与兜底填充，不改变镜头实质内容；checklist 条目仍按
    原始简化数据组装。已是完整 schema 的输入原样返回（幂等）。
    非法输入（非 dict / shots 非 list）原样返回，让 validator 按原逻辑报错。
    修复点：此前只看首镜有没有 shot_number 就原样返回，混格式
    （首镜完整、其余简化）会原样进 validator 触发 v2 全量规则；
    而简化路径映射时又把已有的 v2 字段丢掉，导致同一份分镜在
    不同入口下校验结果不一致。现在改为：只有全部镜头都是完整
    schema 时才原样返回，否则统一走映射，且映射时保留已有的 v2 字段。
    """
    if not isinstance(sb, dict):
        return sb
    shots = sb.get("shots")
    if not isinstance(shots, list):
        return sb
    if shots and all(isinstance(s, dict) and "shot_number" in s for s in shots):
        # 全部是完整 schema：补齐顶层默认值（已有则不动），否则只传
        # {"shots": [...]} 进 validate_dict 会因缺 product_summary 等报结构错，
        # 掩盖真正的 v2 字段问题——这正是第6步内部校验要避免的坑。
        total = sum(float(s.get("duration_seconds") or 0) for s in shots
                    if isinstance(s, dict))
        normalized = {
            "product_summary": "AI 助手生成",
            "target_audience": "目标用户",
            "creative_direction": "AI 创意",
            "target_duration_seconds": total if total > 0 else 1,
            "shoot": {"device": "手机", "people": 1},
        }
        normalized.update(sb)
        normalized["shots"] = shots
        return normalized
    # 映射时保留的 v2 字段（缺了它们，validator 的 v2 全量规则会被误判）
    _V2_SHOT_FIELDS = (
        "shoot_steps", "acceptance_criteria", "failure_modes",
        "reshoot_guide", "record_duration_seconds", "final_duration_seconds",
        "usable_duration_seconds", "shot_purpose", "prerequisites",
        "phone_setup", "alt_approach", "transition", "edit_notes",
        "props", "required_assets",
    )
    norm_shots = []
    for i, s in enumerate(shots):
        if not isinstance(s, dict):
            norm_shots.append(s)
            continue
        st = s.get("shot_type")
        mapped = {
            "shot_number": s.get("shot_number", s.get("shot_no", i + 1)),
            "duration_seconds": s.get("duration_seconds",
                                      s.get("duration_sec", 0)),
            "shot_type": st if st in SHOT_TYPES else "中景",
            "scene_description": s.get("scene_description") or s.get("scene") or "",
            "shooting_action": (s.get("shooting_action")
                                or s.get("shoot_notes") or s.get("scene") or ""),
            "voiceover": s.get("voiceover") or "",
            "subtitle": s.get("subtitle") or "",
            "camera_movement": s.get("camera_movement") or "固定机位",
        }
        for f in _V2_SHOT_FIELDS:
            if f in s and s[f] is not None:
                mapped[f] = s[f]
        norm_shots.append(mapped)
    total = sum(float(x.get("duration_seconds") or 0) for x in norm_shots
                if isinstance(x, dict))
    normalized = {
        "product_summary": "AI 助手生成",
        "target_audience": "目标用户",
        "creative_direction": "AI 创意",
        "target_duration_seconds": total if total > 0 else 1,
        "shoot": {"device": "手机", "people": 1},
    }
    for k, v in sb.items():
        if k != "shots":
            normalized[k] = v
    normalized["shots"] = norm_shots
    return normalized

def rule_feasibility_scan(sb: "Storyboard") -> list:
    """规则层可行性预检：快速、确定性。返回 [(shot_number, status, reason, constraints)]。

    只标记明确违规的；不确定的交给模型判断。
    """
    findings = []
    device = sb.shoot.device or ""
    people = sb.shoot.people or 1
    for s in sb.shots:
        text = f"{s.shooting_action} {s.camera_movement} {s.scene_description}"
        # 设备条件：单人手机 vs 专业设备
        if device == "手机" and people == 1:
            hits = [k for k in FORBIDDEN_SOLO_PHONE if k in text]
            if hits:
                findings.append((s.shot_number, "不可执行",
                                 f"需要专业设备（{','.join(hits)}），但用户只有手机单人拍摄",
                                 ["设备条件"]))
                continue
        # 人员条件
        if people == 1:
            hits = [k for k in NEEDS_MULTI_PEOPLE if k in text]
            if hits:
                findings.append((s.shot_number, "不可执行",
                                 f"需要多人配合（{','.join(hits)}），但用户只有 1 人",
                                 ["人员条件"]))
                continue
        # 场地条件
        hits = [k for k in NEEDS_SPECIAL_LOCATION if k in text]
        if hits:
            findings.append((s.shot_number, "建议调整",
                             f"依赖特殊场地（{','.join(hits)}），用户未确认有此场地",
                             ["场地条件"]))
    return findings


def validate_business(sb: Storyboard) -> List[str]:
    """业务规则校验，返回错误信息列表（空=通过）。"""
    errors: List[str] = []
    shots = sb.shots

    # 1. 镜头序号连续且不重复
    nums = [s.shot_number for s in shots]
    if len(set(nums)) != len(nums):
        errors.append(f"[规则] shot_number 重复: {nums}")
    if sorted(nums) != list(range(1, len(nums) + 1)):
        errors.append(f"[规则] shot_number 不连续，应为 1..{len(nums)}，实际: {sorted(nums)}")

    # 1b. 镜头数量上限：防止模型失控生成过多镜头（服务端最终校验，不只靠前端）
    MAX_SHOTS = 60
    if len(shots) > MAX_SHOTS:
        errors.append(f"[规则] 镜头数量 {len(shots)} 超过上限 {MAX_SHOTS}，请精简分镜")

    # 2. 总时长误差 ≤ 容差（默认 2s，可用 LUPIN_DURATION_TOLERANCE 配置；v2 用
    # final_duration_seconds；v1 回退到 duration_seconds，行为不变）
    from .core.config import get_settings
    tol = get_settings().LUPIN_DURATION_TOLERANCE
    total = sum(s.effective_final_seconds for s in shots)
    if abs(total - sb.target_duration_seconds) > tol:
        errors.append(
            f"[规则] 总成片时长 {total}s 与目标 {sb.target_duration_seconds}s "
            f"误差 {abs(total - sb.target_duration_seconds):.1f}s，超过 {tol:g}s 上限"
        )

    # 2b. 单镜头时长不能超过目标总时长（服务端最终校验）
    for s in shots:
        if s.effective_final_seconds > sb.target_duration_seconds:
            errors.append(
                f"[规则] 镜头 {s.shot_number}: 单镜头成片时长 {s.effective_final_seconds}s "
                f"超过目标总时长 {sb.target_duration_seconds}s"
            )

    # 2c. v2：录制时长必须 ≥ 成片使用时长（要多拍几条备选）
    for s in shots:
        if s.record_duration_seconds is not None:
            if s.record_duration_seconds < s.effective_final_seconds:
                errors.append(
                    f"[规则] 镜头 {s.shot_number}: 建议录制时长 {s.record_duration_seconds}s "
                    f"小于成片使用时长 {s.effective_final_seconds}s，录制时长应≥成片时长"
                )

    # 2d. v2 模式：验收标准与拍摄步骤必填（v1 数据无这些字段，跳过以保持向后兼容）
    if _is_v2_storyboard(sb):
        for s in shots:
            if not s.acceptance_criteria:
                errors.append(f"[规则] 镜头 {s.shot_number}: acceptance_criteria 为空，"
                              f"必须给出可观察的画面验收标准")
            if not s.shoot_steps:
                errors.append(f"[规则] 镜头 {s.shot_number}: shoot_steps 为空，"
                              f"必须给出按顺序的具体拍摄步骤")
            if not s.failure_modes:
                errors.append(f"[规则] 镜头 {s.shot_number}: failure_modes 为空，"
                              f"至少给出1个常见失败情况及解决步骤")
            if not s.reshoot_guide:
                errors.append(f"[规则] 镜头 {s.shot_number}: reshoot_guide 为空，"
                              f"必须给出补拍指引")

    # 3. 单人手机拍摄约束
    if sb.shoot.device == "手机" and sb.shoot.people == 1:
        for s in shots:
            text = f"{s.shooting_action} {s.camera_movement}"
            hits = [k for k in FORBIDDEN_SOLO_PHONE if k in text]
            if hits:
                errors.append(
                    f"[约束] 镜头 {s.shot_number}: 单人手机拍摄下出现不可行方式 {hits}"
                    f"（动作: {s.shooting_action[:30]}…）"
                )

    # 4. 素材清单聚合一致性（宽松：镜头有需求但清单为空则警告为错误，要求程序补齐）
    aggregated = set()
    for s in shots:
        aggregated.update(s.props or [])
        aggregated.update(s.required_assets or [])
    if aggregated and not sb.materials_checklist:
        errors.append("[规则] materials_checklist 为空，但镜头中有道具/素材需求，需补齐")

    return errors


def validate_warnings(sb: Storyboard) -> List[str]:
    """非阻断性检查，返回警告信息列表（不影响通过/不通过）。

    目前只做抽象词检查：shooting_action / camera_movement / shoot_steps 中出现
    "缓慢/稍微/适当/轻轻/慢慢"等无量化词，且同一文本中没有具体做法
    （数字单位或方法关键词）时，提示创作者补充具体做法。
    """
    warnings: List[str] = []
    for s in sb.shots:
        texts = [s.shooting_action or "", s.camera_movement or ""] + list(s.shoot_steps or [])
        for text in texts:
            for word in ABSTRACT_ACTION_WORDS:
                if word in text and not any(m in text for m in CONCRETE_MARKERS):
                    warnings.append(
                        f"[提示] 镜头 {s.shot_number}: 出现抽象词「{word}」但无具体做法"
                        f"（{text[:30]}…），建议补充量化（如X秒/距离）或手法（如双手持机/焦点锁定）"
                    )
                    break  # 每个文本只报一次，避免刷屏

    # V2.1：首尾镜头结构建议（非阻断）
    if sb.shots:
        first = sb.shots[0]
        if not any(k in (first.shot_type or "") for k in ("远景", "全景", "大远景")):
            warnings.append(
                f"[结构] 首镜景别为「{first.shot_type or '未填'}」，"
                "建议开场用远景/全景先交代环境，再切入主体"
            )
        last = sb.shots[-1]
        closing_text = " ".join([
            last.voiceover or "", last.subtitle or "",
            last.shot_purpose or "", last.transition or "",
        ])
        if not any(k in closing_text for k in
                   ("结尾", "收尾", "总结", "回顾", "购买", "下单",
                    "关注", "再见", "谢谢", "结语")):
            warnings.append(
                f"[结构] 末镜（镜头 {last.shot_number}）缺少收尾感，"
                "建议加一句收束（总结/购买引导/关注），避免视频戛然而止"
            )
    return warnings


def validate_dict(data: dict) -> Tuple[List[str], "Storyboard | None"]:
    """先做 Pydantic 结构校验，再做业务规则校验。返回 (错误列表, Storyboard或None)。"""
    errors: List[str] = []
    try:
        sb = Storyboard.model_validate(data)
    except Exception as e:
        # 把 Pydantic 错误转成人类可读的具体错误
        for err in getattr(e, "errors", lambda: [])():
            loc = "/".join(str(p) for p in err.get("loc", ())) or "(root)"
            errors.append(f"[结构] {loc}: {err.get('msg', '')}（输入: {str(err.get('input', ''))[:40]}）")
        if not errors:
            errors.append(f"[结构] 数据结构校验失败: {e}")
        return errors, None
    errors.extend(validate_business(sb))
    return errors, sb
