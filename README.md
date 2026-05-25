# 通用 BLE 玩具远程控制插件（toy_ble_control）

基于 AstrBot 的蓝牙玩具远程控制插件，通过 Web Bluetooth API 实现跨设备控制。**所有 UUID、指令格式、功能定义均可通过配置文件填写**，可适配 Svakom、Lovense（BLE 型号）、Magic Motion、Kiiroo 等任意基于 GATT 写入的 BLE 玩具。

---

## 一、原理

```
LLM 指令  →  AstrBot 插件 (HTTP 中继服务)  →  手机浏览器 (Web Bluetooth)  →  蓝牙玩具
```

- **AstrBot 插件**：在服务器上启动一个 HTTP 服务（默认端口 5122），提供中继网页 + 状态接口。
- **手机浏览器**：打开中继网页，通过 Web Bluetooth API 连接蓝牙玩具，并轮询服务器获取最新指令。
- **LLM 工具调用**：AI 通过插件注册的工具（`toy_set` / `toy_stop` / `toy_status` / `toy_list_functions`）修改服务器状态；手机端轮询到新状态后通过 BLE 发送对应字节指令。

---

## 二、安装

1. 把整个仓库克隆 / 解压到 AstrBot 的 `data/plugins/` 目录下，目录结构如下：

   ```
   data/plugins/toy_ble_control/
   ├── main.py
   ├── metadata.yaml
   ├── _conf_schema.json
   ├── README.md
   └── LICENSE
   ```

2. 在 AstrBot 管理面板重启 / 重载插件。首次加载会按 `_conf_schema.json` 生成配置项（默认值是 Svakom SA253B 的协议，开箱即用）。
3. 插件启动后，HTTP 中继服务监听配置中的端口（默认 5122）。

---

## 三、环境要求

- **服务器**：能运行 AstrBot 的 Python 3.8+ 环境。
- **手机 / 平板浏览器**：必须支持 Web Bluetooth：
  - Android Chrome / Edge：原生支持。
  - iOS Safari：**不支持**。需用 Bluefy 等支持 Web Bluetooth 的浏览器。
- **网络**：手机要能访问到服务器的 HTTP 端口。本地局域网直接用 `http://服务器IP:5122`；外网需要内网穿透 / 反向代理（cpolar、frp、natapp、Cloudflare Tunnel 都行）。
- **蓝牙设备**：任意支持 BLE GATT 写入控制的玩具。

---

## 四、配置说明

在 AstrBot 管理面板找到插件 `toy_ble_control` 的配置页，填写以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `port` | int | HTTP 中继服务端口，默认 5122 |
| `toy_name` | string | 玩具显示名，仅用于网页标题 |
| `service_uuid` | string | BLE 主服务 UUID，例 `0xFFE0` 或 `0000ffe0-0000-1000-8000-00805f9b34fb` |
| `write_characteristic_uuid` | string | 写入特征 UUID，例 `0xFFE1` |
| `fallback_service_uuids` | list | 主服务失败时按顺序尝试的备选 UUID 列表 |
| `optional_services` | list | Web Bluetooth `optionalServices`：搜索时必须声明所有可能用到的 UUID，否则浏览器会拒绝读取它们 |
| `write_without_response` | bool | 设备特征只有 `WRITE WITHOUT RESPONSE` 时打开 |
| `name_filter_prefix` | string | 蓝牙设备名前缀过滤（可选）。留空则手机弹窗显示所有蓝牙设备，填 `Svakom` 则只显示名字以 Svakom 开头的，便于在多设备环境快速找到目标 |
| `functions_json` | text | **核心配置**：功能定义 JSON 数组，详见下文 |

### `functions_json` 详解

这是一个 JSON 数组，每一项描述一个功能（如振动、吮吸、拍打、加热、电击等），插件会自动注册成 LLM 可调用的 `toy_set function=xxx` 指令。

每个功能对象包含：

```json
{
  "name": "vibrate",
  "display_name": "振动",
  "command_template": "55 03 00 00 {mode} {intensity} 00",
  "stop_template":    "55 03 00 00 00 00 00",
  "mode_min": 1,
  "mode_max": 10,
  "intensity_min": 1,
  "intensity_max": 3
}
```

| 字段 | 说明 |
|------|------|
| `name` | 英文 ID（LLM 调用时填这个，需唯一） |
| `display_name` | 中文 / 任意显示名 |
| `command_template` | 指令字节模板：**空格分隔的 2 位 16 进制**，用 `{mode}` 和 `{intensity}` 作为占位符（不区分大小写） |
| `stop_template` | 停止该功能的字节序列（一般是把 mode/intensity 都改成 0） |
| `mode_min` / `mode_max` | 模式取值范围（含边界），LLM 传入超界值会被裁剪 |
| `intensity_min` / `intensity_max` | 强度取值范围（含边界），同样会被裁剪 |

> 占位符 `{mode}` / `{intensity}` 会被替换成对应数值的 2 位 16 进制（如 mode=5 → `05`）。  
> 如果你的玩具协议参数不止两个，可以把固定参数直接写进模板里，或自由扩展占位符（修改 `main.py` 中的 `parseHexTemplate`）。

---

## 五、如何探测自己玩具的协议（首次配置必读）

不同型号的 BLE 玩具协议都不一样，必须先用工具抓出 UUID 和指令码。推荐流程：

### 1. 安装 nRF Connect（手机 APP，Android/iOS 均可免费）

### 2. 找到 UUID

1. 打开 nRF Connect → SCAN → 找到你的设备 → CONNECT。
2. 展开服务列表，会看到若干 Service UUID（如 `FFE0`、`FFF0`、`180A` 等）。
3. 进入每个 Service 看它下面的 Characteristic：找一个 **Properties 含 WRITE 或 WRITE-WITHOUT-RESPONSE** 的特征，这就是写入特征。
4. 把它的 Service UUID 填到 `service_uuid`，把 Characteristic UUID 填到 `write_characteristic_uuid`。
5. 如果该特征只有 `WRITE-WITHOUT-RESPONSE` 而没有 `WRITE`，把 `write_without_response` 设为 `true`。

### 3. 抓指令码

最方便的办法是抓官方 APP 的真实通信：

1. **手机端**：Android 打开 *设置 → 开发者选项 → 启用蓝牙 HCI 日志（snoop log）*。
2. 用官方 APP 连接玩具，依次操作每个功能的每档强度。
3. 把 `/sdcard/btsnoop_hci.log` 导出，用 Wireshark 打开。
4. 过滤 `btatt && (btatt.opcode == 0x12 || btatt.opcode == 0x52)`（write request / write command），就能看到每次写入的 HEX 字节。
5. 找出每个功能的指令模板，把变化的字节标成 `{mode}` 或 `{intensity}` 占位符即可。

### 4. 也可以直接在 nRF Connect 里手测

进入写入特征，手动输入 HEX（如 `5503000001010001`），看玩具有没有反应，逐步试出协议。

---

## 六、参考配置：常见玩具示例

### Svakom SA253B（默认配置，开箱即用）

```json
[
  {
    "name": "suck",
    "display_name": "吮吸",
    "command_template": "55 09 00 00 {mode} {intensity} 00",
    "stop_template": "55 09 00 00 00 00 00",
    "mode_min": 1, "mode_max": 10,
    "intensity_min": 1, "intensity_max": 3
  },
  {
    "name": "vibrate",
    "display_name": "振动",
    "command_template": "55 03 00 00 {mode} {intensity} 00",
    "stop_template": "55 03 00 00 00 00 00",
    "mode_min": 1, "mode_max": 10,
    "intensity_min": 1, "intensity_max": 3
  },
  {
    "name": "pat",
    "display_name": "拍打",
    "command_template": "55 07 00 {mode} {intensity} 00",
    "stop_template": "55 07 00 00 00 00",
    "mode_min": 1, "mode_max": 4,
    "intensity_min": 1, "intensity_max": 3
  }
]
```

UUID：`service_uuid=0xFFE0`，`write_characteristic_uuid=0xFFE1`。

### 通用单功能振动器（示例）

许多廉价 BLE 振动器协议非常简单，比如直接发一个字节代表强度：

```json
[
  {
    "name": "vibrate",
    "display_name": "振动",
    "command_template": "{intensity}",
    "stop_template": "00",
    "mode_min": 1, "mode_max": 1,
    "intensity_min": 0, "intensity_max": 20
  }
]
```

> 实际指令请用 nRF Connect 抓包确认。这里只是格式示例。

### Lovense（BLE 型号，示例占位）

Lovense 的 BLE 协议使用 ASCII 字符串（`Vibrate:10;` 这种）。要适配的话需要扩展 `parseHexTemplate` 支持 ASCII 模板，目前默认实现只接 HEX 字节。可以提 issue 或自行修改。

---

## 七、使用流程

### 1. 配好插件 → 重启 AstrBot

### 2. 手机打开中继页

浏览器访问 `http://<服务器IP>:<port>`（如 `http://192.168.1.100:5122`）：

1. 点击 **「连接设备」**。
2. 弹窗里选你的玩具。
3. 状态变绿即连接成功。
4. **保持此网页在前台、不要锁屏**（锁屏会暂停 JS 轮询）。

### 3. 让 AI 控制

插件向 LLM 注册了 4 个工具：

| 工具 | 作用 | 参数 |
|------|------|------|
| `toy_list_functions` | 列出所有可用功能及取值范围 | 无 |
| `toy_set` | 设置功能 | `function`（功能英文 ID）, `mode`（档位数字）, `intensity`（强度数字） |
| `toy_stop` | 停止 | `function`（功能 ID 或 `all`） |
| `toy_status` | 查看当前状态 | 无 |

AI 会根据上下文自动调用。例如你跟它说"开振动 5 档强度 2"，它会调用 `toy_set(function="vibrate", mode="5", intensity="2")`。

---

## 八、排障

| 现象 | 排查方向 |
|------|----------|
| 浏览器搜不到设备 | 确认玩具已开机、没被别的 APP 占用；浏览器要支持 Web Bluetooth（iOS Safari 不行）；网页必须通过 `http://localhost`、`http://<内网IP>` 或 `https://` 打开（远程 `http://公网IP` 部分浏览器会拒绝，需要 HTTPS 或本地隧道） |
| 连上了但没反应 | 检查 `write_without_response` 设置；用 nRF Connect 手发指令验证字节码 |
| 部分功能反应、部分没反应 | 仔细对比对应功能的 `command_template`，确认占位符位置和固定字节都正确 |
| 网页打不开 | 防火墙是否放行端口；内网穿透是否配置 |
| 指令延迟约 1 秒 | 当前轮询间隔写死 1 秒。需要更快可改 `main.py` 里 `setInterval(..., 1000)` |
| 全部停止时玩具只停了部分功能 | `functions_json` 中每个功能必须填正确的 `stop_template` |

---

## 九、安全提示

- 这个插件没有任何鉴权。**默认监听 `0.0.0.0`，任何能访问到端口的人都能控制玩具。**
- 不要把端口直接暴露到公网。建议方案：
  - 仅在局域网使用。
  - 用带鉴权的反向代理（Nginx Basic Auth、Cloudflare Access、Tailscale 等）。
  - 用 SSH 隧道转发。

---

## 十、Changelog

- **v2.0.0** —— 重构为配置驱动：UUID、指令模板、功能列表均可在配置面板填写，可适配任意 BLE 玩具；新增 `toy_list_functions` 工具；HTTP 新增 `/config` 端点；端口可配置。
- **v1.0.0** —— 初版，硬编码 Svakom SA253B 协议。

---

## License

MIT
