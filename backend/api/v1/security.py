"""安全辅助端点（Phase 9-1）。

- POST /api/v1/security/csp-report：接收浏览器 CSP 违规报告
  （Content-Security-Policy-Report-Only 的 report-uri）。
  只记摘要（blocked-uri / violated-directive），不存完整报告正文。
"""
import logging

from fastapi import APIRouter, Request

router = APIRouter(prefix="/security", tags=["v1-security"])

log = logging.getLogger("security.csp")


@router.post("/csp-report")
async def csp_report(request: Request):
    """CSP 违规上报。限流中间件已豁免此路径；失败不影响前端。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    report = body.get("csp-report", body) if isinstance(body, dict) else {}
    if isinstance(report, dict):
        log.warning(
            "CSP 违规: blocked=%s directive=%s uri=%s",
            report.get("blocked-uri"), report.get("violated-directive"),
            report.get("document-uri"))
    return {"ok": True}
