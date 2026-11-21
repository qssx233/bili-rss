#!/usr/bin/env python3
"""
Bilibili UP主视频 RSS 订阅生成器（国内直连版 v2）

策略：
  1. 优先从国内可用的 RSSHub 镜像拉取 RSS XML，修正标题后重新生成干净的 feed
  2. 镜像全挂时回退到 B站 API 直连（需 WBI 签名 + cookie 绕过风控）
  3. 支持 --cookie 加载登录 cookie（用 bili_login.py 抓取）

用法:
    python bilibili_rss.py                          # 默认: unixchad (34569411)
    python bilibili_rss.py <UID>                    # 指定 UID
    python bilibili_rss.py 34569411 --name unixchad # 指定名称
    python bilibili_rss.py 34569411 --cookie bili_cookies.json  # 用登录 cookie
    python bilibili_rss.py 34569411 --serve 8080     # 启动本地 HTTP 服务器

依赖:
    pip install requests
"""

import sys
import os
import json
import time
import re
import argparse
import hashlib
import http.server
import socketserver
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

try:
    import requests
except ImportError:
    print("缺少 requests 库，正在安装...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# ============================================================
# 配置
# ============================================================

CST = timezone(timedelta(hours=8))

# RSSHub 镜像（按优先级排序）
RSSHUB_MIRRORS = [
    "https://rss.injahow.cn",        # 主力，免费免 key
    "https://rsshub.feeded.xyz",
    "https://rsshub.rssforever.com",
    "https://rss.shab.fun",
    "https://rsshub.feedlab.xyz",
    "https://rsshub.app",
    "https://rsshub.agrreader.com",  # 备选，需 access key
]

# B站 API
BILI_API = "https://api.bilibili.com/x/space/wbi/arc/search"
BILI_INFO_API = "https://api.bilibili.com/x/space/acc/info"
BILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
BILI_FINGER_API = "https://api.bilibili.com/x/frontend/finger/spi"
BILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILI_REPLY_API = "https://api.bilibili.com/x/v2/reply/main"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

WBI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


# ============================================================
# Cookie 加载
# ============================================================

def load_cookies(cookie_path: str | None) -> dict:
    """从 JSON 文件加载 cookie（selenium 保存的格式）"""
    if not cookie_path:
        return {}
    p = Path(cookie_path)
    if not p.exists():
        print(f"[警告] Cookie 文件不存在: {cookie_path}")
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        # selenium 格式: [{"name": "...", "value": "...", ...}, ...]
        if isinstance(raw, list):
            return {c["name"]: c["value"] for c in raw}
        # 简单 dict 格式
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        print(f"[警告] 加载 cookie 失败: {e}")
    return {}


# ============================================================
# 方案一：RSSHub 镜像拉取
# ============================================================

def fetch_from_rsshub(uid: int, up_name: str = "") -> tuple[str | None, str]:
    """从 RSSHub 镜像拉取 RSS XML，返回 (xml_content, source_url)"""
    route = f"/bilibili/user/video/{uid}"

    for base in RSSHUB_MIRRORS:
        url = base + route
        for attempt in range(3):
            try:
                if attempt > 0:
                    print(f"  重试 {attempt}/3 ...", end=" ")
                    time.sleep(5)
                else:
                    print(f"  尝试镜像: {base} ...", end=" ")
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                ct = resp.headers.get("content-type", "")
                is_xml = "xml" in ct or "<rss" in resp.text[:200] or "<feed" in resp.text[:200]

                if resp.status_code == 200 and is_xml:
                    print("✅")
                    return resp.text, url
                elif resp.status_code in (503, 502, 429):
                    print(f"❌ ({resp.status_code} 限流)")
                    continue
                else:
                    print(f"❌ ({resp.status_code})")
                    break
            except Exception as e:
                print(f"❌ ({str(e)[:40]})")
                break

    return None, ""


def _strip_cdata(html: str) -> str:
    """去掉 HTML 里残留的 CDATA 边界、嵌套标签等"""
    html = html.replace("]]>", "").replace("<![CDATA[", "")
    html = re.sub(r"<title>.*?</title>", "", html, flags=re.DOTALL)
    html = html.replace("</title>", "")
    html = re.sub(r"</?description>", "", html)
    html = re.sub(r"<link>.*?</link>", "", html, flags=re.DOTALL)
    html = re.sub(r"<guid[^>]*>.*?</guid>", "", html, flags=re.DOTALL)
    html = re.sub(r"<pubDate>.*?</pubDate>", "", html, flags=re.DOTALL)
    return html.strip()


def parse_rsshub_xml(xml_text: str, uid: int, up_name: str) -> list[dict]:
    """从 RSSHub 返回的 XML 中解析视频列表"""
    videos = []

    items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)

    for item in items:
        title_m = re.search(r"<!\[CDATA\[(.*?)\]\]>\s*</title>", item, re.DOTALL)
        if not title_m:
            title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        title = title_m.group(1).strip() if title_m else "未知标题"

        link_m = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        link = link_m.group(1).strip() if link_m else ""

        desc_m = re.search(r"<!\[CDATA\[(.*?)\]\]>\s*</description>", item, re.DOTALL)
        desc = desc_m.group(1).strip() if desc_m else ""
        desc = _strip_cdata(desc)

        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
        pub_date_str = pub_m.group(1).strip() if pub_m else ""
        try:
            from email.utils import parsedate_to_datetime
            pub_dt = parsedate_to_datetime(pub_date_str)
            pub_ts = int(pub_dt.timestamp())
        except Exception:
            pub_ts = 0

        bvid_m = re.search(r"BV\w+", link)
        bvid = bvid_m.group(0) if bvid_m else ""

        pic_m = re.search(r'<img src="(.*?)"', desc)
        pic = pic_m.group(1) if pic_m else ""

        videos.append({
            "title": title,
            "link": link,
            "description": desc,
            "bvid": bvid,
            "pic": pic,
            "created": pub_ts,
            "pub_date_str": pub_date_str,
        })

    return videos


# ============================================================
# 方案二：B站 API 直连（带 cookie 绕过风控）
# ============================================================

def _build_session(cookies: dict) -> requests.Session:
    """构建带 cookie 的 session"""
    session = requests.Session()
    # 获取 buvid3/buvid4
    try:
        fp = session.get(BILI_FINGER_API, headers=HEADERS, timeout=10)
        if fp.json().get("code") == 0:
            session.cookies.set("buvid3", fp.json()["data"]["b_3"], domain=".bilibili.com")
            session.cookies.set("buvid4", fp.json()["data"]["b_4"], domain=".bilibili.com")
    except Exception:
        pass
    # 加载用户 cookie
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".bilibili.com")
    # 访问首页激活
    session.get("https://www.bilibili.com/", headers=HEADERS, timeout=10)
    return session


def get_wbi_keys(session: requests.Session) -> tuple[str | None, str | None]:
    """获取 WBI 签名 key"""
    try:
        resp = session.get(BILI_NAV_API, headers=HEADERS, timeout=10)
        data = resp.json().get("data", {})
        img_url = data.get("wbi_img", {}).get("img_url", "")
        sub_url = data.get("wbi_img", {}).get("sub_url", "")
        if img_url and sub_url:
            img_key = img_url.rsplit("/", 1)[1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
            return img_key, sub_key
    except Exception as e:
        print(f"[警告] 获取 WBI keys 失败: {e}")
    return None, None


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """WBI 参数签名"""
    orig = img_key + sub_key
    mixin_key = "".join(orig[i] for i in WBI_MIXIN_TAB)[:32]
    params["ts"] = round(time.time())
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params


def get_up_info_api(uid: int, cookies: dict) -> dict:
    """从 B站 API 获取 UP 主信息"""
    session = _build_session(cookies)
    try:
        resp = session.get(BILI_INFO_API, params={"mid": uid}, headers=HEADERS, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            info = data["data"]
            return {
                "name": info.get("name", f"UID_{uid}"),
                "sign": info.get("sign", ""),
                "face": info.get("face", ""),
            }
    except Exception as e:
        print(f"[警告] 获取 UP 主信息失败: {e}")
    return {"name": f"UID_{uid}", "sign": "", "face": ""}


def get_video_detail(bvid: str, cookies: dict) -> dict:
    """获取单个视频详情（播放量、弹幕、封面大图等）"""
    session = _build_session(cookies)
    try:
        resp = session.get(BILI_VIEW_API, params={"bvid": bvid}, headers=HEADERS, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            d = data["data"]
            return {
                "aid": d.get("aid", ""),
                "bvid": d.get("bvid", ""),
                "pic": d.get("pic", ""),
                "title": d.get("title", ""),
                "desc": d.get("desc", ""),
                "duration": d.get("duration", 0),
                "pubdate": d.get("pubdate", 0),
                "view": d.get("stat", {}).get("view", 0),
                "danmaku": d.get("stat", {}).get("danmaku", 0),
                "reply": d.get("stat", {}).get("reply", 0),
                "favorite": d.get("stat", {}).get("favorite", 0),
                "coin": d.get("stat", {}).get("coin", 0),
                "share": d.get("stat", {}).get("share", 0),
                "like": d.get("stat", {}).get("like", 0),
                "owner_name": d.get("owner", {}).get("name", ""),
                "owner_mid": d.get("owner", {}).get("mid", 0),
            }
    except Exception:
        pass
    return {}


def _clean_comment_text(text: str) -> str:
    """清掉 ChillRoundM 字体不支持的字符（emoji 等），避免渲染成空白方块/空隙。
    保留 CJK、拉丁、数字、常见标点；emoji 直接丢弃（多为装饰，丢掉不影响语义）。"""
    out = []
    for ch in text:
        cp = ord(ch)
        # 丢弃 emoji 与装饰符号区
        if (0x1F000 <= cp <= 0x1FAFF or   # emoji
            0x2600 <= cp <= 0x27BF or     # 杂项符号/装饰
            0x2B00 <= cp <= 0x2BFF or     # 杂项符号箭头等
            cp in (0xFE0F, 0x200D)):      # 变体选择符/零宽连接
            continue
        out.append(ch)
    return "".join(out).strip()


def get_video_comments(aid, cookies: dict, limit: int = 3) -> list[dict]:
    """获取视频热门评论（需 cookie）。返回 [{user, text, like}, ...]"""
    if not aid:
        return []
    session = _build_session(cookies)
    try:
        resp = session.get(
            BILI_REPLY_API,
            params={"type": 1, "oid": aid, "mode": 3, "next": 0, "ps": limit},
            headers=HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            return []
        replies = (data.get("data") or {}).get("replies") or []
        result = []
        for r in replies[:limit]:
            text = _clean_comment_text((r.get("content") or {}).get("message", ""))
            if not text:
                continue
            result.append({
                "user": _clean_comment_text((r.get("member") or {}).get("uname", "网友")) or "网友",
                "text": text,
                "like": r.get("like", 0),
            })
        return result
    except Exception:
        return []


def get_videos_api(uid: int, count: int, cookies: dict) -> list[dict]:
    """从 B站 API 获取视频列表（带 cookie 绕过风控）"""
    session = _build_session(cookies)

    params = {
        "mid": uid,
        "pn": 1,
        "ps": min(count, 50),
        "tid": 0,
        "order": "pubdate",
    }
    img_key, sub_key = get_wbi_keys(session)
    if img_key and sub_key:
        params = enc_wbi(params, img_key, sub_key)

    try:
        resp = session.get(
            BILI_API,
            params=params,
            headers={**HEADERS, "Referer": f"https://space.bilibili.com/{uid}/upload/video"},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            vlist = data["data"]["list"]["vlist"]
            return vlist
        else:
            print(f"[API错误] code={data.get('code')}, msg={data.get('message', '')}")
    except Exception as e:
        print(f"[API错误] {e}")
    return []


# ============================================================
# RSS XML 生成（优化视频呈现）
# ============================================================

def ts_to_rfc2822(ts: int) -> str:
    if ts:
        dt = datetime.fromtimestamp(ts, tz=CST)
    else:
        dt = datetime.now(CST)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0800")


def _format_duration(seconds: int) -> str:
    """秒数转 mm:ss 或 hh:mm:ss"""
    if not seconds:
        return "?"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_count(n) -> str:
    """播放量等数字格式化：万"""
    if not n:
        return "0"
    if n >= 10000:
        return f"{n/10000:.1f}万"
    return str(n)


def build_item_html(v: dict, link: str, bili_link: str, space_url: str, embed: bool = True) -> str:
    """构造单条视频的富 HTML 正文（feed 与播放页共用）。

    link: 封面/标题点击目标（feed 里指向播放页，播放页里指向 B站）
    bili_link: B站原视频链接（「在 B站观看」用）

    样式全部内联：RSS 阅读器会剥离 <style>，只有内联样式才能保证观感一致。
    设计语言：暖白打底 + 半透明毛玻璃圆角块 + 单一字重（寒蝉全圆体），
    层级靠字号/颜色而不是伪粗体，避免阅读器渲染出两种字体。
    """
    title = v.get("title", "未知标题")
    desc_text = v.get("description", "")
    pic = v.get("pic", "")
    bvid = v.get("bvid", "")
    view_count = v.get("view", 0)
    play_count = v.get("play", 0)
    danmaku = v.get("danmaku", 0)
    reply = v.get("reply", 0)
    like = v.get("like", 0)
    coin = v.get("coin", 0)
    favorite = v.get("favorite", 0)
    duration = v.get("duration", 0)
    length = v.get("length", "")

    if pic:
        pic_url = pic if pic.startswith("http") else f"https:{pic}"
    else:
        pic_url = ""

    # —— 设计变量（内联，避免阅读器丢样式） ——
    TEXT = "hsl(28,32%,25%)"
    MID = "hsl(28,18%,44%)"
    SOFT = "hsl(28,13%,62%)"
    NICK = "hsl(4,62%,42%)"
    GLASS = "rgba(255,255,255,0.56)"
    HAIR = "rgba(152,120,103,0.15)"
    SHADOW = "0 6px 18px -12px rgba(70,45,35,0.35)"

    parts = []

    # 1. 封面大图（第一位，点它进播放页）
    if pic_url:
        parts.append(
            f'<div style="margin:0 0 14px;line-height:0;font-size:0;">'
            f'<a href="{link}"><img src="{pic_url}" alt="{escape(title)}" referrerpolicy="no-referrer" '
            f'style="display:block;width:100%;border-radius:18px;'
            f'box-shadow:0 14px 32px -20px rgba(70,45,35,0.5);" /></a></div>'
        )

    # 2. 操作按钮（粉色主按钮 + 玻璃副按钮）
    buttons = []
    if pic_url:
        buttons.append(
            f'<a href="{link}" style="display:inline-block;padding:9px 22px;'
            f'background:linear-gradient(135deg,#fc86a7,#e95f8a);color:#ffffff;'
            f'text-decoration:none;border-radius:999px;font-size:13px;'
            f'box-shadow:0 8px 18px -10px rgba(233,95,138,0.9);">播放视频</a>'
        )
    buttons.append(
        f'<a href="{bili_link}" style="display:inline-block;padding:9px 20px;'
        f'background:{GLASS};border:1px solid {HAIR};color:{MID};'
        f'text-decoration:none;border-radius:999px;font-size:13px;">在 B站打开</a>'
    )
    parts.append(
        '<div style="margin:0 0 16px;display:flex;flex-wrap:wrap;gap:8px;">'
        + "".join(buttons) + '</div>'
    )

    # 3. 官方 PC 播放器（真实网页里可播；部分阅读器会过滤 iframe）
    if embed and bvid:
        player_url = (
            f"https://player.bilibili.com/player.html?bvid={bvid}&page=1"
            f"&high_quality=1&danmaku=0&autoplay=0"
        )
        parts.append(
            f'<div style="position:relative;width:100%;padding-top:56.25%;margin:0 0 16px;'
            f'border-radius:18px;overflow:hidden;background:#0d0d0f;'
            f'box-shadow:0 14px 32px -20px rgba(70,45,35,0.5);">'
            f'<iframe src="{player_url}" scrolling="no" frameborder="0" '
            f'allowfullscreen="true" '
            f'style="position:absolute;inset:0;width:100%;height:100%;border:0;display:block;">'
            f'</iframe></div>'
        )

    # 4. 标题
    parts.append(
        f'<h3 style="margin:0 0 12px;font-size:18px;line-height:1.55;font-weight:400;'
        f'letter-spacing:0.01em;">'
        f'<a href="{link}" style="text-decoration:none;color:{TEXT};">'
        f'{escape(title)}</a></h3>'
    )

    # 5. 统计（纯文字胶囊，避免 emoji 在字体下显示空白方块）
    stats = []
    if view_count or play_count:
        stats.append(f'播放 {_format_count(view_count or play_count)}')
    if danmaku:
        stats.append(f'弹幕 {_format_count(danmaku)}')
    if reply:
        stats.append(f'评论 {_format_count(reply)}')
    if like:
        stats.append(f'点赞 {_format_count(like)}')
    if coin:
        stats.append(f'投币 {_format_count(coin)}')
    if favorite:
        stats.append(f'收藏 {_format_count(favorite)}')
    if duration:
        stats.append(f'时长 {_format_duration(duration)}')
    elif length:
        stats.append(f'时长 {length}')
    if stats:
        chips = "".join(
            f'<span style="display:inline-block;padding:4px 11px;border-radius:999px;'
            f'background:{GLASS};border:1px solid {HAIR};font-size:11.5px;'
            f'color:{MID};letter-spacing:0.01em;">{s}</span>'
            for s in stats
        )
        parts.append(
            f'<div style="margin:0 0 14px;display:flex;flex-wrap:wrap;gap:6px;">{chips}</div>'
        )

    # 6. 完整简介
    if desc_text:
        import html as _html
        desc_text = _html.unescape(desc_text)
        clean_desc = re.sub(r'<iframe[^>]*>.*?</iframe>', '', desc_text, flags=re.DOTALL)
        clean_desc = re.sub(r'<br\s*/?>', '\n', clean_desc)
        clean_desc = re.sub(r'<[^>]+>', '', clean_desc).strip()
        if clean_desc:
            parts.append(
                f'<div style="margin:0 0 4px;font-size:13.5px;color:hsl(28,25%,30%);'
                f'line-height:1.9;">'
                f'{escape(clean_desc).replace(chr(10), "<br>")}</div>'
            )

    # 7. 热门评论（每条一个圆角毛玻璃小块）
    comments = v.get("comments") or []
    if comments:
        cm = ['<div style="margin:18px 0 0;">',
              f'<div style="margin:0 0 10px;font-size:11.5px;letter-spacing:0.1em;'
              f'color:{SOFT};">热门评论</div>']
        for c in comments:
            user = escape(str(c.get("user", "网友")))
            text = escape(str(c.get("text", ""))).replace("\n", "<br>")
            like_num = c.get("like", 0)
            like_html = (
                f'<div style="margin:6px 0 0;text-align:right;font-size:11px;'
                f'color:{SOFT};">{like_num} 赞</div>'
            ) if like_num else ""
            cm.append(
                f'<div style="margin:0 0 9px;padding:12px 15px;border-radius:18px;'
                f'background:{GLASS};border:1px solid {HAIR};'
                f'-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);'
                f'box-shadow:{SHADOW};">'
                f'<div style="margin:0 0 4px;font-size:12px;color:{NICK};">{user}</div>'
                f'<div style="font-size:12.5px;color:hsl(28,25%,30%);line-height:1.72;">'
                f'{text}</div>{like_html}</div>'
            )
        cm.append('</div>')
        parts.append("".join(cm))

    # 8. 兜底链接
    parts.append(
        f'<div style="margin:18px 0 0;display:flex;flex-wrap:wrap;gap:8px;font-size:12px;">'
        f'<a href="{bili_link}" style="padding:6px 14px;border-radius:999px;'
        f'background:rgba(255,255,255,0.5);border:1px solid {HAIR};'
        f'color:hsl(340,62%,52%);text-decoration:none;">在 B站观看原视频</a>'
        f'<a href="{space_url}" style="padding:6px 14px;border-radius:999px;'
        f'background:rgba(255,255,255,0.5);border:1px solid {HAIR};'
        f'color:{SOFT};text-decoration:none;">UP主主页</a></div>'
    )
    return "\n".join(parts)


def generate_rss_xml(uid: int, up_name: str, videos: list[dict], up_sign: str = "",
                     embed: bool = True, post_base_url: str = "") -> str:
    """生成优化的 RSS 2.0 XML"""
    now = datetime.now(CST).strftime("%a, %d %b %Y %H:%M:%S +0800")
    space_url = f"https://space.bilibili.com/{uid}"
    upload_url = f"https://space.bilibili.com/{uid}/upload/video"

    # Channel
    channel_desc = f"B站UP主 {up_name} (UID:{uid}) 的视频投稿 RSS 订阅"
    if up_sign:
        channel_desc += f" — {up_sign}"

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/">',
        '  <channel>',
        f'    <title><![CDATA[{up_name} 的 bilibili 投稿]]></title>',
        f'    <link>{space_url}</link>',
        f'    <atom:link href="{space_url}" rel="self" type="application/rss+xml" />',
        f'    <description><![CDATA[{channel_desc}]]></description>',
        '    <generator>bilibili_rss.py v2</generator>',
        '    <language>zh-CN</language>',
        f'    <lastBuildDate>{now}</lastBuildDate>',
        f'    <ttl>60</ttl>',
    ]

    for v in videos:
        title = v.get("title", "未知标题")
        bili_link = v.get("link") or v.get("bvid", "")
        if bili_link and not bili_link.startswith("http"):
            bili_link = f"https://www.bilibili.com/video/{bili_link}"
        bvid = v.get("bvid", "")
        pub_ts = v.get("created", 0) or v.get("pub_date_str", "")
        pub_date = ts_to_rfc2822(pub_ts) if isinstance(pub_ts, int) and pub_ts else (pub_ts or now)

        # feed 条目的 link 指向托管播放页（AgrReader 内嵌网页打开时是干净页面，不是 B站乱页面）
        link = f"{post_base_url}posts/{bvid}.html" if post_base_url and bvid else bili_link

        pic = v.get("pic", "")
        pic_url = (pic if pic.startswith("http") else f"https:{pic}") if pic else ""

        content = build_item_html(v, link, bili_link, space_url, embed=embed)

        # guid 用 B站原链接保持稳定，避免阅读器把旧条目标成新的
        guid = f"https://www.bilibili.com/video/{bvid}" if bvid else bili_link

        media_tags = ""
        if pic_url:
            media_tags = f'\n      <media:thumbnail url="{pic_url}" />'

        xml_parts.extend([
            '    <item>',
            f'      <title><![CDATA[{title}]]></title>',
            f'      <link>{link}</link>',
            f'      <guid isPermaLink="false">{guid}</guid>',
            f'      <pubDate>{pub_date}</pubDate>',
            f'      <description><![CDATA[{content}]]></description>',
        ])
        if media_tags:
            xml_parts.append(media_tags)
        xml_parts.append('    </item>')

    xml_parts.extend(['  </channel>', '</rss>'])
    return "\n".join(xml_parts)


# ============================================================
# 本地 HTTP 服务器
# ============================================================

class RSSFeedHandler(http.server.BaseHTTPRequestHandler):
    feed_xml = ""
    feed_content_type = "application/rss+xml; charset=utf-8"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", self.feed_content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(self.feed_xml.encode("utf-8"))

    def log_message(self, format, *args):
        print(f"  [{self.log_date_time_string()}] {format % args}")


# ============================================================
# 主逻辑
# ============================================================

def ts_to_str(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d %H:%M:%S")


def generate_post_page(v: dict, up_name: str, space_url: str, bili_link: str) -> str:
    """生成单条视频的独立播放页 HTML（干净、可在 AgrReader 内嵌网页里播放）

    设计语言与首页一致：暖白底 + 半透明毛玻璃圆角 + 单一字重寒蝉全圆体，
    背景图（loliapi 随机图）做高斯模糊后再压一层白纱，保证"通透"且不抢正文。
    """
    title = v.get("title", "未知标题")
    body = build_item_html(v, bili_link, bili_link, space_url, embed=True)
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)} - {escape(up_name)}</title>
<link rel="icon" type="image/svg+xml" href="../assets/bili.svg">
<meta name="theme-color" content="#faf5f0">
<meta name="referrer" content="no-referrer">
<style>
@font-face {{
  font-family:'ChillRound';
  src:url('../assets/ChillRoundF.woff2') format('woff2');
  font-weight:400; font-style:normal; font-display:swap;
}}
:root {{
  --text:hsl(28,32%,25%); --text-mid:hsl(28,18%,44%); --text-soft:hsl(28,13%,62%);
  --accent-deep:hsl(31,30%,40%); --pink-deep:hsl(340,62%,52%);
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; padding:0; min-height:100%; overflow-x:hidden; }}
body {{
  font-family:'ChillRound',-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",
    "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif;
  font-synthesis:none; -webkit-font-smoothing:antialiased;
  color:var(--text); line-height:1.8; background-color:hsl(35,36%,97%);
  background-image:
    radial-gradient(900px 420px at 10% -10%, hsla(342,85%,90%,0.5), transparent 62%),
    radial-gradient(760px 400px at 92% 2%, hsla(197,70%,91%,0.45), transparent 60%),
    radial-gradient(700px 520px at 50% 104%, hsla(40,80%,91%,0.42), transparent 65%);
  background-attachment:fixed;
}}
.bg {{
  position:fixed; inset:-46px; z-index:0; background-size:cover; background-position:center;
  background-image:url('../assets/portrait.jpg');
  filter:blur(16px) saturate(1.22) brightness(1.03); opacity:0.92;
}}
.veil {{
  position:fixed; inset:0; z-index:0;
  background:linear-gradient(180deg, rgba(255,255,255,0.22), rgba(255,255,255,0.34) 50%,
    rgba(255,255,255,0.26));
}}
.page {{ position:relative; z-index:1; max-width:860px; margin:0 auto; padding:22px 16px 44px; }}
.card {{
  background:rgba(255,255,255,0.46); border:1px solid rgba(255,255,255,0.62);
  border-radius:26px; padding:20px;
  box-shadow:0 26px 62px -36px rgba(70,45,35,0.42), 0 2px 6px rgba(70,45,35,0.04);
  -webkit-backdrop-filter:blur(30px) saturate(1.2); backdrop-filter:blur(30px) saturate(1.2);
  animation:rise 0.5s cubic-bezier(0.22,1,0.36,1) both;
}}
@keyframes rise {{ from {{ opacity:0; transform:translateY(12px); }} to {{ opacity:1; transform:none; }} }}
@media (prefers-reduced-motion:reduce) {{ .card {{ animation:none; }} }}
.topbar {{
  display:flex; align-items:center; gap:10px; margin:0 0 16px;
  font-size:12px; color:var(--text-soft); letter-spacing:0.02em;
}}
.topbar .brand {{
  display:inline-flex; align-items:center; gap:9px; flex:0 0 auto;
  text-decoration:none; color:var(--text-soft);
}}
.topbar .brand img {{ width:24px; height:24px; border-radius:8px; display:block; }}
.me {{
  margin-left:auto; display:flex; align-items:center; gap:9px; overflow:hidden;
  min-width:0; max-width:72%; padding:4px 5px 4px 13px; border-radius:999px;
  background:rgba(255,255,255,0.66); border:1px solid rgba(152,120,103,0.13);
  box-shadow:0 6px 16px -12px rgba(70,45,35,0.4);
  -webkit-backdrop-filter:blur(12px); backdrop-filter:blur(12px);
}}
.me .quote {{
  font-size:11.5px; color:var(--text-mid); white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; line-height:1.5;
  flex:1 1 auto; min-width:0;
}}
.me img {{
  width:30px; height:30px; border-radius:999px; object-fit:cover;
  display:block; flex:0 0 auto; border:1px solid rgba(255,255,255,0.9);
}}
a {{ color:var(--pink-deep); text-decoration:none; -webkit-tap-highlight-color:transparent; }}
a:active {{ opacity:0.7; }}
h3 {{ font-weight:400; }}
img {{ max-width:100%; height:auto; }}
@media (max-width:600px) {{
  .topbar {{ flex-wrap:wrap; row-gap:8px; }}
  .me {{ max-width:100%; margin-left:0; }}
  .bg {{ filter:blur(14px) saturate(1.12); opacity:0.9; }}
  .page {{ padding:12px 10px 30px; }}
  .card {{ padding:14px; border-radius:20px; }}
}}
</style>
</head>
<body>
<div class="bg" id="bg"></div><div class="veil"></div>
<div class="page"><div class="card">
<div class="topbar">
  <a class="brand" href="../index.html" title="返回订阅首页">
    <img src="../assets/bili.svg" alt="bilibili">
    <span>{escape(up_name)} · bilibili</span>
  </a>
  <div class="me">
    <span class="quote" id="hk"></span>
    <img src="../assets/portrait.jpg" alt="头像">
  </div>
</div>
{body}
</div></div>
<script>
(function(){{
  var bg=document.getElementById('bg');
  var urls=['https://www.dmoe.cc/random.php?t='+Date.now(),
            'https://t.alcy.cc/moe?t='+Date.now()];
  var i=0;
  function tryNext(){{
    if(i>=urls.length) return;
    var img=new Image(), u=urls[i++];
    img.onload=function(){{ bg.style.backgroundImage="url('"+u+"')"; }};
    img.onerror=tryNext;
    img.src=u;
  }}
  tryNext();
}})();
(function(){{
  var el=document.getElementById('hk');
  if(!el) return;
  el.textContent='\u300c\u4eca\u5929\u4e5f\u8981\u597d\u597d\u5199\u4ee3\u7801\u5440\u300d';
  fetch('https://v1.hitokoto.cn/?encode=json&c=e&c=l')
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      if(d && d.hitokoto){{
        el.textContent='\u300c'+d.hitokoto+'\u300d';
        el.title=d.hitokoto+(d.from?' \u2014\u2014 '+d.from:'');
      }}
    }})
    .catch(function(){{}});
}})();
</script>
</body>
</html>'''


def run(uid: int, up_name: str, output_path: str, count: int, cookies: dict,
        embed: bool = True, post_base_url: str = "", posts_dir: str = "") -> str | None:
    """执行获取 + 生成，返回 XML 内容"""
    space_url = f"https://space.bilibili.com/{uid}"
    has_cookie = bool(cookies)
    cookie_status = "有 cookie（可绕过风控）" if has_cookie else "无 cookie（依赖 RSSHub 镜像）"
    print(f"{'='*60}")
    print(f"B站 UP主 RSS 生成器 v2 (国内直连版)")
    print(f"UID: {uid}  |  名称: {up_name or '(自动获取)'}  |  数量: {count}")
    print(f"Cookie: {cookie_status}")
    print(f"{'='*60}")

    # Step 1: 从 RSSHub 镜像获取
    print(f"\n[1/3] 从 RSSHub 镜像拉取...")
    xml_text, source_url = fetch_from_rsshub(uid, up_name)

    if xml_text:
        print(f"\n[2/3] 解析 RSSHub XML (来源: {source_url})...")
        videos = parse_rsshub_xml(xml_text, uid, up_name)

        if videos:
            if not up_name or up_name == "UID_{}".format(uid):
                chan_title_m = re.search(r"<channel>.*?<title><!\[CDATA\[(.*?)\]\]></title>", xml_text, re.DOTALL)
                if chan_title_m:
                    chan_title = chan_title_m.group(1)
                    name_m = re.match(r"(.+?)\s*的\s*bilibili\s*空间", chan_title)
                    if name_m and name_m.group(1).strip() != "undefined":
                        up_name = name_m.group(1).strip()

            if not up_name or up_name == "UID_{}".format(uid):
                up_name = f"UID_{uid}"
                print(f"  [提示] UP主名称未知，使用默认值。可用 --name 指定。")

            videos = videos[:count]
            print(f"  获取到 {len(videos)} 条视频")

        else:
            print("  [警告] XML 解析无结果，回退到 API 方案")
            xml_text = None
    else:
        print("\n  所有镜像均不可用，回退到 B站 API 方案...")

    # Step 2: 如果镜像失败，用 API
    if not xml_text or not videos:
        print(f"\n[2/3] 从 B站 API 获取...")
        if not up_name or up_name == f"UID_{uid}":
            info = get_up_info_api(uid, cookies)
            up_name = info.get("name", f"UID_{uid}")
            print(f"  UP主: {up_name}")

        raw_videos = get_videos_api(uid, count, cookies)
        if raw_videos:
            videos = []
            for v in raw_videos:
                bvid = v.get("bvid", "")
                videos.append({
                    "title": v.get("title", ""),
                    "link": f"https://www.bilibili.com/video/{bvid}",
                    "bvid": bvid,
                    "aid": v.get("aid", ""),
                    "description": v.get("description", ""),
                    "pic": v.get("pic", ""),
                    "created": v.get("created", 0),
                    "play": v.get("play", 0),
                    "length": v.get("length", ""),
                })
            print(f"  获取到 {len(videos)} 条视频 (API)")
        else:
            print("  [错误] API 也失败了！")
            if not has_cookie:
                print("  建议: 用 bili_login.py 抓取 cookie 后重试:")
                print(f'    python bili_login.py')
                print(f'    python bilibili_rss.py {uid} --name "{up_name}" --cookie bili_cookies.json')
            print("  或使用 RSSHub 镜像地址直接订阅:")
            for base in RSSHUB_MIRRORS[:3]:
                print(f"    {base}/bilibili/user/video/{uid}")
            return None

    # 统一增强：用 cookie 补充详情 + 热门评论（两条路径都经过这里）
    if has_cookie:
        print(f"\n  [增强] 用 cookie 补充视频详情和热门评论...")
        enriched = 0
        commented = 0
        for v in videos:
            bvid = v.get("bvid", "")
            if not bvid:
                continue
            # 详情：补 aid / 播放量等统计
            detail = get_video_detail(bvid, cookies)
            if detail:
                if detail.get("aid"):
                    v["aid"] = detail["aid"]
                # 用 API 的完整简介覆盖 RSSHub 的截断简介
                if detail.get("desc"):
                    v["description"] = detail["desc"]
                for key in ("view", "danmaku", "reply", "like", "coin", "favorite", "duration"):
                    if detail.get(key):
                        v[key] = detail[key]
                enriched += 1
            time.sleep(0.25)
            # 热门评论（用 aid）
            cs = get_video_comments(v.get("aid", ""), cookies, limit=10)
            if cs:
                v["comments"] = cs
                commented += 1
            time.sleep(0.25)
        print(f"  [增强] 补充了 {enriched} 条详情、{commented} 条视频的评论")

    # 显示最新视频
    print(f"\n  最新视频:")
    for i, v in enumerate(videos[:5]):
        ts = v.get("created", 0)
        print(f"    {i+1}. [{ts_to_str(ts)}] {v.get('title', '?')}")

    # Step 3: 生成 RSS XML + 播放页
    print(f"\n[3/3] 生成 RSS XML 和播放页...")
    xml_content = generate_rss_xml(uid, up_name, videos, embed=embed,
                                   post_base_url=post_base_url)

    output_path = str(Path(output_path).resolve())
    Path(output_path).write_text(xml_content, encoding="utf-8")

    # 生成每条视频的独立播放页
    if posts_dir and post_base_url:
        posts_path = Path(posts_dir)
        posts_path.mkdir(parents=True, exist_ok=True)
        made = 0
        for v in videos:
            bvid = v.get("bvid", "")
            if not bvid:
                continue
            bili_link = v.get("link") or f"https://www.bilibili.com/video/{bvid}"
            page = generate_post_page(v, up_name, space_url, bili_link)
            (posts_path / f"{bvid}.html").write_text(page, encoding="utf-8")
            made += 1
        print(f"   播放页: {made} 个 → {posts_path}")

    print(f"\n✅ RSS feed 已生成: {output_path}")
    print(f"   UP主: {up_name}  |  共 {len(videos)} 条视频")

    return xml_content


def main():
    parser = argparse.ArgumentParser(
        description="B站 UP主视频 RSS 订阅生成器 v2 (国内直连版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                                      # 默认生成 unixchad (34569411) 的 RSS
  %(prog)s 34569411 --name unixchad             # 指定 UID 和名称
  %(prog)s 34569411 --cookie bili_cookies.json  # 用登录 cookie 绕过风控
  %(prog)s 34569411 -o ./feed.xml               # 指定输出路径
  %(prog)s 34569411 --serve 8080                # 生成并启动本地 HTTP 服务器

抓取 cookie:
  python bili_login.py                          # 打开浏览器登录 B站，保存 cookie
  python bili_login.py -c my_cookies.json       # 指定 cookie 保存路径
        """,
    )
    parser.add_argument("uid", nargs="?", type=int, default=34569411,
                        help="UP主 UID (默认: 34569411 = unixchad)")
    parser.add_argument("--name", type=str, default="",
                        help="UP主名称 (留空自动获取)")
    parser.add_argument("--output", "-o", type=str, default="",
                        help="输出文件路径 (默认: feed_<uid>.xml)")
    parser.add_argument("--count", "-n", type=int, default=30,
                        help="获取视频数量 (默认: 30)")
    parser.add_argument("--cookie", "-c", type=str, default="",
                        help="B站 cookie JSON 文件路径 (用 bili_login.py 抓取)")
    parser.add_argument("--serve", type=int, default=0, metavar="PORT",
                        help="生成后启动本地 HTTP 服务器 (指定端口号)")
    parser.add_argument("--embed", dest="embed", action="store_true", default=True,
                        help="在条目内嵌 B站官方 PC 播放器，可在阅读器里直接播放 (默认开启)")
    parser.add_argument("--no-embed", dest="embed", action="store_false",
                        help="不内嵌播放器，只显示封面+链接（阅读器屏蔽 iframe 时用）")
    parser.add_argument("--base-url", type=str, default="",
                        help="站点根地址（如 https://qssx233.github.io/bili-rss/），用于生成播放页链接")
    parser.add_argument("--posts-dir", type=str, default="",
                        help="播放页输出目录（如 L:/desktopfiles/RSS/github-pages/posts）")

    args = parser.parse_args()
    uid = args.uid

    # 默认名称
    if not args.name and uid == 34569411:
        up_name = "unixchad"
    else:
        up_name = args.name

    # 输出路径
    output_path = args.output or f"feed_{uid}.xml"

    # 加载 cookie
    cookies = load_cookies(args.cookie) if args.cookie else {}

    # 执行
    xml_content = run(uid, up_name, output_path, args.count, cookies, embed=args.embed,
                      post_base_url=args.base_url, posts_dir=args.posts_dir)
    if not xml_content:
        sys.exit(1)

    # 可选：启动 HTTP 服务器
    if args.serve:
        port = args.serve
        print(f"\n{'='*60}")
        print(f"启动本地 RSS HTTP 服务器")
        print(f"订阅地址: http://localhost:{port}")
        print(f"按 Ctrl+C 停止")
        print(f"{'='*60}\n")

        RSSFeedHandler.feed_xml = xml_content
        with socketserver.TCPServer(("127.0.0.1", port), RSSFeedHandler) as httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n服务器已停止")

    print(f"\n{'='*60}")
    print(f"完成！")
    print(f"RSS 文件: {output_path}")
    print(f"使用方法:")
    print(f"  1. 导入到 RSS 阅读器 (Feedly/Inoreader/FreshRSS 等)")
    print(f"  2. 或用 --serve 启动本地服务器供阅读器订阅")
    print(f"  3. 或上传到 GitHub Pages / 个人网站供远程访问")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
