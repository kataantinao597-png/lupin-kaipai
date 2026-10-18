"""S10 计费脚手架：价格配置 + 对账单。

价格模型：
- price_configs 只增版本、不改历史；估算/对账取"最新版本"。
- item: ai_token_1k（每千 token）| ai_call（每次调用）
        | storage_gb_month（每 GB·月）| member_seat（每成员席位）。
- seed 数据为 0 占位（ensure_price_seed），**上线前必须由运营配置真实价格**；
  价格全 0 时对账单金额为 0 并标注 prices_unset=true。

对账流程（脚手架）：用量记录（ai_calls 台账）→ build_statement 生成对账单 →
issue_record 开出账单记录（issued）。支付/退款/发票不在本阶段范围。
"""
import calendar
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func

from .. import models

logger = logging.getLogger(__name__)

ITEM_AI_TOKEN_1K = "ai_token_1k"
ITEM_AI_CALL = "ai_call"
ITEM_STORAGE_GB_MONTH = "storage_gb_month"
ITEM_MEMBER_SEAT = "member_seat"
PRICE_ITEMS = (ITEM_AI_TOKEN_1K, ITEM_AI_CALL, ITEM_STORAGE_GB_MONTH,
               ITEM_MEMBER_SEAT)

CURRENCY = "CNY"


def _month_bounds(period: str):
    """period=YYYY-MM → (start, end) naive UTC datetimes。"""
    year, month = period.split("-")
    year, month = int(year), int(month)
    start = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, 999999)
    return start, end


def ensure_price_seed(db) -> None:
    """幂等：缺失的 item 补一条 0 占位价格（version=1，note 标注待运营配置）。"""
    existing = {row[0] for row in db.query(models.PriceConfig.item).distinct()}
    for item in PRICE_ITEMS:
        if item not in existing:
            db.add(models.PriceConfig(
                item=item, unit_price_cents=0, currency=CURRENCY, version=1,
                note="占位价格（0）：上线前必须由运营配置真实价格"))
    db.commit()


def current_prices(db) -> dict:
    """最新版本价格：{item: PriceConfig}。"""
    ensure_price_seed(db)
    out = {}
    for item in PRICE_ITEMS:
        row = (
            db.query(models.PriceConfig)
            .filter(models.PriceConfig.item == item)
            .order_by(models.PriceConfig.version.desc())
            .first()
        )
        out[item] = row
    return out


def set_price(db, item: str, unit_price_cents: int, currency: str = CURRENCY,
              note: str = "") -> models.PriceConfig:
    """新增一个价格版本（不改历史）。"""
    if item not in PRICE_ITEMS:
        raise ValueError(f"未知计费项: {item}；可选 {list(PRICE_ITEMS)}")
    latest = (
        db.query(func.max(models.PriceConfig.version))
        .filter(models.PriceConfig.item == item)
        .scalar() or 0
    )
    row = models.PriceConfig(item=item,
                             unit_price_cents=int(unit_price_cents),
                             currency=currency or CURRENCY,
                             version=latest + 1, note=note or "")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _org_ai_usage(db, org_id: str, start: datetime, end: datetime) -> dict:
    """周期内组织 AI 用量（经 ai_tasks 归属组织；同步无 task_id 的调用无法归属）。"""
    rows = (
        db.query(models.AICall.total_tokens, models.AICall.cost_cents,
                 models.AICall.model, models.AICall.task_type)
        .join(models.AITask, models.AICall.task_id == models.AITask.id)
        .filter(models.AITask.org_id == org_id,
                models.AICall.created_at >= start,
                models.AICall.created_at <= end)
        .all()
    )
    total_tokens = sum(r[0] or 0 for r in rows)
    recorded_cost = sum(r[1] or 0 for r in rows)
    by_model = {}
    by_task = {}
    for tokens, cost, model, task_type in rows:
        m = by_model.setdefault(model or "unknown",
                                {"calls": 0, "tokens": 0, "cost_cents": 0})
        m["calls"] += 1
        m["tokens"] += tokens or 0
        m["cost_cents"] += cost or 0
        t = by_task.setdefault(task_type or "unknown",
                               {"calls": 0, "tokens": 0})
        t["calls"] += 1
        t["tokens"] += tokens or 0
    return {
        "calls": len(rows),
        "total_tokens": total_tokens,
        "recorded_cost_cents": recorded_cost,  # 调用时按当时价格估算的
        "by_model": by_model,
        "by_task_type": by_task,
    }


def _org_storage_gb(db, org_id: str) -> float:
    total_bytes = (
        db.query(func.coalesce(func.sum(func.length(models.Work.data_json)), 0))
        .filter(models.Work.org_id == org_id,
                models.Work.deleted_at.is_(None))
        .scalar() or 0
    )
    return round(int(total_bytes) / (1024 ** 3), 4)


def _org_members(db, org_id: str) -> int:
    return (
        db.query(func.count(models.Membership.id))
        .filter(models.Membership.org_id == org_id)
        .scalar() or 0
    )


def build_statement(db, org_id: str, period: str) -> dict:
    """生成对账单（不落库）。period=YYYY-MM。"""
    try:
        start, end = _month_bounds(period)
    except (ValueError, AttributeError):
        raise ValueError("period 格式应为 YYYY-MM")
    prices = current_prices(db)
    ai = _org_ai_usage(db, org_id, start, end)
    storage_gb = _org_storage_gb(db, org_id)
    members = _org_members(db, org_id)

    def _p(item):
        row = prices[item]
        return row.unit_price_cents if row else 0

    lines = [
        {"item": ITEM_AI_TOKEN_1K, "quantity": round(ai["total_tokens"] / 1000, 2),
         "unit": "千token", "unit_price_cents": _p(ITEM_AI_TOKEN_1K),
         "amount_cents": int(ai["total_tokens"] / 1000 * _p(ITEM_AI_TOKEN_1K))},
        {"item": ITEM_AI_CALL, "quantity": ai["calls"],
         "unit": "次", "unit_price_cents": _p(ITEM_AI_CALL),
         "amount_cents": ai["calls"] * _p(ITEM_AI_CALL)},
        {"item": ITEM_STORAGE_GB_MONTH, "quantity": storage_gb,
         "unit": "GB·月", "unit_price_cents": _p(ITEM_STORAGE_GB_MONTH),
         "amount_cents": int(storage_gb * _p(ITEM_STORAGE_GB_MONTH))},
        {"item": ITEM_MEMBER_SEAT, "quantity": members,
         "unit": "席位", "unit_price_cents": _p(ITEM_MEMBER_SEAT),
         "amount_cents": members * _p(ITEM_MEMBER_SEAT)},
    ]
    amount = sum(li["amount_cents"] for li in lines)
    prices_unset = all((prices[i].unit_price_cents if prices[i] else 0) == 0
                       for i in PRICE_ITEMS)
    return {
        "org_id": org_id,
        "period": period,
        "currency": CURRENCY,
        "lines": lines,
        "amount_cents": amount,
        "prices_unset": prices_unset,
        "price_versions": {i: (prices[i].version if prices[i] else None)
                           for i in PRICE_ITEMS},
        "usage": {
            "ai": ai,
            "storage_gb": storage_gb,
            "members": members,
        },
        "note": ("当前价格为 0 占位，上线前须由运营配置真实价格；"
                 if prices_unset else ""),
    }


def issue_record(db, org_id: str, period: str, created_by: str = ""):
    """开出对账单记录（issued）。同一组织+周期只允许一张。"""
    stmt = build_statement(db, org_id, period)
    existing = (
        db.query(models.BillingRecord)
        .filter(models.BillingRecord.org_id == org_id,
                models.BillingRecord.period == period)
        .first()
    )
    if existing is not None:
        return None  # 调用方转 409
    rec = models.BillingRecord(
        org_id=org_id, period=period,
        usage_json=json.dumps(stmt, ensure_ascii=False),
        amount_cents=stmt["amount_cents"], currency=stmt["currency"],
        status="issued", issued_at=datetime.now(timezone.utc),
        created_by=created_by or "")
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec
