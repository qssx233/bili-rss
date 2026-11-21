#!/usr/bin/env python3
"""订阅「二级页面」生成器：把某个 feed 的条目渲染成一页可点的视频列表。

每个订阅一个页面（如 uploads.html / watchlater.html），页内每张卡片都链到
posts/<BV>.html（自建播放页），所以从首页 → 订阅列表 → 单条视频是三级结构。

视觉沿用站点规范：本地 ChillRoundM 优先 → ChillRoundF woff2 兜底、背景不模糊、
内容区毛玻璃、卡片圆角。
"""

import html
from datetime import datetime
from xml.sax.saxutils import escape

from bilibili_rss import CST, _format_count, _format_duration

FONT_FACE = """@font-face {{
  font-family:'ChillRound';
  src:local('ChillRoundM'), local('寒蝉半圆体'), local('ChillRoundM Regular'),
      url('assets/ChillRoundF.woff2') format('woff2');
  font-weight:400; font-style:normal; font-display:swap;
}}"""

CSS = """
:root {
  --text:hsl(28,32%,25%); --text-mid:hsl(28,18%,44%); --text-soft:hsl(28,13%,62%);
  --accent-deep:hsl(31,30%,40%); --pink-deep:hsl(340,62%,52%);
}
* { box-sizing:border-box; }
html,body { margin:0; padding:0; min-height:100%; overflow-x:hidden; }
body {
  font-family:'ChillRound',-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",
    "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif;
  font-synthesis:none; -webkit-font-smoothing:antialiased;
  color:var(--text); line-height:1.8; background-color:hsl(35,36%,97%);
  background-image:
    radial-gradient(900px 420px at 10% -10%, hsla(342,85%,90%,0.5), transparent 62%),
    radial-gradient(760px 400px at 92% 2%, hsla(197,70%,91%,0.45), transparent 60%),
    radial-gradient(700px 520px at 50% 104%, hsla(40,80%,91%,0.42), transparent 65%);
  background-attachment:fixed;
}
.bg {
  position:fixed; inset:-46px; z-index:0; background-size:cover; background-position:center;
  background-color:hsl(35,32%,94%);
  filter:saturate(1.06) brightness(1.02); opacity:0.94;
}
.veil {
  position:fixed; inset:0; z-index:0;
  background:linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.10) 50%,
    rgba(255,255,255,0.06));
}
.page { position:relative; z-index:1; max-width:1080px; margin:0 auto; padding:22px 16px 48px; }
.card {
  background:rgba(255,255,255,0.62); border:1px solid rgba(255,255,255,0.72);
  border-radius:26px; padding:18px 20px;
  box-shadow:0 26px 62px -36px rgba(70,45,35,0.42), 0 2px 6px rgba(70,45,35,0.04);
  -webkit-backdrop-filter:blur(30px) saturate(1.2); backdrop-filter:blur(30px) saturate(1.2);
}
.topbar {
  display:flex; align-items:center; gap:10px; margin:0 0 14px;
  font-size:12px; color:var(--text-soft); letter-spacing:0.02em;
}
.topbar .brand {
  display:inline-flex; align-items:center; gap:9px; flex:0 0 auto;
  text-decoration:none; color:var(--accent-deep); font-size:12px;
  padding:4px 13px 4px 5px; border-radius:999px;
  background:rgba(255,255,255,0.72); border:1px solid rgba(152,120,103,0.13);
  box-shadow:0 4px 14px -10px rgba(70,45,35,0.4);
  -webkit-backdrop-filter:blur(12px); backdrop-filter:blur(12px);
}
.topbar .brand img { width:24px; height:24px; border-radius:8px; display:block; }
.me {
  margin-left:auto; display:flex; align-items:center; gap:9px; overflow:hidden;
  min-width:0; max-width:60%; padding:4px 5px 4px 13px; border-radius:999px;
  background:rgba(255,255,255,0.66); border:1px solid rgba(152,120,103,0.13);
  box-shadow:0 6px 16px -12px rgba(70,45,35,0.4);
  -webkit-backdrop-filter:blur(12px); backdrop-filter:blur(12px);
}
.me .quote {
  font-size:11.5px; color:var(--text-mid); white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; line-height:1.5; flex:1 1 auto; min-width:0;
}
.me img {
  width:30px; height:30px; border-radius:999px; object-fit:cover;
  display:block; flex:0 0 auto; border:1px solid rgba(255,255,255,0.9);
}
.head { padding:6px 6px 18px; }
.head h1 { margin:0 0 9px; font-size:26px; font-weight:400; letter-spacing:0.02em; line-height:1.35; }
.head p { margin:0; font-size:13px; color:var(--text-mid); }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
.btn {
  display:inline-flex; align-items:center; gap:6px; padding:9px 20px; border-radius:999px;
  font-size:13px; text-decoration:none; color:#fff;
  background:linear-gradient(135deg,#fc86a7,#e95f8a);
  box-shadow:0 8px 18px -10px rgba(233,95,138,0.9);
}
.btn.ghost {
  color:var(--accent-deep); background:rgba(255,255,255,0.72);
  border:1px solid rgba(152,120,103,0.14); box-shadow:none;
}
.btn:active { opacity:0.85; }
.grid {
  display:grid; grid-template-columns:repeat(auto-fill, minmax(238px, 1fr)); gap:14px;
  padding:2px 2px 8px;
}
.item {
  display:flex; flex-direction:column; overflow:hidden; text-decoration:none; color:inherit;
  border-radius:20px; background:rgba(255,255,255,0.56);
  border:1px solid rgba(255,255,255,0.7);
  box-shadow:0 10px 26px -20px rgba(70,45,35,0.5);
  -webkit-backdrop-filter:blur(16px) saturate(1.15); backdrop-filter:blur(16px) saturate(1.15);
  transition:transform 0.18s ease, background 0.18s ease, box-shadow 0.18s ease;
}
.item:hover {
  transform:translateY(-3px); background:rgba(255,255,255,0.74);
  box-shadow:0 18px 34px -22px rgba(70,45,35,0.55);
}
.item:active { opacity:0.9; }
.thumb { position:relative; width:100%; aspect-ratio:16/9; overflow:hidden; background:rgba(255,255,255,0.4); }
.thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.thumb .dur {
  position:absolute; right:8px; bottom:8px; padding:2px 7px; border-radius:8px;
  font-size:11px; color:#fff; background:rgba(30,22,18,0.6);
  -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px);
}
.body { padding:11px 13px 13px; }
.body h3 {
  margin:0 0 7px; font-size:14px; font-weight:400; line-height:1.58; color:var(--text);
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
}
.meta { font-size:12px; color:hsl(28,20%,46%); display:flex; flex-wrap:wrap; gap:7px; align-items:center; }
.meta span { white-space:nowrap; }
.note {
  text-align:center; font-size:11.5px; color:hsl(28,26%,28%);
  margin:24px 0 0; letter-spacing:0.04em;
  -webkit-text-stroke:2.4px rgba(255,255,255,0.96);
  paint-order:stroke fill;
  text-shadow:0 1px 2px rgba(255,255,255,0.55);
}
@media (max-width:600px) {
  .page { padding:12px 10px 32px; }
  .card { padding:14px; border-radius:20px; }
  .topbar { flex-wrap:wrap; row-gap:8px; }
  .topbar .brand { position:relative; z-index:2; }
  .topbar .me { flex:0 0 100%; max-width:100%; margin-left:0; }
  .head { padding:2px 4px 14px; }
  .head h1 { font-size:20px; }
  .grid { grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:10px; }
  .item { border-radius:16px; }
  .body { padding:9px 10px 11px; }
  .body h3 { font-size:12.5px; }
}
"""

SCRIPT = """<script>
(function(){
  var bg=document.getElementById('bg');
  if(!bg) return;
  var urls=['https://www.dmoe.cc/random.php?t='+Date.now(),
            'https://t.alcy.cc/moe?t='+Date.now()];
  var i=0;
  function tryNext(){
    if(i>=urls.length){ bg.style.backgroundImage="url('assets/bg.jpg')"; return; }
    var img=new Image(), u=urls[i++];
    img.onload=function(){ bg.style.backgroundImage="url('"+u+"')"; };
    img.onerror=tryNext;
    img.src=u;
  }
  tryNext();
})();
(function(){
  var el=document.getElementById('hk');
  if(!el) return;
  el.textContent='\\u300c\\u4eca\\u5929\\u4e5f\\u8981\\u597d\\u597d\\u5199\\u4ee3\\u7801\\u5440\\u300d';
  fetch('https://v1.hitokoto.cn/?encode=json&c=e&c=l')
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d && d.hitokoto){
        el.textContent='\\u300c'+d.hitokoto+'\\u300d';
        el.title=d.hitokoto+(d.from?' \\u2014\\u2014 '+d.from:'');
      }
    })
    .catch(function(){});
})();
</script>"""


def _date_str(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=CST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _https(url: str) -> str:
    return "https://" + url[7:] if url.startswith("http://") else url


def generate_list_page(items: list[dict], *, page_title: str, page_sub: str,
                       feed_file: str, home_file: str = "index.html",
                       empty_hint: str = "这个订阅暂时还没有条目") -> str:
    """items: 每项需要 bvid/title/pic/duration/link/created，可选 owner_name/view/danmaku"""
    cards = []
    for v in items:
        bvid = v.get("bvid") or ""
        if not bvid:
            continue
        title = escape(str(v.get("title") or "未知标题"))
        pic = _https(v.get("pic") or "")
        dur = _format_duration(v.get("duration") or 0)
        meta = []
        if v.get("owner_name"):
            meta.append(f'<span>{escape(str(v["owner_name"]))}</span>')
        d = _date_str(v.get("created"))
        if d:
            meta.append(f"<span>{d}</span>")
        if v.get("view"):
            meta.append(f'<span>播放 {_format_count(v["view"])}</span>')
        thumb_img = (
            f'<img src="{pic}" alt="{title}" referrerpolicy="no-referrer" loading="lazy">'
            if pic else ""
        )
        cards.append(f"""      <a class="item" href="posts/{bvid}.html">
        <div class="thumb">{thumb_img}<span class="dur">{dur}</span></div>
        <div class="body">
          <h3>{title}</h3>
          <div class="meta">{''.join(meta)}</div>
        </div>
      </a>""")

    grid = "\n".join(cards) if cards else f'  <p class="note">{empty_hint}</p>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(page_title)} · qssx's RSS</title>
<link rel="icon" type="image/svg+xml" href="assets/bili.svg">
<meta name="theme-color" content="#faf5f0">
<style>
{FONT_FACE.format()}
{CSS}
</style>
</head>
<body>
<div class="bg" id="bg"></div><div class="veil"></div>
<div class="page">
  <div class="topbar">
    <a class="brand" href="{home_file}" title="返回首页">
      <img src="assets/bili.svg" alt="bilibili">
      <span>qssx's RSS</span>
    </a>
    <div class="me">
      <span class="quote" id="hk"></span>
      <img src="assets/portrait.jpg" alt="头像">
    </div>
  </div>

  <div class="card">
    <div class="head">
      <h1>{escape(page_title)}</h1>
      <p>{page_sub}</p>
      <div class="actions">
        <a class="btn" href="{feed_file}">打开 feed</a>
        <a class="btn ghost" href="{home_file}">返回首页</a>
      </div>
    </div>
  </div>

  <div class="grid">
{grid}
  </div>

  <p class="note">共 {len(cards)} 条 · 点封面进单条播放页 · 视频版权归原作者所有</p>
</div>
{SCRIPT}
</body>
</html>"""
