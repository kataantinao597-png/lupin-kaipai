"""Phase 5：Brief 解析器 —— 自然语言 → 结构化 Brief。

任务书 §5.1 / §9：从用户一句话提取并校验 product_id/产品名、平台/内容形式、
营销目标/目标人群、预算/人员/设备/场地、视频时长、风格、禁用表达、交付要求、
已确认默认项、待核实项。

规则：
- 走 gateway.generate_with_retry（或等价的 generate(prompt, feedback) 可调用对象），
  Pydantic 校验；JSON 无效允许 ≤2 次受控修复（max_repairs=2）；
- 一般字段缺失 → 用透明默认值并在 confirmed_defaults 中标出；
- 关键事实（产地/认证/功效/价格/销量等）用户未提供或依据不足时**绝不臆造**，
  必须列入 items_to_verify（待核实）；
- 绝不触碰密钥：Brief 只含业务字段。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "Brief",
    "BriefParseError",
    "DEFAULTS",
    "MAX_BRIEF_REPAIRS",
    "parse_brief_from_text",
    "merge_brief",
    "merge_brief_dict",
    "brief_to_prompt_text",
]

#: JSON 无效时的受控修复上限（初次 + 2 次修复）
MAX_BRIEF_REPAIRS = 2

#: 缺失一般字段时的透明默认值
DEFAULTS: dict[str, Any] = {
    "platform": "抖音",
    "content_form": "短视频",
    "video_duration_sec": 60,
    "style": "拉呱风（山东农人口语，实在、唠嗑、不端着）",
    "deliverables": ["口播脚本", "分镜", "拍摄清单", "平台文案"],
}


class BriefParseError(ValueError):
    """brief 解析失败：超过修复上限仍未得到合法 JSON/结构。"""


class Brief(BaseModel):
    """任务书 §5.1 的结构化 Brief。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    product_id: Optional[str] = Field(default=None, max_length=64)
    product_name: str = Field(default="", max_length=200)
    platform: str = Field(default="", max_length=64)
    content_form: str = Field(default="", max_length=64)
    marketing_goal: str = Field(default="", max_length=500)
    target_audience: str = Field(default="", max_length=500)
    budget_yuan: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    people_count: Optional[int] = Field(default=None, ge=1, le=1000)
    devices: list[str] = Field(default_factory=list, max_length=20)
    location: str = Field(default="", max_length=200)
    video_duration_sec: int = Field(default=60, ge=5, le=3600)
    style: str = Field(default="", max_length=200)
    banned_phrases: list[str] = Field(default_factory=list, max_length=50)
    deliverables: list[str] = Field(default_factory=list, max_length=20)
    # 已确认的默认项：说明哪些字段用了透明默认值
    confirmed_defaults: list[str] = Field(default_factory=list, max_length=50)
    # 待核实项：关键事实不明，绝不臆造
    items_to_verify: list[str] = Field(default_factory=list, max_length=50)
    raw_text: str = Field(default="", max_length=4000)

    @field_validator("devices", "banned_phrases", "deliverables",
                     "confirmed_defaults", "items_to_verify",
                     mode="before")
    @classmethod
    def _coerce_str_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return [str(x) for x in v if str(x).strip()][:50]

    @field_validator("video_duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, v: Any) -> Any:
        # 模型按提示用 0 表示"未指定" → 转回默认 60
        if v in (None, 0, "0", ""):
            return 60
        return v

    @field_validator("budget_yuan", "people_count", mode="before")
    @classmethod
    def _coerce_zero_to_none(cls, v: Any) -> Any:
        # 0 表示"未指定"
        if v in (0, "0", ""):
            return None
        return v


_BRIEF_FIELDS_HINT = (
    "product_id, product_name, platform, content_form, marketing_goal, "
    "target_audience, budget_yuan, people_count, devices(数组), location, "
    "video_duration_sec(整数秒), style, banned_phrases(数组), "
    "deliverables(数组), confirmed_defaults(数组), items_to_verify(数组)"
)

_SYSTEM_RULES = (
    "你是农产品短视频项目的 brief 解析器。把用户的一句话需求解析为结构化 brief，"
    "只输出纯 JSON（不要 markdown、不要解释），字段：" + _BRIEF_FIELDS_HINT + "。"
    "规则："
    "1) 用户没说的一般字段填空字符串/空数组/0（由后端填透明默认值），"
    "不要编造具体数值；"
    "2) 产地、认证（有机/绿色食品/地理标志等）、功效（治病/减肥/降糖等）、"
    "价格、销量等关键事实：用户没明确说的一律不许写进正文，"
    "必须逐条列入 items_to_verify（待核实），措辞如\"产地待核实\"；"
    "3) 用户明确说的禁用表达放入 banned_phrases；"
    "4) video_duration_sec 解析为整数秒，\"一分钟\"→60；"
    "5) budget_yuan 解析为数字（元），\"300 块\"→300；"
    "6) confirmed_defaults 留空数组（由后端填写）。"
)


def _make_prompt(user_text: str, feedback: Optional[str]) -> str:
    prompt = f"{_SYSTEM_RULES}\n用户原话：{user_text}"
    if feedback:
        prompt += f"\n\n上次输出未通过校验，请修正后重新输出完整 JSON：\n{feedback}"
    return prompt


def parse_brief_from_text(
    user_text: str,
    generate: Callable[[str, Optional[str]], dict[str, Any]],
    *,
    max_repairs: int = MAX_BRIEF_REPAIRS,
) -> Brief:
    """自然语言 → Brief。

    generate(prompt, feedback) -> dict：prompt→解析后 JSON 的可调用对象；
    feedback 非空时为修复轮。JSON/结构非法时做 ≤max_repairs 次受控修复，
    仍失败抛 BriefParseError。
    """
    text = (user_text or "").strip()
    if not text:
        raise BriefParseError("用户输入为空，无法解析 brief")
    last_errors: list[str] = []
    feedback: Optional[str] = None
    for _ in range(max_repairs + 1):
        try:
            data = generate(_make_prompt(text, feedback), feedback)
        except Exception as e:  # noqa: BLE001  模型调用异常直接失败，不吞
            raise BriefParseError(f"brief 解析模型调用失败: {e}") from e
        if not isinstance(data, dict):
            last_errors = ["模型输出不是 JSON 对象"]
            feedback = "上次输出不是合法 JSON 对象，请只输出纯 JSON。"
            continue
        try:
            brief = Brief.model_validate(data)
        except Exception as e:  # pydantic ValidationError
            last_errors = [str(e)[:500]]
            feedback = f"字段校验失败：{last_errors[0]}"
            continue
        return _apply_transparent_defaults(brief, raw_text=text,
                                           raw_dict=data)
    raise BriefParseError(
        "brief 解析失败（已达修复上限）："
        + "; ".join(last_errors[:3]))


#: 视为"未指定"的值（模型按提示用空值表示用户没说）
_UNSPECIFIED_VALUES = (None, "", [], {}, 0, "0")


def _apply_transparent_defaults(brief: Brief, raw_text: str,
                                raw_dict: Optional[dict[str, Any]] = None,
                                ) -> Brief:
    """缺失的一般字段填透明默认值，并在 confirmed_defaults 中标出。

    是否"缺失"按模型原始输出判定（raw_dict），而不是按 Pydantic 填充后的值，
    否则无法区分"用户没说"和"默认值恰好命中"。
    """
    raw = raw_dict or {}
    data = brief.model_dump()
    marked: list[str] = list(data.get("confirmed_defaults") or [])
    for field, default in DEFAULTS.items():
        if raw.get(field) in _UNSPECIFIED_VALUES:
            data[field] = default
            label = {
                "platform": "平台",
                "content_form": "内容形式",
                "video_duration_sec": "视频时长",
                "style": "风格",
                "deliverables": "交付要求",
            }.get(field, field)
            marked.append(f"{label}：未指定，使用默认“{default}”")
    # 去重保序
    seen: set[str] = set()
    data["confirmed_defaults"] = [
        x for x in marked if not (x in seen or seen.add(x))]
    data["raw_text"] = raw_text
    return Brief.model_validate(data)


def merge_brief(base: Brief, update: Brief) -> Brief:
    """多轮对话的 brief 增量更新：update 中非空字段覆盖 base，同名字段合并去重。

    用于同一 conversation 内后续消息复用上下文（4b 遗留③）。
    """
    b, u = base.model_dump(), update.model_dump()
    merged: dict[str, Any] = {}
    list_fields = {"devices", "banned_phrases", "deliverables",
                   "confirmed_defaults", "items_to_verify"}
    for key in b:
        if key in list_fields:
            merged[key] = _merge_str_list(b[key] or [], u.get(key) or [])
        elif key == "raw_text":
            merged[key] = u.get(key) or b[key]
        else:
            uv = u.get(key)
            empty = uv in (None, "", [], 0)
            merged[key] = b[key] if empty else uv
    return Brief.model_validate(merged)


def _merge_str_list(a: list[str], b: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in list(a) + list(b):
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def merge_brief_dict(base: Optional[dict[str, Any]],
                     delta: Optional[dict[str, Any]]) -> dict[str, Any]:
    """会话 brief_json 的增量合并（多轮对话复用 conversation 时用）。

    delta 中非空值覆盖/补齐 base；嵌套 dict 递归合并；list 合并去重。
    """
    b = dict(base or {})
    for k, v in (delta or {}).items():
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            b[k] = merge_brief_dict(b[k], v)
        elif isinstance(v, list) and isinstance(b.get(k), list):
            b[k] = _merge_str_list([str(x) for x in b[k]],
                                   [str(x) for x in v])
        elif v not in (None, "", [], {}):
            b[k] = v
        elif k not in b:
            b[k] = v
    return b


def brief_to_prompt_text(brief: Brief) -> str:
    """把 Brief 渲染成下游工具 prompt 用的紧凑文本（含已确认事实与待核实标记）。"""
    lines = [
        f"产品：{brief.product_name or '（未指定）'}",
        f"平台/形式：{brief.platform} / {brief.content_form}",
        f"营销目标：{brief.marketing_goal or '（未指定）'}",
        f"目标人群：{brief.target_audience or '（未指定）'}",
        f"预算：{brief.budget_yuan if brief.budget_yuan is not None else '（未指定）'}元",
        f"人员：{brief.people_count if brief.people_count is not None else '（未指定）'}人",
        f"设备：{'、'.join(brief.devices) if brief.devices else '（未指定）'}",
        f"场地：{brief.location or '（未指定）'}",
        f"时长：{brief.video_duration_sec} 秒",
        f"风格：{brief.style}",
    ]
    if brief.banned_phrases:
        lines.append(f"禁用表达：{'、'.join(brief.banned_phrases)}")
    if brief.items_to_verify:
        lines.append("待核实（绝不作为确定宣传）："
                     + "；".join(brief.items_to_verify))
    if brief.confirmed_defaults:
        lines.append("已用默认项：" + "；".join(brief.confirmed_defaults))
    return "\n".join(lines)
