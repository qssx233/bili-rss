#!/usr/bin/env python3
"""把多个外部博客 RSS 合并成一个订阅源，繁体（台湾正体）自动转简体，并生成站点列表页。

为什么不直接让阅读器订阅原地址：
  - 有些博客（「只贏」）用繁体写，作者本人不改，但读起来费劲 → 这里统一转简体
  - 两个博客合成一个地址，少订一条是一条
  - 顺手加一个卡片列表页，和 B 站那两个源保持同一套视觉

只输出摘要 + 首图 + 原文链接，不转载全文。
两种格式都支持：RSS 2.0（<item>）和 Atom 1.0（<entry>，Bear Blog 用的这个）。

用法：
  python blog_feed.py --output-feed ../feed_blog.xml --output-page ../blog.html
"""

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import requests
from opencc import OpenCC

from bilibili_list_page import CSS, FONT_FACE, SCRIPT

CST = timezone(timedelta(hours=8))
_cc = OpenCC("tw2sp")  # 台湾正体 → 简体，含词汇级转换（睡著→睡着、網路→网络）

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 要合并的博客。t2s=True 表示原文是繁体，统一转成简体再输出。
BLOGS = [
    {"name": "狡猫三窝", "feed": "https://slykiten.com/feed/", "t2s": False},
    {"name": "只贏", "feed": "https://www.onlywin.work/feed", "t2s": True},
    {"name": "So!azy", "feed": "https://blog.solazy.me/feed/", "t2s": False},
]

ATOM = "{http://www.w3.org/2005/Atom}"

EXTRA_CSS = """
.item .thumb { aspect-ratio:16/10; }
.item .excerpt {
  margin:7px 0 0; font-size:11.5px; color:var(--text-soft); line-height:1.7;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
}
.item .src {
  display:inline-block; padding:1px 7px; border-radius:7px; font-size:10.5px;
  color:var(--pink-deep); background:hsla(340,80%,93%,0.95);
  border:1px solid hsla(340,70%,80%,0.55);
}
.thumb .noimg {
  width:100%; height:100%; display:flex; align-items:center; justify-content:center;
  font-size:12px; letter-spacing:0.08em; color:var(--text-soft);
  background:linear-gradient(135deg, hsla(340,72%,95%,0.95), hsla(200,62%,95%,0.95) 55%,
    hsla(38,78%,94%,0.95));
}
.filters { display:flex; gap:8px; flex-wrap:wrap; margin:0 2px 12px; }
.filters button {
  padding:6px 15px; border-radius:999px; font-size:12px; font-family:inherit;
  cursor:pointer; color:var(--accent-deep); background:rgba(255,255,255,0.72);
  border:1px solid rgba(152,120,103,0.15); box-shadow:none; font-weight:400;
}
.filters button.on {
  color:#fff; border-color:transparent;
  background:linear-gradient(135deg,#fc86a7,#e95f8a);
  box-shadow:0 8px 18px -12px rgba(233,95,138,0.9);
}
.filters button:hover { transform:none; }
"""

TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r'<img[^>]+?src=["\']([^"\']+)["\']', re.I)


def t2s(text: str) -> str:
    return _cc.convert(text) if text else text


def strip_tags(s: str) -> str:
    s = TAG_RE.sub(" ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def first_image(*chunks: str) -> str:
    for c in chunks:
        for m in IMG_RE.finditer(c or ""):
            url = m.group(1)
            if any(k in url.lower() for k in ("wp-smiley", "emoji", "gravatar", "avatar")):
                continue
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http://"):
                url = "https://" + url[7:]
            return url
    return ""


def fetch(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Accept": "application/rss+xml, application/xml, */*"},
                     timeout=timeout)
    r.raise_for_status()
    return r.text


def _iso_dt(raw: str):
    try:
        return datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _finish(blog: dict, title: str, link: str, dt, cats: list[str],
            summary: str, image: str) -> dict:
    if blog["t2s"]:
        title = t2s(title)
        summary = t2s(summary)
        cats = [t2s(c) for c in cats]
    return {
        "source": blog["name"],
        "title": title,
        "link": link,
        "dt": dt,
        "date": dt.astimezone(CST).strftime("%Y-%m-%d"),
        "cats": cats[:2],
        "summary": summary,
        "image": image,
    }


def parse_atom(root, blog: dict, limit: int) -> list[dict]:
    """Atom 1.0（Bear Blog、Hugo 默认等都是这个）：entry / link href / published。"""
    out = []
    for e in root.findall(ATOM + "entry"):
        title = (e.findtext(ATOM + "title") or "").strip()
        link = ""
        for l in e.findall(ATOM + "link"):
            if l.get("rel") in (None, "alternate"):
                link = (l.get("href") or "").strip()
                if link:
                    break
        if not title or not link:
            continue

        summary_html = e.findtext(ATOM + "summary") or ""
        content_html = e.findtext(ATOM + "content") or ""
        summary = strip_tags(summary_html) or strip_tags(content_html)[:180]
        image = first_image(content_html, summary_html)
        cats = [c.get("term").strip() for c in e.findall(ATOM + "category")
                if (c.get("term") or "").strip()]
        dt = _iso_dt(e.findtext(ATOM + "published") or e.findtext(ATOM + "updated") or "")

        out.append(_finish(blog, title, link, dt, cats, summary, image))
        if len(out) >= limit:
            break
    return out


def parse_blog(blog: dict, limit: int) -> list[dict]:
    raw = fetch(blog["feed"])
    root = ET.fromstring(raw)

    # Atom 的根是 <feed>，RSS 的根是 <rss>
    if root.tag.lower().endswith("feed"):
        return parse_atom(root, blog, limit)

    ch = root.find("channel")
    if ch is None:
        raise ValueError("既不是 RSS 也不是 Atom，找不到 <channel>/<feed>")

    out = []
    for it in ch.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue

        desc_html = html.unescape(it.findtext("description") or "")
        content_html = html.unescape(it.findtext("content:encoded", namespaces=NS) or "")

        summary = strip_tags(desc_html) or strip_tags(content_html)[:180]
        image = first_image(content_html, desc_html)
        cats = [c.text.strip() for c in it.findall("category") if (c.text or "").strip()]

        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            dt = datetime.now(timezone.utc)

        out.append(_finish(blog, title, link, dt, cats, summary, image))
        if len(out) >= limit:
            break
    return out


def build_feed(items: list[dict], site_url: str) -> str:
    now = datetime.now(CST)
    rows = []
    for p in items:
        body = ""
        if p["image"]:
            body += (f'<p><img src="{html.escape(p["image"])}" alt="" '
                     f'referrerpolicy="no-referrer" style="max-width:100%;border-radius:10px"></p>')
        body += f"<p>{html.escape(p['summary'])}</p>"
        body += f'<p><a href="{html.escape(p["link"])}">阅读原文 →</a></p>'
        rows.append(f"""    <item>
      <title>{html.escape('[' + p['source'] + '] ' + p['title'])}</title>
      <link>{html.escape(p['link'])}</link>
      <guid isPermaLink="true">{html.escape(p['link'])}</guid>
      <pubDate>{format_datetime(p['dt'])}</pubDate>
      <description><![CDATA[{body}]]></description>
    </item>""")

    names = "、".join(sorted({p["source"] for p in items}))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>qssx's 博客订阅</title>
    <link>{html.escape(site_url)}blog.html</link>
    <description>{html.escape(names)}　·　繁体已自动转为简体　·　只放摘要，点进去看原文</description>
    <language>zh-CN</language>
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>
    <generator>qssx-rss blog merger</generator>
{chr(10).join(rows)}
  </channel>
</rss>
"""


def card_html(p: dict) -> str:
    title = html.escape(p["title"])
    meta = f'<span class="src">{html.escape(p["source"])}</span>'
    meta += "".join(f"<span>{html.escape(c)}</span>" for c in p["cats"])
    meta += f"<span>{p['date']}</span>"
    thumb = (f'<img src="{html.escape(p["image"])}" alt="{title}" '
             f'referrerpolicy="no-referrer" loading="lazy">') if p["image"] \
        else f'<div class="noimg">{html.escape(p["source"])}</div>'
    excerpt = f'<p class="excerpt">{html.escape(p["summary"])}</p>' if p["summary"] else ""
    return f"""      <a class="item" data-source="{html.escape(p['source'])}" href="{html.escape(p['link'])}" target="_blank" rel="noopener noreferrer">
        <div class="thumb">{thumb}</div>
        <div class="body">
          <h3>{title}</h3>
          <div class="meta">{meta}</div>
          {excerpt}
        </div>
      </a>"""


def build_page(items: list[dict], feed_file: str, home_file: str = "index.html") -> str:
    grid = "\n".join(card_html(p) for p in items)
    sources = sorted({p["source"] for p in items})
    counts = {s: sum(1 for p in items if p["source"] == s) for s in sources}
    filter_buttons = "\n".join(
        f'    <button data-src="{html.escape(s)}">{html.escape(s)} {counts[s]}</button>'
        for s in sources)
    sub = (f"{'、'.join(sources)}　·　共 {len(items)} 篇　·　繁体已自动转为简体，"
           f"点卡片跳原站阅读")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>博客 · qssx's RSS</title>
<link rel="icon" type="image/svg+xml" href="assets/bili.svg">
<meta name="theme-color" content="#faf5f0">
<style>
{FONT_FACE.format()}
{CSS}
{EXTRA_CSS}
</style>
</head>
<body>
<div class="bg" id="bg"></div><div class="veil"></div>
<div class="page">
  <div class="topbar">
    <a class="brand" href="{home_file}" title="返回首页">
      <img src="assets/bili.svg" alt="RSS">
      <span>qssx's RSS</span>
    </a>
    <div class="me">
      <span class="quote" id="hk"></span>
      <img src="assets/portrait.jpg" alt="头像">
    </div>
  </div>

  <div class="card">
    <div class="head">
      <h1>博客</h1>
      <p>{sub}</p>
      <div class="actions">
        <a class="btn" href="{feed_file}">打开 feed</a>
        <a class="btn ghost" href="{home_file}">返回首页</a>
      </div>
    </div>
  </div>

  <div class="filters">
    <button class="on" data-src="all">全部 {len(items)}</button>
{filter_buttons}
  </div>

  <div class="grid">
{grid}
  </div>

  <p class="note">文字与图片版权归原作者所有 · 本页只做聚合，不转载全文</p>
</div>
{SCRIPT}
<script>
(function(){{
  var btns=document.querySelectorAll('.filters button');
  Array.prototype.forEach.call(btns, function(b){{
    b.addEventListener('click', function(){{
      Array.prototype.forEach.call(btns, function(x){{ x.classList.remove('on'); }});
      b.classList.add('on');
      var want=b.dataset.src;
      Array.prototype.forEach.call(document.querySelectorAll('.grid .item'), function(it){{
        it.style.display = (want==='all' || it.dataset.source===want) ? '' : 'none';
      }});
    }});
  }});
}})();
</script>
</body>
</html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并多个博客 feed（繁转简）并生成列表页")
    ap.add_argument("--output-feed", required=True, help="合并后的 feed 输出路径")
    ap.add_argument("--output-page", required=True, help="列表页 html 输出路径")
    ap.add_argument("--per-blog", type=int, default=10, help="每个博客取几条（默认 10）")
    ap.add_argument("--site-url", default="https://qssx233.github.io/bili-rss/")
    args = ap.parse_args(argv)

    all_items, failed = [], []
    for blog in BLOGS:
        try:
            got = parse_blog(blog, args.per_blog)
            print(f"   {blog['name']}: {len(got)} 篇" + ("（已转简体）" if blog["t2s"] else ""))
            all_items.extend(got)
        except Exception as e:
            failed.append(blog["name"])
            print(f"   ⚠ {blog['name']} 抓取失败：{e}", file=sys.stderr)

    if not all_items:
        print("❌ 两个博客都没抓到，保留原有文件", file=sys.stderr)
        return 1

    all_items.sort(key=lambda p: p["dt"], reverse=True)

    feed_path = Path(args.output_feed)
    page_path = Path(args.output_page)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.parent.mkdir(parents=True, exist_ok=True)

    feed_path.write_text(build_feed(all_items, args.site_url), encoding="utf-8")
    page_path.write_text(build_page(all_items, feed_path.name), encoding="utf-8")

    print(f"✅ {feed_path.name}：{len(all_items)} 条")
    print(f"✅ {page_path.name} 已生成")
    if failed:
        print(f"⚠ 失败的源：{'、'.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
