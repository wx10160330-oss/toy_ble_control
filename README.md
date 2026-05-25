# Svakom BLE 远程控制插件

基于 AstrBot 的蓝牙玩具远程控制插件，通过 Web Bluetooth API 实现跨设备控制。

---

## 原理

```
LLM 指令 → AstrBot 插件(HTTP服务) → 手机浏览器(Web Bluetooth) → 蓝牙设备
```

- **AstrBot 插件**：在服务器上运行一个 HTTP 服务（端口 5122），提供中继网页和状态接口。
- **手机浏览器**：打开中继网页，通过 Web Bluetooth API 连接蓝牙设备，并轮询服务器获取指令。
- **LLM 工具调用**：AI 通过调用插件注册的工具函数，修改服务器上的状态，手机端轮询到新状态后发送对应的蓝牙指令。

---

## 环境要求

- **服务器**：能运行 AstrBot 的环境（Python 3.8+），需要公网 IP 或内网穿透（让手机能访问到端口 5122）。
- **手机端**：支持 Web Bluetooth 的浏览器（推荐 Chrome / Edge for Android）。iOS 的 Safari 不支持 Web Bluetooth，需使用第三方浏览器如 Bluefy。
- **蓝牙设备**：支持 BLE（蓝牙低功耗）协议的 Svakom 设备。

---

## 安装

1. 将 `svakom_ble_control` 文件夹放入 AstrBot 的 `data/plugins/` 目录下。
2. 重启 AstrBot 或在管理面板中重载插件。
3. 插件启动后会在端口 **5122** 开启 HTTP 服务。

---

## 使用步骤

### 1. 蓝牙协议探测（首次配置）

使用 **nRF Connect**（Android/iOS 均可下载）探测你的设备协议：

1. 打开 nRF Connect，扫描并连接你的蓝牙设备。
2. 找到设备的服务和特征 UUID：
   - **Service UUID**：通常为 `0xFFE0`
   - **Write Characteristic UUID**：通常为 `0xFFE1`
3. 确认写入方式：查看特征的 Properties 是 `WRITE`（有响应）还是 `WRITE WITHOUT RESPONSE`（无响应）。
   - 如果是 `WRITE`，代码中使用 `writeValue()`
   - 如果是 `WRITE WITHOUT RESPONSE`，代码中使用 `writeValueWithoutResponse()`
4. 测试指令码：在 nRF Connect 中手动发送十六进制指令，确认设备有响应。

### 2. 指令码格式（以 Svakom SA253B 为例）

| 功能 | 指令格式 | 示例 |
|------|---------|------|
| 振动 | `55-03-00-00-{模式}-{强度}-00` | `55-03-00-00-01-01-00`（模式1 强度1） |
| 吮吸 | `55-09-00-00-{模式}-{强度}-00` | `55-09-00-00-02-01-00`（模式2 强度1） |
| 拍打 | `55-07-00-{模式}-{强度}-00` | `55-07-00-01-01-00`（模式1 强度1） |
| 停止振动 | `55-03-00-00-00-00-00` | - |
| 停止吮吸 | `55-09-00-00-00-00-00` | - |
| 停止拍打 | `55-07-00-00-00-00` | - |

- **模式**：振动和吮吸为 1~10，拍打为 1~4
- **强度**：1（低）、2（中）、3（高）

> ⚠️ 不同型号的指令码可能不同，务必用 nRF Connect 实测确认。

### 3. 连接设备

1. 手机浏览器打开 `http://{服务器IP}:5122`
2. 点击「连接设备」按钮
3. 在弹出的蓝牙设备列表中选择你的设备
4. 看到状态变为绿色「已连接」即可
5. 保持该网页在前台运行，不要关闭

### 4. LLM 控制

插件注册了以下 LLM 工具：

| 工具名 | 功能 | 参数 |
|--------|------|------|
| `toy_set` | 设置功能/模式/强度 | `function`(suck/vibrate/pat), `mode`(档位), `intensity`(强度1-3) |
| `toy_stop` | 停止功能 | `function`(suck/vibrate/pat/all)，默认 all |
| `toy_status` | 查看当前状态 | 无 |

AI 会根据对话语境自动调用这些工具。

---

## 适配其他设备

如果你的设备不是 SA253B，需要修改以下内容：

1. **UUID**：修改 `SERVICE_UUID` 和 `WRITE_UUID` 为你设备的实际值。
2. **指令码**：修改 `buildCmd()` 和 `buildStopCmd()` 中的字节数组。
3. **写入方式**：根据设备特征的 Properties，选择 `writeValue()` 或 `writeValueWithoutResponse()`。

所有修改都在 `main.py` 的 JavaScript 部分（`RELAY_HTML` 变量内）。

---

## 排障

| 问题 | 排查方向 |
|------|---------|
| 搜不到设备 | 确认设备已开机且未被其他 APP 连接；确认浏览器支持 Web Bluetooth |
| 连接成功但无反应 | 检查写入方式（writeValue vs writeValueWithoutResponse）；用 nRF Connect 确认指令码 |
| 网页打不开 | 检查服务器防火墙是否放行 5122 端口；确认内网穿透配置 |
| 指令有延迟 | 轮询间隔默认 1 秒，可在 JS 中调整 `setInterval` 的值 |

---

## 文件结构

```
svakom_ble_control/
├── main.py          # 插件主文件（含 HTTP 服务 + 中继网页 + LLM 工具）
├── README.md        # 本文档
└── _metadata.yaml   # 插件元数据（AstrBot 自动生成）
```
