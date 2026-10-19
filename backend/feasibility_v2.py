"""V2.0 P0：拍摄可行性检查（纯规则层，不调模型，可单元测试）。

在生成分镜方案前，根据用户填写的拍摄条件做可行性预检，
输出每项检查的状态：
  - ready：条件已满足
  - need_confirm：需要用户确认
  - need_alternative：当前不具备，给出替代路线（绝不直接拒单）

check_feasibility(product, shoot, video_purpose) 为纯函数。
"""
from __future__ import annotations

from typing import Dict, List

# 状态常量
READY = "ready"
NEED_CONFIRM = "need_confirm"
NEED_ALTERNATIVE = "need_alternative"

_SEVERITY = {READY: 0, NEED_CONFIRM: 1, NEED_ALTERNATIVE: 2}


def _check(item: str, status: str, message: str, alternative: str = "") -> Dict:
    return {"item": item, "status": status, "message": message,
            "alternative": alternative}


def _infer_required_scenes(product: Dict, video_purpose: str) -> List[tuple]:
    """按产品关键词 + 视频用途，推断可能需要的场景。

    返回 [(场景名, 用途说明)]。注意这是"可能需要"，最终以用户确认为准，
    不得因推断就假定用户拥有该场景。
    """
    text = (product.get("product_name") or "") + (product.get("description") or "")
    scenes: List[tuple] = []
    if any(k in text for k in ["樱桃", "苹果", "果园", "水果", "采摘", "大枣", "葡萄"]):
        scenes.append(("果园", "果园实拍 / 采摘过程镜头"))
    if any(k in text for k in ["扒鸡", "厨房", "烹饪", "制作", "加工", "蒸", "煮"]):
        scenes.append(("厨房", "制作过程 / 加工环节镜头"))
    if video_purpose == "产地记录" and not any(s[0] == "果园" for s in scenes):
        scenes.append(("产地场景", "产地记录类视频需要真实产地画面作证据"))
    # 兜底：几乎所有产品都需要室内桌面摆拍
    scenes.append(("室内桌面", "产品摆拍 / 特写镜头"))
    return scenes


def _check_scenes(product: Dict, shoot: Dict, video_purpose: str) -> List[Dict]:
    """a. 场景依赖：需要的场景 vs location + unavailable_scenes。"""
    location = shoot.get("location") or ""
    unavailable = shoot.get("unavailable_scenes") or []
    checks = []
    # 场景别名：location 里出现任一别名即视为具备该场景
    SCENE_ALIASES = {
        "果园": ["果园", "果树", "采摘园"],
        "厨房": ["厨房"],
        "室内桌面": ["室内", "桌面", "家里", "客厅", "房间"],
        "产地场景": ["产地", "基地"],
    }
    for scene, usage in _infer_required_scenes(product, video_purpose):
        if scene in unavailable or any(scene in u for u in unavailable):
            # 关键规则：不得拒单，必须给替代路线
            alt = _alternative_for_scene(scene, product)
            checks.append(_check(
                f"场景依赖：{scene}",
                NEED_ALTERNATIVE,
                f"「{scene}」不可用（{usage}），原方案中的相关镜头无法按原样拍摄。",
                alt,
            ))
        elif any(a in location for a in SCENE_ALIASES.get(scene, [scene])):
            checks.append(_check(f"场景依赖：{scene}", READY,
                                 f"拍摄地点「{location}」覆盖「{scene}」，{usage}可正常安排。"))
        else:
            checks.append(_check(
                f"场景依赖：{scene}",
                NEED_CONFIRM,
                f"未确认是否有「{scene}」可用（{usage}）。请确认，或选择替代路线。",
                _alternative_for_scene(scene, product),
            ))
    return checks


def _alternative_for_scene(scene: str, product: Dict) -> str:
    """为缺失场景生成替代路线。绝不返回空字符串，绝不拒单。"""
    name = product.get("product_name") or "产品"
    routes = {
        "果园": f"无果园→改用网购/市购{name}＋室内桌面摆拍＋包装/果实特写重排分镜，"
                 f"用果园来信式文案转场，不虚构采摘画面。",
        "厨房": f"无厨房拍摄条件→改用成品摆拍＋食用场景（餐桌/户外野餐）＋"
                 f"包装与配料特写，制作环节用口播一句话带过，不摆拍假制作过程。",
        "产地场景": f"无产地实拍条件→改用产品实物多角度特写＋包装上的产地信息特写＋"
                     f"口播说明产地，涉及产地的内容全部进待确认清单，不做确定性宣传。",
        "室内桌面": f"无室内桌面→可用干净床单/纸箱作临时背景板，手机开大光圈模式虚化背景。",
    }
    return routes.get(scene, f"「{scene}」不可用→改用现有场地＋产品特写重排，创意目标不变。")


def _check_props(shoot: Dict) -> List[Dict]:
    """b. 道具依赖：常用道具 vs available_resources。"""
    resources = shoot.get("available_resources") or []
    res_text = "、".join(resources)
    has = lambda *names: any(n in res_text for n in names)
    checks = []
    if has("支架", "三脚架", "自拍杆"):
        checks.append(_check("道具：手机支架", READY, "有支架，固定机位镜头可正常拍摄。"))
    else:
        checks.append(_check(
            "道具：手机支架", NEED_CONFIRM,
            "未填写手机支架。固定机位、延时类镜头会抖动，影响成片质量。",
            "替代：用书本/水杯/纸箱固定手机；或全部改手持＋后期防抖。",
        ))
    lighting = shoot.get("lighting") or ""
    if "自然光" in lighting or has("补光灯", "灯"):
        checks.append(_check("道具：光线", READY,
                             f"当前光线条件「{lighting or '有补光设备'}」，满足基础拍摄。"))
    else:
        checks.append(_check(
            "道具：光线", NEED_CONFIRM,
            "未说明光线条件。光线不足会导致画面发灰、有噪点。",
            "替代：优先白天靠窗拍摄；晚上用台灯＋白纸反光应急。",
        ))
    if has("背景布", "背景纸"):
        checks.append(_check("道具：背景布", READY, "有背景布，产品特写背景更干净。"))
    else:
        checks.append(_check(
            "道具：背景布", NEED_CONFIRM,
            "无背景布不影响拍摄，特写时注意避开杂乱背景。",
            "替代：用纯色床单/大张白纸作临时背景。",
        ))
    return checks


def _check_people(shoot: Dict) -> List[Dict]:
    """c. 人力：单人 vs 多人镜头需求。"""
    try:
        people = int(shoot.get("people") or 1)
    except (TypeError, ValueError):
        people = 1
    if people >= 2:
        return [_check("人力", READY, f"{people}人协作，一人拍摄一人出镜/递道具，可安排。")]
    return [_check(
        "人力", NEED_CONFIRM,
        "单人拍摄：无法同时出镜又掌机，需要出镜的镜头要另想办法。",
        "替代：全部用固定机位＋自拍模式；或请家人临时帮忙按 3 秒快门。",
    )]


def _check_skill(shoot: Dict) -> List[Dict]:
    """d. 熟练度：新手标记高难度运镜，给简化替代。"""
    level = (shoot.get("skill_level") or "").strip()
    if level == "beginner":
        return [_check(
            "拍摄熟练度：新手", NEED_CONFIRM,
            "新手用户：推/拉/环绕等运镜容易抖、容易跟丢主体，返工率高。",
            "替代：多用固定机位＋后期裁剪模拟推近；每个镜头多录 2 秒方便剪辑。",
        )]
    if level in ("intermediate", "advanced"):
        return [_check(f"拍摄熟练度：{level}", READY,
                       "有拍摄基础，可按常规方案执行。")]
    # 未填写时按新手对待，但只提醒不阻断
    return [_check(
        "拍摄熟练度：未填写", NEED_CONFIRM,
        "未填写熟练程度，默认按新手标准生成保守方案（多固定机位）。",
        "如有拍摄经验，可在 shoot.skill_level 中填 intermediate / advanced。",
    )]


def check_feasibility(product: Dict, shoot: Dict, video_purpose: str) -> Dict:
    """可行性检查主入口（纯函数）。

    Args:
        product: {"product_name": str, "description": str, ...}
        shoot: {"device": str, "people": int, "lighting": str, "location": str,
                "available_resources": [str], "unavailable_scenes": [str],
                "skill_level": "beginner"|"intermediate"|"advanced"|""}
        video_purpose: 品牌宣传 | 商品销售 | 产品介绍 | 产地记录 等

    Returns:
        {"status": "ready"|"need_confirm"|"need_alternative",
         "checks": [{"item", "status", "message", "alternative"}]}
    """
    product = product or {}
    shoot = shoot or {}
    checks: List[Dict] = []
    checks.extend(_check_scenes(product, shoot, video_purpose or ""))
    checks.extend(_check_props(shoot))
    checks.extend(_check_people(shoot))
    checks.extend(_check_skill(shoot))

    overall = READY
    for c in checks:
        if _SEVERITY[c["status"]] > _SEVERITY[overall]:
            overall = c["status"]
    return {"status": overall, "checks": checks}
