#!/usr/bin/env python3
"""
Sleep Tracker v3 — трекер сна в стиле Sleep Monitor (Flask + SQLite, один файл).
Готов к деплою на Render:  Build: pip install -r requirements.txt   Start: gunicorn app:app

Что внутри:
  • тёмный интерфейс в палитре Chocolate Brown / Soft Sage / Warm Cream
  • чек-лист перед сном, ночной режим для OLED и LCD, умный будильник
  • 5-минутные интервалы: шум (усл. дБ), движение (DeviceMotion), частотный профиль
  • гипнограмма, эффективность сна, оценка ночи, индекс храпа и беспокойства
  • localStorage на телефоне = главная копия; SQLite на сервере = зеркало
  • экспорт / импорт JSON, PWA (manifest + service worker + PNG-иконки без доп. библиотек)
"""
import json
import math
import os
import re
import sqlite3
import struct
import zlib
from contextlib import closing
from functools import lru_cache

from flask import Flask, Response, abort, jsonify, request

APP_VERSION = "3"
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sleep.db")
)


# ───────────────────────────── База данных ─────────────────────────────
def q(sql, args=(), one=False, commit=False):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        if commit:
            c.commit()
        return (rows[0] if rows else None) if one else rows


def init_db():
    # id ночи = время начала сна (мс). meta — лёгкая сводка, data — тяжёлые интервалы.
    q(
        """CREATE TABLE IF NOT EXISTS nights_v2(
            start_ts INTEGER PRIMARY KEY,
            end_ts INTEGER NOT NULL,
            meta TEXT NOT NULL,
            data TEXT NOT NULL
        )""",
        commit=True,
    )
    migrate_v1()


def migrate_v1():
    """Переносит ночи из таблицы `nights` первой версии (если она есть) и удаляет её."""
    try:
        old = q("SELECT * FROM nights")
    except sqlite3.OperationalError:
        return
    for r in old:
        meta = {
            "id": r["start_ts"], "start": r["start_ts"], "end": r["end_ts"], "v": 1,
            "alarm": r["alarm"] or "", "duration": r["duration_min"],
            "quality": r["quality"], "peaks": r["peaks"], "legacy": True,
        }
        q(
            "INSERT OR IGNORE INTO nights_v2(start_ts,end_ts,meta,data) VALUES(?,?,?,?)",
            (r["start_ts"], r["end_ts"], json.dumps(meta), json.dumps({"minutes": json.loads(r["data"])})),
            commit=True,
        )
    q("DROP TABLE nights", commit=True)


# ───────────────────────────── Проверка входных данных ─────────────────────────────
HHMM = re.compile(r"^\d{1,2}:\d{2}$")
STAGES_RE = re.compile(r"^[WLD]{0,600}$")
INT_FIELDS = ("duration", "quality", "peaks", "eff", "tst", "wake", "light", "deep", "snore", "rest")
EPOCH_KEYS = ("a", "m", "lo", "hi", "sn", "sh", "pk")


def _int(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_epoch(e):
    if not isinstance(e, dict):
        return None
    out = {k: _int(e.get(k)) for k in EPOCH_KEYS}
    out["mv"] = None if e.get("mv") is None else _int(e.get("mv"))
    return out


def clean_record(j):
    """Возвращает (start, end, meta, heavy) или бросает ValueError/KeyError."""
    start, end = _int(j["start"]), _int(j["end"])
    if end <= start or end - start > 48 * 3600 * 1000:
        raise ValueError("range")
    meta = {"id": start, "start": start, "end": end, "v": 2}
    alarm = str(j.get("alarm", ""))
    meta["alarm"] = alarm if HHMM.match(alarm) else ""
    meta["duration"] = max(1, _int(j.get("duration"), (end - start) // 60000))
    for k in INT_FIELDS:
        if j.get(k) is not None:
            meta[k] = _int(j[k])
    meta["quality"] = max(0, min(100, meta.get("quality", 0)))
    stages = j.get("stages")
    if isinstance(stages, str) and STAGES_RE.match(stages):
        meta["stages"] = stages
    meta["motion"] = bool(j.get("motion"))
    ai = j.get("alarmInfo")
    if isinstance(ai, dict):
        target = str(ai.get("target", ""))
        meta["alarmInfo"] = {
            "target": target if HHMM.match(target) else meta["alarm"],
            "targetTs": _int(ai.get("targetTs")),
            "windowMin": _int(ai.get("windowMin")),
            "firedAt": _int(ai.get("firedAt")) if ai.get("firedAt") else None,
            "reason": ai.get("reason") if ai.get("reason") in ("smart", "target") else None,
            "early": _int(ai.get("early")),
        }
    heavy = {}
    eps, mins = j.get("epochs"), j.get("minutes")
    if isinstance(eps, list) and len(eps) <= 600:
        heavy["epochs"] = [_clean_epoch(e) for e in eps]
    elif isinstance(mins, list) and len(mins) <= 3000:
        heavy["minutes"] = [
            ({"a": _int(m.get("a")), "m": _int(m.get("m")), "p": _int(m.get("p"))} if isinstance(m, dict) else None)
            for m in mins
        ]
    return start, end, meta, heavy


# ───────────────────────────── API ─────────────────────────────
@app.post("/api/save")
def api_save():
    """Идемпотентное сохранение ночи (можно слать сколько угодно раз)."""
    j = request.get_json(force=True, silent=True)
    try:
        start, end, meta, heavy = clean_record(j or {})
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "bad data"}), 400
    q(
        "INSERT OR REPLACE INTO nights_v2(start_ts,end_ts,meta,data) VALUES(?,?,?,?)",
        (start, end, json.dumps(meta), json.dumps(heavy)),
        commit=True,
    )
    return jsonify({"ok": True, "id": start})


@app.get("/api/history")
def api_history():
    rows = q("SELECT meta FROM nights_v2 ORDER BY start_ts DESC")
    return jsonify({"sessions": [json.loads(r["meta"]) for r in rows]})


@app.get("/api/night/<int:sid>")
def api_night(sid):
    r = q("SELECT meta, data FROM nights_v2 WHERE start_ts=?", (sid,), one=True)
    if not r:
        return jsonify({"error": "not found"}), 404
    d = json.loads(r["meta"])
    d.update(json.loads(r["data"]))
    return jsonify(d)


@app.delete("/api/night/<int:sid>")
def api_delete(sid):
    q("DELETE FROM nights_v2 WHERE start_ts=?", (sid,), commit=True)
    return jsonify({"ok": True})


@app.get("/healthz")
def healthz():
    return "ok"


# ───────────────────────────── PWA: manifest, service worker, иконки ─────────────────────────────
@app.get("/manifest.json")
def manifest():
    data = {
        "name": "Sleep Tracker",
        "short_name": "Сон",
        "description": "Трекер сна: шум, движения, умный будильник",
        "lang": "ru",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1B110C",
        "theme_color": "#1B110C",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


SW_JS = r"""
const CACHE = 'sleep-v__V__';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => c.addAll(['/', '/manifest.json', '/icon-192.png']))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
// Оболочка приложения: сначала кэш (открывается мгновенно, даже если Render спит), в фоне обновляется.
// Запросы к /api/ в кэш не попадают: за данные отвечает localStorage + синхронизация.
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/healthz' || url.pathname === '/sw.js') return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req, { ignoreSearch: true });
    const net = fetch(req).then(res => { if (res && res.ok) cache.put(req, res.clone()); return res; }).catch(() => null);
    if (hit) { e.waitUntil(net); return hit; }
    const res = await net;
    return res || new Response('Нет связи с сервером', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  })());
});
"""


@app.get("/sw.js")
def service_worker():
    resp = Response(SW_JS.replace("__V__", APP_VERSION), mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" fill="#2B1B12"/>
<mask id="m"><rect width="512" height="512" fill="#fff"/><circle cx="305" cy="228" r="118" fill="#000"/></mask>
<circle cx="256" cy="256" r="138" fill="#F1E7D3" mask="url(#m)"/>
<circle cx="380" cy="150" r="14" fill="#A3B18A"/></svg>"""


@app.get("/icon.svg")
def icon_svg():
    return Response(ICON_SVG, mimetype="image/svg+xml")


def _png(size, rows):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


@lru_cache(maxsize=4)
def make_icon(size):
    """Рисует иконку (полумесяц + звезда) в PNG без сторонних библиотек."""
    bg, cream, sage = (0x2B, 0x1B, 0x12), (0xF1, 0xE7, 0xD3), (0xA3, 0xB1, 0x8A)
    cx = cy = size / 2
    r1 = size * 0.2695
    c2x, c2y, r2 = cx + size * 0.0957, cy - size * 0.0547, size * 0.2305
    c3x, c3y, r3 = cx + size * 0.242, cy - size * 0.207, size * 0.027
    lo, hi = int(size * 0.15), int(size * 0.85)

    def clamp(v):
        return 0.0 if v < 0 else 1.0 if v > 1 else v

    base = bytes(bg) * size
    rows = []
    for y in range(size):
        if y < lo or y >= hi:
            rows.append(b"\x00" + base)
            continue
        row = bytearray(base)
        for x in range(lo, hi):
            px = bg
            a = clamp(0.5 - (math.hypot(x + .5 - cx, y + .5 - cy) - r1)) * clamp(
                0.5 + (math.hypot(x + .5 - c2x, y + .5 - c2y) - r2)
            )
            if a > 0:
                px = tuple(round(b + (c - b) * a) for b, c in zip(bg, cream))
            s = clamp(0.5 - (math.hypot(x + .5 - c3x, y + .5 - c3y) - r3))
            if s > 0:
                px = tuple(round(p + (c - p) * s) for p, c in zip(px, sage))
            row[3 * x:3 * x + 3] = bytes(px)
        rows.append(b"\x00" + bytes(row))
    return _png(size, rows)


@app.get("/icon-<int:size>.png")
def icon_png(size):
    if size not in (180, 192, 512):
        abort(404)
    resp = Response(make_icon(size), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


# ───────────────────────────── Интерфейс ─────────────────────────────
HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1B110C">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Сон">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>Sleep Tracker</title>
<style>
:root{--bg:#1B110C;--card:#5C3A26;--card2:#4A2E1E;--cream:#F1E7D3;--dim:rgba(241,231,211,.62);--sage:#A3B18A;--tan:#C9A27E;--line:rgba(241,231,211,.14)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--cream);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;-webkit-text-size-adjust:100%}
body{padding:calc(env(safe-area-inset-top) + 8px) 16px calc(92px + env(safe-area-inset-bottom));min-height:100vh;touch-action:manipulation}
body.inNight{padding:0;overflow:hidden}
.hidden{display:none!important}
.center{text-align:center}
.dim{color:var(--dim)}
.sm{font-size:13px}
h2{font-weight:650;margin:20px 0 6px;font-size:26px}
h3{font-weight:600;margin:0 0 10px;font-size:17px}
.clock{font-size:88px;font-weight:200;letter-spacing:-2px;line-height:1;margin:26px 0 4px;font-variant-numeric:tabular-nums}
.clock small{font-size:26px;color:var(--dim);letter-spacing:0;margin-left:4px}
.card{background:var(--card);border-radius:22px;padding:16px;margin:12px 0}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.syncline{font-size:13px;margin:8px 0;color:var(--dim)}
input[type=time]{background:var(--card2);color:var(--cream);border:0;border-radius:14px;font-size:30px;padding:8px 12px;font-family:inherit;color-scheme:dark}
button{font:inherit;font-weight:600;font-size:18px;border:0;border-radius:18px;padding:17px;width:100%;color:var(--bg);background:var(--sage);touch-action:manipulation}
button:active{opacity:.8}
button.alt{background:var(--card2);color:var(--cream)}
button.link{background:none;color:var(--dim);font-weight:400;font-size:15px;width:auto;padding:10px}
.btns{display:grid;gap:12px;margin-top:16px}
.btns2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.btns2 button{font-size:15px;padding:14px}
.seg{display:flex;background:var(--card2);border-radius:14px;padding:3px;gap:3px;margin-top:10px}
.seg button{background:none;color:var(--dim);font-size:14px;padding:10px 4px;border-radius:11px;flex:1;width:auto}
.seg button.on{background:var(--sage);color:var(--bg)}
.big{font-size:64px;font-weight:300;font-variant-numeric:tabular-nums;line-height:1.1}
.meter{height:14px;background:var(--card2);border-radius:7px;overflow:hidden;margin:12px 0}
.meter>div{height:100%;width:0;background:var(--sage)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}
.stat{background:var(--card);border-radius:18px;padding:12px;text-align:center}
.stat b{display:block;font-size:21px;font-weight:600;margin-top:2px}
.stat span{font-size:12px;color:var(--dim)}
canvas{width:100%;height:170px;display:block}
canvas.tall{height:230px}
.item{background:var(--card);border-radius:20px;padding:14px;margin:10px 0}
.badge{border-radius:12px;padding:5px 11px;font-weight:700;font-size:16px;background:var(--card2)}
.strip{display:flex;height:8px;border-radius:4px;overflow:hidden;margin-top:9px;background:var(--card2)}
.strip i{display:block;height:100%}
.bar{height:10px;border-radius:5px;background:var(--card2);overflow:hidden}
.bar>i{display:block;height:100%}
.srow{display:grid;grid-template-columns:104px 1fr 78px;gap:10px;align-items:center;margin:9px 0;font-size:14px}
.srow b{font-weight:600;text-align:right;font-size:13px}
.tabs{position:fixed;left:0;right:0;bottom:0;display:flex;background:rgba(27,17,12,.97);border-top:1px solid var(--line);padding:8px 16px calc(8px + env(safe-area-inset-bottom));gap:8px;z-index:20}
.tabs button{background:none;color:var(--dim);font-size:15px;padding:11px;border-radius:13px}
.tabs button.on{color:var(--cream);background:var(--card)}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:8px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:-1px}

/* модальное окно-чек-лист */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.62);display:flex;align-items:flex-end;z-index:200}
.sheet{background:var(--card);width:100%;border-radius:26px 26px 0 0;padding:20px 18px calc(16px + env(safe-area-inset-bottom));max-height:92vh;overflow:auto}
.check{list-style:none;margin:10px 0 16px;padding:0}
.check li{display:flex;gap:12px;background:var(--card2);border-radius:16px;padding:12px;margin:8px 0;font-size:15px;line-height:1.38}
.check li span{font-size:22px}

/* ночной экран */
#night{position:fixed;inset:0;z-index:100;background:var(--bg);display:flex;flex-direction:column;justify-content:space-between;padding:calc(env(safe-area-inset-top) + 18px) 20px calc(env(safe-area-inset-bottom) + 22px)}
.n-clock{font-size:104px;font-weight:200;text-align:center;font-variant-numeric:tabular-nums;letter-spacing:-2px;line-height:1}
.n-sub{text-align:center;color:var(--dim);margin-top:10px;font-size:15px}
#night.dim,#night.dim *{transition:none!important;animation:none!important}
#night.dim .n-top{display:none}
#night.oled{background:#000}
#night.oled .n-clock{color:var(--cream);opacity:.25}
#night.oled .n-sub{color:var(--sage);opacity:.2}
#night.oled #btn-wake{background:#000;color:var(--sage);border:1px solid rgba(163,177,138,.3);opacity:.3}
#night.oled #btn-cancel{opacity:.25}
#night.oled.touch #btn-wake{opacity:.8}
#night.oled.touch #btn-cancel{opacity:.6}
#night.lcd{background:#120A06}
#night.lcd .n-clock{color:#6B5442}
#night.lcd .n-sub{color:#4F3C2D}
#night.lcd #btn-wake{background:#2A1B13;color:#8A7358}
#night.lcd #btn-cancel{color:#4F3C2D}
#night.lcd.touch #btn-wake{color:#C9B596}
#night.ringing .n-clock{opacity:.9!important;color:var(--cream)!important}
#night.ringing .n-sub{opacity:1!important;color:var(--sage)!important}
#night.ringing #btn-wake{opacity:1!important;background:var(--sage)!important;color:var(--bg)!important;border:0!important}
</style></head>
<body>

<!-- ========== СОН ========== -->
<div id="view-sleep">

  <div id="idle">
    <div class="center">
      <div class="clock"><span id="clock">--:--</span><small id="secs">00</small></div>
      <div class="dim" id="date"></div>
    </div>

    <div id="resume" class="card hidden">
      <div style="margin-bottom:10px">Найдена незавершённая запись (с <span id="resume-from"></span>)</div>
      <div class="btns" style="margin-top:0"><button id="btn-resume">Продолжить</button>
      <button class="alt" id="btn-drop">Сбросить</button></div>
    </div>

    <div class="card">
      <div class="row">
        <div><div>Будильник</div><div class="dim sm" id="alarm-left"></div></div>
        <input type="time" id="alarm" value="07:00">
      </div>
      <div class="dim sm" style="margin-top:12px">Умный будильник — окно пробуждения (будит в лёгкой фазе)</div>
      <div class="seg" id="win-seg">
        <button data-w="0">Выкл (обычный)</button><button data-w="15">15 мин</button>
        <button data-w="20">20 мин</button><button data-w="30">30 мин</button>
      </div>
    </div>

    <div class="btns"><button id="btn-start">🌙 Начать сон</button></div>
    <p class="dim center sm" style="line-height:1.45;margin:12px 4px 0">
      🔌 Телефон на зарядку, на открытую поверхность (не под подушку и одеяло), без блокировки кнопкой и без беззвучного режима.</p>

    <div class="card" style="margin-top:18px">
      <div>Экран ночью</div>
      <div class="seg" id="scr-seg">
        <button data-s="oled" id="scr-oled">iPhone 13 Pro / OLED</button>
        <button data-s="lcd" id="scr-lcd">iPhone 11 / LCD</button>
      </div>
      <div class="dim sm" id="scr-note" style="margin-top:8px"></div>
    </div>
  </div>

  <!-- ночной режим -->
  <div id="night" class="hidden">
    <div class="n-top center">
      <div class="dim">Идёт запись сна</div>
      <div class="card" style="margin-top:16px;text-align:left">
        <div class="row"><span class="dim">Уровень шума</span><b id="n-level">0</b></div>
        <div class="meter"><div id="n-bar"></div></div>
        <div class="row"><span class="dim">Пиков шума</span><b id="n-peaks">0</b></div>
        <div class="row" style="margin-top:6px"><span class="dim">В кровати</span><b id="n-elapsed">0:00</b></div>
      </div>
      <div class="dim sm" id="n-warn"></div>
    </div>
    <div>
      <div class="n-clock" id="n-clock">--:--</div>
      <div class="n-sub" id="n-sub"></div>
    </div>
    <div class="btns">
      <button id="btn-wake">☀️ Я проснулся</button>
      <button class="link" id="btn-cancel">Отменить запись</button>
    </div>
  </div>

  <!-- утренний отчёт -->
  <div id="report" class="hidden">
    <h2 id="r-title">Утренний отчёт</h2>
    <div class="dim sm" id="r-range"></div>
    <div class="card center">
      <div class="dim sm">Оценка ночи</div>
      <div class="big" id="r-score">0</div>
      <div id="r-text" class="dim"></div>
    </div>
    <div class="card hidden" id="r-alarm"></div>
    <div class="grid2">
      <div class="stat"><span>В постели</span><b id="r-tib"></b></div>
      <div class="stat"><span>Время сна</span><b id="r-tst"></b></div>
      <div class="stat"><span>Эффективность сна</span><b id="r-eff"></b></div>
      <div class="stat"><span>Засыпание</span><b id="r-onset"></b></div>
    </div>
    <div class="card">
      <h3>Фазы сна</h3>
      <div id="r-stages"></div>
      <canvas id="c-hyp"></canvas>
    </div>
    <div class="card">
      <h3>Шум и движения</h3>
      <canvas id="c-noise" class="tall"></canvas>
      <div class="legend dim sm">
        <span><i class="dot" style="background:rgba(241,231,211,.4)"></i>макс. шум</span>
        <span><i class="dot" style="background:var(--sage)"></i>средний (усл. дБ)</span>
        <span><i class="dot" style="background:var(--tan)"></i>движение</span>
        <span>● храп · ○ пик шума</span>
      </div>
    </div>
    <div class="card">
      <h3>Храп и беспокойство по отрезкам ночи</h3>
      <div id="r-seg"></div>
    </div>
    <div class="dim sm" id="r-note" style="line-height:1.45;margin:4px 4px 0"></div>
    <div class="btns">
      <button id="btn-close-report">Готово</button>
      <button class="link" id="btn-del">Удалить эту ночь</button>
    </div>
    <div class="syncline center"></div>
  </div>
</div>

<!-- ========== ИСТОРИЯ ========== -->
<div id="view-history" class="hidden">
  <h2>История</h2>
  <div class="syncline"></div>
  <div class="grid2" id="h-stats"></div>
  <div id="h-list"></div>
  <div class="btns2">
    <button class="alt" id="btn-export">Экспорт данных (JSON)</button>
    <button class="alt" id="btn-import">Импорт данных (JSON)</button>
  </div>
  <input type="file" id="file" accept="application/json,.json" class="hidden">
</div>

<div class="tabs" id="tabs">
  <button class="on" data-v="sleep">🌙 Сон</button>
  <button data-v="history">📊 История</button>
</div>

<!-- чек-лист перед сном -->
<div id="modal" class="modal hidden">
  <div class="sheet">
    <h3 style="font-size:21px">Перед сном</h3>
    <ul class="check">
      <li><span>🔌</span><div>Поставьте телефон на зарядку (трекер работает с активным экраном).</div></li>
      <li><span>🛏️</span><div>Положите телефон на край кровати/матраса экраном вверх или вниз на открытую поверхность. Не накрывайте подушкой или одеялом — это защитит от перегрева и позволит датчикам улавливать движения.</div></li>
      <li><span>🔊</span><div>Не блокируйте телефон боковой кнопкой и проверьте, что выключен беззвучный режим и прибавлена громкость.</div></li>
    </ul>
    <button id="btn-ok">Понятно, начать сон</button>
    <button class="link" id="btn-no" style="display:block;margin:6px auto 0">Отмена</button>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const pad = n => String(n).padStart(2, '0');
const fmtT = ts => { const d = new Date(ts); return pad(d.getHours()) + ':' + pad(d.getMinutes()); };
const fmtDur = m => { m = Math.round(m); return Math.floor(m / 60) + ' ч ' + pad(m % 60) + ' м'; };
const MONTHS = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
const DAYS = ['воскресенье','понедельник','вторник','среда','четверг','пятница','суббота'];
const fmtDate = ts => { const d = new Date(ts); return d.getDate() + ' ' + MONTHS[d.getMonth()]; };
const scoreColor = q => q >= 80 ? '#A3B18A' : q >= 60 ? '#C9A27E' : '#D08C60';
const scoreText = q => q >= 80 ? 'Отличный сон' : q >= 60 ? 'Нормальный сон' : 'Сон можно улучшить';

/* ================= АНАЛИТИКА (чистые функции, без DOM) ================= */
/* ANALYTICS-BEGIN */
const EP = 5;   // длина интервала, минут
const STAGE = {
  W: { n: 'Бодрствование', c: '#F1E7D3' },
  L: { n: 'Лёгкий сон',    c: '#C9A27E' },
  D: { n: 'Глубокий сон',  c: '#A3B18A' }
};
const num = (v, d = 0) => { v = Number(v); return Number.isFinite(v) ? v : d; };

/* ночи из первой версии (поминутные данные 0..100) → 5-минутные интервалы */
function legacyToEpochs(mins) {
  const out = [], n = Math.ceil((mins || []).length / EP);
  const rel = v => Math.round(v * 0.7 + 20);
  for (let i = 0; i < n; i++) {
    const g = mins.slice(i * EP, i * EP + EP).filter(Boolean);
    if (!g.length) { out.push(null); continue; }
    out.push({ a: rel(g.reduce((s, m) => s + m.a, 0) / g.length), m: rel(Math.max(...g.map(m => m.m))),
               mv: null, lo: 0, hi: 0, sn: 0, sh: 0, pk: g.reduce((s, m) => s + (m.p || 0), 0) });
  }
  return out;
}
const getEpochs = r => r.epochs ? r.epochs : (r.minutes ? legacyToEpochs(r.minutes) : []);

/* «активность» интервала 0..1: движение (60%) + шум (40%); без датчика — только шум */
function epochScore(e, base) {
  if (!e) return 0.25;
  const peakEx = Math.min(1, Math.max(0, (e.m - base - 6) / 20));
  const evAct = Math.min(1, ((e.pk || 0) + (e.sh || 0)) / 4);
  let noise = Math.min(1, 0.5 * peakEx + 0.5 * evAct);
  if ((e.sn || 0) >= 30) noise *= 0.4;          // храп = человек спит, шум не считаем бодрствованием
  if (e.mv == null) return Math.min(1, noise * 1.2);
  return 0.6 * (e.mv / 100) + 0.4 * noise;
}

function analyze(rec) {
  const E = getEpochs(rec), tib = rec.duration;
  const n = Math.max(E.length, Math.ceil(tib / EP), 1);
  const arr = []; for (let i = 0; i < n; i++) arr.push(E[i] || null);
  const avs = arr.filter(Boolean).map(e => e.a).sort((x, y) => x - y);
  const base = avs.length ? avs[avs.length >> 1] : 0;
  const act = arr.map(e => epochScore(e, base));
  const sm = act.map((v, i) => 0.25 * (i > 0 ? act[i - 1] : v) + 0.5 * v + 0.25 * (i < n - 1 ? act[i + 1] : v));
  const isWake = sm.map((v, i) => v >= 0.38 || act[i] >= 0.75);

  // засыпание = первый интервал, после которого два подряд без бодрствования
  let onset = -1;
  for (let i = 0; i < n; i++) if (!isWake[i] && (i + 1 >= n || !isWake[i + 1])) { onset = i; break; }

  const st = new Array(n).fill('W');
  if (onset >= 0) {
    const idx = []; for (let i = onset; i < n; i++) if (!isWake[i]) idx.push(i);
    idx.forEach(i => st[i] = 'L');
    // глубокий сон: самые «тихие» интервалы (не больше 28% сна), при равенстве — более ранние
    const cand = idx.filter(i => sm[i] < 0.10).sort((a, b) => sm[a] - sm[b] || a - b);
    cand.slice(0, Math.floor(idx.length * 0.28)).forEach(i => st[i] = 'D');
  }

  let w = 0, l = 0, d = 0;
  for (let i = 0; i < n; i++) {
    const m = Math.max(0, Math.min(EP, tib - EP * i));
    if (st[i] === 'W') w += m; else if (st[i] === 'L') l += m; else d += m;
  }
  const tst = l + d, eff = tib ? Math.round(100 * tst / tib) : 0;

  const sleepIdx = []; for (let i = 0; i < n; i++) if (st[i] !== 'W') sleepIdx.push(i);
  const snoreAll = sleepIdx.length ? sleepIdx.reduce((s, i) => s + (arr[i] ? arr[i].sn || 0 : 0), 0) / sleepIdx.length : 0;
  const from = onset >= 0 ? onset : 0;
  const restAll = (n - from) ? act.slice(from).reduce((s, v) => s + v * 100, 0) / (n - from) : 0;
  const peaks = arr.reduce((s, e) => s + (e ? e.pk || 0 : 0), 0);

  // по отрезкам ночи (треть)
  const segN = Math.min(3, n), seg = [];
  for (let s = 0; s < segN; s++) {
    const a = Math.floor(s * n / segN), b = Math.floor((s + 1) * n / segN);
    let sn = 0, sc = 0, rs = 0;
    for (let i = a; i < b; i++) { rs += act[i] * 100; if (st[i] !== 'W') { sn += arr[i] ? arr[i].sn || 0 : 0; sc++; } }
    seg.push({ from: rec.start + a * EP * 60000, to: Math.min(rec.end, rec.start + b * EP * 60000),
               snore: sc ? Math.round(sn / sc) : 0, rest: b > a ? Math.round(rs / (b - a)) : 0 });
  }

  const hours = Math.max(tib / 60, 0.1);
  const durS = Math.min(tst / 480, 1) * 100 - (tst > 600 ? Math.min((tst - 600) / 6, 20) : 0);
  const effS = Math.max(0, Math.min(1, (eff - 65) / 30)) * 100;
  const deepS = Math.min((tst ? 100 * d / tst : 0) / 20, 1) * 100;
  const restS = Math.max(0, 100 - restAll * 1.5);
  const noiseS = Math.max(0, 100 - Math.min(100, (peaks / hours) * 8 + snoreAll * 0.3));
  const score = Math.round(Math.max(0, Math.min(100, 0.30 * durS + 0.25 * effS + 0.15 * deepS + 0.20 * restS + 0.10 * noiseS)));

  return { stages: st.join(''), wake: Math.round(w), light: Math.round(l), deep: Math.round(d), tst: Math.round(tst), eff,
           onsetMin: onset >= 0 ? onset * EP : tib, snore: Math.round(snoreAll), rest: Math.round(restAll), peaks, seg, score };
}

/* проверка и очистка записи (импорт из файла, данные с сервера) */
function sanitizeRec(r) {
  if (!r || typeof r !== 'object') return null;
  const start = num(r.start, NaN), end = num(r.end, NaN);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end - start > 48 * 3600000) return null;
  const hhmm = v => /^\d{1,2}:\d{2}$/.test(String(v)) ? String(v) : '';
  const o = { v: 2, id: Math.round(start), start: Math.round(start), end: Math.round(end), alarm: hhmm(r.alarm) };
  o.duration = Math.max(1, Math.round(num(r.duration, (end - start) / 60000)));
  for (const k of ['quality', 'eff', 'tst', 'wake', 'light', 'deep', 'snore', 'rest', 'peaks'])
    if (r[k] != null) o[k] = Math.round(num(r[k]));
  o.quality = Math.max(0, Math.min(100, o.quality || 0));
  if (typeof r.stages === 'string' && /^[WLD]{0,600}$/.test(r.stages)) o.stages = r.stages;
  o.motion = !!r.motion;
  const ai = r.alarmInfo;
  if (ai && typeof ai === 'object') o.alarmInfo = {
    target: hhmm(ai.target) || o.alarm, targetTs: Math.round(num(ai.targetTs)), windowMin: Math.round(num(ai.windowMin)),
    firedAt: ai.firedAt ? Math.round(num(ai.firedAt)) : null,
    reason: ai.reason === 'smart' ? 'smart' : ai.reason === 'target' ? 'target' : null, early: Math.round(num(ai.early)) };
  if (Array.isArray(r.epochs) && r.epochs.length <= 600) {
    o.epochs = r.epochs.map(e => (e && typeof e === 'object') ? {
      a: num(e.a), m: num(e.m), mv: e.mv == null ? null : num(e.mv), lo: num(e.lo), hi: num(e.hi), sn: num(e.sn), sh: num(e.sh), pk: num(e.pk) } : null);
  } else if (Array.isArray(r.minutes) && r.minutes.length <= 3000) {
    o.minutes = r.minutes.map(m => (m && typeof m === 'object') ? { a: num(m.a), m: num(m.m), p: num(m.p) } : null);
  }
  return o;
}
/* ANALYTICS-END */

const AM = new Map();   // кэш расчётов для списка истории
function sumOf(n) {
  if (n.epochs || n.minutes) {
    const key = n.id + ':' + (n.epochs ? n.epochs.length : 'm' + n.minutes.length);
    let a = AM.get(key); if (!a) { a = analyze(n); AM.set(key, a); }
    return { score: a.score, eff: a.eff, tst: a.tst, stages: a.stages };
  }
  return { score: n.quality || 0, eff: n.eff == null ? null : n.eff, tst: n.tst != null ? n.tst : n.duration, stages: n.stages || '' };
}

/* ================= localStorage (главное хранилище на телефоне) ================= */
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} }
};
const getNights = () => LS.get('nights', []);
const getDeleted = () => LS.get('deleted', []);   // «надгробия»: удалено у себя, сервер ещё не знает
function addNight(n) {
  const a = getNights().filter(x => x.id !== n.id); a.push(n);
  if (!LS.set('nights', a)) alert('Не удалось записать ночь в память телефона (localStorage переполнен). Сделайте экспорт данных.');
}

/* ================= сервер + синхронизация ================= */
async function api(url, opt = {}, ms = 30000) {
  const c = new AbortController(), t = setTimeout(() => c.abort(), ms);
  try {
    const r = await fetch(url, {
      method: opt.method || 'GET', headers: { 'Content-Type': 'application/json' },
      body: opt.body ? JSON.stringify(opt.body) : undefined, signal: c.signal });
    if (!r.ok) throw new Error('HTTP ' + r.status);   // 502/503 — Render ещё просыпается
    return await r.json();
  } finally { clearTimeout(t); }
}
const setSync = t => document.querySelectorAll('.syncline').forEach(e => e.textContent = t);
const lite = n => { const { epochs, minutes, ...rest } = n; return rest; };

let serverList = [], serverLoaded = false, syncing = false, retryTimer = null, curView = 'sleep';
const RETRY = [3, 5, 10, 15, 20, 30, 30, 60, 60, 60];

/* Сверка: сервер → список; чего нет на сервере — досылаем; удалённое — удаляем;
   чего нет на телефоне — подтягиваем (так localStorage становится полной резервной копией). */
async function syncAll(attempt = 0) {
  if (syncing || S) return;
  clearTimeout(retryTimer);
  syncing = true; setSync('⏳ Синхронизация с сервером…');
  try {
    const srv = await api('/api/history');
    const dels = getDeleted();
    for (const id of dels) await api('/api/night/' + id, { method: 'DELETE' });
    if (dels.length) LS.set('deleted', getDeleted().filter(id => !dels.includes(id)));
    const have = new Set(srv.sessions.map(s => s.id));
    serverList = srv.sessions.filter(s => !dels.includes(s.id));
    const local = getNights(), localIds = new Set(local.map(n => n.id));
    for (const n of local) {
      if (!have.has(n.id) && !dels.includes(n.id)) {
        await api('/api/save', { method: 'POST', body: n });
        serverList.push(lite(n));
      }
    }
    for (const s of serverList) {                     // резервная копия на телефон
      if (S) break;
      if (localIds.has(s.id) || dels.includes(s.id)) continue;
      try { const full = sanitizeRec(await api('/api/night/' + s.id)); if (full) { addNight(full); localIds.add(full.id); } }
      catch (e) { break; }
    }
    serverLoaded = true; syncing = false;
    setSync('☁️ Всё сохранено на сервере');
  } catch (e) {
    syncing = false;
    if (attempt < RETRY.length) {
      const d = RETRY[attempt];
      setSync('⏳ Сервер просыпается, повторю через ' + d + ' с…');
      retryTimer = setTimeout(() => syncAll(attempt + 1), d * 1000);
    } else setSync('⚠️ Нет связи с сервером. Ночи хранятся в телефоне, отправлю позже.');
  }
  if (curView === 'history') renderHistory();
}
window.addEventListener('online', () => syncAll());

/* ================= настройки ================= */
const AUTO_OLED = (window.devicePixelRatio || 1) >= 3;   // iPhone 13 Pro = 3x (OLED), iPhone 11 = 2x (LCD)
function screenType() { const v = localStorage.getItem('screenType'); return (v === 'oled' || v === 'lcd') ? v : (AUTO_OLED ? 'oled' : 'lcd'); }
function winMin() { const v = Number(localStorage.getItem('winMin')); return [0, 15, 20, 30].includes(v) ? v : 0; }
function renderSettings() {
  const t = screenType();
  $('#scr-oled').classList.toggle('on', t === 'oled'); $('#scr-lcd').classList.toggle('on', t === 'lcd');
  $('#scr-note').textContent = localStorage.getItem('screenType') ? '' : 'Определено автоматически';
  document.querySelectorAll('#win-seg button').forEach(b => b.classList.toggle('on', Number(b.dataset.w) === winMin()));
  updateAlarmLeft();
}
document.querySelectorAll('#scr-seg button').forEach(b => b.onclick = () => { localStorage.setItem('screenType', b.dataset.s); renderSettings(); });
document.querySelectorAll('#win-seg button').forEach(b => b.onclick = () => { localStorage.setItem('winMin', b.dataset.w); renderSettings(); });

/* ================= часы ================= */
function nextAlarmTs(str) {
  const [h, m] = str.split(':').map(Number);
  const d = new Date(); d.setHours(h, m, 0, 0);
  if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
  return d.getTime();
}
function updateAlarmLeft() {
  const ts = nextAlarmTs($('#alarm').value), left = Math.round((ts - Date.now()) / 60000), w = winMin();
  $('#alarm-left').textContent = 'через ' + Math.floor(left / 60) + ' ч ' + (left % 60) + ' мин' + (w ? ' · окно с ' + fmtT(ts - w * 60000) : '');
}
function updateClock() {
  if (S) return;   // ночью экран не перерисовываем
  const d = new Date();
  $('#clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
  $('#secs').textContent = pad(d.getSeconds());
  $('#date').textContent = DAYS[d.getDay()] + ', ' + d.getDate() + ' ' + MONTHS[d.getMonth()];
  updateAlarmLeft();
}
$('#alarm').value = localStorage.getItem('alarm') || '07:00';
$('#alarm').addEventListener('change', e => { localStorage.setItem('alarm', e.target.value); updateAlarmLeft(); });

/* ================= навигация ================= */
function showView(v) {
  curView = v;
  $('#view-sleep').classList.toggle('hidden', v !== 'sleep');
  $('#view-history').classList.toggle('hidden', v !== 'history');
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  if (v === 'history') { renderHistory(); syncAll(); }
}
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => showView(b.dataset.v));
function pane(name) {
  for (const id of ['idle', 'night', 'report']) $('#' + id).classList.toggle('hidden', id !== name);
  $('#tabs').classList.toggle('hidden', name === 'night');
  document.body.classList.toggle('inNight', name === 'night');
}

/* ================= ночной режим ================= */
const TICK_MS = 330;                                  // ~3 раза в секунду вместо 60 FPS
let S = null;                                         // состояние ночи (дублируется в localStorage)
let stream, actx, analyser, tbuf, fbuf, B, tickTimer, pingTimer, dimTimer, touchT;
let base = null, lastPeak = 0, lastSharp = 0, lastMot = 0, evs = [], tickN = 0, lastMin = -1, dimmed = false;
let ringing = false, master = null, melTimer = null;
const mot = { max: 0, n: 0, ema: null, last: 0 };

function saveState() { if (S) LS.set('sleepState', S); }

/* движение: DeviceMotion, ~10 выборок/с, отклонение ускорения от скользящего среднего + вращение */
function onMotion(e) {
  const t = performance.now();
  if (t - mot.last < 100) return;
  mot.last = t;
  const a = e.accelerationIncludingGravity;
  if (!a || a.x == null) return;
  const mag = Math.hypot(a.x, a.y, a.z);
  if (mot.ema === null) mot.ema = mag;
  let v = Math.max(0, Math.abs(mag - mot.ema) - 0.004);   // 0.004 — шум датчика
  mot.ema += (mag - mot.ema) * 0.1;
  const r = e.rotationRate;
  if (r && r.alpha != null) v += 0.002 * (Math.abs(r.alpha) + Math.abs(r.beta) + Math.abs(r.gamma));
  mot.n++; if (v > mot.max) mot.max = v;
}

/* удержание экрана: Wake Lock → запасной способ (скрытое видео) → подсказка про автоблокировку */
let wl = null, awakeMode = 'none', nsVideo = null, nsTimer = null;
async function keepAwake() {
  try {
    if ('wakeLock' in navigator) {
      wl = await navigator.wakeLock.request('screen'); awakeMode = 'wakelock';
      wl.addEventListener('release', () => { wl = null; });
      return;
    }
  } catch (e) {}
  try {
    const c = document.createElement('canvas'); c.width = c.height = 2;
    if (c.captureStream) {
      const g = c.getContext('2d'), v = document.createElement('video');
      v.setAttribute('playsinline', ''); v.muted = true; v.loop = true;
      v.style.cssText = 'position:fixed;width:1px;height:1px;opacity:.01;pointer-events:none';
      v.srcObject = c.captureStream(1); document.body.appendChild(v);
      g.fillStyle = '#000'; g.fillRect(0, 0, 2, 2);
      nsTimer = setInterval(() => g.fillRect(0, 0, 2, 2), 2000);
      await v.play(); nsVideo = v; awakeMode = 'video'; return;
    }
  } catch (e) {}
  awakeMode = 'none';
  $('#n-warn').textContent = 'Браузер не умеет удерживать экран включённым: в Настройки → Экран и яркость → Автоблокировка выберите «Никогда».';
}
function releaseAwake() {
  if (wl) wl.release().catch(() => {}); wl = null;
  clearInterval(nsTimer); if (nsVideo) { nsVideo.pause(); nsVideo.remove(); nsVideo = null; }
}

/* ВАЖНО: AudioContext создаётся синхронно внутри нажатия кнопки (требование iOS Safari) */
async function beginSleep(saved) {
  actx = new (window.AudioContext || window.webkitAudioContext)();
  if (actx.resume) actx.resume();

  let motionOk = false;                               // разрешение на датчики — тоже строго по нажатию
  try {
    if (typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function')
      motionOk = (await DeviceMotionEvent.requestPermission()) === 'granted';
    else motionOk = typeof DeviceMotionEvent !== 'undefined';
  } catch (e) { motionOk = false; }

  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
  } catch (e) { alert('Нет доступа к микрофону: ' + e.message + '\nРазрешите микрофон для сайта и откройте приложение по HTTPS.'); actx.close(); return; }

  const src = actx.createMediaStreamSource(stream);
  analyser = actx.createAnalyser(); analyser.fftSize = 2048; analyser.smoothingTimeConstant = 0;
  const mute = actx.createGain(); mute.gain.value = 0;   // Safari считает звук, только если узел подключён к выходу
  src.connect(analyser); analyser.connect(mute); mute.connect(actx.destination);
  tbuf = new Float32Array(analyser.fftSize); fbuf = new Float32Array(analyser.frequencyBinCount);
  const hz = actx.sampleRate / analyser.fftSize;
  B = { l0: Math.max(1, Math.round(50 / hz)), l1: Math.round(500 / hz), h0: Math.round(2000 / hz), h1: Math.min(fbuf.length - 1, Math.round(8000 / hz)) };

  if (saved) { S = saved; S.cur = S.cur || null; }
  else {
    const a = $('#alarm').value;
    S = { start: Date.now(), alarmStr: a, alarmTs: nextAlarmTs(a), windowMin: winMin(), epochs: [], cur: null, peaks: 0,
          motion: motionOk, screen: screenType(), ringAt: null, ringReason: null };
  }
  if (motionOk) { mot.ema = null; mot.max = 0; mot.n = 0; window.addEventListener('devicemotion', onMotion); }
  else S.motion = false;

  base = null; evs = []; tickN = 0; lastMin = -1; ringing = false; dimmed = false;
  const nt = $('#night'); nt.className = ''; nt.style.cssText = '';
  $('#n-warn').textContent = '';
  $('#n-sub').textContent = 'Будильник ' + S.alarmStr + (S.windowMin ? ' · окно ' + S.windowMin + ' мин' : '');
  $('#n-peaks').textContent = S.peaks;
  $('#n-clock').textContent = fmtT(Date.now());
  pane('night');
  keepAwake();
  tickTimer = setInterval(tick, TICK_MS);
  dimTimer = setTimeout(goDim, 10000);                // через 10 с — энергосберегающий режим
  fetch('/healthz').catch(() => {});                  // раз в 10 минут «будим» бесплатный сервер
  pingTimer = setInterval(() => fetch('/healthz').catch(() => {}), 600000);
  saveState();
}

function goDim() {
  dimmed = true;
  const nt = $('#night'); nt.classList.add('dim', S && S.screen ? S.screen : screenType());
}
function shiftClock() {   // защита OLED от выгорания: сдвиг часов на 1–3 px раз в 3 минуты, без анимации
  const r = () => (Math.random() < 0.5 ? -1 : 1) * (1 + Math.floor(Math.random() * 3));
  $('#n-clock').style.transform = 'translate(' + r() + 'px,' + r() + 'px)';
}
$('#night').addEventListener('pointerdown', () => {
  const el = $('#night'); el.classList.add('touch');
  clearTimeout(touchT); touchT = setTimeout(() => el.classList.remove('touch'), 6000);
});

function closeBucket() {
  const c = S && S.cur; if (!c || !c.n) { if (S) S.cur = null; return; }
  let mv = null;
  if (c.mvN > 0) mv = Math.round(100 * (0.4 * Math.min(c.mvS / c.mvN / 0.01, 1) + 0.6 * Math.min(c.mvX / 0.12, 1)));
  S.epochs[c.idx] = { a: Math.round(c.sa / c.n), m: Math.round(c.mx), mv,
    lo: Math.round(100 * c.slo / c.n), hi: Math.round(100 * c.shi / c.n),
    sn: Math.round(100 * c.sn / c.n), sh: c.sh, pk: c.pk };
  S.cur = null;
}

function tick() {
  const now = Date.now();
  analyser.getFloatTimeDomainData(tbuf);
  let s = 0; for (let i = 0; i < tbuf.length; i++) s += tbuf[i] * tbuf[i];
  const dbfs = 20 * Math.log10(Math.sqrt(s / tbuf.length) + 1e-7);
  const level = Math.max(0, Math.min(90, dbfs + 90));     // условные дБ (не калиброваны): 0..90

  // частотный профиль: низкие 50–500 Гц (дыхание/храп) и высокие 2–8 кГц (резкие бытовые звуки)
  analyser.getFloatFrequencyData(fbuf);
  let lo = 0, hi = 0, tot = 0;
  for (let i = 1; i < fbuf.length; i++) {
    const p = Math.pow(10, fbuf[i] / 10); tot += p;
    if (i >= B.l0 && i <= B.l1) lo += p; else if (i >= B.h0 && i <= B.h1) hi += p;
  }
  const loR = tot > 0 ? lo / tot : 0, hiR = tot > 0 ? hi / tot : 0;

  if (base === null) base = level;
  let isPeak = false, isSharp = false, isSnore = false;
  if (level > base + 9 && level > 34 && now - lastPeak > 10000) { isPeak = true; lastPeak = now; }
  if (hiR >= 0.35 && level > base + 9 && now - lastSharp > 5000) { isSharp = true; lastSharp = now; }
  if (level > base + 4 && level > 30 && loR >= 0.55 && hiR <= 0.12) isSnore = true;
  base += (level - base) * 0.007;

  let mvT = null;
  if (mot.n > 0) { mvT = mot.max; mot.max = 0; mot.n = 0; }
  let isMove = false;
  if (mvT !== null && mvT > 0.05 && now - lastMot > 8000) { isMove = true; lastMot = now; }

  // 5-минутные интервалы
  const idx = Math.floor((now - S.start) / (EP * 60000));
  let c = S.cur;
  if (!c || c.idx !== idx) {
    closeBucket(); saveState();
    c = S.cur = { idx, n: 0, sa: 0, mx: 0, slo: 0, shi: 0, sn: 0, sh: 0, pk: 0, mvS: 0, mvN: 0, mvX: 0 };
  }
  c.n++; c.sa += level; if (level > c.mx) c.mx = level; c.slo += loR; c.shi += hiR;
  if (isSnore) c.sn++; if (isSharp) c.sh++;
  if (isPeak) { c.pk++; S.peaks++; }
  if (mvT !== null) { c.mvS += mvT; c.mvN++; if (mvT > c.mvX) c.mvX = mvT; }
  if (++tickN % 180 === 0 || isPeak) saveState();

  // умный будильник: серия событий (шум/движение) за 3 минуты = поверхностная фаза сна
  if ((isPeak || isSharp || isMove) && now - (evs[evs.length - 1] || 0) > 5000) evs.push(now);
  while (evs.length && now - evs[0] > 180000) evs.shift();
  if (!ringing) {
    if (now >= S.alarmTs) startRing('target');                       // гарантированно в целевое время
    else if (S.windowMin > 0 && now >= S.alarmTs - S.windowMin * 60000 && now - S.start > 20 * 60000 && evs.length >= 3) startRing('smart');
  }

  // экран: до перехода в ночной режим — живые цифры; после — только часы раз в минуту
  if (!dimmed) {
    $('#n-level').textContent = Math.round(level) + ' дБ';
    $('#n-bar').style.width = (level / 90 * 100) + '%';
    $('#n-peaks').textContent = S.peaks;
    const el = Math.floor((now - S.start) / 60000);
    $('#n-elapsed').textContent = Math.floor(el / 60) + ':' + pad(el % 60);
  }
  const mn = Math.floor(now / 60000);
  if (mn !== lastMin) { lastMin = mn; $('#n-clock').textContent = fmtT(now); if (dimmed && mn % 3 === 0) shiftClock(); }
}

/* мелодия с плавно нарастающей громкостью */
const NOTES = [523.25, 659.25, 783.99, 1046.5, 783.99, 659.25];
function playBar() {
  const t0 = actx.currentTime + 0.05;
  NOTES.forEach((f, i) => {
    const o = actx.createOscillator(), g = actx.createGain(), t = t0 + i * 0.4;
    o.type = 'triangle'; o.frequency.value = f;
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(0.5, t + 0.04); g.gain.exponentialRampToValueAtTime(0.001, t + 0.38);
    o.connect(g); g.connect(master); o.start(t); o.stop(t + 0.4);
  });
}
function startRing(reason) {
  ringing = true;
  if (!S.ringAt) { S.ringAt = Date.now(); S.ringReason = reason; }
  saveState();
  $('#night').classList.add('ringing');
  $('#n-sub').textContent = 'Пора вставать — нажмите «Я проснулся»';
  master = actx.createGain();
  const t = actx.currentTime;
  master.gain.setValueAtTime(0.02, t); master.gain.linearRampToValueAtTime(0.9, t + 45);
  master.connect(actx.destination);
  playBar(); melTimer = setInterval(playBar, 2600);
  if (navigator.vibrate) navigator.vibrate([400, 200, 400]);
}
function stopRing() {
  if (!ringing) return;
  clearInterval(melTimer); try { master.disconnect(); } catch (e) {}
  ringing = false;
}

function stopCapture() {
  stopRing(); clearInterval(tickTimer); clearInterval(pingTimer); clearTimeout(dimTimer);
  window.removeEventListener('devicemotion', onMotion);
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (actx) actx.close();
  releaseAwake();
  $('#n-clock').style.transform = '';
}

async function finish() {
  closeBucket(); stopCapture();
  const end = Date.now();
  if (end - S.start < 120000) {
    alert('Запись слишком короткая (меньше 2 минут), не сохраняю.');
    S = null; LS.del('sleepState'); pane('idle'); return;
  }
  const duration = Math.max(1, Math.round((end - S.start) / 60000));
  const nEp = Math.ceil(duration / EP), epochs = [];
  for (let i = 0; i < nEp; i++) epochs.push(S.epochs[i] || null);
  const fired = S.ringAt || null;
  const rec = {
    v: 2, id: S.start, start: S.start, end, alarm: S.alarmStr, duration, epochs, motion: !!S.motion,
    alarmInfo: { target: S.alarmStr, targetTs: S.alarmTs, windowMin: S.windowMin, firedAt: fired, reason: fired ? S.ringReason : null,
                 early: fired ? Math.max(0, Math.round((S.alarmTs - fired) / 60000)) : 0 }
  };
  const an = analyze(rec);
  Object.assign(rec, { quality: an.score, eff: an.eff, tst: an.tst, wake: an.wake, light: an.light, deep: an.deep,
                       snore: an.snore, rest: an.rest, peaks: an.peaks, stages: an.stages });
  addNight(rec);            // 1) сначала в память телефона
  LS.del('sleepState');
  S = null;
  showReport(rec);          // 2) отчёт показываем сразу, не ждём сервер
  syncAll();                // 3) отправка с повторами, пока бесплатный сервер просыпается
}

/* кнопки главного экрана */
$('#btn-start').onclick = () => $('#modal').classList.remove('hidden');
$('#btn-no').onclick = () => $('#modal').classList.add('hidden');
$('#btn-ok').onclick = () => { $('#modal').classList.add('hidden'); beginSleep(null); };   // сон стартует только после подтверждения
$('#btn-wake').onclick = finish;
$('#btn-cancel').onclick = () => {
  if (!confirm('Отменить запись без сохранения?')) return;
  stopCapture(); S = null; LS.del('sleepState'); pane('idle');
};
$('#btn-resume').onclick = () => { const s = LS.get('sleepState', null); if (s) beginSleep(s); };
$('#btn-drop').onclick = () => { LS.del('sleepState'); $('#resume').classList.add('hidden'); };
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState !== 'visible') return;
  if (S) { if (awakeMode === 'wakelock' && !wl) keepAwake(); }
  else syncAll();
});

/* ================= утренний отчёт ================= */
let curReportId = null;
function showReport(r) {
  curReportId = r.id;
  showView('sleep'); pane('report');
  const an = analyze(r);
  $('#r-title').textContent = 'Ночь на ' + fmtDate(r.end);
  $('#r-range').textContent = fmtT(r.start) + ' → ' + fmtT(r.end);
  $('#r-score').textContent = an.score; $('#r-score').style.color = scoreColor(an.score);
  $('#r-text').textContent = scoreText(an.score);
  $('#r-tib').textContent = fmtDur(r.duration); $('#r-tst').textContent = fmtDur(an.tst);
  $('#r-eff').textContent = an.eff + '%'; $('#r-onset').textContent = Math.round(an.onsetMin) + ' мин';

  // будильник
  const ai = r.alarmInfo, box = $('#r-alarm');
  let txt = '';
  if (ai && ai.firedAt) {
    txt = 'Будильник сработал в ' + fmtT(ai.firedAt) + ' — ' +
      (ai.reason === 'smart' && ai.early > 0 ? 'на ' + ai.early + ' мин раньше цели (' + ai.target + '), в лёгкой фазе сна'
                                              : 'ровно в целевое время (' + ai.target + ')');
  } else if (ai) txt = 'Вы проснулись раньше будильника (цель ' + ai.target + ')';
  else if (r.alarm) txt = 'Будильник: ' + r.alarm;
  box.textContent = '⏰ ' + txt; box.classList.toggle('hidden', !txt);

  // фазы
  const tib = Math.max(r.duration, 1);
  $('#r-stages').innerHTML = [['W', an.wake], ['L', an.light], ['D', an.deep]].map(([k, m]) => {
    const p = Math.round(100 * m / tib);
    return '<div class="srow"><span>' + STAGE[k].n + '</span><div class="bar"><i style="width:' + p + '%;background:' + STAGE[k].c + '"></i></div><b>' + fmtDur(m) + ' · ' + p + '%</b></div>';
  }).join('');

  // по отрезкам ночи
  $('#r-seg').innerHTML = '<div class="dim sm" style="margin-bottom:8px">Общий индекс храпа: ' + an.snore + ' · беспокойства: ' + an.rest + ' (0–100)</div>' +
    an.seg.map(g => '<div style="margin:10px 0"><div class="dim sm">' + fmtT(g.from) + '–' + fmtT(g.to) + '</div>' +
      '<div class="srow"><span>Храп</span><div class="bar"><i style="width:' + Math.min(100, g.snore) + '%;background:var(--sage)"></i></div><b>' + g.snore + '</b></div>' +
      '<div class="srow"><span>Беспокойство</span><div class="bar"><i style="width:' + Math.min(100, g.rest) + '%;background:var(--tan)"></i></div><b>' + g.rest + '</b></div></div>').join('');

  $('#r-note').textContent = (r.v === 2 && !r.motion ? 'Датчик движения недоступен или не разрешён: фазы определены только по шуму, точность ниже. ' : '') +
    'Фазы сна — оценка по шуму и движению телефона, а не медицинское измерение. Уровень шума в условных дБ (не калиброван).';

  drawHypno($('#c-hyp'), an.stages, r.start, r.duration);
  drawNoise($('#c-noise'), getEpochs(r), r.start, !(r.v === 2 && !r.motion));
}
$('#btn-close-report').onclick = () => pane('idle');
$('#btn-del').onclick = () => {
  if (!confirm('Удалить эту ночь из истории?')) return;
  const id = curReportId;
  LS.set('nights', getNights().filter(n => n.id !== id));
  const d = getDeleted(); if (!d.includes(id)) { d.push(id); LS.set('deleted', d); }
  serverList = serverList.filter(s => s.id !== id);
  pane('idle'); showView('history');   // showView запустит синхронизацию: удаление уйдёт на сервер
};

/* ================= графики (рисуются только утром) ================= */
function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.font = '11px -apple-system,system-ui,sans-serif';
  return { g, w, h };
}
function timeLabels(g, n, x, startTs, w, h) {
  const total = n * EP, step = total > 360 ? 120 : total > 150 ? 60 : 30;
  g.fillStyle = 'rgba(241,231,211,.62)';
  for (let t = 0; t <= total; t += step) g.fillText(fmtT(startTs + t * 60000), Math.min(x(t / EP) - 14, w - 32), h - 5);
}
function drawHypno(cv, stages, startTs) {
  const { g, w, h } = setupCanvas(cv), n = Math.max(stages.length, 1);
  const padL = 70, padR = 6, padT = 4, padB = 20, cw = w - padL - padR, rowH = (h - padT - padB) / 3;
  const x = i => padL + cw * i / n, rows = { W: 0, L: 1, D: 2 };
  ['Бодрств.', 'Лёгкий', 'Глубокий'].forEach((t, i) => {
    g.fillStyle = 'rgba(241,231,211,.62)'; g.fillText(t, 2, padT + rowH * i + rowH / 2 + 4);
    g.strokeStyle = 'rgba(241,231,211,.12)'; g.beginPath(); g.moveTo(padL, padT + rowH * (i + 1)); g.lineTo(w - padR, padT + rowH * (i + 1)); g.stroke();
  });
  for (let i = 0; i < stages.length; i++) {
    const k = stages[i]; g.fillStyle = STAGE[k].c;
    g.fillRect(x(i), padT + rows[k] * rowH + 4, Math.max(cw / n - 0.5, 1), rowH - 8);
  }
  g.strokeStyle = 'rgba(241,231,211,.5)'; g.lineWidth = 1.2; g.beginPath();
  for (let i = 0; i < stages.length; i++) {
    const yy = padT + rows[stages[i]] * rowH + rowH / 2;
    if (i === 0) g.moveTo(x(i), yy); else g.lineTo(x(i), yy);
    g.lineTo(x(i + 1), yy);
  }
  g.stroke();
  timeLabels(g, n, x, startTs, w, h);
}
function drawNoise(cv, E, startTs, hasMotion) {
  const { g, w, h } = setupCanvas(cv), n = Math.max(E.length, 1);
  const padL = 30, padR = 4, padB = 20, cw = w - padL - padR;
  const aT = 8, aH = (h - padB) * 0.6, bT = aT + aH + 14, bH = h - padB - bT;   // панель шума и панель движения
  const x = i => padL + cw * i / n, bw = Math.max(cw / n - 1, 1);
  const yA = v => aT + aH * (1 - v / 90);
  g.lineWidth = 1;
  for (const v of [0, 45, 90]) {
    g.strokeStyle = 'rgba(241,231,211,.12)'; g.beginPath(); g.moveTo(padL, yA(v)); g.lineTo(w - padR, yA(v)); g.stroke();
    g.fillStyle = 'rgba(241,231,211,.62)'; g.fillText(v, 2, yA(v) + 4);
  }
  g.fillStyle = 'rgba(241,231,211,.4)';
  E.forEach((e, i) => { if (e) { const yy = yA(e.m); g.fillRect(x(i), yy, bw, yA(0) - yy); } });
  g.strokeStyle = '#A3B18A'; g.lineWidth = 2; g.beginPath();
  let st = false;
  E.forEach((e, i) => { if (!e) { st = false; return; } const px = x(i) + bw / 2; if (!st) { g.moveTo(px, yA(e.a)); st = true; } else g.lineTo(px, yA(e.a)); });
  g.stroke();
  E.forEach((e, i) => {
    if (!e) return; const px = x(i) + bw / 2;
    if ((e.sn || 0) >= 25) { g.fillStyle = '#A3B18A'; g.beginPath(); g.arc(px, aT + 5, 3.5, 0, 7); g.fill(); }
    if ((e.pk || 0) > 0) { g.strokeStyle = '#F1E7D3'; g.lineWidth = 1.5; g.beginPath(); g.arc(px, aT + 15, 3, 0, 7); g.stroke(); }
  });
  // движение
  g.fillStyle = 'rgba(241,231,211,.62)'; g.fillText('движ.', 0, bT + 10);
  g.strokeStyle = 'rgba(241,231,211,.12)'; g.lineWidth = 1; g.beginPath(); g.moveTo(padL, bT + bH); g.lineTo(w - padR, bT + bH); g.stroke();
  if (hasMotion) {
    g.fillStyle = '#C9A27E';
    E.forEach((e, i) => { if (e && e.mv != null) { const hh = Math.max(1, bH * e.mv / 100); g.fillRect(x(i), bT + bH - hh, bw, hh); } });
  } else { g.fillStyle = 'rgba(241,231,211,.4)'; g.fillText('нет данных датчика', padL + 6, bT + bH / 2 + 4); }
  timeLabels(g, n, x, startTs, w, h);
}

/* ================= история: телефон + сервер без дублей (id = время старта) ================= */
function merged() {
  const dead = new Set(getDeleted()), m = new Map();
  serverList.forEach(s => { if (!dead.has(s.id)) m.set(s.id, s); });
  getNights().forEach(n => { if (!dead.has(n.id)) m.set(n.id, n); });   // локальная копия главнее (в ней есть графики)
  return [...m.values()].sort((a, b) => b.start - a.start);
}
function stripHtml(stages) {
  if (!stages) return '';
  let out = '', i = 0;
  while (i < stages.length) { let j = i; while (j < stages.length && stages[j] === stages[i]) j++; out += '<i style="flex:' + (j - i) + ';background:' + STAGE[stages[i]].c + '"></i>'; i = j; }
  return '<div class="strip">' + out + '</div>';
}
function renderHistory() {
  const list = merged(), n = list.length, sums = list.map(sumOf);
  const avg = f => n ? Math.round(sums.reduce((a, s) => a + f(s), 0) / n) : 0;
  const withEff = sums.filter(s => s.eff != null), avgEff = withEff.length ? Math.round(withEff.reduce((a, s) => a + s.eff, 0) / withEff.length) : null;
  const avgQ = avg(s => s.score), onSrv = new Set(serverList.map(s => s.id));
  $('#h-stats').innerHTML = n ?
    '<div class="stat"><span>Ср. время сна</span><b>' + fmtDur(avg(s => s.tst)) + '</b></div>' +
    '<div class="stat"><span>Ср. оценка</span><b style="color:' + scoreColor(avgQ) + '">' + avgQ + '</b></div>' +
    '<div class="stat"><span>Ср. эффективность</span><b>' + (avgEff == null ? '—' : avgEff + '%') + '</b></div>' +
    '<div class="stat"><span>Ночей</span><b>' + n + '</b></div>' : '';
  $('#h-list').innerHTML = n ? list.map((s, i) => {
    const u = sums[i];
    return '<div class="item" data-id="' + s.id + '"><div class="row"><div><b>' + fmtDate(s.end) + '</b>' +
      '<div class="dim sm">' + fmtT(s.start) + ' → ' + fmtT(s.end) + ' · сон ' + fmtDur(u.tst) + (u.eff != null ? ' · эфф. ' + u.eff + '%' : '') +
      (serverLoaded && !onSrv.has(s.id) ? ' · ⏳' : '') + '</div></div>' +
      '<span class="badge" style="color:' + scoreColor(u.score) + '">' + u.score + '</span></div>' + stripHtml(u.stages) + '</div>';
  }).join('') : '<p class="dim center">Пока нет записанных ночей</p>';
  document.querySelectorAll('.item').forEach(el => el.onclick = () => openNight(Number(el.dataset.id)));
}
async function openNight(id) {
  let n = merged().find(x => x.id === id);
  if (!n) return;
  if (!n.epochs && !n.minutes) {   // ночь есть только на сервере — подгружаем интервалы
    try { n = sanitizeRec(await api('/api/night/' + id)); if (!n) throw 0; addNight(n); }
    catch (e) { alert('Сервер ещё просыпается, попробуйте через минуту.'); return; }
  }
  showReport(n);
}

/* ================= экспорт / импорт JSON ================= */
$('#btn-export').onclick = () => {
  const nights = getNights();
  if (!nights.length) { alert('Пока нечего экспортировать.'); return; }
  const text = JSON.stringify({ app: 'sleep-tracker', version: 2, exported: new Date().toISOString(), nights });
  const name = 'sleep-backup-' + new Date().toISOString().slice(0, 10) + '.json';
  const file = new File([text], name, { type: 'application/json' });
  if (navigator.canShare && navigator.canShare({ files: [file] })) { navigator.share({ files: [file], title: name }).catch(() => {}); return; }
  const url = URL.createObjectURL(file), a = document.createElement('a');
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
};
$('#btn-import').onclick = () => $('#file').click();
$('#file').onchange = async e => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  if (f.size > 20 * 1024 * 1024) { alert('Файл слишком большой.'); return; }
  let data; try { data = JSON.parse(await f.text()); } catch (err) { alert('Не удалось прочитать файл: это не JSON.'); return; }
  const arr = Array.isArray(data) ? data : (data && Array.isArray(data.nights) ? data.nights : null);
  if (!arr) { alert('В файле нет списка ночей.'); return; }
  const cur = new Map(getNights().map(n => [n.id, n]));
  let added = 0, bad = 0;
  for (const raw of arr) {
    const r = sanitizeRec(raw);
    if (!r) { bad++; continue; }
    const old = cur.get(r.id);
    if (!old) { cur.set(r.id, r); added++; }
    else if (!old.epochs && !old.minutes && (r.epochs || r.minutes)) cur.set(r.id, r);   // дополняем данными
  }
  const ids = new Set([...cur.keys()]);
  LS.set('nights', [...cur.values()]);
  LS.set('deleted', getDeleted().filter(id => !ids.has(id)));
  alert('Импорт завершён. Новых ночей: ' + added + (bad ? ', пропущено повреждённых: ' + bad : '') + '.');
  renderHistory(); syncAll();
};

/* ================= старт ================= */
setInterval(updateClock, 1000); updateClock(); renderSettings();
(function () {
  const s = LS.get('sleepState', null);
  if (s && s.start) { $('#resume-from').textContent = fmtT(s.start); $('#resume').classList.remove('hidden'); }
})();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
syncAll();   // досылаем недостающие ночи и подтягиваем сервер
</script>
</body></html>
"""


# ───────────────────────────── Запуск ─────────────────────────────
# Таблицы создаются при импорте модуля: работает и с `gunicorn app:app`, и с `python app.py`.
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
