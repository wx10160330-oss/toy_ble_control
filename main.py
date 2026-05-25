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
_state = {
    "cmd": "stop",
    "function": "none",
    "mode": 0,
    "intensity": 0,
    "updated_at": 0,
}
_state_lock = threading.Lock()


def _get_state():
    with _state_lock:
        return dict(_state)


def _set_state(cmd, function="none", mode=0, intensity=0):
    with _state_lock:
        _state["cmd"] = cmd
        _state["function"] = function
        _state["mode"] = mode
        _state["intensity"] = intensity
        _state["updated_at"] = time.time()


def _stop_function(function="all"):
    with _state_lock:
        _state["cmd"] = "stop"
        _state["function"] = function
        _state["mode"] = 0
        _state["intensity"] = 0
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
let lastUpdatedAt = 0;

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

function startPolling() {
  if (polling) return;
  polling = setInterval(async () => {
    try {
      const res = await fetch(POLL_URL);
      const state = await res.json();
      if (state.updated_at > lastUpdatedAt) {
        lastUpdatedAt = state.updated_at;
        const funcName = state.function || 'none';
        const display = document.getElementById('cmdDisplay');
        if (state.cmd === 'stop') {
          display.textContent = '当前: 停止';
          if (state.function === 'all' || state.function === 'none') {
            for (const f of (TOY_CONFIG.functions || [])) {
              const cmd = parseHexTemplate(f.stop_template, 0, 0);
              if (cmd) { try { await writeBytes(cmd); } catch(e){} }
            }
            log('全部停止');
          } else {
            const cmd = buildStopCmd(state.function);
            if (cmd) { await writeBytes(cmd); log('停止' + state.function); }
            else { log('未知功能: ' + state.function); }
          }
        } else if (state.cmd === 'set') {
          display.textContent = `${funcName} M${state.mode} I${state.intensity}`;
          const cmd = buildCmd(funcName, state.mode, state.intensity);
          if (cmd) { await writeBytes(cmd); log(`${funcName} M${state.mode} I${state.intensity}`); }
          else { log('未知功能: ' + funcName); }
        }
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


def _normalize_functions(raw):
    """校验并归一化 functions 列表。允许部分字段缺失，给出合理默认。"""
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_FUNCTIONS)
    normalized = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning(f"functions[{idx}] 不是字典，跳过")
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            logger.warning(f"functions[{idx}] 缺少 name，跳过")
            continue
        normalized.append({
            "name": name,
            "display_name": str(item.get("display_name", name)),
            "command_template": str(item.get("command_template", "")),
            "stop_template": str(item.get("stop_template", "")),
            "mode_min": int(item.get("mode_min", 1)),
            "mode_max": int(item.get("mode_max", 10)),
            "intensity_min": int(item.get("intensity_min", 1)),
            "intensity_max": int(item.get("intensity_max", 3)),
        })
    return normalized or list(DEFAULT_FUNCTIONS)


def _parse_functions_config(value):
    """支持配置里既可以是 JSON 字符串，也可以是已经解析好的 list/dict。"""
    if value is None or value == "":
        return list(DEFAULT_FUNCTIONS)
    if isinstance(value, list):
        return _normalize_functions(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            logger.error(f"functions_json 解析失败，使用默认配置: {e}")
            return list(DEFAULT_FUNCTIONS)
        return _normalize_functions(parsed)
    logger.warning(f"未知 functions 配置类型 {type(value)}，使用默认")
    return list(DEFAULT_FUNCTIONS)


@register("toy_ble_control", "SXH", "可配置的 BLE 玩具远程控制插件", "2.0.0")
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
        self.functions = _parse_functions_config(g("functions_json", ""))
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
        data = await request.json()
        _set_state(
            cmd=data.get("cmd", "stop"),
            function=data.get("function", "none"),
            mode=data.get("mode", 0),
            intensity=data.get("intensity", 0),
        )
        return web.json_response({"ok": True})

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
    @llm_tool(name="toy_set")
    async def toy_set(self, event: AstrMessageEvent, function: str, mode: str, intensity: str):
        """控制玩具，设置功能、模式和强度。可用功能由插件配置决定，未知功能调用 toy_list_functions 查询。

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
        _set_state("set", func, m, i)
        return f"已设置 {fcfg.get('display_name', func)} 模式{m} 强度{i}"

    @llm_tool(name="toy_stop")
    async def toy_stop(self, event: AstrMessageEvent, function: str = "all"):
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

    @llm_tool(name="toy_status")
    async def toy_status(self, event: AstrMessageEvent):
        """查看玩具当前运行状态。"""
        s = _get_state()
        if s["cmd"] == "stop":
            return "当前状态: 停止"
        func = s["function"]
        display = self.functions_by_name.get(func, {}).get("display_name", func)
        return f"当前状态: {display} 模式{s['mode']} 强度{s['intensity']}"

    @llm_tool(name="toy_list_functions")
    async def toy_list_functions(self, event: AstrMessageEvent):
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
