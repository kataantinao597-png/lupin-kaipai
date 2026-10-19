"""028：标准化业务错误码。

编码规则（千位 = 域）：
- 0xxx      成功 / 通用客户端错误
- 1xxx      作品与业务数据
- 2xxx      AI 模型调用
- 3xxx      产品事实库
- 4xxx      认证 / 组织 / 权限
- 5xxx      系统与基础设施

前端按 code 展示提示（先定义，后端用，前端留 TODO 映射文案）。
HTTP 状态码由异常类型决定，业务码用于精确定位问题。
"""

# ---- 通用 ----
SUCCESS = 0
INVALID_PARAM = 4000        # 请求参数非法（422 校验失败时用）
UNAUTHENTICATED = 4010      # 未登录
TOKEN_EXPIRED = 4011        # 登录已过期
ACCOUNT_DISABLED = 4020     # 账号不可用
QUOTA_EXCEEDED = 4021       # S10：组织配额超限（402/403，由场景决定）
FORBIDDEN = 4030            # 通用禁止
RESOURCE_NOT_FOUND = 4040   # 通用资源不存在
CONFLICT = 4090             # 通用冲突
VERSION_CONFLICT = 4090     # 乐观锁版本冲突（与 CONFLICT 同值，别名）

# ---- 1xxx 作品与业务数据 ----
WORK_NOT_FOUND = 1001
WORK_IN_TRASH = 1002        # 作品在回收站，正常业务流不可见
WORK_VERSION_NOT_FOUND = 1003
INVALID_WORK_STATUS = 1004
EXPORT_FAILED = 1005

# ---- 2xxx AI 模型调用 ----
AI_TIMEOUT = 2001
AI_UNAUTHORIZED = 2002      # Key 无效 / 认证失败
AI_BAD_RESPONSE = 2003      # 模型返回无法解析
AI_RATE_LIMITED = 2004      # 触发限流（429 / 本地限流）
AI_NOT_CONFIGURED = 2005    # 未配置可用 Key
AI_PROVIDER_ERROR = 2006    # 提供商 5xx 等服务端错误
IDEMPOTENCY_KEY_CONFLICT = 2007  # 同一幂等键被用于不同请求体（V2.1 #3）

# ---- 3xxx 产品事实库 ----
FACT_UNVERIFIED = 3001      # 事实未核实 / 不允许使用
FACT_NOT_FOUND = 3002
FACT_EXPIRED = 3003
FACT_CONFLICT = 3004        # 同一属性存在冲突事实
PRODUCT_NOT_FOUND = 3005    # 产品档案不存在（V3.0 B-1）
PRODUCT_CODE_CONFLICT = 3006  # 同一组织内产品编号重复（V3.0 B-1）
PRODUCT_HAS_FACTS = 3007    # 仍有事实引用该产品，禁止删除（V3.0 B-1）
COPY_DOC_NOT_FOUND = 3008   # 文案文档不存在（V3.0 D）
COPY_VERSION_NOT_FOUND = 3009  # 文案版本不存在（V3.0 D）

# ---- 4xxx 组织与权限 ----
PERMISSION_DENIED = 4001
ORG_NOT_FOUND = 4002
MEMBER_NOT_FOUND = 4003
INVITE_INVALID = 4004       # 邀请码无效或已过期
ROLE_INVALID = 4005
ORG_HAS_WORKS = 4006        # 组织下仍有作品时的限制操作可复用

# ---- 5xxx 系统 ----
SYSTEM_ERROR = 5000
DB_ERROR = 5001
SERVICE_UNAVAILABLE = 5030


ERROR_MESSAGES: dict[int, str] = {
    SUCCESS: "成功",
    INVALID_PARAM: "请求参数非法",
    UNAUTHENTICATED: "未登录",
    TOKEN_EXPIRED: "登录已过期，请重新登录",
    ACCOUNT_DISABLED: "账号不可用",
    QUOTA_EXCEEDED: "组织配额已用完，请联系管理员调整",
    FORBIDDEN: "禁止访问",
    RESOURCE_NOT_FOUND: "资源不存在",
    CONFLICT: "请求冲突",
    WORK_NOT_FOUND: "作品不存在",
    WORK_IN_TRASH: "作品已在回收站",
    WORK_VERSION_NOT_FOUND: "作品版本不存在",
    INVALID_WORK_STATUS: "非法作品状态",
    EXPORT_FAILED: "导出失败",
    AI_TIMEOUT: "模型请求超时",
    AI_UNAUTHORIZED: "模型认证失败，请检查 API Key",
    AI_BAD_RESPONSE: "模型返回内容无法解析",
    AI_RATE_LIMITED: "请求过于频繁，请稍后再试",
    AI_NOT_CONFIGURED: "模型服务未配置",
    AI_PROVIDER_ERROR: "模型服务异常",
    IDEMPOTENCY_KEY_CONFLICT: "该幂等键已用于另一份不同的请求内容，请更换幂等键后重试",
    FACT_UNVERIFIED: "事实未经核实或不允许使用",
    FACT_NOT_FOUND: "事实不存在",
    FACT_EXPIRED: "事实已过期，需重新核实",
    FACT_CONFLICT: "事实存在冲突",
    PRODUCT_NOT_FOUND: "产品档案不存在",
    PRODUCT_CODE_CONFLICT: "同一组织内产品编号已存在",
    PRODUCT_HAS_FACTS: "该产品仍有事实引用，无法删除",
    COPY_DOC_NOT_FOUND: "文案文档不存在",
    COPY_VERSION_NOT_FOUND: "文案版本不存在",
    PERMISSION_DENIED: "权限不足",
    ORG_NOT_FOUND: "组织不存在",
    MEMBER_NOT_FOUND: "成员不存在",
    INVITE_INVALID: "邀请码无效或已过期",
    ROLE_INVALID: "无效角色",
    ORG_HAS_WORKS: "组织下仍有作品，无法执行该操作",
    SYSTEM_ERROR: "系统内部错误",
    DB_ERROR: "数据库错误",
    SERVICE_UNAVAILABLE: "服务暂不可用",
}


def message_for(code: int, default: str = "操作失败") -> str:
    """取错误码默认文案；未知码返回 default。"""
    return ERROR_MESSAGES.get(code, default)
