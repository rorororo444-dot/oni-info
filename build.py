#!/usr/bin/env python3
"""ONI Global の free-chat 各言語チャンネルを読み取り、GitHub Pages 用の静的ページを作る。

環境変数 DISCORD_TOKEN にボットのトークンを入れて実行する(GitHub Actions の Secrets から渡す)。
初回は全履歴を取得し、2回目以降は新着と直近の編集・削除だけを反映する。
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
IMG = DOCS / "img"

API = "https://discord.com/api/v10"
TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()

CHANNELS = [
    # (key, 表示名, チャンネルID)
    ("jp", "日本語", "1315597739031003236"),
    ("en", "English", "1315627214342983741"),
    ("ru", "Русский", "1315627293707599923"),
    ("es", "Español", "1562047983678259210"),
    ("ko", "한국어", "1574253565260861440"),
]

RECHECK = 100          # 毎回取り直す直近件数(編集・削除の反映用)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
JST = timezone(timedelta(hours=9))

UI = {
    "jp": {"updated": "最終更新", "auto": "5分ごとに自動更新されます(混雑時は遅れることがあります)。", "back": "トップへ", "empty": "まだ投稿はありません。"},
    "en": {"updated": "Last updated", "auto": "This page updates automatically about every 5 minutes.", "back": "Top", "empty": "No messages yet."},
    "ru": {"updated": "Обновлено", "auto": "Страница обновляется автоматически примерно каждые 5 минут.", "back": "Наверх", "empty": "Сообщений пока нет."},
    "es": {"updated": "Última actualización", "auto": "Esta página se actualiza automáticamente cada 5 minutos aprox.", "back": "Inicio", "empty": "Aún no hay mensajes."},
    "ko": {"updated": "마지막 업데이트", "auto": "이 페이지는 약 5분마다 자동으로 업데이트됩니다.", "back": "처음으로", "empty": "아직 메시지가 없습니다."},
}


def api_get(path):
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": "Bot " + TOKEN,
            "User-Agent": "DiscordBot (oni-info, 1.0)",
        },
    )
    for _ in range(8):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode("utf-8")).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(3)
                continue
            raise
    raise RuntimeError("Discord API retry limit: " + path)


def fetch_page(channel_id, before=None):
    q = "?limit=100" + (f"&before={before}" if before else "")
    return api_get(f"/channels/{channel_id}/messages{q}")


def slim(m):
    a = m.get("author", {})
    name = a.get("global_name") or a.get("username") or "?"
    if m.get("webhook_id"):
        name = a.get("username") or name
    atts = []
    for at in m.get("attachments", []):
        atts.append({
            "id": at.get("id"),
            "filename": at.get("filename", ""),
            "url": at.get("url", ""),
            "ctype": at.get("content_type", ""),
            "size": at.get("size", 0),
        })
    embeds = []
    for e in m.get("embeds", []):
        if e.get("type") == "image" and e.get("thumbnail", {}).get("url"):
            embeds.append({"image": e["thumbnail"]["url"], "url": e.get("url", "")})
        elif e.get("image", {}).get("url"):
            embeds.append({"image": e["image"]["url"], "url": e.get("url", "")})
    stickers = [s.get("name", "") for s in m.get("sticker_items", [])]
    return {
        "id": m["id"],
        "name": name,
        "ts": m.get("timestamp", ""),
        "edited": bool(m.get("edited_timestamp")),
        "content": m.get("content", ""),
        "atts": atts,
        "embeds": embeds,
        "stickers": stickers,
    }


def sync_channel(key, channel_id):
    path = DATA / f"{key}.json"
    msgs = {}
    if path.exists():
        for m in json.loads(path.read_text("utf-8")):
            msgs[m["id"]] = m
    known_max = max((int(i) for i in msgs), default=0)

    fetched = []
    before = None
    window_ids = set()
    first_page = True
    while True:
        page = fetch_page(channel_id, before)
        if not page:
            break
        for m in page:
            fetched.append(slim(m))
        if first_page:
            window_ids = {m["id"] for m in page}
            window_min = min(int(m["id"]) for m in page)
            first_page = False
        oldest = min(int(m["id"]) for m in page)
        # 既知の範囲まで来て、かつ再確認の範囲も取り終えたら止める
        if known_max and oldest <= known_max and len(fetched) >= RECHECK:
            break
        before = oldest
        if len(page) < 100:
            break

    # 直近ウィンドウ内で消えた投稿は削除扱い
    if not first_page and known_max:
        for mid in list(msgs):
            if int(mid) >= window_min and mid not in window_ids:
                del msgs[mid]
    for m in fetched:
        msgs[m["id"]] = m

    out = sorted(msgs.values(), key=lambda m: int(m["id"]))
    path.write_text(json.dumps(out, ensure_ascii=False, indent=0), "utf-8")
    return out


def download_images(messages):
    for m in messages:
        for at in m["atts"]:
            if not at["ctype"].startswith("image/"):
                continue
            ext = Path(at["filename"]).suffix.lower() or ".png"
            local = IMG / f"{at['id']}{ext}"
            at["local"] = f"img/{local.name}"
            if local.exists():
                continue
            if at.get("size", 0) > MAX_IMAGE_BYTES:
                at["local"] = ""
                continue
            try:
                req = urllib.request.Request(at["url"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    local.write_bytes(r.read())
            except Exception as e:  # 画像が取れなくてもページ生成は続ける
                print("image failed", at["url"][:80], e, file=sys.stderr)
                at["local"] = ""


URL_RE = re.compile(r"(https?://[^\s<>]+)")
CUSTOM_EMOJI_RE = re.compile(r"&lt;a?:(\w+):\d+&gt;")
MENTION_RE = re.compile(r"&lt;[@#][!&]?\d+&gt;")


def render_text(s):
    t = html.escape(s)
    t = CUSTOM_EMOJI_RE.sub(r":\1:", t)
    t = MENTION_RE.sub("@", t)
    t = URL_RE.sub(lambda mo: f'<a href="{mo.group(1)}" rel="noopener" target="_blank">{mo.group(1)}</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return t.replace("\n", "<br>")


def fmt_time(ts):
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
        return d.strftime("%Y-%m-%d %H:%M") + " JST"
    except Exception:
        return ts


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2025;--muted:#667;--accent:#2b59c3;--line:#e3e6ea}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#1f2228;--text:#e8eaed;--muted:#9aa0a8;--accent:#8ab4f8;--line:#2e333b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP","Noto Sans KR",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:.4em 0}
nav.langs{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 18px}
nav.langs a{flex:1 1 140px;text-align:center;padding:14px 10px;background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--accent);text-decoration:none;font-weight:600;font-size:1.05rem}
.meta{color:var(--muted);font-size:.85rem}
.msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;word-wrap:break-word;overflow-wrap:anywhere}
.msg .who{font-weight:600}.msg .when{color:var(--muted);font-size:.8rem;margin-left:6px}
.msg img{max-width:100%;height:auto;border-radius:6px;margin-top:6px;display:block}
a{color:var(--accent)}
.top{display:inline-block;margin:6px 0}
"""


def page(title, body, lang="ja"):
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>
"""


def build_lang_page(key, label, messages, now):
    ui = UI[key]
    parts = [
        f'<a class="top" href="index.html">← {ui["back"]}</a>',
        f"<h1>ONI — {html.escape(label)}</h1>",
        f'<p class="meta">{ui["updated"]}: {now} · {ui["auto"]}</p>',
    ]
    if not messages:
        parts.append(f"<p>{ui['empty']}</p>")
    for m in reversed(messages):  # 新しい順
        body = render_text(m["content"]) if m["content"] else ""
        imgs = ""
        for at in m["atts"]:
            if at.get("local"):
                imgs += f'<a href="{at["local"]}" target="_blank"><img loading="lazy" src="{at["local"]}" alt=""></a>'
            elif at["filename"]:
                imgs += f'<div class="meta">[{html.escape(at["filename"])}]</div>'
        for e in m.get("embeds", []):
            if e.get("image"):
                imgs += f'<img loading="lazy" src="{html.escape(e["image"])}" alt="">'
        if m.get("stickers"):
            imgs += '<div class="meta">[' + html.escape(", ".join(m["stickers"])) + "]</div>"
        if not body and not imgs:
            continue
        parts.append(
            f'<div class="msg"><span class="who">{html.escape(m["name"])}</span>'
            f'<span class="when">{fmt_time(m["ts"])}</span><div>{body}</div>{imgs}</div>'
        )
    parts.append(f'<a class="top" href="index.html">← {ui["back"]}</a>')
    return page(f"ONI — {label}", "\n".join(parts), lang={"jp": "ja"}.get(key, key))


def build_index(now):
    links = "".join(f'<a href="{k}.html">{html.escape(lbl)}</a>' for k, lbl, _ in CHANNELS)
    body = f"""<h1>ONI Alliance — Chat / チャット</h1>
<p>Choose your language · 言語を選んでください · Выберите язык · Elige tu idioma · 언어를 선택하세요</p>
<nav class="langs">{links}</nav>
<p class="meta">Last updated / 最終更新: {now}<br>Auto-updates about every 5 minutes / 約5分ごとに自動更新</p>"""
    return page("ONI Alliance — Chat", body)


def main():
    if not TOKEN:
        sys.exit("DISCORD_TOKEN is not set")
    DATA.mkdir(exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    for key, label, cid in CHANNELS:
        try:
            msgs = sync_channel(key, cid)
        except Exception as e:
            print(f"[{key}] fetch failed: {e}", file=sys.stderr)
            p = DATA / f"{key}.json"
            msgs = json.loads(p.read_text("utf-8")) if p.exists() else []
        download_images(msgs)
        (DOCS / f"{key}.html").write_text(build_lang_page(key, label, msgs, now), "utf-8")
        print(f"[{key}] {len(msgs)} messages")
    (DOCS / "index.html").write_text(build_index(now), "utf-8")


if __name__ == "__main__":
    main()
