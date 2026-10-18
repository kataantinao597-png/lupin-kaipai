"""regen_shot prompt v1。版本规则见 ideas_v1.py。"""
from typing import List

VERSION = "regen_shot_v1"


def build_regen_shot_messages(sb: dict, shot_number: int, instruction: str,
                              locked: List[int], feedback: str = None) -> List[dict]:
    import json as _json
    prompt = f"""你是短视频分镜师。请根据以下完整分镜方案，只重新生成第 {shot_number} 号镜头。

完整分镜（JSON）：
{_json.dumps(sb, ensure_ascii=False)[:6000]}

修改指令：{instruction}

锁定镜头（绝对不许改动）：{locked if locked else "无"}

要求：
1. 只输出重新生成的单个镜头 JSON，字段与原分镜的 shot 字段完全一致（shot_number 保持为 {shot_number}）
2. 新镜头的拍摄动作必须与前后镜头在逻辑上衔接
3. 时长尽量与原镜头一致；如需调整，总时长变化不要超过2秒
4. 保持单人手机等拍摄约束；不要编造无依据的销量/功效/认证
5. 只输出 JSON，不要任何解释文字"""
    msgs = [{"role": "user", "content": prompt}]
    if feedback:
        msgs.append({"role": "user", "content": feedback})
    return msgs
