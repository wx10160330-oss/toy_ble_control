import time
import json
import threading
import socket
import html
import secrets
import hmac
from aiohttp import web
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, llm_tool


# 全局状态
#
# functions: 按功能名独立跟踪的状态表，每个功能能独立开关、互不干扰，
#            这样 LLM 连着调 set(suck) + set(vibrate) + set(pat) 会三个同时跑，
#            而不是后一个覆盖前一个。
# version:   每次状态变化递增，浏览器轮询依赖这个判断“是否需要重新计算下发”。
_state = {
    "functions": {},  # name -> {"active": bool, "mode": int, "intensity": int}
    "version": 0,
    "updated_at": 0,
}
_state_lock = threading.Lock()


def _snapshot_state():
    return {
        "functions": {k: dict(v) for k, v in _state["functions"].items()},
        "version": _state["version"],
        "updated_at": _state["updated_at"],
    }


def _get_state():
    with _state_lock:
        return _snapshot_state()


def _set_function(name, mode, intensity):
    with _state_lock:
        _state["functions"][name] = {
            "active": True,
            "mode": int(mode),
            "intensity": int(intensity),
        }
        _state["version"] += 1
        _state["updated_at"] = time.time()


def _stop_function(function="all"):
    with _state_lock:
        if function == "all":
            for n in list(_state["functions"].keys()):
                _state["functions"][n]["active"] = False
                _state["functions"][n]["mode"] = 0
                _state["functions"][n]["intensity"] = 0
        else:
            entry = _state["functions"].setdefault(
                function, {"active": False, "mode": 0, "intensity": 0}
            )
            entry["active"] = False
            entry["mode"] = 0
            entry["intensity"] = 0
        _state["version"] += 1
        _state["updated_at"] = time.time()


# 兜底默认配置：与原 Svakom SA253B 行为一致
DEFAULT_FUNCTIONS = [
    {
        "name": "suck",
        "display_name": "吮吸",
        "command_template": "55 09 00 00 {mode} {intensity} 00",
        "stop_template": "55 09 00 00 00 00 00",
        "mode_min": 1,
        "mode_max": 10,
        "intensity_min": 1,
        "intensity_max": 3,
        "mode_description": "",
    },
    {
        "name": "vibrate",
        "display_name": "振动",
        "command_template": "55 03 00 00 {mode} {intensity} 00",
        "stop_template": "55 03 00 00 00 00 00",
        "mode_min": 1,
        "mode_max": 10,
        "intensity_min": 1,
        "intensity_max": 3,
        "mode_description": "",
    },
    {
        "name": "pat",
        "display_name": "拍打",
        "command_template": "55 07 00 {mode} {intensity} 00",
        "stop_template": "55 07 00 00 00 00",
        "mode_min": 1,
        "mode_max": 4,
        "intensity_min": 1,
        "intensity_max": 3,
        "mode_description": "",
    },
]

DEFAULT_OPTIONAL_SERVICES = ["0xFFE0", "0xFFE5", "0xFFF0", "0x1800", "0x1801"]


# 中继网页 HTML 模板。TOY_CONFIG_JSON 占位符会在运行时替换为序列化后的配置。
RELAY_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TOY_TITLE__</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Inter:wght@400;500;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}

/* ============ 配色仿照 Xavier Memory · 清晨薄雾 / 月光透纸 ============ */
:root{
  --font-serif:'Cormorant Garamond','Songti SC','Noto Serif SC',Georgia,serif;
  --font-sans:'Inter',-apple-system,BlinkMacSystemFont,'PingFang SC','Segoe UI',sans-serif;
  --radius-lg:28px;--radius-md:22px;--radius-sm:16px;
  --ease:cubic-bezier(0.22,0.61,0.36,1);
}
/* 浅色（默认） */
[data-theme="light"]{
  --bg:#FCFAFF;--bg-2:#F7F4FB;--bg-3:#FFF8F9;
  --glow-1:#C4A8E0;--glow-2:#EFB7C9;--glow-3:#A8B4E0;
  --text:#5C4B66;--text-strong:#4A3A55;
  --muted:rgba(92,75,102,0.55);--caption:rgba(110,90,122,0.5);
  --accent:#6D5A9C;--accent-soft:#8A76B8;--accent-glow:rgba(124,92,180,0.2);--blush:#EFB7C9;
  --glass-bg:rgba(255,255,255,0.46);--glass-highlight:rgba(255,255,255,0.9);
  --card-solid:rgba(255,253,255,0.82);
  --surface:rgba(255,255,255,0.4);--surface-hover:rgba(255,255,255,0.72);
  --line:rgba(92,75,102,0.12);
  --danger:#C96A6A;--success:#6BAE8E;--dot-warn:#E0A93C;
  --card-shadow:0 14px 50px rgba(92,75,102,0.09);
  --card-shadow-soft:0 8px 30px rgba(92,75,102,0.06);
  --moon-display:none;--sun-display:inline;
}
/* 深色 */
[data-theme="dark"]{
  --bg:#17171A;--bg-2:#1C1A20;--bg-3:#201A1E;
  --glow-1:#5B4A7A;--glow-2:#7A4A5E;--glow-3:#3E4A6E;
  --text:#E4CFD2;--text-strong:#F0DCDF;
  --muted:rgba(228,207,210,0.5);--caption:rgba(220,200,205,0.42);
  --accent:#D8B4B8;--accent-soft:#C99AA0;--accent-glow:rgba(216,180,184,0.2);--blush:#E8A9C4;
  --glass-bg:rgba(255,255,255,0.035);--glass-highlight:rgba(255,255,255,0.05);
  --card-solid:rgba(38,36,45,0.72);
  --surface:rgba(255,255,255,0.035);--surface-hover:rgba(255,255,255,0.075);
  --line:rgba(255,255,255,0.07);
  --danger:#E28B8B;--success:#86D0AE;--dot-warn:#FFD34E;
  --card-shadow:0 16px 54px rgba(0,0,0,0.52);
  --card-shadow-soft:0 10px 34px rgba(0,0,0,0.4);
  --moon-display:inline;--sun-display:none;
}
body{
  font-family:var(--font-sans);color:var(--text);min-height:100vh;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:24px;gap:4px;position:relative;overflow-x:hidden;
  background:
    radial-gradient(ellipse 80% 50% at 20% 0%,var(--bg-2),transparent 70%),
    radial-gradient(ellipse 70% 50% at 80% 100%,var(--bg-3),transparent 70%),
    var(--bg);
  transition:background .4s var(--ease),color .3s var(--ease);
}
/* 背景三团柔光 */
body::before,body::after{
  content:"";position:fixed;border-radius:50%;filter:blur(90px);opacity:.5;z-index:-1;
  animation:float 16s ease-in-out infinite;
}
body::before{width:380px;height:380px;background:var(--glow-1);top:-100px;left:-80px}
body::after{width:340px;height:340px;background:var(--glow-2);bottom:-110px;right:-90px;animation-delay:-8s}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(34px)}}

h1{
  font-family:var(--font-serif);
  font-size:2em;font-weight:600;margin-bottom:20px;letter-spacing:.5px;color:var(--text-strong);
}
.status{
  padding:18px 24px;border-radius:var(--radius-md);margin-bottom:18px;text-align:center;min-width:300px;
  background:var(--glass-bg);border:1px solid var(--glass-highlight);
  backdrop-filter:blur(18px) saturate(1.3);-webkit-backdrop-filter:blur(18px) saturate(1.3);
  box-shadow:var(--card-shadow);
}
.dot{
  display:inline-block;width:11px;height:11px;border-radius:50%;
  margin-right:8px;vertical-align:middle;transition:all .3s;
}
.dot.off{background:var(--danger);box-shadow:0 0 0 0 rgba(201,106,106,.4)}
.dot.on{background:var(--success);box-shadow:0 0 12px var(--accent-glow);animation:pulse 1.8s ease-out infinite}
@keyframes pulse{
  0%{box-shadow:0 0 0 0 var(--accent-glow)}
  70%{box-shadow:0 0 0 10px transparent}
  100%{box-shadow:0 0 0 0 transparent}
}
#statusText{font-size:1.05em;font-weight:500;vertical-align:middle;color:var(--text-strong)}

/* 主题切换按钮 */
#themeToggle{
  position:fixed;top:18px;right:18px;width:44px;height:44px;padding:0;margin:0;border-radius:999px;
  background:var(--surface);border:1px solid var(--line);color:var(--text);font-size:1.1em;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);box-shadow:var(--card-shadow-soft);
}
#themeToggle:hover{background:var(--surface-hover);box-shadow:0 0 18px var(--accent-glow)}
#themeToggle .moon{display:var(--moon-display)}
#themeToggle .sun{display:var(--sun-display)}

button{
  padding:13px 34px;border:none;border-radius:999px;font-size:1em;font-weight:500;
  cursor:pointer;margin:6px;transition:transform .15s,box-shadow .3s var(--ease),background .3s var(--ease);
}
button:active{transform:scale(.96)}
#btnConnect{
  background:linear-gradient(135deg,var(--accent),var(--accent-soft));color:#fff;
  font-family:var(--font-serif);font-size:1.15em;font-weight:600;letter-spacing:.5px;
  box-shadow:0 8px 26px var(--accent-glow);
}
#btnConnect:hover{box-shadow:0 12px 36px var(--accent-glow),0 0 24px var(--accent-glow)}
#btnDisconnect{
  background:var(--surface);color:var(--danger);border:1px solid var(--line);display:none;
  font-family:var(--font-serif);font-size:1.1em;box-shadow:var(--card-shadow-soft);
}
#btnDisconnect:hover{background:var(--surface-hover);box-shadow:0 0 18px rgba(201,106,106,.25)}
#log{
  margin-top:18px;padding:14px;border-radius:var(--radius-sm);width:100%;max-width:420px;height:200px;
  overflow-y:auto;font-size:0.8em;line-height:1.6;font-family:ui-monospace,"SF Mono",Consolas,monospace;
  color:var(--muted);background:var(--surface);border:1px solid var(--line);
  white-space:pre-wrap;
}
#log::-webkit-scrollbar{width:6px}
#log::-webkit-scrollbar-thumb{background:var(--accent-glow);border-radius:3px}
.cmd-display{
  font-size:1em;color:var(--accent);margin-top:12px;padding:8px 18px;border-radius:999px;
  background:var(--surface);border:1px solid var(--line);
}
.toy-name{font-size:0.85em;color:var(--muted);margin-top:6px}

/* 控制面板 */
#panel{width:100%;max-width:420px;display:flex;flex-direction:column;gap:14px;margin-bottom:4px}
.func-card{
  background:var(--glass-bg);border:1px solid var(--glass-highlight);border-radius:var(--radius-md);
  padding:16px 18px;backdrop-filter:blur(14px) saturate(1.2);-webkit-backdrop-filter:blur(14px) saturate(1.2);
  box-shadow:var(--card-shadow-soft);transition:border-color .3s var(--ease),box-shadow .3s var(--ease);
}
.func-card.active{border-color:var(--accent-soft);box-shadow:0 8px 30px var(--accent-glow)}
.func-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.func-title{font-family:var(--font-serif);font-size:1.35em;font-weight:600;color:var(--text-strong)}
.func-title .en{font-family:var(--font-sans);font-size:.6em;font-weight:400;color:var(--muted);margin-left:6px}
.func-off-btn{
  padding:5px 14px;font-size:.78em;border-radius:999px;margin:0;
  background:var(--surface);color:var(--danger);border:1px solid var(--line);
}
.func-off-btn:hover{background:var(--surface-hover)}
.row{display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap}
.row-label{font-size:.8em;color:var(--muted);min-width:34px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{
  min-width:34px;height:34px;padding:0 8px;border-radius:12px;cursor:pointer;margin:0;
  display:inline-flex;align-items:center;justify-content:center;font-size:.9em;font-weight:500;
  background:var(--surface);color:var(--text);border:1px solid var(--line);
  transition:all .15s var(--ease);
}
.chip:hover{background:var(--surface-hover);color:var(--text-strong)}
.chip.sel{background:linear-gradient(135deg,var(--accent),var(--accent-soft));color:#fff;border-color:transparent;box-shadow:0 4px 14px var(--accent-glow)}
.dots{display:flex;gap:7px}
.idot{
  width:16px;height:16px;border-radius:50%;cursor:pointer;
  background:var(--surface);border:1px solid var(--line);transition:all .15s var(--ease);
}
.idot:hover{background:var(--surface-hover)}
.idot.on{background:linear-gradient(135deg,var(--accent-soft),var(--blush));border-color:transparent;box-shadow:0 0 10px var(--accent-glow)}
#btnStopAll{
  background:var(--surface);color:var(--danger);border:1px solid var(--line);
  font-family:var(--font-serif);font-size:1.1em;width:100%;max-width:420px;
  box-shadow:var(--card-shadow-soft);
}
#btnStopAll:hover{background:var(--surface-hover);box-shadow:0 0 18px rgba(201,106,106,.25)}
.panel-hint{font-size:.8em;color:var(--caption);text-align:center;margin:2px 0 6px}
.mode-desc{margin-top:10px;padding:10px 12px;border-radius:14px;background:var(--surface);border:1px solid var(--line);font-size:.82em;line-height:1.55;color:var(--muted);white-space:pre-wrap;word-break:break-word}.mode-desc .mode-desc-label{display:block;font-size:.75em;color:var(--caption);letter-spacing:.5px;margin-bottom:4px;text-transform:uppercase}.panel-empty{text-align:center;color:var(--muted);font-size:.9em;padding:20px}
</style>
<link rel="apple-touch-icon" href="/icon.png">
</head>
<body>
<button id="themeToggle" onclick="toggleTheme()" title="切换主题"><span class="moon">🌙</span><span class="sun">☀️</span></button>
<h1>__TOY_TITLE__</h1>
<div class="status">
  <span class="dot off" id="dot"></span>
  <span id="statusText">未连接</span>
  <div class="toy-name" id="toyName"></div>
</div>
<div>
  <button id="btnConnect" onclick="connectBLE()">连接设备</button>
  <button id="btnDisconnect" onclick="disconnectBLE()">断开</button>
</div>
<div class="cmd-display" id="cmdDisplay">等待指令...</div>
<div class="panel-hint">点击档位即可控制，与 AI 共享状态</div>
<div id="panel"></div>
<button id="btnStopAll" onclick="stopAll()">全部停止</button>
<div id="log"></div>
<script>
const TOY_CONFIG = __TOY_CONFIG_JSON__;
const POLL_URL = window.location.origin + '/state';

document.getElementById('toyName').textContent = TOY_CONFIG.toy_name || '';

// 主题切换：记住上次选择，默认浅色（清晨薄雾）
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('toy_theme'); } catch (e) {}
  const theme = (saved === 'dark' || saved === 'light') ? saved : 'light';
  document.documentElement.setAttribute('data-theme', theme);
})();
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('toy_theme', next); } catch (e) {}
}

// 将用户输入的 UUID 规范化：0xFFE0 / FFE0 -> 数字；完整 UUID -> 小写字符串
function parseUUID(s) {
  if (typeof s === 'number') return s;
  if (s == null) return null;
  s = String(s).trim();
  if (s === '') return null;
  if (/^0x[0-9a-fA-F]+$/.test(s)) return parseInt(s, 16);
  if (/^[0-9a-fA-F]{4}$/.test(s)) return parseInt(s, 16);
  if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(s)) return s.toLowerCase();
  return s;
}

const SERVICE_UUID = parseUUID(TOY_CONFIG.service_uuid);
const WRITE_UUID = parseUUID(TOY_CONFIG.write_characteristic_uuid);
const FALLBACK_SERVICE_UUIDS = (TOY_CONFIG.fallback_service_uuids || []).map(parseUUID).filter(x => x !== null);
const OPTIONAL_SERVICES = (TOY_CONFIG.optional_services || []).map(parseUUID).filter(x => x !== null);
const WRITE_WITHOUT_RESPONSE = !!TOY_CONFIG.write_without_response;
const NAME_FILTER_PREFIX = (TOY_CONFIG.name_filter_prefix || '').trim();

let device = null;
let writeChar = null;
let polling = null;
let lastVersion = -1;
// 记住上一轮各功能的状态，用来 diff，避免重复下发相同指令。
const lastFuncState = {}; // name -> {active, mode, intensity}

function log(msg) {
  const el = document.getElementById('log');
  const t = new Date().toLocaleTimeString();
  el.textContent = `[${t}] ${msg}\n` + el.textContent;
}

function setStatus(connected) {
  document.getElementById('dot').className = 'dot ' + (connected ? 'on' : 'off');
  document.getElementById('statusText').textContent = connected ? '已连接' : '未连接';
  document.getElementById('btnConnect').style.display = connected ? 'none' : 'inline-block';
  document.getElementById('btnDisconnect').style.display = connected ? 'inline-block' : 'none';
}

// 把模板字符串 (空格分隔的 hex byte，含 {mode}/{intensity} 占位符) 解析为 Uint8Array
function parseHexTemplate(template, mode, intensity) {
  if (!template) return null;
  const filled = String(template)
    .replace(/\{mode\}/gi, Number(mode || 0).toString(16).padStart(2, '0'))
    .replace(/\{intensity\}/gi, Number(intensity || 0).toString(16).padStart(2, '0'));
  const tokens = filled.trim().split(/\s+/).filter(Boolean);
  const bytes = [];
  for (const tok of tokens) {
    const v = parseInt(tok, 16);
    if (Number.isNaN(v)) { log('指令模板解析失败: ' + filled); return null; }
    bytes.push(v & 0xff);
  }
  return new Uint8Array(bytes);
}

function findFunction(name) {
  return (TOY_CONFIG.functions || []).find(f => f.name === name) || null;
}

function buildCmd(funcName, mode, intensity) {
  const f = findFunction(funcName);
  if (!f) return null;
  return parseHexTemplate(f.command_template, mode, intensity);
}

function buildStopCmd(funcName) {
  const f = findFunction(funcName);
  if (!f) return null;
  return parseHexTemplate(f.stop_template, 0, 0);
}

async function writeBytes(arr) {
  if (!writeChar || !arr) return;
  if (WRITE_WITHOUT_RESPONSE && writeChar.writeValueWithoutResponse) {
    await writeChar.writeValueWithoutResponse(arr);
  } else {
    await writeChar.writeValue(arr);
  }
}

async function finishConnect(server) {
  log('GATT已连接，扫描服务...');
  try {
    const services = await server.getPrimaryServices();
    for (const svc of services) log('服务: ' + svc.uuid);
  } catch(e) { log('枚举服务失败: ' + e.message); }

  const candidates = [SERVICE_UUID, ...FALLBACK_SERVICE_UUIDS].filter(x => x !== null);
  let service = null;
  let lastErr = null;
  for (const uuid of candidates) {
    try {
      service = await server.getPrimaryService(uuid);
      log('使用服务 UUID: ' + uuid);
      break;
    } catch(e) {
      lastErr = e;
      log('服务 ' + uuid + ' 未找到，尝试下一个...');
    }
  }
  if (!service) {
    log('所有服务 UUID 都连接失败: ' + (lastErr && lastErr.message));
    return;
  }
  writeChar = await service.getCharacteristic(WRITE_UUID);
  log('连接成功!');
  setStatus(true);
  startPolling();
}

async function connectBLE() {
  try {
    log('搜索设备...');

    // 尝试自动重连已授权的设备
    if (navigator.bluetooth.getDevices) {
      try {
        const devices = await navigator.bluetooth.getDevices();
        for (const d of devices) {
          if (d.gatt) {
            log('尝试自动重连: ' + d.name);
            d.addEventListener('gattserverdisconnected', onDisconnect);
            const server = await d.gatt.connect();
            device = d;
            await finishConnect(server);
            return;
          }
        }
      } catch(e) { log('自动重连失败，手动搜索...'); }
    }

    // 手动搜索
    const requestOpts = { optionalServices: OPTIONAL_SERVICES };
    if (NAME_FILTER_PREFIX) {
      requestOpts.filters = [{ namePrefix: NAME_FILTER_PREFIX }];
    } else {
      requestOpts.acceptAllDevices = true;
    }
    device = await navigator.bluetooth.requestDevice(requestOpts);
    device.addEventListener('gattserverdisconnected', onDisconnect);
    const server = await device.gatt.connect();
    await finishConnect(server);
  } catch (e) {
    log('连接失败: ' + e.message);
  }
}

function onDisconnect() {
  log('设备断开');
  setStatus(false);
  stopPolling();
}

function disconnectBLE() {
  if (device && device.gatt.connected) device.gatt.disconnect();
  setStatus(false);
  stopPolling();
  log('已断开');
}

function describeActive(funcs) {
  const parts = [];
  for (const [name, st] of Object.entries(funcs || {})) {
    if (st.active) {
      const display = (findFunction(name) || {}).display_name || name;
      parts.push(`${display} M${st.mode} I${st.intensity}`);
    }
  }
  return parts.length ? parts.join(' / ') : '停止';
}

// 把用户在页面上的操作写回后端共享状态（与 LLM 工具共用同一份 _state）。
// 写成功后立刻拉一次 /state，让面板高亮与设备下发尽快跟上，不用等下一个轮询周期。
async function postState(body) {
  try {
    await fetch(POLL_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    lastVersion = -1; // 强制下个轮询周期重新计算下发
  } catch (e) {
    log('操作发送失败: ' + e.message);
  }
}

function setFunc(name, mode, intensity) {
  postState({ cmd: 'set', function: name, mode, intensity });
}

function stopFunc(name) {
  postState({ cmd: 'stop', function: name });
}

function stopAll() {
  postState({ cmd: 'stop', function: 'all' });
}

// 首次构建控制面板：每个功能一行，模式一排档位按钮，强度一排圆点。
// 只在加载时构建一次；之后靠 highlightPanel() 更新选中态，不重建 DOM。
function renderPanel() {
  const panel = document.getElementById('panel');
  if (!panel) return;
  panel.innerHTML = '';
  const funcs = TOY_CONFIG.functions || [];
  if (!funcs.length) {
    panel.innerHTML = '<div class="panel-empty">未配置任何功能</div>';
    return;
  }
  for (const f of funcs) {
    const row = document.createElement('div');
    row.className = 'func-card';
    row.dataset.func = f.name;

    const head = document.createElement('div');
    head.className = 'func-head';
    const title = document.createElement('span');
    title.className = 'func-title';
    title.textContent = f.display_name || f.name;
    if ((f.display_name || '') && f.name && f.display_name !== f.name) {
      const en = document.createElement('span');
      en.className = 'en';
      en.textContent = f.name;
      title.appendChild(en);
    }
    const stopBtn = document.createElement('button');
    stopBtn.className = 'func-off-btn';
    stopBtn.textContent = '停止';
    stopBtn.onclick = () => stopFunc(f.name);
    head.appendChild(title);
    head.appendChild(stopBtn);
    row.appendChild(head);

    // 模式描述（可选，填了才显示）
    const descText = (f.mode_description || '').trim();
    if (descText) {
      const descBox = document.createElement('div');
      descBox.className = 'mode-desc';
      const descLabel = document.createElement('span');
      descLabel.className = 'mode-desc-label';
      descLabel.textContent = '模式说明';
      const descBody = document.createElement('span');
      descBody.textContent = descText;
      descBox.appendChild(descLabel);
      descBox.appendChild(descBody);
      row.appendChild(descBox);
    }

    // 模式档位
    const modeWrap = document.createElement('div');
    modeWrap.className = 'row';
    const modeLabel = document.createElement('span');
    modeLabel.className = 'row-label';
    modeLabel.textContent = '模式';
    modeWrap.appendChild(modeLabel);
    const modeBtns = document.createElement('div');
    modeBtns.className = 'chips';
    for (let m = f.mode_min; m <= f.mode_max; m++) {
      const chip = document.createElement('button');
      chip.className = 'chip mode-chip';
      chip.dataset.mode = m;
      chip.textContent = m;
      chip.onclick = () => onChipClick(f, 'mode', m);
      modeBtns.appendChild(chip);
    }
    modeWrap.appendChild(modeBtns);
    row.appendChild(modeWrap);

    // 强度档位（用圆点表示）
    const intWrap = document.createElement('div');
    intWrap.className = 'row';
    const intLabel = document.createElement('span');
    intLabel.className = 'row-label';
    intLabel.textContent = '强度';
    intWrap.appendChild(intLabel);
    const intBtns = document.createElement('div');
    intBtns.className = 'dots';
    for (let i = f.intensity_min; i <= f.intensity_max; i++) {
      const dot = document.createElement('button');
      dot.className = 'idot int-dot';
      dot.dataset.intensity = i;
      dot.title = '强度 ' + i;
      dot.onclick = () => onChipClick(f, 'intensity', i);
      intBtns.appendChild(dot);
    }
    intWrap.appendChild(intBtns);
    row.appendChild(intWrap);

    panel.appendChild(row);
  }
}

// 点击某个档位时：以“该功能当前状态”为基准改动被点的那一维，另一维沿用现值。
// 若该功能当前未运行，则用配置的最小值兜底，保证点一下就能立即启动。
function onChipClick(f, dim, value) {
  const cur = lastFuncState[f.name] || { mode: 0, intensity: 0, active: false };
  let mode = cur.active ? cur.mode : f.mode_min;
  let intensity = cur.active ? cur.intensity : f.intensity_min;
  if (dim === 'mode') mode = value;
  else intensity = value;
  setFunc(f.name, mode, intensity);
}

// 根据最新状态更新面板高亮，不重建 DOM。
function highlightPanel(funcs) {
  const rows = document.querySelectorAll('.func-card');
  rows.forEach(row => {
    const name = row.dataset.func;
    const st = (funcs || {})[name] || { active: false, mode: 0, intensity: 0 };
    row.classList.toggle('active', !!st.active);
    row.querySelectorAll('.mode-chip').forEach(chip => {
      chip.classList.toggle('sel', st.active && Number(chip.dataset.mode) === st.mode);
    });
    row.querySelectorAll('.int-dot').forEach(dot => {
      // 圆点填充：强度是“累计”表示，选中值及以下都点亮
      dot.classList.toggle('on', st.active && Number(dot.dataset.intensity) <= st.intensity);
    });
  });
}

function startPolling() {
  if (polling) return;
  polling = setInterval(async () => {
    try {
      const res = await fetch(POLL_URL);
      const state = await res.json();
      if (state.version === lastVersion) return;
      lastVersion = state.version;
      const funcs = state.functions || {};
      const display = document.getElementById('cmdDisplay');
      display.textContent = '当前: ' + describeActive(funcs);
      highlightPanel(funcs);
      // diff 逐个功能：变动才下发。多个功能可以同时运行。
      const allNames = new Set([
        ...Object.keys(lastFuncState),
        ...Object.keys(funcs),
      ]);
      for (const name of allNames) {
        const prev = lastFuncState[name] || { active: false, mode: 0, intensity: 0 };
        const cur = funcs[name] || { active: false, mode: 0, intensity: 0 };
        if (cur.active) {
          // 之前不是 active，或档位变了 -> 重发启动指令
          if (!prev.active || prev.mode !== cur.mode || prev.intensity !== cur.intensity) {
            const cmd = buildCmd(name, cur.mode, cur.intensity);
            if (cmd) {
              try { await writeBytes(cmd); log(`${name} M${cur.mode} I${cur.intensity}`); }
              catch(e) { log('下发失败 ' + name + ': ' + e.message); }
            } else {
              log('未知功能: ' + name);
            }
          }
        } else {
          // 之前 active、现在不 active 了 -> 下发停止指令
          if (prev.active) {
            const cmd = buildStopCmd(name);
            if (cmd) {
              try { await writeBytes(cmd); log('停止 ' + name); }
              catch(e) { log('停止失败 ' + name + ': ' + e.message); }
            }
          }
        }
        lastFuncState[name] = { active: cur.active, mode: cur.mode, intensity: cur.intensity };
      }
    } catch (e) {}
  }, 1000);
}

function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }

// 页面加载：构建控制面板，并自动尝试重连
window.addEventListener('load', () => {
  renderPanel();
  if (navigator.bluetooth && navigator.bluetooth.getDevices) {
    setTimeout(connectBLE, 500);
  }
});
</script>
</body>
</html>"""


LOGIN_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TOY_TITLE__ · 登录</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Inter:wght@400;500;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --font-serif:'Cormorant Garamond','Songti SC','Noto Serif SC',Georgia,serif;
  --font-sans:'Inter',-apple-system,BlinkMacSystemFont,'PingFang SC','Segoe UI',sans-serif;
}
[data-theme="light"]{
  --bg:#FCFAFF;--bg-2:#F7F4FB;--bg-3:#FFF8F9;
  --glow-1:#C4A8E0;--glow-2:#EFB7C9;
  --text:#5C4B66;--text-strong:#4A3A55;
  --muted:rgba(92,75,102,0.55);
  --accent:#6D5A9C;--accent-soft:#8A76B8;--accent-glow:rgba(124,92,180,0.2);
  --glass-bg:rgba(255,255,255,0.46);--glass-highlight:rgba(255,255,255,0.9);
  --surface:rgba(255,255,255,0.4);
  --line:rgba(92,75,102,0.12);
  --danger:#C96A6A;
  --card-shadow:0 14px 50px rgba(92,75,102,0.09);
}
[data-theme="dark"]{
  --bg:#17171A;--bg-2:#1C1A20;--bg-3:#201A1E;
  --glow-1:#5B4A7A;--glow-2:#7A4A5E;
  --text:#E4CFD2;--text-strong:#F0DCDF;
  --muted:rgba(228,207,210,0.5);
  --accent:#D8B4B8;--accent-soft:#C99AA0;--accent-glow:rgba(216,180,184,0.2);
  --glass-bg:rgba(255,255,255,0.035);--glass-highlight:rgba(255,255,255,0.05);
  --surface:rgba(255,255,255,0.035);
  --line:rgba(255,255,255,0.07);
  --danger:#E28B8B;
  --card-shadow:0 16px 54px rgba(0,0,0,0.52);
}
body{
  font-family:var(--font-sans);color:var(--text);min-height:100vh;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:24px;gap:16px;position:relative;overflow-x:hidden;
  background:
    radial-gradient(ellipse 80% 50% at 20% 0%,var(--bg-2),transparent 70%),
    radial-gradient(ellipse 70% 50% at 80% 100%,var(--bg-3),transparent 70%),
    var(--bg);
}
body::before,body::after{
  content:"";position:fixed;border-radius:50%;filter:blur(90px);opacity:.5;z-index:-1;
}
body::before{width:380px;height:380px;background:var(--glow-1);top:-100px;left:-80px}
body::after{width:340px;height:340px;background:var(--glow-2);bottom:-110px;right:-90px}
h1{
  font-family:var(--font-serif);
  font-size:2em;font-weight:600;letter-spacing:.5px;color:var(--text-strong);
  margin-bottom:6px;
}
.card{
  width:100%;max-width:360px;padding:28px 24px;border-radius:26px;
  background:var(--glass-bg);border:1px solid var(--glass-highlight);
  backdrop-filter:blur(18px) saturate(1.3);-webkit-backdrop-filter:blur(18px) saturate(1.3);
  box-shadow:var(--card-shadow);
  display:flex;flex-direction:column;gap:14px;
}
.hint{font-size:.85em;color:var(--muted);text-align:center;line-height:1.5}
input[type="password"]{
  width:100%;padding:13px 16px;border-radius:14px;
  background:var(--surface);border:1px solid var(--line);
  color:var(--text-strong);font-size:1em;font-family:var(--font-sans);
  outline:none;transition:border-color .2s,box-shadow .2s;
}
input[type="password"]:focus{border-color:var(--accent-soft);box-shadow:0 0 0 3px var(--accent-glow)}
button{
  width:100%;padding:13px 20px;border:none;border-radius:999px;
  font-size:1.05em;font-weight:600;letter-spacing:.5px;
  font-family:var(--font-serif);cursor:pointer;
  background:linear-gradient(135deg,var(--accent),var(--accent-soft));color:#fff;
  box-shadow:0 8px 26px var(--accent-glow);
  transition:transform .15s,box-shadow .3s;
}
button:hover{box-shadow:0 12px 36px var(--accent-glow)}
button:active{transform:scale(.97)}
button[disabled]{opacity:.5;cursor:not-allowed}
.msg{min-height:1.2em;text-align:center;font-size:.9em;color:var(--danger)}
</style>
<link rel="apple-touch-icon" href="/icon.png">
</head>
<body>
<h1>__TOY_TITLE__</h1>
<form class="card" id="loginForm" onsubmit="return doLogin(event)">
  <div class="hint">请输入访问密码</div>
  <input type="password" id="pwd" name="password" autocomplete="current-password" autofocus>
  <button type="submit" id="submitBtn">进入</button>
  <div class="msg" id="msg">__ERROR_MSG__</div>
</form>
<script>
async function doLogin(ev){
  ev.preventDefault();
  const pwd = document.getElementById('pwd').value;
  const btn = document.getElementById('submitBtn');
  const msg = document.getElementById('msg');
  btn.disabled = true;
  msg.textContent = '';
  try{
    const res = await fetch('/login', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd})
    });
    const data = await res.json().catch(() => ({}));
    if(res.ok && data.ok){
      window.location.href = '/';
    } else {
      msg.textContent = (data && data.error) || '登录失败';
      btn.disabled = false;
    }
  } catch(e){
    msg.textContent = '网络错误: ' + e.message;
    btn.disabled = false;
  }
  return false;
}
</script>
</body>
</html>"""


def _normalize_functions(raw):
    """校验并归一化 functions 列表。允许部分字段缺失，给出合理默认。丢弃 __template_key 等平台元数据。"""
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_FUNCTIONS)
    normalized = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning(f"functions[{idx}] 不是字典，跳过")
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            # 空名字的槽位在 template_list UI 上是合法状态（用户开了块但还没填），静默跳过
            continue
        normalized.append({
            "name": name,
            "display_name": str(item.get("display_name", "")).strip() or name,
            "command_template": str(item.get("command_template", "")).strip(),
            "stop_template": str(item.get("stop_template", "")).strip(),
            "mode_min": _safe_int(item.get("mode_min"), 1),
            "mode_max": _safe_int(item.get("mode_max"), 10),
            "intensity_min": _safe_int(item.get("intensity_min"), 1),
            "intensity_max": _safe_int(item.get("intensity_max"), 3),
            "mode_description": str(item.get("mode_description", "") or "").strip(),
        })
    return normalized or list(DEFAULT_FUNCTIONS)


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_functions_config(value):
    """接受以下几种输入，都返回归一化后的 functions 列表：

    — None / "" / [] / {}             → 使用默认配置
    — list (template_list 保存后的格式)  → 直接归一化
    — str (JSON 字符串，为了兼容手填)        → 先 JSON.parse 再归一化
    """
    if value is None or value == "" or value == [] or value == {}:
        return list(DEFAULT_FUNCTIONS)
    if isinstance(value, list):
        return _normalize_functions(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            logger.error(f"functions 配置解析失败，使用默认: {e}")
            return list(DEFAULT_FUNCTIONS)
        return _normalize_functions(parsed)
    logger.warning(f"未知 functions 配置类型 {type(value)}，使用默认")
    return list(DEFAULT_FUNCTIONS)


@register("toy_ble_control", "SXH", "可配置的 BLE 玩具远程控制插件", "2.6.0")
class ToyBLEPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.runner = None
        self.site = None
        self._sock = None
        # 访问控制：会话 token 集合 + 登录失败计数（重启即清空）
        self._sessions = set()
        self._login_attempts = {}  # ip -> {"count": int, "locked_until": float}
        self._auth_lock = threading.Lock()
        self._load_toy_config()

    def _load_toy_config(self):
        cfg = self.config or {}
        # 兼容 dict-like 和 AstrBotConfig
        def g(key, default):
            try:
                v = cfg.get(key, default) if hasattr(cfg, "get") else getattr(cfg, key, default)
            except Exception:
                v = default
            return v if v is not None else default

        try:
            self.port = int(g("port", 5122))
        except (TypeError, ValueError):
            self.port = 5122
        # 密码：strip 后为空视为未设置
        self.access_password = str(g("access_password", "") or "").strip()
        self.toy_name = str(g("toy_name", "BLE Toy")) or "BLE Toy"
        self.service_uuid = str(g("service_uuid", "0xFFE0")) or "0xFFE0"
        self.write_char_uuid = str(g("write_characteristic_uuid", "0xFFE1")) or "0xFFE1"

        fb = g("fallback_service_uuids", ["0xFFF0"])
        self.fallback_uuids = list(fb) if isinstance(fb, (list, tuple)) else ["0xFFF0"]

        opt = g("optional_services", DEFAULT_OPTIONAL_SERVICES)
        self.optional_services = list(opt) if isinstance(opt, (list, tuple)) else list(DEFAULT_OPTIONAL_SERVICES)

        self.write_without_response = bool(g("write_without_response", False))
        self.name_filter_prefix = str(g("name_filter_prefix", "")) or ""
        # 优先读取新的 template_list 格式；如果老配置里还剩 functions_json （从旧版本迁移过来）也能读
        raw_functions = g("functions", None)
        if raw_functions is None or raw_functions == "" or raw_functions == [] or raw_functions == {}:
            legacy_json = g("functions_json", "")
            if legacy_json:
                logger.info("[toy_ble_control] 检测到旧版 functions_json 配置，已自动迁移。建议在配置面板里重新编辑 functions 后保存")
                raw_functions = legacy_json
        self.functions = _parse_functions_config(raw_functions)
        self.functions_by_name = {f["name"]: f for f in self.functions}
        logger.info(
            f"[toy_ble_control] 加载配置: 端口={self.port}, "
            f"功能数={len(self.functions)}, "
            f"服务UUID={self.service_uuid}, 写入UUID={self.write_char_uuid}"
        )

    def _build_relay_html(self):
        front_cfg = {
            "toy_name": self.toy_name,
            "service_uuid": self.service_uuid,
            "write_characteristic_uuid": self.write_char_uuid,
            "fallback_service_uuids": self.fallback_uuids,
            "optional_services": self.optional_services,
            "write_without_response": self.write_without_response,
            "name_filter_prefix": self.name_filter_prefix,
            "functions": self.functions,
        }
        cfg_json = json.dumps(front_cfg, ensure_ascii=False)
        title = html.escape(self.toy_name or "BLE Toy")
        return (
            RELAY_HTML_TEMPLATE
            .replace("__TOY_TITLE__", title)
            .replace("__TOY_CONFIG_JSON__", cfg_json)
        )

    async def initialize(self):
        """启动 HTTP 中继服务"""
        if self.runner:
            try:
                await self.runner.cleanup()
            except Exception:
                pass
            self.runner = None
            self.site = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        if not self.access_password:
            logger.error(
                "[toy_ble_control] 未设置访问密码 (access_password)，出于安全考虑 HTTP 中继服务未启动。"
                "请到插件配置里填写密码后重新加载插件。"
            )
            return

        # 每次启动重置会话与失败计数
        with self._auth_lock:
            self._sessions.clear()
            self._login_attempts.clear()

        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/", self._handle_relay_page)
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/login", self._handle_login_submit)
        app.router.add_post("/logout", self._handle_logout)
        app.router.add_get("/state", self._handle_get_state)
        app.router.add_post("/state", self._handle_set_state)
        app.router.add_get("/config", self._handle_get_config)
        app.router.add_get("/icon.png", self._handle_icon)

        self.runner = web.AppRunner(app)
        await self.runner.setup()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.listen(128)
        sock.setblocking(False)
        self.site = web.SockSite(self.runner, sock)
        await self.site.start()
        self._sock = sock
        logger.info(f"[toy_ble_control] HTTP 中继服务已启动，端口 {self.port}")

    # --- HTTP ---
    async def _handle_relay_page(self, request):
        return web.Response(text=self._build_relay_html(), content_type="text/html")

    async def _handle_get_state(self, request):
        return web.json_response(_get_state())

    async def _handle_set_state(self, request):
        """外部 HTTP 接口，按新的状态模型操作：

        - 设置某个功能 运行： `{"cmd":"set","function":"vibrate","mode":5,"intensity":2}`
        - 停止某个功能       ： `{"cmd":"stop","function":"vibrate"}`
        - 停止所有           ： `{"cmd":"stop","function":"all"}` 或 `{"cmd":"stop"}`

        多个功能可以独立调起，会同时运行。
        """
        data = await request.json()
        cmd = data.get("cmd", "stop")
        func = data.get("function", "all")
        if cmd == "set":
            _set_function(func, data.get("mode", 0), data.get("intensity", 0))
        else:
            _stop_function(func or "all")
        return web.json_response({"ok": True})

    async def _handle_icon(self, request):
        import os
        from aiohttp import web
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(plugin_dir, "icon-512.png")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(plugin_dir, "icon.png")
        if os.path.exists(icon_path):
            return web.FileResponse(icon_path)
        return web.Response(status=404)

    async def _handle_get_config(self, request):
        """暴露给前端 / 调试用：返回当前生效的玩具配置（不含端口）。"""
        return web.json_response({
            "toy_name": self.toy_name,
            "service_uuid": self.service_uuid,
            "write_characteristic_uuid": self.write_char_uuid,
            "fallback_service_uuids": self.fallback_uuids,
            "optional_services": self.optional_services,
            "write_without_response": self.write_without_response,
            "name_filter_prefix": self.name_filter_prefix,
            "functions": self.functions,
        })

    # --- 认证相关 ---
    COOKIE_NAME = "toy_session"
    MAX_ATTEMPTS = 5
    LOCK_SECONDS = 60

    def _client_ip(self, request):
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if peer:
                return peer[0]
        except Exception:
            pass
        return request.remote or "unknown"

    def _is_authed(self, request):
        token = request.cookies.get(self.COOKIE_NAME)
        if not token:
            return False
        with self._auth_lock:
            return token in self._sessions

    @web.middleware
    async def _auth_middleware(self, request, handler):
        # 登录相关路径直接放行
        if request.path in ("/login",):
            return await handler(request)
        if self._is_authed(request):
            return await handler(request)
        # 未登录：HTML 请求跳登录页，其他返回 401 JSON
        accept = request.headers.get("Accept", "")
        if request.method == "GET" and "text/html" in accept:
            raise web.HTTPFound("/login")
        return web.json_response({"error": "unauthorized"}, status=401)

    async def _handle_login_page(self, request):
        if self._is_authed(request):
            raise web.HTTPFound("/")
        title = html.escape(self.toy_name or "BLE Toy")
        page = (LOGIN_HTML_TEMPLATE
                .replace("__TOY_TITLE__", title)
                .replace("__ERROR_MSG__", ""))
        return web.Response(text=page, content_type="text/html", charset="utf-8")

    async def _handle_login_submit(self, request):
        ip = self._client_ip(request)
        now = time.time()
        # 检查锁定
        with self._auth_lock:
            rec = self._login_attempts.get(ip)
            if rec and rec.get("locked_until", 0) > now:
                remain = int(rec["locked_until"] - now)
                return web.json_response(
                    {"error": f"尝试次数过多，请 {remain} 秒后再试"},
                    status=429,
                )

        try:
            body = await request.json()
        except Exception:
            body = {}
        pwd = str(body.get("password", "") or "")

        expected = self.access_password or ""
        # 常量时间比较
        ok = bool(expected) and hmac.compare_digest(pwd.encode("utf-8"), expected.encode("utf-8"))

        if not ok:
            with self._auth_lock:
                rec = self._login_attempts.setdefault(ip, {"count": 0, "locked_until": 0.0})
                rec["count"] += 1
                if rec["count"] >= self.MAX_ATTEMPTS:
                    rec["locked_until"] = now + self.LOCK_SECONDS
                    rec["count"] = 0
                    logger.warning(f"[toy_ble_control] IP {ip} 登录失败次数过多，已锁定 {self.LOCK_SECONDS}s")
            return web.json_response({"error": "密码错误"}, status=401)

        # 登录成功：生成 token
        token = secrets.token_urlsafe(32)
        with self._auth_lock:
            self._sessions.add(token)
            self._login_attempts.pop(ip, None)

        resp = web.json_response({"ok": True})
        # 注意：本地/局域网 HTTP 场景不启用 Secure；如果你用 HTTPS 反代请自行加 Secure 标记
        resp.set_cookie(
            self.COOKIE_NAME,
            token,
            httponly=True,
            samesite="Lax",
            path="/",
            max_age=7 * 24 * 3600,
        )
        return resp

    async def _handle_logout(self, request):
        token = request.cookies.get(self.COOKIE_NAME)
        if token:
            with self._auth_lock:
                self._sessions.discard(token)
        resp = web.json_response({"ok": True})
        resp.del_cookie(self.COOKIE_NAME, path="/")
        return resp

    # --- LLM 工具 ---
    @llm_tool(name="toy_ble_set")
    async def toy_ble_set(self, event: AstrMessageEvent, function: str, mode: str, intensity: str, **kwargs):
        """控制玩具，设置功能、模式和强度。可用功能由插件配置决定，未知功能调用 toy_ble_list_functions 查询。

        Args:
            function(string): 功能英文 ID（如 vibrate、suck、pat 等），具体取决于插件配置。
            mode(string): 模式档位，整数，需在该功能配置的 mode_min~mode_max 之间，否则会被裁剪。
            intensity(string): 强度档位，整数，需在该功能配置的 intensity_min~intensity_max 之间，否则会被裁剪。
        """
        func = (function or "").strip()
        if func not in self.functions_by_name:
            available = ", ".join(self.functions_by_name.keys()) or "(无)"
            return f"未知功能 '{func}'，当前可用功能: {available}"
        fcfg = self.functions_by_name[func]
        try:
            m = int(str(mode).strip())
        except (TypeError, ValueError):
            m = fcfg["mode_min"]
        try:
            i = int(str(intensity).strip())
        except (TypeError, ValueError):
            i = fcfg["intensity_min"]
        m = max(fcfg["mode_min"], min(fcfg["mode_max"], m))
        i = max(fcfg["intensity_min"], min(fcfg["intensity_max"], i))
        _set_function(func, m, i)
        return f"已设置 {fcfg.get('display_name', func)} 模式{m} 强度{i}"

    @llm_tool(name="toy_ble_stop")
    async def toy_ble_stop(self, event: AstrMessageEvent, function: str = "all", **kwargs):
        """停止玩具功能。

        Args:
            function(string): 要停止的功能英文 ID，或填 'all' 停止全部。默认为 all。
        """
        func = (function or "all").strip() or "all"
        if func != "all" and func not in self.functions_by_name:
            available = ", ".join(list(self.functions_by_name.keys()) + ["all"])
            return f"未知功能 '{func}'，当前可停止: {available}"
        _stop_function(func)
        if func == "all":
            return "已全部停止"
        display = self.functions_by_name[func].get("display_name", func)
        return f"已停止 {display}"

    @llm_tool(name="toy_ble_status")
    async def toy_ble_status(self, event: AstrMessageEvent, **kwargs):
        """查看玩具当前运行状态，会列出所有正在运行的功能。"""
        s = _get_state()
        active = []
        for name, st in s["functions"].items():
            if st.get("active"):
                display = self.functions_by_name.get(name, {}).get("display_name", name)
                active.append(f"{display} 模式{st['mode']} 强度{st['intensity']}")
        if not active:
            return "当前状态: 全部停止"
        return "当前状态: " + " / ".join(active)

    @llm_tool(name="toy_ble_list_functions")
    async def toy_ble_list_functions(self, event: AstrMessageEvent, **kwargs):
        """列出本玩具当前配置中所有可用功能及其取值范围。若功能配置了模式描述，会一并返回，便于根据用户偏好选择模式。"""
        if not self.functions:
            return "未配置任何功能"
        lines = []
        for f in self.functions:
            lines.append(
                f"- {f['name']} ({f.get('display_name', f['name'])}): "
                f"mode {f['mode_min']}~{f['mode_max']}, "
                f"intensity {f['intensity_min']}~{f['intensity_max']}"
            )
            desc = (f.get("mode_description") or "").strip()
            if desc:
                indented = "\n".join("    " + line for line in desc.splitlines() if line.strip())
                if indented:
                    lines.append("  模式描述:")
                    lines.append(indented)
        return "可用功能:\n" + "\n".join(lines)

    async def terminate(self):
        """停止 HTTP 服务"""
        if self.runner:
            await self.runner.cleanup()
        if self._sock:
            self._sock.close()
        logger.info("[toy_ble_control] HTTP 中继服务已停止")
