"""V3.1 展示页静态断言：SEO meta、标题层级、视频降级、能力清单与真实功能一致。

只读 frontend/landing.html 做静态检查，不启动浏览器。
"""
import pathlib
import re

LANDING = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "landing.html"
HTML = LANDING.read_text(encoding="utf-8")


def test_landing_file_exists():
    assert LANDING.exists()


def test_basic_seo_meta():
    assert "<title>" in HTML and "</title>" in HTML
    assert 'name="description"' in HTML
    assert 'rel="canonical"' in HTML
    assert 'rel="icon"' in HTML


def test_open_graph_tags():
    for prop in ("og:type", "og:site_name", "og:locale", "og:title",
                 "og:description", "og:url", "og:image"):
        assert f'property="{prop}"' in HTML, f"missing {prop}"


def test_twitter_card_tags():
    for name in ("twitter:card", "twitter:title", "twitter:description", "twitter:image"):
        assert f'name="{name}"' in HTML, f"missing {name}"


def test_og_urls_are_absolute():
    m = re.search(r'property="og:url" content="([^"]+)"', HTML)
    assert m and m.group(1).startswith("https://"), "og:url 必须为绝对地址"
    m = re.search(r'property="og:image" content="([^"]+)"', HTML)
    assert m and m.group(1).startswith("https://"), "og:image 必须为绝对地址"


def test_heading_hierarchy_no_skip():
    """标题层级不得跳级（如 h2 直接到 h4）。"""
    levels = [int(x) for x in re.findall(r"<h([1-6])[\s>]", HTML)]
    assert levels, "页面没有任何标题"
    assert levels[0] == 1, "首个标题应为 h1"
    for prev, cur in zip(levels, levels[1:]):
        assert cur - prev <= 1, f"标题跳级：h{prev} -> h{cur}"


def test_hero_video_preload_and_poster():
    """桌面端保持 preload=auto；移动端减负由 landing.js 按设备切换（见下）。
    无论哪端，video 必须有 poster 静态封面降级。"""
    m = re.search(r'<video[^>]*id="heroVideo"[^>]*>', HTML)
    assert m, "heroVideo 不存在"
    tag = m.group(0)
    assert 'preload="auto"' in tag, "桌面端默认应为 preload=auto"
    assert "poster=" in tag, "hero 视频缺少 poster 降级"


def test_mobile_video_lighten_in_js():
    """移动端（窄屏/触屏）才减负：不自动播放、preload=metadata、优先静态封面。"""
    js = (pathlib.Path(__file__).resolve().parent.parent
          / "frontend" / "js" / "landing.js").read_text(encoding="utf-8")
    assert "(max-width: 768px)" in js
    assert "(pointer: coarse)" in js
    assert 'removeAttribute("autoplay")' in js
    assert 'preload = "metadata"' in js


def test_hero_video_has_error_fallback_js():
    """landing.js 必须处理视频加载失败/减少动效时的回退。"""
    js = (pathlib.Path(__file__).resolve().parent.parent
          / "frontend" / "js" / "landing.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js
    assert "heroVideo" in js


def test_reduced_motion_css():
    assert "prefers-reduced-motion" in HTML, "缺少减少动效的 CSS 媒体查询"


def test_brand_positioning_kept():
    assert "把山东好物，" in HTML and "值得记住的故事" in HTML


def test_capability_cards_match_real_modules():
    """六卡片必须覆盖 V3.x 真实模块，不得停留在 V2.0 口径。"""
    for kw in ("产品知识中心", "内容策划", "拉呱版", "多平台", "发布中心",
               "效果复盘", "店播", "素材库"):
        assert kw in HTML, f"能力卡片缺失真实模块关键词：{kw}"


def test_capability_checklist_comment():
    """能力清单注释存在，防后续迭代文案滞后。"""
    assert "产品能力清单" in HTML and "更新功能时请同步改这里" in HTML


def test_cases_marked_as_concept():
    """演示案例必须明确示例性质，不得伪装成真实客户案例。"""
    assert "概念" in HTML
    assert "AI 生成" in HTML


def test_no_fabricated_marketing_numbers():
    """不得出现编造的客户数/销量/获奖数字。"""
    for bad in ("10万+", "100万+", "1000+", "TOP1", "第一", "获奖"):
        assert bad not in HTML, f"疑似编造的宣传表述：{bad}"


def test_cta_links_to_login():
    assert 'href="/login"' in HTML


def test_images_have_alt():
    """内容图片必须有 alt（装饰性图片 alt 为空且 aria-hidden 可接受）。"""
    for m in re.finditer(r"<img[^>]*>", HTML):
        tag = m.group(0)
        assert "alt=" in tag, f"图片缺少 alt：{tag[:80]}"
