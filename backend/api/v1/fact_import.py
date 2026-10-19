"""产品文档导入 API（v2 产品资料「导入已有产品」）。

- POST /api/v1/facts/import-document   上传 .txt/.md → 解析文本 → 事实候选
- POST /api/v1/facts/batch             批量创建事实（用户勾选确认后写入）

权限：fact:create（与 facts.py 一致，无权一律 404 防枚举）。
AI 增强：走现有 AI 网关 task_type=fact_extract；失败/未配置时降级为
规则抽取（ai_used=false），绝不伪造 AI 结果。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success
from ...rbac import get_membership, has_permission, assert_can
from ...repositories.fact_repository import FactRepository
from .schemas import FactOut

router = APIRouter(prefix="/facts", tags=["v1-fact-import"])

MAX_DOC_BYTES = 2 * 1024 * 1024  # 文档上限 2MB
ALLOWED_EXT = {".txt", ".md"}

CATEGORIES = ["产地", "品种", "口感", "工艺", "规格", "保存方式", "认证", "其他"]

# 分类关键词（规则抽取用）
_CATEGORY_KEYWORDS = [
    ("口感", ["口感", "脆", "甜", "香", "嫩", "多汁", "酥", "爽口", "细腻"]),
    ("产地", ["产自", "产地", "种植", "基地", "果园", "山东", "烟台", "章丘", "德州", "济南"]),
    ("品种", ["品种", "品系", "栽培"]),
    ("工艺", ["工艺", "制作", "卤制", "熏", "烤", "传统", "手工", "秘方", "流程"]),
    ("规格", ["规格", "斤", "克", "kg", "KG", "装", "包装", "净含量",
             "mm", "cm", "果径", "直径", "尺寸"]),
    ("保存方式", ["保存", "冷藏", "冷冻", "保质期", "存放", "储存"]),
    ("认证", ["认证", "绿色", "有机", "地理标志", "检测", "报告"]),
]


def _require_fact_create(db: Session, user: User, org_id: str):
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, "fact:create"):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, "fact:create", "导入产品文档")
    return ms


def guess_category(text: str) -> str:
    """按关键词猜分类；命中多个取第一个，命中无返回"其他"。"""
    for cat, kws in _CATEGORY_KEYWORDS:
        if any(k in text for k in kws):
            return cat
    return "其他"


def _clean_markdown(text: str) -> str:
    """去 markdown 标记：代码围栏/标题/图片/链接/加粗/引用/表格线。"""
    lines = []
    in_fence = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)          # 标题
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)  # 图片
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)  # 链接留文字
        s = re.sub(r"[*_`~]{1,3}", "", s)          # 加粗/斜体/行内代码
        s = re.sub(r"^>\s*", "", s)                # 引用
        s = re.sub(r"^\|?[\s:\-|]+\|?$", "", s)     # 表格分隔线
        s = s.replace("|", " ")
        if s:
            lines.append(s)
    return "\n".join(lines)


def extract_rule_candidates(text: str, limit: int = 30) -> List[dict]:
    """规则抽取：纯文本解析打底。返回 [{content, category, source}]。

    规则：按行切分，过长行再按句号切；保留 8~200 字、去重、去纯数字行。
    """
    cleaned = _clean_markdown(text or "")
    seen = set()
    out: List[dict] = []
    chunks: List[str] = []
    for ln in cleaned.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) > 120:
            chunks.extend(re.split(r"[。！？；\n]", ln))
        else:
            chunks.append(ln)
    for ch in chunks:
        ch = ch.strip(" -•·\t")
        if len(ch) < 8 or len(ch) > 200:
            continue
        if re.fullmatch(r"[\d\s.,，。、：:；;%％\-–—/]+", ch):
            continue
        if ch in seen:
            continue
        seen.add(ch)
        out.append({"content": ch, "category": guess_category(ch),
                    "source": "rule"})
        if len(out) >= limit:
            break
    return out


def _validate_candidates(data: dict):
    items = data.get("candidates")
    if not isinstance(items, list):
        return ["[结构] 缺少 candidates 数组"], None
    out = []
    for raw in items[:30]:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content", "")).strip()
        if len(content) < 4:
            continue
        cat = str(raw.get("category", "")).strip() or "其他"
        if cat not in CATEGORIES:
            cat = "其他"
        out.append({"content": content[:200], "category": cat,
                    "source": "ai"})
    return [], out


def _build_extract_messages(text: str):
    cats = "、".join(CATEGORIES)
    return [
        {"role": "system",
         "content": "你是农产品资料整理助手。从用户提供的产品文档中抽取可核实的"
                    "产品事实（如产地、品种、口感、工艺、规格、保存方式、认证等），"
                    "每条事实独立成句、客观陈述、不编造。只输出纯 JSON，"
                    "格式：{\"candidates\": [{\"content\": \"...\", \"category\": \"...\"}]}，"
                    f"category 只能是以下之一：{cats}。最多 20 条。"},
        {"role": "user", "content": text[:6000]},
    ]


def try_ai_candidates(gw, model, text: str) -> Optional[List[dict]]:
    """AI 抽取；任何失败返回 None（调用方降级为规则抽取）。"""
    try:
        cands, _meta = gw.generate_with_retry(
            "fact_extract", lambda _fb: _build_extract_messages(text),
            _validate_candidates, model=model or None, temperature=0.3)
        return cands
    except (BudgetExceededError, TaskLimitExceededError,
            AIFatalError, AIRetryableError, AIConfigError, Exception):
        return None


class ImportDocOut(BaseModel):
    filename: str
    ai_used: bool
    candidates: List[dict]


def _optional_gateway(request):
    """懒加载 AI 网关；未配置/失败返回 None（调用方降级为规则抽取）。

    注意：get_ai_gateway 依赖在无 Key 时直接抛 503，必须在路由内
    try，绝不能作为 Depends 硬依赖，否则连格式校验都到不了。
    """
    try:
        from ...core.deps import get_ai_gateway as _get_gw
        gen = _get_gw(request)
        return next(gen)
    except Exception:
        return None


@router.post("/import-document", response_model=ApiResponse[ImportDocOut])
async def import_document(
    request: Request,
    file: UploadFile = File(...),
    org_id: str = Form(...),
    product_id: str = Form(default=""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传产品文档（.txt/.md），返回事实候选列表供前端勾选确认。"""
    _require_fact_create(db, user, org_id)
    name = (file.filename or "").strip()
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise BusinessError("只支持 .txt / .md 文本文件",
                            code=error_codes.INVALID_PARAM, http_status=400)
    raw = await file.read(MAX_DOC_BYTES + 1)
    if len(raw) > MAX_DOC_BYTES:
        raise BusinessError("文件过大（上限 2MB）",
                            code=error_codes.INVALID_PARAM, http_status=400)
    if not raw.strip():
        raise BusinessError("文件内容为空",
                            code=error_codes.INVALID_PARAM, http_status=400)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="ignore")

    gw = _optional_gateway(request)
    model = request.headers.get("X-Lupin-Model", "").strip() or None
    ai_cands = try_ai_candidates(gw, model, text) if gw else None
    if ai_cands:
        cands, ai_used = ai_cands, True
    else:
        cands, ai_used = extract_rule_candidates(text), False
    return success(ImportDocOut(
        filename=name, ai_used=ai_used, candidates=cands).model_dump())


class FactBatchItem(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)
    category: str = Field(default="其他", max_length=16)
    source_type: str = Field(default="文档导入", max_length=32)
    source_ref: str = Field(default="", max_length=256)
    allow_use: bool = Field(default=True)


class FactBatchReq(BaseModel):
    org_id: str
    product_id: str = Field(default="", max_length=128)
    facts: List[FactBatchItem] = Field(..., min_length=1, max_length=50)


@router.post("/batch", response_model=ApiResponse[List[FactOut]],
             status_code=status.HTTP_201_CREATED)
def batch_create_facts(
    req: FactBatchReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量创建事实（导入确认时写入）。初始状态均为 draft。"""
    _require_fact_create(db, user, req.org_id)
    repo = FactRepository(db)
    created = []
    for item in req.facts:
        cat = item.category if item.category in CATEGORIES else "其他"
        f = repo.create(
            org_id=req.org_id, content=item.content.strip(),
            product_id=req.product_id or None, category=cat,
            source_type=item.source_type, source_ref=item.source_ref or None,
        )
        created.append(f)
    db.commit()
    return success([FactOut(
        id=f.id, org_id=f.org_id, product_id=f.product_id or "",
        content=f.content or "", category=f.category or "",
        source_type=f.source_type or "", source_ref=f.source_ref or "",
        status="draft", allow_use=bool(f.allow_use),
        verified_by=f.verified_by, verified_at=f.verified_at,
        expires_at=f.expires_at, version=f.version or 1,
        created_at=f.created_at, updated_at=f.updated_at,
    ).model_dump() for f in created])
