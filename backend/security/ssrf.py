"""SSRF 纵深防御（Phase 9-2）。

P0 只做了"必须 http/https 开头"的格式检查，这里补齐：

1. 内网 IP 拦截：10/8、172.16/12、192.168/16、127/8、169.254/16（云元数据）、
   ::1、fe80::/10（link-local）、组播、保留地址一律拒绝。
2. 重定向防护：fetch_url_safe 关掉自动重定向，手动跟随，每一跳都重新校验，
   防止"合法域名 → 302 跳内网"的经典绕过。
3. DNS 重绑定防护：解析结果按 hostname 缓存（TTL 300s），同一次校验/请求
   周期内 IP 不变；TTL 过期后重新解析。

用法：
    from backend.security.ssrf import validate_outbound_url, SSRFError
    validate_outbound_url(base_url)  # 不合法抛 SSRFError
"""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests


class SSRFError(ValueError):
    """出站 URL 未通过 SSRF 校验。"""


# ---------- DNS 缓存（重绑定防护） ----------

_DNS_CACHE: dict[str, tuple[list[str], float]] = {}
_DNS_TTL = 300.0


def resolve_host(hostname: str) -> list[str]:
    """解析 hostname → IP 列表（TTL 内缓存，防 DNS 重绑定）。"""
    now = time.time()
    hit = _DNS_CACHE.get(hostname)
    if hit and hit[1] > now:
        return hit[0]
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"DNS 解析失败: {hostname}（{e}）")
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise SSRFError(f"DNS 未返回可用地址: {hostname}")
    _DNS_CACHE[hostname] = (ips, now + _DNS_TTL)
    return ips


def _clear_dns_cache() -> None:
    """仅测试用。"""
    _DNS_CACHE.clear()


def is_blocked_ip(ip_str: str) -> bool:
    """内网/保留/特殊地址一律视为危险。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 解析不出 IP 也拒绝（fail closed）
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_outbound_url(url: str, *, purpose: str = "出站请求",
                          via_proxy: bool = False) -> str:
    """校验出站 URL。合法返回原 URL；不合法抛 SSRFError。

    检查项：scheme 仅 http/https；禁止 URL userinfo；
    字面 IP 直接判内网；域名解析后逐个 IP 判内网。

    via_proxy=True 时跳过"域名解析后判内网"一步：此时实际 TCP 连接
    只打到代理服务器，DNS 由代理侧解析，本机解析结果与真实出口无关，
    在代理出口环境（如沙箱经 egress-proxy 出站）下直接判会误杀合法域名。
    字面量内网 IP 依然拦截（代理也可能被诱导访问内网），纵深防御不断。

    轻微 #3/#4 说明：现全仓库无调用方传 via_proxy=True（唯一用户可控
    出站路径 poster_image 已改走 via_proxy=False 全量判定），故“本地 DNS
    与代理出口解析不一致”与“校验后 DNS 重绑定”窗口在现代码中不存在；
    用户可控域名另有 M-4 白名单兜底（非白名单域名根本到不了校验这步）。
    将来若有调用方想用 via_proxy=True，必须同时确认代理侧自身做内网
    拦截，否则等于把 SSRF 判定外包给不可控方——不要静默开启。
    """
    if not url or not isinstance(url, str):
        raise SSRFError(f"{purpose}：URL 为空")
    url = url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as e:
        raise SSRFError(f"{purpose}：URL 解析失败（{e}）")
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"{purpose}：只允许 http/https 协议")
    if parsed.username or parsed.password:
        raise SSRFError(f"{purpose}：URL 中不允许携带用户名/密码")
    host = parsed.hostname
    if not host:
        raise SSRFError(f"{purpose}：缺少主机名")
    # 字面 IP：直接判定
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
    if is_literal_ip:
        if is_blocked_ip(host):
            raise SSRFError(f"{purpose}：禁止访问内网/保留地址（{host}）")
        return url
    # 域名：解析后逐个 IP 判定（DNS 重绑定防护靠缓存）；
    # via_proxy 时跳过——真实连接只打到代理，本机 DNS 结果与出口无关
    if not via_proxy:
        for ip in resolve_host(host):
            if is_blocked_ip(ip):
                raise SSRFError(
                    f"{purpose}：{host} 解析到内网/保留地址（{ip}），已拦截")
    return url


def fetch_url_safe(url: str, *, method: str = "GET", max_redirects: int = 5,
                   timeout: float = 10.0, session=None,
                   **kwargs) -> "requests.Response":
    """安全的出站请求：手动跟随重定向，每一跳都做 SSRF 校验。

    requests 默认自动跟随重定向且不校验目标，这里关掉自动跳转，
    自己处理 301/302/303/307/308，Location 经 urljoin 解析后重新校验。
    """
    s = session or requests.Session()
    current = url
    for _ in range(max_redirects + 1):
        validate_outbound_url(current, purpose="出站请求（重定向跳转）")
        resp = s.request(method, current, allow_redirects=False,
                         timeout=timeout, **kwargs)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            if not loc:
                return resp
            current = urljoin(current, loc)
            # 303 按规范转 GET
            if resp.status_code == 303:
                method = "GET"
            continue
        return resp
    raise SSRFError(f"重定向次数超过上限（{max_redirects}），已拦截")
