import time
import asyncio
import threading
import socket
from aiohttp import web
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, llm_tool

# 全局状态
_state = {
    "cmd": "stop",
    "function": "none",
    "mode": 0,
    "intensity": 0,
    "updated_at": 0
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

# --- 中继网页 HTML ---
RELAY_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BLE Relay</title>
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
</style>
</head>
<body>
<h1>Svakom BLE Relay</h1>
<div class="status">
  <span class="dot off" id="dot"></span>
  <span id="statusText">未连接</span>
</div>
<div>
  <button id="btnConnect" onclick="connectBLE()">连接设备</button>
  <button id="btnDisconnect" onclick="disconnectBLE()">断开</button>
</div>
<div class="cmd-display" id="cmdDisplay">等待指令...</div>
<div id="log"></div>
<script>
const SERVICE_UUID = 0xFFE0;
const WRITE_UUID   = 0xFFE1;
const POLL_URL     = window.location.origin + '/state';

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

function buildCmd(func, mode, intensity) {
  if (func === 'suck') return new Uint8Array([0x55, 0x09, 0x00, 0x00, mode, intensity, 0x00]);
  if (func === 'vibrate') return new Uint8Array([0x55, 0x03, 0x00, 0x00, mode, intensity, 0x00]);
  if (func === 'pat') return new Uint8Array([0x55, 0x07, 0x00, mode, intensity, 0x00]);
  return null;
}

function buildStopCmd(func) {
  if (func === 'suck') return new Uint8Array([0x55, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00]);
  if (func === 'vibrate') return new Uint8Array([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00]);
  if (func === 'pat') return new Uint8Array([0x55, 0x07, 0x00, 0x00, 0x00, 0x00]);
  return new Uint8Array([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00]);
}

async function finishConnect(server) {
  log('GATT已连接，扫描服务...');
  try {
    const services = await server.getPrimaryServices();
    for (const svc of services) log('服务: ' + svc.uuid);
  } catch(e) { log('枚举服务失败: ' + e.message); }

  let service;
  try { service = await server.getPrimaryService(SERVICE_UUID); }
  catch(e) {
    log('FFE0未找到，尝试FFF0...');
    service = await server.getPrimaryService(0xFFF0);
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
    device = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true,
      optionalServices: [SERVICE_UUID, 0xFFE5, 0xFFF0, 0x1800, 0x1801]
    });
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
            const cmds = [
              new Uint8Array([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00]),
              new Uint8Array([0x55, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00]),
              new Uint8Array([0x55, 0x07, 0x00, 0x00, 0x00, 0x00])
            ];
            if (writeChar) { for (const c of cmds) { try { await writeChar.writeValue(c); } catch(e){} } log('全部停止'); }
          } else {
            const cmd = buildStopCmd(state.function);
            if (writeChar && cmd) { await writeChar.writeValue(cmd); log('停止' + state.function); }
          }
        } else if (state.cmd === 'set') {
          display.textContent = `${funcName} M${state.mode} I${state.intensity}`;
          const cmd = buildCmd(funcName, state.mode, state.intensity);
          if (writeChar && cmd) { await writeChar.writeValue(cmd); log(`${funcName} M${state.mode} I${state.intensity}`); }
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


@register("svakom_ble_control", "SXH", "BLE 玩具远程控制插件", "1.0.0")
class SvakomBLEPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.runner = None
        self.site = None
        self._sock = None

    async def initialize(self):
        """启动 HTTP 中继服务"""
        if self.runner:
            try: await self.runner.cleanup()
            except: pass
            self.runner = None
            self.site = None
        if self._sock:
            try: self._sock.close()
            except: pass
            self._sock = None

        app = web.Application()
        app.router.add_get('/', self._handle_relay_page)
        app.router.add_get('/state', self._handle_get_state)
        app.router.add_post('/state', self._handle_set_state)

        self.runner = web.AppRunner(app)
        await self.runner.setup()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', 5122))
        sock.listen(128)
        sock.setblocking(False)
        self.site = web.SockSite(self.runner, sock)
        await self.site.start()
        self._sock = sock
        logger.info("Svakom BLE 控制服务已启动，端口 5122")

    # --- HTTP ---
    async def _handle_relay_page(self, request):
        return web.Response(text=RELAY_HTML, content_type='text/html')

    async def _handle_get_state(self, request):
        return web.json_response(_get_state())

    async def _handle_set_state(self, request):
        data = await request.json()
        _set_state(
            cmd=data.get("cmd", "stop"),
            function=data.get("function", "none"),
            mode=data.get("mode", 0),
            intensity=data.get("intensity", 0)
        )
        return web.json_response({"ok": True})

    # --- LLM 工具 ---
    @llm_tool(name="toy_set")
    async def toy_set(self, event: AstrMessageEvent, function: str, mode: str, intensity: str):
        '''控制玩具，设置功能、模式和强度。

        Args:
            function(string): 功能类型，可选值 suck(吮吸) vibrate(振动) pat(拍打)
            mode(string): 模式档位，吮吸和振动为1到10，拍打为1到4
            intensity(string): 强度，1为低 2为中 3为高
        '''
        func = function
        m = int(mode)
        i = int(intensity)
        if func in ("suck", "vibrate"): m = max(1, min(10, m))
        elif func == "pat": m = max(1, min(4, m))
        i = max(1, min(3, i))
        _set_state("set", func, m, i)
        func_names = {"suck": "吮吸", "vibrate": "振动", "pat": "拍打"}
        return f"已设置{func_names.get(func, func)} 模式{m} 强度{i}"

    @llm_tool(name="toy_stop")
    async def toy_stop(self, event: AstrMessageEvent, function: str = "all"):
        '''停止玩具功能。

        Args:
            function(string): 要停止的功能。可选值：suck(吮吸) vibrate(振动) pat(拍打) all(全部停止)。默认为all。
        '''
        func = function if function else "all"
        _stop_function(func)
        if func == "all":
            return "已全部停止"
        func_names = {"suck": "吮吸", "vibrate": "振动", "pat": "拍打"}
        return f"已停止{func_names.get(func, func)}"

    @llm_tool(name="toy_status")
    async def toy_status(self, event: AstrMessageEvent):
        '''查看玩具当前运行状态。'''
        s = _get_state()
        if s["cmd"] == "stop": return "当前状态: 停止"
        func_names = {"suck": "吮吸", "vibrate": "振动", "pat": "拍打"}
        return f"当前状态: {func_names.get(s['function'], s['function'])} 模式{s['mode']} 强度{s['intensity']}"

    async def terminate(self):
        """停止 HTTP 服务"""
        if self.runner: await self.runner.cleanup()
        if self._sock: self._sock.close()
        logger.info("Svakom BLE 控制服务已停止")
