"""鲁品开拍 - 分镜业务规则校验（由实验 validate.py 移植，逻辑一致）。"""
from typing import List, Tuple
from .schemas import Storyboard

# 单人手机拍摄下不可用的拍摄方式关键词
FORBIDDEN_SOLO_PHONE = ["航拍", "摇臂", "多机位", "轨道", "无人机", "斯坦尼康", "大摇臂", "稳定器", "滑轨"]

# 可行性规则：需要多人配合的关键词
NEEDS_MULTI_PEOPLE = ["双人", "多人", "群演", "演员", "助手协助", "摄影师", "跟拍人员"]

# 可行性规则：需要特殊场地的关键词
NEEDS_SPECIAL_LOCATION = ["摄影棚", "影棚", "演播室", "专业厨房", "实验室"]


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

    # 2. 总时长误差 ≤ 2 秒
    total = sum(s.duration_seconds for s in shots)
    if abs(total - sb.target_duration_seconds) > 2:
        errors.append(
            f"[规则] 总时长 {total}s 与目标 {sb.target_duration_seconds}s "
            f"误差 {abs(total - sb.target_duration_seconds):.1f}s，超过 2s 上限"
        )

    # 2b. 单镜头时长不能超过目标总时长（服务端最终校验）
    for s in shots:
        if s.duration_seconds > sb.target_duration_seconds:
            errors.append(
                f"[规则] 镜头 {s.shot_number}: 单镜头时长 {s.duration_seconds}s "
                f"超过目标总时长 {sb.target_duration_seconds}s"
            )

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
