"""鲁品开拍 - AI 服务层（独立封装：调用、重试、错误映射、token 记录）。"""
import os
import time
import yaml
import requests
from typing import Callable, Dict, List, Optional, Tuple

BASE_URL = os.environ.get("LUPIN_API_BASE", "https://deepkey.top/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("LUPIN_MODEL", "gpt-6-luna")
MAX_RETRIES = 3
TIMEOUT = 120

CONFIG_PATH = "/home/hatch/.omp/agent/models.yml"


class AIConfigError(Exception):
    """配置类错误（Key 缺失等），不可重试。"""


class AIRetryableError(Exception):
    """可重试的调用错误（429/5xx/超时）。"""


class AIFatalError(Exception):
    """不可重试的调用错误（401/400 等）。"""


def load_api_key() -> str:
    """启动时从配置现读一次，放内存。绝不打印。
    
    优先级：环境变量 LUPIN_API_KEY > 配置文件（本地开发用）。
    云端部署时必须用环境变量，不要在镜像中打包密钥文件。
    """
    env_key = os.environ.get("LUPIN_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = yaml.safe_load(f)
        key = d["providers"]["deepkey"]["apiKey"]
    except Exception as e:
        raise AIConfigError(f"读取模型配置失败: {type(e).__name__}（可改用 LUPIN_API_KEY 环境变量）")
    if not key:
        raise AIConfigError("模型配置中未找到 apiKey")
    return key


class AIService:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or load_api_key()
        raw = (base_url or BASE_URL).rstrip("/")
        # 基础 URL 校验：防止 SSRF，只允许 http/https
        if not raw.startswith(("http://", "https://")):
            raise AIConfigError("API Base URL 必须以 http:// 或 https:// 开头")
        # 兼容两种输入：完整 endpoint 或仅 base（如 https://api.openai.com/v1）
        if raw.endswith("/chat/completions"):
            self.endpoint = raw
        else:
            self.endpoint = raw + "/chat/completions"
        self.session = requests.Session()
        self.total_tokens = 0
        self.call_count = 0

    def _post(self, payload: dict) -> dict:
        """单次 HTTP 调用，带错误映射。
        Key 仅放在 Authorization 头中发送；本函数不打印、不记录 Key。
        """
        try:
            r = self.session.post(
                self.endpoint, json=payload, timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except requests.Timeout:
            raise AIRetryableError("请求超时（120s）")
        except requests.RequestException as e:
            raise AIRetryableError(f"网络错误: {type(e).__name__}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            raise AIFatalError("鉴权失败（401）：API Key 无效或过期，请检查配置")
        if r.status_code == 429 or 500 <= r.status_code < 600:
            raise AIRetryableError(f"服务端限流/错误（HTTP {r.status_code}）")
        raise AIFatalError(f"请求失败（HTTP {r.status_code}）: {r.text[:120]}")

    def chat(self, messages: List[dict], model: str = DEFAULT_MODEL,
             temperature: float = 0.5, json_object: bool = False) -> Tuple[str, dict]:
        """返回 (content, usage)。HTTP 层重试最多 2 次（退避）。"""
        payload = {"model": model, "temperature": temperature, "messages": messages}
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        last_err = None
        for i in range(3):
            try:
                data = self._post(payload)
                break
            except AIRetryableError as e:
                last_err = e
                time.sleep(2 ** i)
        else:
            raise last_err
        self.call_count += 1
        usage = data.get("usage", {})
        self.total_tokens += usage.get("total_tokens", 0)
        return data["choices"][0]["message"]["content"], usage

    def generate_with_retry(
        self,
        make_messages: Callable[[Optional[str]], List[dict]],
        validate_fn: Callable[[dict], Tuple[List[str], object]],
        model: str = DEFAULT_MODEL,
        temperature: float = 0.3,
        chat_fn: Optional[Callable] = None,
    ) -> Tuple[object, dict]:
        """带校验重试的生成。

        make_messages(error_feedback) -> messages；error_feedback 为 None 表示首次。
        validate_fn(parsed_dict) -> (errors, obj)。
        返回 (校验通过的对象, {attempts, usage_list})。
        全部失败则抛 AIFatalError，消息为用户可读的错误说明。
        """
        chat = chat_fn or (lambda msgs: self.chat(msgs, model=model, temperature=temperature, json_object=True))
        feedback: Optional[str] = None
        attempts = 0
        usage_list: List[dict] = []
        last_errors: List[str] = []
        for n in range(1, MAX_RETRIES + 2):  # 1 次初试 + 最多 3 次重试
            attempts = n
            messages = make_messages(feedback)
            content, usage = chat(messages)
            usage_list.append(usage)
            import json as _json
            try:
                data = _json.loads(content)
            except _json.JSONDecodeError as e:
                last_errors = [f"[解析] 模型输出不是合法 JSON: {e}"]
                feedback = "上次输出不是合法 JSON，请只输出纯 JSON，不要加任何解释文字或 markdown 标记。"
                continue
            errors, obj = validate_fn(data)
            if not errors:
                return obj, {"attempts": attempts, "usage": usage_list}
            last_errors = errors
            feedback = (
                "上次输出未通过校验，请修正后重新输出完整 JSON（只输出 JSON）：\n"
                + "\n".join(f"- {e}" for e in errors)
            )
        raise AIFatalError(
            f"已重试 {MAX_RETRIES} 次仍未生成合法结果（共尝试 {attempts} 次）。"
            f"最后一次的校验问题：{'; '.join(last_errors[:5])}。"
            "建议：简化产品描述或缩短目标时长后重试。"
        )
