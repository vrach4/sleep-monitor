#!/usr/bin/env python3
"""
Sleep Tracker — простой трекер сна в стиле Sleep Monitor.
Flask + SQLite, один файл. Готов к деплою на Render (gunicorn app:app).

Локально:  pip install -r requirements.txt && python app.py   (http://localhost:5000)
Render:    Build: pip install -r requirements.txt
           Start: gunicorn app:app

Данные хранятся в двух местах: в localStorage телефона (главный источник)
и в SQLite на сервере (копия). Бесплатный Render может обнулить диск при
перезапуске, поэтому телефон сам досылает недостающие ночи на сервер.
"""
import json
import os
import sqlite3
from contextlib import closing

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # защита от слишком больших запросов
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
    # id ночи = время начала сна (мс). Повторная отправка одной ночи ничего не дублирует.
    q(
        """CREATE TABLE IF NOT EXISTS nights(
            start_ts INTEGER PRIMARY KEY,
            end_ts INTEGER NOT NULL,
            alarm TEXT,
            duration_min INTEGER NOT NULL,
            quality INTEGER NOT NULL,
            peaks INTEGER NOT NULL,
            data TEXT NOT NULL            -- JSON: поминутные {a, m, p}
        )""",
        commit=True,
    )


def row_to_dict(r, full=False):
    d = {
        "id": r["start_ts"],
        "start": r["start_ts"],
        "end": r["end_ts"],
        "alarm": r["alarm"],
        "duration": r["duration_min"],
        "quality": r["quality"],
        "peaks": r["peaks"],
    }
    if full:
        d["minutes"] = json.loads(r["data"])
    return d


# ───────────────────────────── API ─────────────────────────────
@app.post("/api/save")
def api_save():
    """Идемпотентное сохранение ночи (можно слать сколько угодно раз)."""
    j = request.get_json(force=True, silent=True) or {}
    try:
        start = int(j["start"])
        end = int(j["end"])
        duration = int(j["duration"])
        quality = max(0, min(100, int(j["quality"])))
        peaks = max(0, int(j["peaks"]))
        minutes = j.get("minutes", [])
        if not isinstance(minutes, list) or len(minutes) > 3000:
            raise ValueError("minutes")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "bad data"}), 400
    q(
        "INSERT OR REPLACE INTO nights(start_ts,end_ts,alarm,duration_min,quality,peaks,data) "
        "VALUES(?,?,?,?,?,?,?)",
        (start, end, str(j.get("alarm", ""))[:8], duration, quality, peaks, json.dumps(minutes)),
        commit=True,
    )
    return jsonify({"ok": True, "id": start})


@app.get("/api/history")
def api_history():
    rows = q("SELECT * FROM nights ORDER BY start_ts DESC")
    return jsonify({"sessions": [row_to_dict(r) for r in rows]})


@app.get("/api/night/<int:sid>")
def api_night(sid):
    r = q("SELECT * FROM nights WHERE start_ts=?", (sid,), one=True)
    if not r:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(r, full=True))


@app.delete("/api/night/<int:sid>")
def api_delete(sid):
    q("DELETE FROM nights WHERE start_ts=?", (sid,), commit=True)
    return jsonify({"ok": True})


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


# ───────────────────────────── Интерфейс ─────────────────────────────
HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d17">
<title>Sleep Tracker</title>
<style>
:root{--bg:#0b0d17;--card:#151a2c;--card2:#1d2440;--txt:#eef0f8;--dim:#8a91ad;--acc:#7c8cff;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--txt);font-family:-apple-system,system-ui,sans-serif}
body{padding:env(safe-area-inset-top) 16px calc(84px + env(safe-area-inset-bottom));min-height:100vh}
.hidden{display:none!important}
.center{text-align:center}
.clock{font-size:84px;font-weight:200;letter-spacing:-2px;line-height:1;margin:28px 0 4px;font-variant-numeric:tabular-nums}
.clock small{font-size:26px;color:var(--dim);letter-spacing:0;margin-left:4px}
.dim{color:var(--dim)}
.syncline{font-size:13px;margin:6px 0}
.card{background:var(--card);border-radius:20px;padding:16px;margin:14px 0}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
input[type=time]{background:var(--card2);color:var(--txt);border:0;border-radius:12px;font-size:28px;padding:8px 12px;font-family:inherit}
button{font-family:inherit;border:0;border-radius:18px;font-size:19px;font-weight:600;padding:18px;width:100%;color:#fff;background:var(--acc)}
button:active{opacity:.8}
button.alt{background:var(--card2)}
button.ok{background:#22a35a}
button.link{background:none;color:var(--dim);font-size:15px;font-weight:400;padding:10px;width:auto}
.btns{display:grid;gap:12px;margin-top:18px}
.big{font-size:52px;font-weight:300;font-variant-numeric:tabular-nums}
.meter{height:16px;background:var(--card2);border-radius:8px;overflow:hidden;margin:12px 0}
.meter>div{height:100%;width:0;background:linear-gradient(90deg,var(--ok),var(--warn),var(--bad));transition:width .3s}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat{background:var(--card);border-radius:16px;padding:12px;text-align:center}
.stat b{display:block;font-size:22px;font-weight:600;margin-top:2px}
.stat span{font-size:12px;color:var(--dim)}
canvas{width:100%;height:190px;display:block}
.item{display:flex;justify-content:space-between;align-items:center;background:var(--card);border-radius:16px;padding:14px;margin:10px 0}
.badge{border-radius:10px;padding:4px 10px;font-weight:600;font-size:15px}
.tabs{position:fixed;left:0;right:0;bottom:0;display:flex;background:rgba(21,26,44,.96);padding:8px 16px calc(8px + env(safe-area-inset-bottom));gap:8px}
.tabs button{background:none;color:var(--dim);font-size:15px;padding:10px;border-radius:12px}
.tabs button.on{color:var(--txt);background:var(--card2)}
h2{font-weight:600;margin:22px 0 8px}
.night{padding-top:10px}
.night .clock{font-size:96px;color:#cfd4ee}
</style></head>
<body>

<!-- ========== СОН ========== -->
<div id="view-sleep">

  <!-- простой режим -->
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
      <div class="row"><div><div>Будильник</div><div class="dim" id="alarm-left" style="font-size:14px"></div></div>
      <input type="time" id="alarm" value="07:00"></div>
    </div>
    <div class="btns"><button id="btn-start">🌙 Начать сон</button></div>
    <p class="dim center" style="font-size:13px;line-height:1.4;margin-top:18px">
      Положите телефон рядом с кроватью на зарядку. Экран должен оставаться включённым,
      звук не в беззвучном режиме.</p>
  </div>

  <!-- ночной режим -->
  <div id="night" class="night hidden">
    <div class="center">
      <div class="dim">Идёт запись сна</div>
      <div class="clock"><span id="n-clock">--:--</span></div>
      <div class="dim">Будильник: <span id="n-alarm"></span> · в кровати <span id="n-elapsed">0:00</span></div>
    </div>
    <div class="card">
      <div class="row"><span class="dim">Уровень шума</span><b id="n-level">0</b></div>
      <div class="meter"><div id="n-bar"></div></div>
      <div class="row"><span class="dim">Пиков шума за ночь</span><b class="big" id="n-peaks" style="font-size:36px">0</b></div>
    </div>
    <div class="btns">
      <button class="ok" id="btn-wake">☀️ Я проснулся</button>
      <button class="link" id="btn-cancel">Отменить запись</button>
    </div>
  </div>

  <!-- утренний отчёт -->
  <div id="report" class="hidden">
    <h2 id="r-title">Утренний отчёт</h2>
    <div class="dim" id="r-range"></div>
    <div class="card center">
      <div class="dim">Качество сна</div>
      <div class="big" id="r-quality">0%</div>
      <div id="r-text" class="dim"></div>
    </div>
    <div class="grid3">
      <div class="stat"><span>Длительность</span><b id="r-dur"></b></div>
      <div class="stat"><span>Пиков шума</span><b id="r-peaks"></b></div>
      <div class="stat"><span>Ср. шум</span><b id="r-avg"></b></div>
    </div>
    <div class="card">
      <div class="dim" style="font-size:13px;margin-bottom:6px">Активность / шум за ночь
        <span style="color:var(--acc)">▮</span> макс. <span style="color:var(--ok)">━</span> средний <span style="color:var(--bad)">●</span> пик</div>
      <canvas id="chart"></canvas>
    </div>
    <div class="btns">
      <button id="btn-close-report">Готово</button>
      <button class="link" id="btn-del">Удалить эту ночь</button>
    </div>
    <div class="syncline dim center"></div>
  </div>
</div>

<!-- ========== ИСТОРИЯ ========== -->
<div id="view-history" class="hidden">
  <h2>История</h2>
  <div class="syncline dim"></div>
  <div class="grid3" id="h-stats"></div>
  <div id="h-list"></div>
</div>

<div class="tabs" id="tabs">
  <button class="on" data-v="sleep">🌙 Сон</button>
  <button data-v="history">📊 История</button>
</div>

<script>
const $ = s => document.querySelector(s);
const pad = n => String(n).padStart(2, '0');
const fmtT = ts => { const d = new Date(ts); return pad(d.getHours()) + ':' + pad(d.getMinutes()); };
const fmtDur = m => Math.floor(m / 60) + ' ч ' + pad(m % 60) + ' м';
const qcolor = q => q >= 80 ? 'var(--ok)' : q >= 60 ? 'var(--warn)' : 'var(--bad)';
const qtext = q => q >= 80 ? 'Отличный сон' : q >= 60 ? 'Нормальный сон' : 'Сон можно улучшить';
const MONTHS = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
const fmtDate = ts => { const d = new Date(ts); return d.getDate() + ' ' + MONTHS[d.getMonth()]; };

/* Оценка 0–100: 60% длительность (цель 8 ч) + 40% тишина (пики шума в час) */
function calcQuality(dur, peaks) {
  const hours = Math.max(dur / 60, 0.1);
  let d = Math.min(dur / 480, 1) * 100;
  if (dur > 600) d -= Math.min((dur - 600) / 6, 20);
  const noise = Math.max(0, 100 - (peaks / hours) * 10);
  return Math.max(0, Math.min(100, Math.round(0.6 * d + 0.4 * noise)));
}

/* ---------- localStorage (главное хранилище на телефоне) ---------- */
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} }
};
const getNights = () => LS.get('nights', []);
const getDeleted = () => LS.get('deleted', []);   // «надгробия»: удалено у себя, но ещё не на сервере
function addNight(n) {
  const a = getNights().filter(x => x.id !== n.id); a.push(n);
  if (!LS.set('nights', a)) alert('Не удалось записать ночь в память телефона (localStorage переполнен).');
}

/* ---------- сервер + синхронизация ---------- */
async function api(url, opt = {}, ms = 30000) {
  const c = new AbortController(), t = setTimeout(() => c.abort(), ms);
  try {
    const r = await fetch(url, {
      method: opt.method || 'GET', headers: { 'Content-Type': 'application/json' },
      body: opt.body ? JSON.stringify(opt.body) : undefined, signal: c.signal
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);   // 502/503 — сервер Render ещё просыпается
    return await r.json();
  } finally { clearTimeout(t); }
}
const setSync = t => document.querySelectorAll('.syncline').forEach(e => e.textContent = t);
const lite = n => { const { minutes, ...rest } = n; return rest; };

let serverList = [], serverLoaded = false, syncing = false, retryTimer = null, curView = 'sleep';
const RETRY = [3, 5, 10, 15, 20, 30, 30, 60, 60, 60];

/* Сверка: сервер → список; чего нет на сервере — досылаем; удалённое — удаляем. Безопасно вызывать сколько угодно раз. */
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
    for (const n of getNights()) {
      if (!have.has(n.id) && !dels.includes(n.id)) {
        await api('/api/save', { method: 'POST', body: n });
        serverList.push(lite(n));
      }
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

/* ---------- часы ---------- */
const DAYS = ['воскресенье','понедельник','вторник','среда','четверг','пятница','суббота'];
function updateClock() {
  const d = new Date();
  const hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
  $('#clock').textContent = hm; $('#n-clock').textContent = hm;
  $('#secs').textContent = pad(d.getSeconds());
  $('#date').textContent = DAYS[d.getDay()] + ', ' + d.getDate() + ' ' + MONTHS[d.getMonth()];
  updateAlarmLeft();
}
function nextAlarmTs(str) {
  const [h, m] = str.split(':').map(Number);
  const d = new Date(); d.setHours(h, m, 0, 0);
  if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
  return d.getTime();
}
function updateAlarmLeft() {
  const left = Math.round((nextAlarmTs($('#alarm').value) - Date.now()) / 60000);
  $('#alarm-left').textContent = 'через ' + Math.floor(left / 60) + ' ч ' + (left % 60) + ' мин';
}
$('#alarm').value = localStorage.getItem('alarm') || '07:00';
$('#alarm').addEventListener('change', e => { localStorage.setItem('alarm', e.target.value); updateAlarmLeft(); });
setInterval(updateClock, 1000); updateClock();

/* ---------- навигация ---------- */
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
}

/* ---------- ночной режим ---------- */
let S = null;            // состояние текущей ночи (дублируется в localStorage)
let stream, actx, analyser, buf, timer, pingTimer, wakeLock;
let cur = null, base = null, lastPeak = 0, ringing = false, ringTimer, osc, gain;

function saveState() { if (S) LS.set('sleepState', S); }

async function startSleep(saved) {
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    });
  } catch (e) { alert('Нет доступа к микрофону: ' + e.message + '\nРазрешите микрофон для сайта в Safari.'); return; }

  actx = new (window.AudioContext || window.webkitAudioContext)();
  if (actx.state === 'suspended') await actx.resume();
  const src = actx.createMediaStreamSource(stream);
  analyser = actx.createAnalyser(); analyser.fftSize = 2048;
  const mute = actx.createGain(); mute.gain.value = 0;     // Safari обрабатывает звук только если узел подключён к выходу
  src.connect(analyser); analyser.connect(mute); mute.connect(actx.destination);
  buf = new Float32Array(analyser.fftSize);

  if (saved) S = saved;
  else {
    const a = $('#alarm').value;
    S = { start: Date.now(), alarmStr: a, alarmTs: nextAlarmTs(a), minutes: [], peaks: 0 };
  }
  cur = null; base = null; lastPeak = 0; ringing = false;
  $('#n-alarm').textContent = S.alarmStr;
  $('#n-peaks').textContent = S.peaks;
  pane('night');
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
  timer = setInterval(tick, 1000);
  // раз в 10 минут «будим» бесплатный сервер, чтобы утром он не спал
  fetch('/healthz').catch(() => {});
  pingTimer = setInterval(() => fetch('/healthz').catch(() => {}), 600000);
  saveState();
}

function closeBucket() {
  if (!cur || !cur.n) return;
  S.minutes[cur.idx] = { a: Math.round(cur.sum / cur.n), m: Math.round(cur.max), p: cur.peaks };
}

function tick() {
  const now = Date.now();
  analyser.getFloatTimeDomainData(buf);
  let s = 0; for (const v of buf) s += v * v;
  const db = 20 * Math.log10(Math.sqrt(s / buf.length) + 1e-6);
  const level = Math.max(0, Math.min(100, (db + 70) * 100 / 70));   // -70..0 дБ → 0..100

  // пик = заметно громче "фонового" шума комнаты
  let isPeak = false;
  if (base === null) base = level;
  if (level > base + 12 && level > 20 && now - lastPeak > 10000) { isPeak = true; lastPeak = now; S.peaks++; }
  base += (level - base) * 0.02;

  // поминутные корзины; каждую минуту (и при пике) состояние уходит в localStorage
  const idx = Math.floor((now - S.start) / 60000);
  if (!cur || cur.idx !== idx) {
    closeBucket(); saveState();
    cur = { idx, sum: 0, n: 0, max: 0, peaks: 0 };
  }
  cur.sum += level; cur.n++; cur.max = Math.max(cur.max, level);
  if (isPeak) { cur.peaks++; saveState(); }

  // экран
  $('#n-level').textContent = Math.round(level);
  $('#n-bar').style.width = level + '%';
  $('#n-peaks').textContent = S.peaks;
  const el = Math.floor((now - S.start) / 60000);
  $('#n-elapsed').textContent = Math.floor(el / 60) + ':' + pad(el % 60);

  if (!ringing && now >= S.alarmTs) startRing();
}

/* будильник (сигнал из Web Audio) */
function startRing() {
  ringing = true;
  osc = actx.createOscillator(); gain = actx.createGain();
  osc.type = 'sine'; osc.frequency.value = 880; gain.gain.value = 0;
  osc.connect(gain); gain.connect(actx.destination); osc.start();
  let on = false;
  ringTimer = setInterval(() => { on = !on; gain.gain.value = on ? 0.6 : 0; }, 450);
  if (navigator.vibrate) navigator.vibrate([500, 300, 500]);
}
function stopRing() {
  if (!ringing) return;
  clearInterval(ringTimer); try { osc.stop(); } catch (e) {} ringing = false;
}

function stopCapture() {
  stopRing(); clearInterval(timer); clearInterval(pingTimer);
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (actx) actx.close();
  if (wakeLock) wakeLock.release().catch(() => {});
}

async function finish() {
  closeBucket(); stopCapture();
  const end = Date.now();
  if (end - S.start < 120000) {
    alert('Запись слишком короткая (меньше 2 минут), не сохраняю.');
    S = null; LS.del('sleepState'); pane('idle'); return;
  }
  const duration = Math.max(1, Math.round((end - S.start) / 60000));
  const rec = {
    id: S.start, start: S.start, end, alarm: S.alarmStr, duration,
    quality: calcQuality(duration, S.peaks), peaks: S.peaks, minutes: S.minutes
  };
  addNight(rec);            // 1) сначала в память телефона
  LS.del('sleepState');
  S = null;
  showReport(rec);          // 2) показываем отчёт сразу, не ждём сервер
  syncAll();                // 3) отправка на сервер с повторами, пока он просыпается
}

$('#btn-start').onclick = () => startSleep(null);
$('#btn-wake').onclick = finish;
$('#btn-cancel').onclick = () => {
  if (!confirm('Отменить запись без сохранения?')) return;
  stopCapture(); S = null; LS.del('sleepState'); pane('idle');
};
$('#btn-resume').onclick = () => { const s = LS.get('sleepState', null); if (s) startSleep(s); };
$('#btn-drop').onclick = () => { LS.del('sleepState'); $('#resume').classList.add('hidden'); };
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState !== 'visible') return;
  if (S) { try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {} }
  else syncAll();
});

/* незавершённая ночь (например, Safari перезагрузил страницу) */
(function () {
  const s = LS.get('sleepState', null);
  if (s && s.start) { $('#resume-from').textContent = fmtT(s.start); $('#resume').classList.remove('hidden'); }
})();

/* ---------- утренний отчёт ---------- */
let curReportId = null;
function showReport(r) {
  curReportId = r.id;
  showView('sleep'); pane('report');
  $('#r-title').textContent = 'Ночь на ' + fmtDate(r.end);
  $('#r-range').textContent = fmtT(r.start) + ' → ' + fmtT(r.end);
  $('#r-quality').textContent = r.quality + '%';
  $('#r-quality').style.color = qcolor(r.quality);
  $('#r-text').textContent = qtext(r.quality);
  $('#r-dur').textContent = fmtDur(r.duration);
  $('#r-peaks').textContent = r.peaks;
  const vals = (r.minutes || []).filter(Boolean);
  $('#r-avg').textContent = vals.length ? Math.round(vals.reduce((a, m) => a + m.a, 0) / vals.length) : 0;
  drawChart($('#chart'), r.minutes || [], r.start);
}
$('#btn-close-report').onclick = () => pane('idle');
$('#btn-del').onclick = () => {
  if (!confirm('Удалить эту ночь из истории?')) return;
  const id = curReportId;
  LS.set('nights', getNights().filter(n => n.id !== id));
  const d = getDeleted(); if (!d.includes(id)) { d.push(id); LS.set('deleted', d); }
  serverList = serverList.filter(s => s.id !== id);
  pane('idle'); showView('history');   // showView запустит синхронизацию (удаление уйдёт на сервер)
};

function drawChart(cv, minutes, startTs) {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
  const n = Math.max(minutes.length, 1), padL = 28, padB = 20, padT = 10;
  const cw = w - padL - 4, ch = h - padB - padT;
  const x = i => padL + cw * i / n, y = v => padT + ch * (1 - v / 100);
  g.font = '10px -apple-system'; g.lineWidth = 1;
  for (const v of [0, 50, 100]) {
    g.strokeStyle = '#252c4a'; g.beginPath(); g.moveTo(padL, y(v)); g.lineTo(w - 4, y(v)); g.stroke();
    g.fillStyle = '#8a91ad'; g.fillText(v, 2, y(v) + 3);
  }
  const bw = Math.max(cw / n - 0.5, 1);
  g.fillStyle = 'rgba(124,140,255,.4)';
  minutes.forEach((m, i) => { if (m) { const yy = y(m.m); g.fillRect(x(i), yy, bw, y(0) - yy); } });
  g.strokeStyle = '#4ade80'; g.lineWidth = 1.5; g.beginPath();
  let started = false;
  minutes.forEach((m, i) => {
    if (!m) { started = false; return; }
    const px = x(i) + bw / 2;
    if (!started) { g.moveTo(px, y(m.a)); started = true; } else g.lineTo(px, y(m.a));
  });
  g.stroke();
  g.fillStyle = '#f87171';
  minutes.forEach((m, i) => { if (m && m.p > 0) { g.beginPath(); g.arc(x(i) + bw / 2, padT, 3.5, 0, 7); g.fill(); } });
  g.fillStyle = '#8a91ad';
  const step = n > 360 ? 120 : n > 120 ? 60 : 30;
  for (let t = 0; t <= n; t += step) g.fillText(fmtT(startTs + t * 60000), Math.min(x(t) - 12, w - 30), h - 5);
}

/* ---------- история: телефон + сервер без дублей (id = время старта) ---------- */
function merged() {
  const dead = new Set(getDeleted());
  const m = new Map();
  serverList.forEach(s => { if (!dead.has(s.id)) m.set(s.id, s); });
  getNights().forEach(n => { if (!dead.has(n.id)) m.set(n.id, n); });   // локальная копия главнее (в ней есть график)
  return [...m.values()].sort((a, b) => b.start - a.start);
}

function renderHistory() {
  const list = merged(), n = list.length;
  const avgD = n ? Math.round(list.reduce((a, s) => a + s.duration, 0) / n) : 0;
  const avgQ = n ? Math.round(list.reduce((a, s) => a + s.quality, 0) / n) : 0;
  const onSrv = new Set(serverList.map(s => s.id));
  $('#h-stats').innerHTML = n
    ? `<div class="stat"><span>Ср. сон</span><b>${fmtDur(avgD)}</b></div>
       <div class="stat"><span>Ср. качество</span><b style="color:${qcolor(avgQ)}">${avgQ}%</b></div>
       <div class="stat"><span>Ночей</span><b>${n}</b></div>`
    : '';
  $('#h-list').innerHTML = n ? list.map(s => `
    <div class="item" data-id="${s.id}">
      <div><b>${fmtDate(s.end)}</b><div class="dim" style="font-size:13px">${fmtT(s.start)} → ${fmtT(s.end)} · пиков: ${s.peaks}${serverLoaded && !onSrv.has(s.id) ? ' · ⏳' : ''}</div></div>
      <div style="text-align:right"><div>${fmtDur(s.duration)}</div>
      <span class="badge" style="background:${qcolor(s.quality)}22;color:${qcolor(s.quality)}">${s.quality}%</span></div>
    </div>`).join('') : '<p class="dim center">Пока нет записанных ночей</p>';
  document.querySelectorAll('.item').forEach(el => el.onclick = () => openNight(Number(el.dataset.id)));
}

async function openNight(id) {
  let n = merged().find(x => x.id === id);
  if (!n) return;
  if (!n.minutes) {   // ночь есть только на сервере — подгружаем график
    try { n = await api('/api/night/' + id); }
    catch (e) { alert('Сервер ещё просыпается, попробуйте через минуту.'); return; }
  }
  showReport(n);
}

/* ---------- старт ---------- */
syncAll();   // при открытии: досылаем недостающие ночи и подтягиваем сервер
</script>
</body></html>
"""


# ───────────────────────────── Запуск ─────────────────────────────
# Таблицы создаются при импорте модуля — работает и с `gunicorn app:app`, и с `python app.py`.
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
