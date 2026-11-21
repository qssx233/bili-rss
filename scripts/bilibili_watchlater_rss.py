#!/usr/bin/env python3
"""B站「稍后再看」RSS 生成器（需要登录 cookie）

数据源：https://api.bilibili.com/x/v2/history/toview（对应网页版
https://www.bilibili.com/?page=WatchLater）
该接口一次返回全部「稍后再看」条目，自带封面 / 简介 / 时长 / 播放弹幕点赞投币收藏等统计，
不需要逐条再调 x/web-interface/view，所以比投稿 feed 快很多。

用法:
  python bilibili_watchlater_rss.py \
      --cookie bili_cookies.json \
      --output "L:/desktopfiles/RSS/feed_bilibili_watchlater.xml" \
      --count 50 \
      --base-url "https://qssx233.github.io/bili-rss/" \
      --posts-dir "L:/desktopfiles/RSS/github-pages/posts"

输出：标准 RSS 2.0，条目 link 指向自建播放页（干净、可内嵌播放），
样式复用 bilibili_rss.build_item_html / generate_post_page，保证和投稿 feed 完全一致。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from bilibili_rss import (
    CST,
    HEADERS,
    _build_session,
    _format_count,
    _format_duration,
    build_item_html,
    generate_post_page,
    get_video_comments,
    load_cookies,
    ts_to_rfc2822,
)

TOVIEW_API = "https://api.bilibili.com/x/v2/history/toview"
WATCHLATER_URL = "https://www.bilibili.com/?page=WatchLater"


def _https(url: str) -> str:
    """B站图片常给 http://，https 页面里会被混合内容策略拦，统一换 https"""
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def fetch_watchlater(cookies: dict) -> list[dict]:
    """拉取稍后再看列表（一次返回全部）"""
    if not cookies:
        print("[错误] 稍后再看接口必须登录，请用 --cookie 指定 cookie 文件")
        return []

    session = _build_session(cookies)
    try:
        resp = session.get(TOVIEW_API, headers=HEADERS, timeout=20)
        payload = resp.json()
    except Exception as e:
        print(f"[错误] 请求失败: {e}")
        return []

    if payload.get("code") != 0:
        print(f"[错误] 接口返回 code={payload.get('code')} {payload.get('message')}")
        print("        cookie 可能已过期，重新跑 bili_login_cdp.py 抓一次")
        return []

    data = payload.get("data") or {}
    raw = data.get("list") or []
    print(f"  稍后再看共 {data.get('count', len(raw))} 条，本次取 {len(raw)} 条")
    return raw


def to_item(it: dict) -> dict:
    """把接口条目转成 build_item_html 需要的结构"""
    bvid = it.get("bvid") or ""
    stat = it.get("stat") or {}
    owner = it.get("owner") or {}
    duration = it.get("duration") or 0
    progress = it.get("progress") or 0

    meta = []
    if owner.get("name"):
        meta.append(f"UP主：{owner['name']}")
    if it.get("tname"):
        meta.append(f"分区：{it['tname']}")
    if progress and progress > 0 and duration:
        meta.append(f"进度：{_format_duration(progress)} / {_format_duration(duration)}")
    if it.get("add_at"):
        added = datetime.fromtimestamp(it["add_at"], tz=CST).strftime("%Y-%m-%d %H:%M")
        meta.append(f"加入时间：{added}")

    desc = (it.get("desc") or "").strip()
    description = "\n".join(meta)
    if desc:
        description += "\n\n" + desc

    return {
        "title": it.get("title") or "未知标题",
        "bvid": bvid,
        "aid": it.get("aid") or "",
        "link": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "description": description,
        "pic": _https(it.get("pic") or ""),
        "created": it.get("add_at") or it.get("pubdate") or 0,
        "view": stat.get("view") or 0,
        "danmaku": stat.get("danmaku") or 0,
        "reply": stat.get("reply") or 0,
        "like": stat.get("like") or 0,
        "coin": stat.get("coin") or 0,
        "favorite": stat.get("favorite") or 0,
        "duration": duration,
        "owner_name": owner.get("name") or "",
        "owner_mid": owner.get("mid") or "",
    }


def generate_xml(items: list[dict], post_base_url: str, embed: bool = True) -> str:
    now = datetime.now(CST).strftime("%a, %d %b %Y %H:%M:%S +0800")
    channel_desc = f"B站「稍后再看」列表，共 {len(items)} 条，按加入时间倒序"

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/">',
        '  <channel>',
        '    <title><![CDATA[qssx 的 bilibili 稍后再看]]></title>',
        f'    <link>{WATCHLATER_URL}</link>',
        f'    <atom:link href="{post_base_url}feed_watchlater.xml" rel="self" type="application/rss+xml" />',
        f'    <description><![CDATA[{channel_desc}]]></description>',
        '    <generator>bilibili_watchlater_rss.py</generator>',
        '    <language>zh-CN</language>',
        f'    <lastBuildDate>{now}</lastBuildDate>',
        '    <ttl>60</ttl>',
    ]

    for v in items:
        bvid = v.get("bvid", "")
        bili_link = v.get("link") or ""
        pub = ts_to_rfc2822(v["created"]) if v.get("created") else now
        link = f"{post_base_url}posts/{bvid}.html" if post_base_url and bvid else bili_link
        space_url = (
            f"https://space.bilibili.com/{v['owner_mid']}" if v.get("owner_mid") else WATCHLATER_URL
        )
        content = build_item_html(v, link, bili_link, space_url, embed=embed)

        parts.extend([
            '    <item>',
            f'      <title><![CDATA[{v["title"]}]]></title>',
            f'      <link>{link}</link>',
            f'      <guid isPermaLink="false">{bili_link}</guid>',
            f'      <pubDate>{pub}</pubDate>',
            f'      <description><![CDATA[{content}]]></description>',
        ])
        if v.get("pic"):
            parts.append(f'      <media:thumbnail url="{v["pic"]}" />')
        parts.append('    </item>')

    parts.extend(['  </channel>', '</rss>'])
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="B站稍后再看 RSS 生成器")
    ap.add_argument("--cookie", "-c", default="", help="B站 cookie JSON（必须，否则接口 401）")
    ap.add_argument("--output", "-o", default="feed_watchlater.xml", help="输出的 feed 路径")
    ap.add_argument("--count", "-n", type=int, default=50, help="最多输出多少条（默认 50）")
    ap.add_argument("--comments", type=int, default=0,
                    help="为前 N 条抓取热门评论（默认 0，不抓；会多花 N*0.3 秒）")
    ap.add_argument("--base-url", default="", help="站点根地址，用于生成播放页链接")
    ap.add_argument("--posts-dir", default="", help="播放页输出目录")
    ap.add_argument("--no-embed", dest="embed", action="store_false", help="不内嵌播放器")
    args = ap.parse_args()
    args.embed = getattr(args, "embed", True)

    cookies = load_cookies(args.cookie) if args.cookie else {}
    print("=" * 60)
    print("B站「稍后再看」RSS 生成器")
    print(f"条目上限: {args.count} | Cookie: {'有' if cookies else '无（会失败）'}")
    print("=" * 60)

    raw = fetch_watchlater(cookies)
    if not raw:
        sys.exit(1)

    items = [to_item(it) for it in raw[: args.count]]
    items = [v for v in items if v.get("bvid")]
    print(f"  有效条目 {len(items)} 条")

    if args.comments > 0 and cookies:
        print(f"  抓取前 {args.comments} 条的热门评论...")
        session_items = items[: args.comments]
        for v in session_items:
            cs = get_video_comments(v.get("aid", ""), cookies, limit=10)
            if cs:
                v["comments"] = cs

    xml = generate_xml(items, args.base_url, embed=args.embed)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml, encoding="utf-8")
    print(f"✅ feed 已生成: {out.resolve()} （{len(xml) // 1024} KB）")

    if args.posts_dir and args.base_url:
        posts = Path(args.posts_dir)
        posts.mkdir(parents=True, exist_ok=True)
        made = 0
        for v in items:
            page = generate_post_page(v, v.get("owner_name") or "bilibili",
                                      f"https://space.bilibili.com/{v.get('owner_mid')}"
                                      if v.get("owner_mid") else WATCHLATER_URL,
                                      v.get("link") or "")
            (posts / f"{v['bvid']}.html").write_text(page, encoding="utf-8")
            made += 1
        print(f"   播放页: {made} 个 → {posts}")


if __name__ == "__main__":
    main()
