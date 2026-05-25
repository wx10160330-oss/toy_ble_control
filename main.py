import time
import json
import threading
import socket
import html
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
    },
]

DEFAULT_OPTIONAL_SERVICES = ["0xFFE0", "0xFFE5", "0xFFF0", "0x1800", "0x1801"]


# 中继网页 HTML 模板。TOY_CONFIG_JSON 占位符会在运行时替换为序列化后的配置。
RELAY_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TOY_TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0f;color:#e0e0e0;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:20px}
h1{font-size:1.2em;margin-bottom:16px;color:#a78bfa}
.status{padding:12px 20px;border-radius:12px;background:#1a1a2e;margin-bottom:16px;text-align:center;min-width:280px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:middle}
.dot.off{background:#ef4444}
.dot.on{background:#22c55e}
button{padding:12px 32px;border:none;border-radius:12px;font-size:1em;cursor:pointer;margin:6px;transition:all .2s}
#btnConnect{background:#7c3aed;color:#fff}
#btnConnect:hover{background:#6d28d9}
#btnDisconnect{background:#ef4444;color:#fff;display:none}
#log{margin-top:16px;padding:12px;background:#111;border-radius:8px;width:100%;max-width:400px;height:200px;overflow-y:auto;font-size:0.8em;font-family:monospace;color:#9ca3af}
.cmd-display{font-size:0.95em;color:#c4b5fd;margin-top:8px}
.toy-name{font-size:0.85em;color:#9ca3af;margin-top:4px}
</style>
</head>
<body>
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
<div id="log"></div>
<script>
const TOY_CONFIG = __TOY_CONFIG_JSON__;
const POLL_URL = window.location.origin + '/state';

document.getElementById('toyName').textContent = TOY_CONFIG.toy_name || '';

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

// 页面加载后自动尝试重连
window.addEventListener('load', () => {
  if (navigator.bluetooth && navigator.bluetooth.getDevices) {
    setTimeout(connectBLE, 500);
  }
});
</script>
</body>
</html>"""


# 协议探测页面：帮助用户半自动地从「未知玩具」抓出指令格式，省掉手工对比字节的活。
# 流程参考 README 第五节「如何探测自己玩具的协议」，把里面的步骤搬进网页。
PROBE_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>协议探测器 - toy_ble_control</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0f;color:#e0e0e0;min-height:100vh;padding:16px;line-height:1.55}
h1{font-size:1.3em;margin-bottom:8px;color:#a78bfa}
h2{font-size:1.05em;margin:20px 0 8px;color:#c4b5fd;border-left:3px solid #7c3aed;padding-left:8px}
p,.muted{color:#9ca3af;font-size:0.9em;margin-bottom:8px}
.warn{background:#451a03;color:#fbbf24;padding:10px 12px;border-left:3px solid #f59e0b;border-radius:6px;margin:8px 0;font-size:0.9em}
.danger{background:#450a0a;color:#fca5a5;padding:10px 12px;border-left:3px solid #ef4444;border-radius:6px;margin:8px 0;font-size:0.9em}
.ok{background:#052e16;color:#86efac;padding:10px 12px;border-left:3px solid #22c55e;border-radius:6px;margin:8px 0;font-size:0.9em}
.card{background:#1a1a2e;border-radius:10px;padding:12px;margin:8px 0}
button{padding:10px 18px;border:none;border-radius:10px;font-size:0.92em;cursor:pointer;margin:4px 4px 4px 0;transition:all .15s;background:#374151;color:#fff}
button:hover{background:#4b5563}
button.primary{background:#7c3aed}
button.primary:hover{background:#6d28d9}
button.danger{background:#dc2626}
button.danger:hover{background:#b91c1c}
button:disabled{background:#1f2937;color:#6b7280;cursor:not-allowed}
button.small{padding:6px 12px;font-size:0.85em}
input,select,textarea{background:#0f0f1a;color:#e0e0e0;border:1px solid #374151;border-radius:8px;padding:8px 10px;font-size:0.9em;font-family:inherit}
input[type=number]{width:70px}
input[type=text]{width:130px}
textarea{width:100%;font-family:monospace;font-size:0.85em}
table{width:100%;border-collapse:collapse;margin:8px 0}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #1f2937;font-size:0.88em;vertical-align:middle}
th{color:#9ca3af;font-weight:500;font-size:0.85em}
.bytes{font-family:monospace;font-size:0.85em;color:#86efac;word-break:break-all}
.tag{display:inline-block;padding:2px 6px;border-radius:4px;background:#374151;color:#d1d5db;font-size:0.75em;margin-right:4px}
.tag.write{background:#1e3a8a;color:#bfdbfe}
.tag.notify{background:#365314;color:#bef264}
.tag.read{background:#374151;color:#d1d5db}
.tag.danger{background:#7f1d1d;color:#fecaca}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.on{background:#22c55e}.dot.off{background:#ef4444}
#log{margin-top:6px;padding:8px;background:#050510;border-radius:6px;max-height:140px;overflow-y:auto;font-size:0.78em;font-family:monospace;color:#9ca3af;white-space:pre-wrap}
.row-actions{white-space:nowrap}
.position-mode{color:#fbbf24;font-weight:bold}
.position-intensity{color:#34d399;font-weight:bold}
.position-fixed{color:#9ca3af}
.position-funccode{color:#a78bfa;font-weight:bold}
.position-unknown{color:#ef4444;font-weight:bold}
a{color:#a78bfa}
</style>
</head>
<body>
<h1>协议探测器</h1>
<p>帮你半自动反解出玩具的 BLE 协议格式。最终会输出一段可以直接粘贴进 toy_ble_control 配置面板的 JSON。<br>
具体原理请先看 <a href="https://github.com/wx10160330-oss/toy_ble_control#五如何探测自己玩具的协议首次配置必读" target="_blank">README 第五节</a>。</p>
<div class="danger">⚠️ <b>重要</b>：BLE 同时只允许一个主机连接。每次你要用<b>官方 APP</b> 操作玩具时，必须先点这里的"断开"，把玩具让出来；操作完再点"重连"。</div>
<div class="danger">⚠️ <b>千万别碰 0xAE00 服务</b>。那是 Telink 芯片的固件升级（OTA）通道，乱写会变砖。下方列表里这个服务会标红，看到就跳过。</div>

<h2>第 1 步：连接玩具</h2>

<div class="card">
  <b>高级：自定义 Service UUID</b>（默认不填，只在玩具连上后看不到任何服务时才需要）<br>
  <span class="muted">浏览器 Web Bluetooth 规定网页必须<b>预先声明</b>要访问哪些服务才能枚举特征。本页面内置了几个常见服务【<code>0xFFE0 0xFFE5 0xFFF0 0xFFF1 0xFFB0 0xFD00 0xFD01 0xFD30 0x1800 0x1801 0x180A 0x180F</code>】，足够覆盖 Svakom 和部分 Magic Motion。其它玩具如果连上后看不到任何特征（第 2 步是空的），就照下面教程查一下 UUID 填进来。</span>
  <div style="margin-top:8px">
    <input type="text" id="customUuids" placeholder="如 ： 7c490001-2e4e-4d8b-9b89-5fef96a98a72  或 0xFD30  或多个逗号隔开" style="width:100%;max-width:520px;font-family:monospace;font-size:0.85em">
    <div class="muted" style="font-size:0.8em;margin-top:4px">多个 UUID 用逗号 / 空格 / 换行分隔都行。填完点下面「连接玩具」生效。</div>
  </div>
</div>

<details class="card" style="border-color:#3b3b5e">
  <summary style="cursor:pointer;color:#c4b5fd"><b>我怎么知道要填哪个 UUID？</b> （用 nRF Connect 查 1 分钟就能看到）</summary>
  <div style="margin-top:8px;font-size:0.88em;line-height:1.6">
    <p>手机装 nRF Connect （iOS 上叫 <i>nRF Connect for Mobile</i>，Android 同名）应用。玩具充电开机、不要连官方 APP。</p>
    <ol style="margin:0 0 8px 22px">
      <li>打开 nRF Connect <code>扫描 (Scanner)</code> 页，点【<b>Start scanning</b>】</li>
      <li>在列表里找到你玩具的名字（按玩具的品牌型号，一般能看出来，如果多个可以靠近手机看信号强度最高的那个）</li>
      <li>点【<b>Connect / 连接</b>】，进入该设备详情页</li>
      <li>页面里会列出几个 Service。除了下面这几个有明确名字的<b>标准 BLE 服务</b>，剩下的都会显示成 <code>Unknown Service</code>：
        <ul style="margin:4px 0 4px 22px">
          <li><code>Generic Access</code> / <code>Generic Attribute</code> / <code>Device Information</code> / <code>Battery Service</code> — 这些是手机和玩具之间的通用协议，<b>不是玩具本体</b>，跳过</li>
          <li><code>0xAE00</code> / <code>0xAE01</code> — Telink 芯片的 OTA 升级通道，<b>千万别点它</b>，乱写会变砖</li>
        </ul>
      </li>
      <li>找标着【<b>Unknown Service</b>】的那一项（一般 1~2 个），<b>把它下面那一行 UUID 整个复制下来</b>，粘到上面的输入框里。
        <ul style="margin:4px 0 4px 22px">
          <li>UUID 可能是<b>短的</b>，像 <code>0xFFE0</code> / <code>0xFFB0</code> 这样的 4 位 hex（这种本页面已经在内置名单里，<b>填了也行不填也行</b>）</li>
          <li>也可能是<b>长的</b>，像 <code>5a300001-0023-4bd4-bbd5-a6920e4c5653</code> 这种带横杠的 36 位（这种<b>必须填</b>否则浏览器看不到）</li>
          <li>不管哪种，看到啥粘啥就对了</li>
        </ul>
      </li>
      <li>多个 Unknown Service 就一起粘进来，逗号 / 空格 / 换行分隔都行</li>
      <li>填完按手机返回键让 nRF Connect 断开玩具，再回本页面点【连接玩具】</li>
    </ol>
    <div class="warn" style="margin-top:8px">
      <b>已知机型直接对号入座</b>：
      <ul style="margin:4px 0 4px 22px">
        <li><b>Svakom</b>（SA253B 等）→ Service <code>0xFFE0</code>、Write/Notify <code>0xFFE1</code>。<b>本页面已经内置 0xFFE0，输入框留空、直接点【连接玩具】就行，不用填任何东西。</b></li>
        <li><b>Lovense</b> → Service UUID 一般 <code>5a30...</code> / <code>5300...</code> 开头</li>
        <li><b>Lelo</b> → Service UUID 一般是 <code>6e400001-...</code>（Nordic UART）</li>
        <li><b>Kiiroo</b> → Service UUID 一般是 <code>88f80001-...</code></li>
      </ul>
      Lovense / Lelo / Kiiroo 这几个如果懒得装 nRF Connect，可以直接拿同品牌别人解出来的完整 UUID 粘进来试一下。反正错了浏览器就连不上服务，不会变砖。
    </div>
  </div>
</details>

<div class="card">
  <span class="dot off" id="dot"></span><span id="connStatus">未连接</span>
  <span id="deviceName" class="muted"></span>
  <div style="margin-top:8px">
    <button id="btnConnect" class="primary" onclick="connectBLE()">连接玩具</button>
    <button id="btnDisconnect" class="danger" onclick="disconnectBLE()" disabled>断开</button>
    <button id="btnReconnect" onclick="reconnectBLE()" disabled>重连（不弹设备选择框）</button>
  </div>
</div>

<h2>第 2 步：选择 Write / Notify 特征</h2>
<p>连接成功后，下面会自动列出玩具暴露的所有特征。<b>带 WRITE 标签</b>的是写入通道（用来发指令），<b>带 NOTIFY 标签</b>的是通知通道（用来读玩具回报的状态字节）。</p>
<div id="charList" class="card muted">尚未连接。</div>

<h2>第 3 步：捕获各档位的字节</h2>
<p>给每一行起一个 <b>功能英文名 / 模式 / 强度</b> 的标签，再按下面的<b>录制循环</b>把对应字节填进去。建议至少为每个功能采集 3~5 个不同档位（例如 mode=1/5/10，intensity=1/2/3）。</p>

<div class="card">
  <b>录制单个状态的标准流程</b>（每次都要这样做）：
  <ol style="margin:6px 0 0 22px;font-size:0.88em">
    <li>点上方"断开"，把玩具让给官方 APP</li>
    <li>用官方 APP 把玩具调到目标状态（如：振动 模式 5 强度 2）</li>
    <li>退出官方 APP（让它释放蓝牙）</li>
    <li>回到这里点"重连"</li>
    <li>找到对应行，点【捕获当前 Notify】</li>
  </ol>
</div>

<div style="margin:8px 0">
  <button class="small" onclick="addRow()">+ 添加一行</button>
  <button class="small" onclick="fillDefaultRows()">一键填入常见档位</button>
  <button class="small danger" onclick="clearRows()">清空</button>
</div>
<table id="captureTable">
  <thead><tr><th>功能名</th><th>模式</th><th>强度</th><th>字节数据 (Notify)</th><th>操作</th></tr></thead>
  <tbody id="captureRows"></tbody>
</table>

<h2>第 4 步：分析协议</h2>
<button class="primary" onclick="analyze()">开始分析</button>
<div id="analysis" class="card muted">捕获至少 2 行（同一个功能下不同档位）后再点分析。</div>

<h2>第 5 步：导出配置</h2>
<p>把下面这段 JSON 复制到 AstrBot 后台的 toy_ble_control 插件配置里。你也可以拆成单独字段，对照填到"功能列表"面板里。</p>
<textarea id="jsonOut" rows="10" readonly placeholder="等待分析..."></textarea>
<div><button onclick="copyJson()">复制 JSON</button></div>

<h2>日志</h2>
<div id="log"></div>

<script>
"use strict";

// 蓝牙状态
let device = null;
let server = null;
let allChars = []; // [{service, char, ref, props, isWrite, isNotify}]
let writeRef = null;
let notifyRef = null;
let lastNotifyHex = null; // string '55 03 00 00 ...'
let notifySubscribed = false;

// 常见服务白名单（要先声明给浏览器，否则后续找不到）
const BUILTIN_OPTIONAL_SERVICES = [
  0xFFE0, 0xFFE5, 0xFFF0, 0xFFF1, 0xFFB0,
  0x1800, 0x1801, 0x180A, 0x180F,
  0xFD00, 0xFD01, 0xFD30,
  // 其它常见的国产玩具方案商服务
];

// 解析用户输入的 UUID，返回可供 Web Bluetooth 使用的格式（数字 / 128 位字符串）。无法解析返回 null。
function parseUuidInput(s) {
  if (!s) return null;
  s = String(s).trim().toLowerCase().replace(/^0x/, '');
  if (/^[0-9a-f]{1,4}$/.test(s)) {
    return parseInt(s, 16);
  }
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(s)) {
    return s;
  }
  if (/^[0-9a-f]{32}$/.test(s)) {
    return s.slice(0, 8) + '-' + s.slice(8, 12) + '-' + s.slice(12, 16) + '-' + s.slice(16, 20) + '-' + s.slice(20);
  }
  return null;
}

function getCustomUuids() {
  const raw = (document.getElementById('customUuids').value || '').trim();
  if (!raw) return [];
  const tokens = raw.split(/[\s,;\n]+/).filter(Boolean);
  const out = [];
  for (const tok of tokens) {
    const parsed = parseUuidInput(tok);
    if (parsed === null) {
      log(`⚠️ 忽略无法解析的 UUID: ${tok}`);
      continue;
    }
    out.push(parsed);
  }
  return out;
}
// OTA / 危险服务（看到就警告，绝对不让操作）
const DANGER_SERVICES = new Set(['0000ae00-0000-1000-8000-00805f9b34fb', '0000ae01-0000-1000-8000-00805f9b34fb']);

const rows = []; // {func, mode, intensity, bytesHex}

function log(msg) {
  const el = document.getElementById('log');
  const t = new Date().toLocaleTimeString();
  el.textContent = `[${t}] ${msg}\n` + el.textContent;
}

function setStatus(connected) {
  document.getElementById('dot').className = 'dot ' + (connected ? 'on' : 'off');
  document.getElementById('connStatus').textContent = connected ? '已连接' : '未连接';
  document.getElementById('btnConnect').disabled = connected;
  document.getElementById('btnDisconnect').disabled = !connected;
  document.getElementById('btnReconnect').disabled = !device;
}

function bytesToHex(buffer) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join(' ');
}

function uuidShort(uuid) {
  // 把 16-bit UUID 简化展示
  const m = /^0000([0-9a-f]{4})-0000-1000-8000-00805f9b34fb$/i.exec(uuid);
  return m ? ('0x' + m[1].toUpperCase()) : uuid;
}

function propsArray(props) {
  const out = [];
  if (props.write) out.push('WRITE');
  if (props.writeWithoutResponse) out.push('WRITE-NO-RESPONSE');
  if (props.notify) out.push('NOTIFY');
  if (props.indicate) out.push('INDICATE');
  if (props.read) out.push('READ');
  return out;
}

async function connectBLE() {
  try {
    log('请求设备...');
    const customs = getCustomUuids();
    if (customs.length) log(`已加入自定义 UUID: ${customs.map(c => typeof c === 'number' ? '0x' + c.toString(16).toUpperCase() : c).join(', ')}`);
    const optionalServices = BUILTIN_OPTIONAL_SERVICES.concat(customs);
    device = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true,
      optionalServices,
    });
    device.addEventListener('gattserverdisconnected', onDisconnected);
    document.getElementById('deviceName').textContent = ' / ' + (device.name || '(无名称)');
    log('已选择: ' + (device.name || device.id));
    await openGatt();
  } catch (e) {
    log('连接失败: ' + e.message);
  }
}

async function reconnectBLE() {
  if (!device) { log('还没选过设备'); return; }
  try {
    log('重连中...');
    await openGatt();
  } catch (e) {
    log('重连失败: ' + e.message);
  }
}

async function openGatt() {
  server = await device.gatt.connect();
  log('GATT 已连接，正在枚举特征...');
  await enumerateAll();
  setStatus(true);
  // 如果之前已经选过 notify 特征，自动重新订阅
  if (notifyRef) {
    try {
      const found = allChars.find(c => c.char === notifyRef.uuid && c.service === notifyRef.service.uuid);
      if (found) {
        notifyRef = found.ref;
        await subscribeNotify();
      }
    } catch (e) { log('自动重订阅 notify 失败: ' + e.message); }
  }
}

async function disconnectBLE() {
  notifySubscribed = false;
  try {
    if (device && device.gatt.connected) {
      device.gatt.disconnect();
    }
  } catch (e) { log('断开异常: ' + e.message); }
  setStatus(false);
  log('已断开，现在可以打开官方 APP 操作玩具了');
}

function onDisconnected() {
  notifySubscribed = false;
  setStatus(false);
  log('设备主动断开');
}

async function enumerateAll() {
  allChars = [];
  let services;
  try {
    services = await server.getPrimaryServices();
  } catch (e) {
    log('枚举服务失败: ' + e.message);
    return;
  }
  for (const svc of services) {
    let chars;
    try { chars = await svc.getCharacteristics(); }
    catch (e) { log('  特征枚举失败 (' + uuidShort(svc.uuid) + '): ' + e.message); continue; }
    for (const c of chars) {
      allChars.push({
        service: svc.uuid,
        char: c.uuid,
        ref: c,
        props: c.properties,
        isWrite: c.properties.write || c.properties.writeWithoutResponse,
        isNotify: c.properties.notify || c.properties.indicate,
        isDanger: DANGER_SERVICES.has(svc.uuid),
      });
    }
  }
  renderCharList();
}

function renderCharList() {
  const cont = document.getElementById('charList');
  if (!allChars.length) { cont.textContent = '没枚举到任何特征。'; return; }
  // 自动选默认（如果用户还没选）
  if (!writeRef) {
    const candidate = allChars.find(c => c.isWrite && !c.isDanger);
    if (candidate) writeRef = candidate.ref;
  }
  if (!notifyRef) {
    const candidate = allChars.find(c => c.isNotify && !c.isDanger);
    if (candidate) notifyRef = candidate.ref;
  }
  let html = '<table><thead><tr><th>服务</th><th>特征</th><th>属性</th><th>用作</th></tr></thead><tbody>';
  for (const c of allChars) {
    const propsLabel = propsArray(c.props).map(p => {
      const cls = p.startsWith('WRITE') ? 'write' : (p.startsWith('NOTIFY') || p === 'INDICATE') ? 'notify' : 'read';
      return `<span class="tag ${cls}">${p}</span>`;
    }).join('');
    const dangerLabel = c.isDanger ? '<span class="tag danger">⚠️ OTA / 别碰</span>' : '';
    const writeBtn = c.isWrite && !c.isDanger
      ? `<button class="small ${writeRef && writeRef.uuid === c.char ? 'primary' : ''}" onclick="selectWrite('${c.service}','${c.char}')">${writeRef && writeRef.uuid === c.char ? '✓ Write' : '设为 Write'}</button>`
      : '';
    const notifyBtn = c.isNotify && !c.isDanger
      ? `<button class="small ${notifyRef && notifyRef.uuid === c.char ? 'primary' : ''}" onclick="selectNotify('${c.service}','${c.char}')">${notifyRef && notifyRef.uuid === c.char ? '✓ Notify' : '设为 Notify'}</button>`
      : '';
    html += `<tr>
      <td>${uuidShort(c.service)} ${dangerLabel}</td>
      <td>${uuidShort(c.char)}</td>
      <td>${propsLabel}</td>
      <td>${writeBtn} ${notifyBtn}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  cont.innerHTML = html;
}

function selectWrite(svcUuid, charUuid) {
  const found = allChars.find(c => c.service === svcUuid && c.char === charUuid);
  if (!found) return;
  writeRef = found.ref;
  log('Write -> ' + uuidShort(charUuid));
  renderCharList();
}

async function selectNotify(svcUuid, charUuid) {
  const found = allChars.find(c => c.service === svcUuid && c.char === charUuid);
  if (!found) return;
  notifyRef = found.ref;
  log('Notify -> ' + uuidShort(charUuid));
  await subscribeNotify();
  renderCharList();
}

async function subscribeNotify() {
  if (!notifyRef) return;
  try {
    notifyRef.addEventListener('characteristicvaluechanged', (e) => {
      const v = e.target.value;
      lastNotifyHex = bytesToHex(v.buffer);
      log('Notify 推送: ' + lastNotifyHex);
    });
    await notifyRef.startNotifications();
    notifySubscribed = true;
    log('已订阅 Notify');
    // 顺手读一次当前值
    try {
      const v = await notifyRef.readValue();
      lastNotifyHex = bytesToHex(v.buffer);
      log('Notify 当前值: ' + lastNotifyHex);
    } catch (e) {/* 不支持 read 也没关系 */}
  } catch (e) {
    log('订阅 Notify 失败: ' + e.message);
  }
}

// ---------- 捕获表格 ----------
function addRow(preset) {
  rows.push(Object.assign({ func: '', mode: 1, intensity: 1, bytesHex: '' }, preset || {}));
  renderRows();
}

function clearRows() {
  if (!confirm('清空所有捕获行？')) return;
  rows.length = 0;
  renderRows();
}

function fillDefaultRows() {
  // 一组合理的默认采样点：3 个功能 × (低、中、高 mode) × (低、高 intensity)
  // 用户后面随便改
  rows.length = 0;
  const funcs = ['vibrate', 'suck', 'pat'];
  for (const f of funcs) {
    for (const m of [1, 5, 10]) {
      for (const i of [1, 3]) {
        rows.push({ func: f, mode: m, intensity: i, bytesHex: '' });
      }
    }
    // 加一个 stop
    rows.push({ func: f, mode: 0, intensity: 0, bytesHex: '' });
  }
  renderRows();
}

function renderRows() {
  const tbody = document.getElementById('captureRows');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="5" class="muted">还没行。点上方按钮添加。</td></tr>'; return; }
  let html = '';
  for (let idx = 0; idx < rows.length; idx++) {
    const r = rows[idx];
    html += `<tr>
      <td><input type="text" value="${r.func.replace(/"/g, '&quot;')}" onchange="rows[${idx}].func=this.value"></td>
      <td><input type="number" value="${r.mode}" onchange="rows[${idx}].mode=parseInt(this.value)||0"></td>
      <td><input type="number" value="${r.intensity}" onchange="rows[${idx}].intensity=parseInt(this.value)||0"></td>
      <td class="bytes">${r.bytesHex || '<span class="muted">(未捕获)</span>'}</td>
      <td class="row-actions">
        <button class="small primary" onclick="captureRow(${idx})">捕获当前 Notify</button>
        <button class="small danger" onclick="deleteRow(${idx})">×</button>
      </td>
    </tr>`;
  }
  tbody.innerHTML = html;
}

function deleteRow(idx) {
  rows.splice(idx, 1);
  renderRows();
}

async function captureRow(idx) {
  if (!device || !device.gatt.connected) { alert('请先连接玩具'); return; }
  if (!notifyRef) { alert('请先在第 2 步选一个 Notify 特征'); return; }
  // 先尝试主动 readValue 拿最新值
  try {
    const v = await notifyRef.readValue();
    lastNotifyHex = bytesToHex(v.buffer);
  } catch (e) {
    // 不支持 read 就用最近一次 push 进来的值
    log('readValue 不支持，用最近一次 push 值');
  }
  if (!lastNotifyHex) { alert('还没有 notify 值。请先用官方 APP 把玩具调到目标状态，再回来点捕获。'); return; }
  rows[idx].bytesHex = lastNotifyHex;
  log(`第 ${idx + 1} 行已捕获: ${lastNotifyHex}`);
  renderRows();
}

// ---------- 分析 ----------
function parseHexStr(s) {
  return (s || '').trim().split(/\s+/).filter(Boolean).map(t => parseInt(t, 16) & 0xff);
}

function analyze() {
  const captured = rows.filter(r => r.func && r.bytesHex);
  if (captured.length < 2) {
    document.getElementById('analysis').innerHTML = '<span class="danger" style="display:inline-block">至少需要 2 条捕获记录才能分析。</span>';
    return;
  }
  // 按功能分组
  const byFunc = {};
  for (const r of captured) {
    (byFunc[r.func] = byFunc[r.func] || []).push(r);
  }
  // 收集功能码候选：跨功能时哪些位变了
  let html = '';
  const cfgFunctions = [];
  for (const [func, list] of Object.entries(byFunc)) {
    html += `<h3 style="margin-top:12px;color:#c4b5fd">功能 <code>${func}</code> （${list.length} 条样本）</h3>`;
    // 校验所有样本字节长度
    const lengths = new Set(list.map(r => parseHexStr(r.bytesHex).length));
    if (lengths.size > 1) {
      html += `<div class="warn">该功能的样本字节长度不一致（${Array.from(lengths).join('/')}）。常见原因：协议本身就是变长（如 ASCII 字符串协议），目前的插件不支持这种。请检查是否捕获了正确的 Notify 通道。</div>`;
      continue;
    }
    const L = list[0].bytesHex ? parseHexStr(list[0].bytesHex).length : 0;
    if (!L) { html += '<div class="warn">样本字节为空</div>'; continue; }
    const positions = [];
    for (let p = 0; p < L; p++) {
      const vals = list.map(r => parseHexStr(r.bytesHex)[p]);
      const modes = list.map(r => r.mode);
      const ints = list.map(r => r.intensity);
      if (vals.every(v => v === vals[0])) {
        positions.push({ p, kind: 'fixed', value: vals[0] });
      } else if (vals.every((v, i) => v === modes[i])) {
        positions.push({ p, kind: 'mode' });
      } else if (vals.every((v, i) => v === ints[i])) {
        positions.push({ p, kind: 'intensity' });
      } else {
        positions.push({ p, kind: 'unknown', values: vals });
      }
    }
    // 渲染表格：每列是一个字节位置
    let posHtml = '<div style="overflow-x:auto"><table><thead><tr><th>样本</th>';
    for (let p = 0; p < L; p++) posHtml += `<th style="text-align:center">[${p}]</th>`;
    posHtml += '</tr></thead><tbody>';
    for (const r of list) {
      const bytes = parseHexStr(r.bytesHex);
      posHtml += `<tr><td><b>${r.func}</b> M${r.mode} I${r.intensity}</td>`;
      for (let p = 0; p < L; p++) {
        const cls = 'position-' + positions[p].kind;
        posHtml += `<td class="${cls}" style="text-align:center">${bytes[p].toString(16).padStart(2,'0')}</td>`;
      }
      posHtml += '</tr>';
    }
    posHtml += '<tr><td><b>位置含义</b></td>';
    for (const pos of positions) {
      let label = pos.kind;
      if (pos.kind === 'fixed') label = pos.value.toString(16).padStart(2,'0');
      else if (pos.kind === 'mode') label = '{mode}';
      else if (pos.kind === 'intensity') label = '{intensity}';
      else label = '?';
      posHtml += `<td class="position-${pos.kind}" style="text-align:center">${label}</td>`;
    }
    posHtml += '</tr></tbody></table></div>';
    html += posHtml;
    // 生成 command_template
    const tplTokens = positions.map(pos => {
      if (pos.kind === 'fixed') return pos.value.toString(16).padStart(2,'0');
      if (pos.kind === 'mode') return '{mode}';
      if (pos.kind === 'intensity') return '{intensity}';
      return '??';
    });
    const commandTpl = tplTokens.join(' ');
    const stopTpl = tplTokens.map(t => (t === '{mode}' || t === '{intensity}') ? '00' : t).join(' ');
    const hasUnknown = positions.some(p => p.kind === 'unknown');
    if (hasUnknown) {
      html += `<div class="warn">检测到 ${positions.filter(p=>p.kind==='unknown').length} 个未知字节位置（红色 ?）。可能是：(1) 该位置由其它输入决定（例如内部计时、电压、校验和）；(2) 你的样本不足以推断；(3) 该协议非「单字节值编码」（如 ASCII 字符串协议，本插件暂不支持）。请检查上表，必要时补充更多档位再分析。</div>`;
    }
    html += `<div class="ok"><b>启动模板</b>: <code>${commandTpl}</code><br><b>停止模板</b>: <code>${stopTpl}</code></div>`;
    // mode / intensity 取值范围
    const modes = list.map(r => r.mode).filter(m => m > 0);
    const ints = list.map(r => r.intensity).filter(i => i > 0);
    cfgFunctions.push({
      name: func,
      display_name: func,
      command_template: commandTpl,
      stop_template: stopTpl,
      mode_min: modes.length ? Math.min(...modes) : 1,
      mode_max: modes.length ? Math.max(...modes) : 10,
      intensity_min: ints.length ? Math.min(...ints) : 1,
      intensity_max: ints.length ? Math.max(...ints) : 3,
    });
  }
  document.getElementById('analysis').innerHTML = html;
  document.getElementById('jsonOut').value = JSON.stringify(cfgFunctions, null, 2);
  log('分析完成，已生成 ' + cfgFunctions.length + ' 个功能的配置');
}

async function copyJson() {
  const t = document.getElementById('jsonOut').value;
  if (!t) { alert('还没分析。请先点"开始分析"。'); return; }
  try {
    await navigator.clipboard.writeText(t);
    log('JSON 已复制到剪贴板');
  } catch (e) {
    // 旧浏览器兜底
    const ta = document.getElementById('jsonOut');
    ta.select(); document.execCommand('copy');
    log('JSON 已复制（兼容模式）');
  }
}

renderRows();
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


@register("toy_ble_control", "SXH", "可配置的 BLE 玩具远程控制插件", "2.4.1")
class ToyBLEPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.runner = None
        self.site = None
        self._sock = None
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

        app = web.Application()
        app.router.add_get("/", self._handle_relay_page)
        app.router.add_get("/state", self._handle_get_state)
        app.router.add_post("/state", self._handle_set_state)
        app.router.add_get("/config", self._handle_get_config)
        app.router.add_get("/probe", self._handle_probe_page)

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

    async def _handle_probe_page(self, request):
        """协议探测页面：帮助用户从未知玩具反解出 BLE 指令格式。不需要服务端状态，全部在浏览器端的 Web Bluetooth API 上运行。"""
        return web.Response(text=PROBE_HTML_TEMPLATE, content_type="text/html")

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

    # --- LLM 工具 ---
    @llm_tool(name="toy_ble_set")
    async def toy_ble_set(self, event: AstrMessageEvent, function: str, mode: str, intensity: str):
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
    async def toy_ble_stop(self, event: AstrMessageEvent, function: str = "all"):
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
    async def toy_ble_status(self, event: AstrMessageEvent):
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
    async def toy_ble_list_functions(self, event: AstrMessageEvent):
        """列出本玩具当前配置中所有可用功能及其取值范围。"""
        if not self.functions:
            return "未配置任何功能"
        lines = []
        for f in self.functions:
            lines.append(
                f"- {f['name']} ({f.get('display_name', f['name'])}): "
                f"mode {f['mode_min']}~{f['mode_max']}, "
                f"intensity {f['intensity_min']}~{f['intensity_max']}"
            )
        return "可用功能:\n" + "\n".join(lines)

    async def terminate(self):
        """停止 HTTP 服务"""
        if self.runner:
            await self.runner.cleanup()
        if self._sock:
            self._sock.close()
        logger.info("[toy_ble_control] HTTP 中继服务已停止")
