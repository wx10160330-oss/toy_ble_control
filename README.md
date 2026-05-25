# 通用 BLE 玩具远程控制插件（toy_ble_control）

基于 AstrBot 的蓝牙玩具远程控制插件，通过 Web Bluetooth API 实现跨设备控制。**所有 UUID、指令格式、功能定义均可通过配置文件填写**，可适配 Svakom、Lovense（BLE 型号）、Magic Motion、Kiiroo 等任意基于 GATT 写入的 BLE 玩具。

> **第一次配置不会填？** 直接跳到 [五、如何探测自己玩具的协议](#五如何探测自己玩具的协议首次配置必读)。那一节告诉你怎么用 nRF Connect 一步步抓出 UUID 和指令字节。

> **致谢**：第五节的协议逆向方法（Notify 观察法）改写自 **吱吱 & Veille** 的《逆向任意 BLE 玩具协议，让 AI 直接控制它》一文（MIT，二传请保留署名）。

---

## 一、原理

```
LLM 指令  →  AstrBot 插件 (HTTP 中继服务)  →  手机浏览器 (Web Bluetooth)  →  蓝牙玩具
```

- **AstrBot 插件**：在服务器上启动一个 HTTP 服务（默认端口 5122），提供中继网页 + 状态接口。
- **手机浏览器**：打开中继网页，通过 Web Bluetooth API 连接蓝牙玩具，并轮询服务器获取最新指令。
- **LLM 工具调用**：AI 通过插件注册的工具（`toy_ble_set` / `toy_ble_stop` / `toy_ble_status` / `toy_ble_list_functions`）修改服务器状态；手机端轮询到新状态后通过 BLE 发送对应字节指令。

> 工具名全部加了 `toy_ble_` 前缀，与其他可能使用同类名字（如 `toy_set`）的玩具控制插件隔离，避免同时启用多个插件时互相覆盖。

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
| `fallback_service_uuids` | list | 高级。主服务连不上时按顺序尝试的备选 UUID。详见下方「高级：服务 UUID 备选与浏览器白名单」一节 |
| `optional_services` | list | 高级。浏览器访问白名单，默认已预填常用服务。详见下方「高级：服务 UUID 备选与浏览器白名单」一节 |
| `write_without_response` | bool | ⚠️ **默认关闭，不确定就别动**。详见下方「写入方式 (write_without_response)」专门一节 |
| `name_filter_prefix` | string | 蓝牙设备名前缀过滤（可选）。留空则手机弹窗显示所有蓝牙设备，填 `Svakom` 则只显示名字以 Svakom 开头的，便于在多设备环境快速找到目标 |
| `functions` | template_list | **核心配置**：玩具功能列表。面板里点 `+` 号添加功能、点垃圾桶删除。详见下文 |

### 写入方式（`write_without_response`）

BLE 写入特征有两种发送方式，**选错了玩具会拿到指令但完全不执行**——这是这个插件第二常见的踩坑点（最常见是 UUID 填错）。

| 方式 | JavaScript API | 对应特征 Properties |
|------|----------------|---------------------|
| Write Request（有响应） | `writeValue()` | `WRITE` / `WRITE REQUEST` |
| Write Without Response（无响应） | `writeValueWithoutResponse()` | `WRITE WITHOUT RESPONSE` / `WRITE NO RESPONSE` |

**判断步骤**：用 nRF Connect 连上玩具 → 找到你填写的写入特征 → 看它的 Properties 一栏。

| 看到的 Properties | 该怎么设置 |
|-------------------|------------|
| 只有 `WRITE` / `WRITE REQUEST` | 保持 **false**（默认）。Svakom 大部分型号是这种 |
| 只有 `WRITE-WITHOUT-RESPONSE` / `WRITE NO RESPONSE` | 设为 **true**。某些 Lovense BLE 型号、部分高频脉冲玩具是这种 |
| 两者都有 | 保持 **false** 更稳；如果发现指令稳定但响应慢可以试着开 true |

**症状对照**：

- 已经连上、网页显示「已连接」、日志显示「已发送指令」、但**玩具毫无反应** → 极大概率是这个开关设错了，先把它反过来试一次。
- 一连接就报错 `GATT operation not permitted` 或 `Unknown error` → 同上，反过来试。

---

### 高级：服务 UUID 备选与浏览器白名单

这两个字段大多数人**不用动**，但换玩具出问题时很可能要回来改它们。

#### `fallback_service_uuids`——备用服务列表

插件连接设备时，会先试 `service_uuid` 的主 UUID；如果该服务不存在，就按顺序尝试这个列表里的 UUID。适用场景：

- 同一型号玩具在不同固件版本上可能用不同的服务 UUID
- 类似型号供用同一份配置，但主 UUID 不同
- 你不确定是 `0xFFE0` 还是 `0xFFF0`，干脆两个都写

不填任何东西也完全 OK，就是失去备选的容错空间而已。

#### `optional_services`——浏览器访问白名单

Web Bluetooth 有个强制安全规则：网页想访问蓝牙设备的哪些服务，必须在调用 `requestDevice()` 时提前声明。没声明的，哪怕设备暴露了，也会被浏览器拒绝：

```
SecurityError: Origin is not allowed to access the service xxx
```

可以理解为：网页跟浏览器申请连蓝牙时，要交一张「我可能要碰这些 UUID」的白名单。这个字段就是那张白名单。

**必须包含**：

- `service_uuid` 里填的主 UUID
- `fallback_service_uuids` 里的所有备选 UUID
- `0x1800` / `0x1801`（BLE 标准 GAP / GATT 服务，部分手机不声明会出难以复现的兼容问题）

默认值 `["0xFFE0", "0xFFE5", "0xFFF0", "0x1800", "0x1801"]` 覆盖了常见国产 BLE 玩具的主要服务范围。**如果你玩具用的是一个不在列表里的服务 UUID（例如 `0xA001`），必须把它加进来**，否则插件连设备时会报 SecurityError。

---

### `functions` 详解——在面板里动手填

这是插件的核心配置：你的玩具支持哪些功能、每个功能的指令样子都在这里填。面板上长这样：

```
「玩具功能列表」  [ 点 + 号添加 ]
├─ 功能 1                  [ 🗑️删除 ]
│    英文名、中文名、启动指令编码、停止指令编码、
│    模式范围、强度范围
├─ 功能 2                  [ 🗑️删除 ]
│    ......
└─ 功能 3                  [ 🗑️删除 ]
     ......
```

默认已预填 Svakom SA253B 的 3 个功能（吮吸 / 振动 / 拍打），你可以照着改成自己玩具的，或者点 `+` 号加新的、点垃圾桶删不要的。

#### 每个功能填什么

| 字段 | 填什么 | 例子 |
|------|--------|------|
| **英文名** | 全小写英文字母 / 数字，同一玩具内不能重复。LLM 调用工具时填这个名字。 | `vibrate` / `suck` / `heat` / `mode3` |
| **中文显示名** | 中文、随意填，只用于网页和日志显示 | 「振动」、「加热」 |
| **启动指令编码** | 空格分隔的 2 位 16 进制字节，位置填 `{mode}` / `{intensity}` 作占位符 | `55 03 00 00 {mode} {intensity} 00` |
| **停止指令编码** | 同上格式，一般是把 mode/intensity 都换成0 | `55 03 00 00 00 00 00` |
| **模式最小值 / 最大值** | 填十进制。LLM 传入超界会被自动裁到这个范围内 | 1 / 10 |
| **强度最小值 / 最大值** | 同上 | 1 / 3 |

> 占位符 `{mode}` / `{intensity}` 会被插件自动替换为对应数值的 2 位 16 进制。  
> 例如 LLM 传入 mode=10，`{mode}` 位置实际发出去的字节是 `0a`；mode=15 是 `0f`；mode=255 是 `ff`。

#### 指令编码的字节格式

插件默认假设你的玩具用的是 **「单字节位置编码」**：玩具本身的固件读取某个字节的数值（如 mode 字节 = `0x0A`），直接拿它当档位用。这是绝大多数国产 BLE 玩具的做法，Svakom、Magic Motion 以及各种深圳方案商都属于这种。

**什么是「单字节位置编码」**：检查你从 nRF Connect Notify 里抓到的字节：

- 一档 vs 十档的帧 **长度一样**、只是有一个字节从 `01` 变成 `0a` → 是单字节位置编码 ✅ 插件支持
- 一档 vs 十档的帧 **长度不一样**（比如 `..31..` vs `..31 30..`）→ 是 ASCII 字符串协议 ⚠️ 底下另说

> **ASCII 字符串协议**（如 Lovense BLE 型号的 `Vibrate:10;`）不是发送原始字节值，而是发送文本字符 `'1' '0'`（= `0x31 0x30`）。这种玩具目前插件不支持，请去 [issue](https://github.com/wx10160330-oss/toy_ble_control/issues) 反馈。幸运的是走 BLE 协议的玩具 99% 不属于这个分类。

#### 进阶：参数超过 mode + intensity 两个怎么办

把固定参数直接写进模板里（比如某玩具的指令里还有时长、温度位），或者扩展占位符——改 `main.py` 里的 `parseHexTemplate`，加一个新的 `{duration}` 之类的占位符即可。

---

## 五、如何探测自己玩具的协议（首次配置必读）

> 本节内容主要改写自 **吱吱 & Veille** 的《逆向任意 BLE 玩具协议，让 AI 直接控制它》一文（MIT，二传请保留署名）。  
> 原作者第一次摸协议时卡了好几天，下面的步骤是他们总结出的最快路径——尤其是「观察 Notify」这招，比抓 HCI 日志省事得多，强烈建议先用它。

**适用范围**：走 GATT 协议的 BLE 玩具。判断方法：用 nRF Connect 能连上设备、能看到 Write 和 Notify 两个通道，就适用。绝大多数国产 BLE 玩具用的是深圳方案商的串口透传方案，都能套这套流程。

### 准备工作

- 玩具本体 + 官方 App
- 安卓手机（iOS 的限制会让后续步骤更难），装好 **nRF Connect**（Nordic Semiconductor 出的，免费，应用商店搜）

### 第一步：侦察设备结构

打开玩具 → 打开 nRF Connect → SCAN → 找到你的设备 → CONNECT。展开 GATT 服务列表，重点找两类 UUID：

| UUID | 用途 |
|------|------|
| `0xFFE0` 或类似 | **控制通道**，下面挂着 Write + Notify 两个 Characteristic |
| `0xAE00` | ⚠️ **Telink OTA 固件升级通道——绝对不要往这写东西，写错会变砖。** |

> ### ⚠️ 严重警告：别碰 `0xAE00`
>
> 国产 BLE 玩具大量使用 Telink 芯片，Telink 的 OTA 升级用的就是 `0xAE00` 这一族服务。**只要你不知道自己在干什么、就不要试图往这个服务下面的 Characteristic 写任何字节**，写错一个字节就可能把玩具刷砖。把它当成定时炸弹绕开。
>
> 同理：任何你不认识的服务，先 google 一下再碰。常见安全可写的就是 `0xFFE0` 这一族厂商自定义服务。

展开 `0xFFE0`（或你设备对应的控制服务），里面一般有两个 Characteristic：

- 一个 Properties 包含 `WRITE` 或 `WRITE WITHOUT RESPONSE` → **写入入口**，UUID 填到插件的 `write_characteristic_uuid`
- 一个 Properties 包含 `NOTIFY` → **状态出口**，第二步用它反推协议

**两个 UUID 都记下来。** 不同品牌的 Write 和 Notify 哪个在前哪个在后**没有规律**，不要拿别人型号的 UUID 直接抄——一定要看你设备实际的属性标注。Svakom 的方向就跟很多国产玩具是反的。

### 第二步：观察 Notify 反推协议（核心招式）

很多教程会教你启用 Android 的「蓝牙 HCI 日志」+ Wireshark 抓包，但这条路在 **荣耀 / 华为以及部分国产 ROM 上抓不到日志**（系统把日志锁在保护分区里），即使能抓到，btsnoop 解析也麻烦。

**有更简单的办法**：很多玩具运行时会通过 Notify 持续推送自己当前的状态帧，而这个状态帧和写入帧的格式往往**完全相同或高度相似**——设备等于在主动告诉你它收到了什么。

具体步骤：

1. nRF Connect 连上玩具，找到 Notify Characteristic，点 **bell 图标** 订阅。
2. **断开** nRF Connect（用完一个软件就断开，BLE 同时只允许一个主机连接）。
3. 用**官方 App** 连上玩具，把它操控到一个明确状态（例如「吮吸模式 1、强度 3」），然后退出官方 App。
4. **重新**用 nRF Connect 连上玩具，**Read** Notify Characteristic 的当前值，记录下来。
5. 换不同的功能 / 模式 / 强度，重复 2~4 几次，把每次的 Notify 值列成表。

> ⚠️ **真机验证过的小坑：第一次往往读到的是上次写入的脏数据（比如 nRF Connect 默认 "CHAR1_VALUE" 这种 ASCII 字符串），不是玩具的真实状态。**  
> **解决方法**：每一档**整套循环跑两次**——第一次跑完先别记录，立刻按同一档位再重新走一遍 2~4 步、第二次的读数才是真实状态字节。亲测有效，原因可能是第一次断开 / 重连把上次 nRF Connect 写过的脏值清出了缓存，第二次才轮到玩具自己的 Notify 推送被读到。  
> 如果第二次读到的还是 ASCII 看起来像英文字符（`43 48 41 52 ...` 解码就是 `CHAR1...`），说明你**之前在 nRF Connect 里手滑点过 Write**——把玩具长按关机再开机清一次缓存，并且**这一整次抓包从头到尾都不要再点 nRF Connect 的 Write 按钮**，只点 Read / Subscribe。

例：原作者逆向 Svakom 时抓到的 Notify 值：

| 操作 | Notify 值 |
|------|-----------|
| 静止 | `55 FE 09 00 00 00 00`（注意：静止帧可能跟运行帧格式不一样） |
| 吮吸 模式 1 强度 3 | `55 09 00 00 01 03 00` |
| 吮吸 模式 8 强度 3 | `55 09 00 00 08 03 00` |

帧格式五分钟之内就出来了：

```
55 09 00 00 <mode> <intensity> 00
```

| 字节 | 含义 |
|------|------|
| `0x55` | 帧头（固定） |
| `0x09` | 功能类型 = 吮吸 |
| `00 00` | 固定填充 |
| `<mode>` | 模式档位 |
| `<intensity>` | 强度档位 |
| `0x00` | 固定填充或校验位 |

对于你自己的玩具：

- 多个操作里**始终不变**的字节 = 帧头 / 功能 ID
- 跟操作**对应变化**的字节 = 参数（mode / intensity / 时长等）
- 末尾不变的字节 = 校验或填充

把对应字节用 `{mode}` / `{intensity}` 替换，就得到「启动指令编码」：

```
55 09 00 00 {mode} {intensity} 00
```

每个功能（吮吸、振动、拍打……）都重复一遍这个流程。多功能玩具通常用 `byte[1]` 区分功能类型。

### 第三步：验证协议

在 nRF Connect 里找到 **Write** Characteristic，点上传箭头，手动输入你拼出来的 HEX（例如 `55 09 00 00 03 02 00`），玩具有反应即协议正确。

如果发了没反应，依次检查：

- Write Characteristic UUID 是不是填错了（拿错的特征 / 拿错的 Service 都常见）
- 写入方式：只有 `WRITE WITHOUT RESPONSE` 的特征不能用 Write 发，反之亦然——详见第四节「写入方式」
- 字节顺序是不是抄反了
- 玩具是不是没电 / 进入待机了

### 第四步：把协议填进配置

回到 AstrBot 插件配置面板：

- `service_uuid` = 第一步抓到的控制服务 UUID
- `write_characteristic_uuid` = Write 那个特征的 UUID
- `write_without_response` = 看 Write 特征的 Properties，按第四节规则设置
- `functions` = 点 `+` 号添加。每个功能一个块，填英文名 / 中文名 / 启动指令 / 停止指令 / 模式范围 / 强度范围。模板里用 `{mode}` / `{intensity}` 占位。

重启插件 → 手机访问中继页 → 让 AI 调用 `toy_ble_set` 测试。

---

### 协议逆向时的常见踩坑

来自原作者的踩坑实录，看完能省掉好几天弯路：

- **BLE 同时只允许一个主机连接。** nRF Connect 连着的时候，官方 App 连不上；反过来也一样。每次切换工具记得先**显式断开**，否则可能要等几十秒才能重新连上。
- **荣耀 / 华为手机抓不到 HCI 日志。** 系统把日志锁在保护分区里，开发者选项里那个开关基本是摆设。直接用上面的 Notify 观察法。
- **Write 和 Notify 的方向因品牌而异。** 不要默认「`FFE1` 是 Notify、`FFE2` 是 Write」之类的规律。看 nRF Connect 里的属性标注，**Write 在哪个特征就是哪个**。原作者第一次就是猜错方向卡了半天。
- **静止帧和运行帧格式可能不同。** 设备在静止时可能回传一个特殊的"我没在干活"帧（如 `55 FE 09 00 00 00 00`），这跟运行控制帧格式不一样。**至少抓 3~5 个不同的运行状态**再下结论，不要只看静止值。
- **APK 反编译看到方法名但拿不到字节值。** 别上来就反编译，太花时间。Notify 观察法 5 分钟搞定的事情，反编译可能要花几小时还找不到。
- **手机锁屏会断 BLE。** 中继页面开着的时候保持屏幕常亮，或者去开发者选项里打开「不锁定屏幕」/「保持唤醒」。
- **Web Bluetooth 只有 Chrome / Edge 支持。** iOS Safari 完全不支持，iOS 用户需要装 Bluefy 这类支持 Web Bluetooth 的浏览器。
- **不认识的服务别乱写。** 再说一次：`0xAE00` 类的 OTA 服务写错就变砖。

---

## 六、参考配置：常见玩具示例

下面列出几个示例填法，供参考。面板里照着这些填到各个子框即可。

### Svakom SA253B（默认配置，开箱即用）

UUID：`service_uuid=0xFFE0`，`write_characteristic_uuid=0xFFE1`。

几个功能（默认 `functions` 里已经预填）：

| 英文名 | 中文名 | 启动指令编码 | 停止指令编码 | 模式 | 强度 |
|--------|--------|----------------|----------------|------|------|
| `suck` | 吮吸 | `55 09 00 00 {mode} {intensity} 00` | `55 09 00 00 00 00 00` | 1–10 | 1–3 |
| `vibrate` | 振动 | `55 03 00 00 {mode} {intensity} 00` | `55 03 00 00 00 00 00` | 1–10 | 1–3 |
| `pat` | 拍打 | `55 07 00 {mode} {intensity} 00` | `55 07 00 00 00 00` | 1–4 | 1–3 |

### 通用单功能振动器（示例）

许多廉价 BLE 振动器协议特别简单，比如直接发一个字节代表强度：

| 英文名 | 中文名 | 启动指令编码 | 停止指令编码 | 模式 | 强度 |
|--------|--------|----------------|----------------|------|------|
| `vibrate` | 振动 | `{intensity}` | `00` | 1–1 | 0–20 |

实际指令请用 nRF Connect Notify 观察法确认。这里只是格式示例。

### Lovense（BLE 型号）⚠️ 当前不支持

Lovense 的 BLE 协议使用 ASCII 字符串（`Vibrate:10;` 这种）。插件目前只支持「单字节位置编码」（什么是“单字节位置编码”参见上面的 functions 详解部分），不支持 ASCII 模板。如果你需要 Lovense 支持，请去 [issue](https://github.com/wx10160330-oss/toy_ble_control/issues) 反馈。

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
| `toy_ble_list_functions` | 列出所有可用功能及取值范围 | 无 |
| `toy_ble_set` | 设置功能 | `function`（功能英文 ID）, `mode`（档位数字）, `intensity`（强度数字） |
| `toy_ble_stop` | 停止 | `function`（功能 ID 或 `all`） |
| `toy_ble_status` | 查看当前状态 | 无 |

AI 会根据上下文自动调用。例如你跟它说"开振动 5 档强度 2"，它会调用 `toy_ble_set(function="vibrate", mode="5", intensity="2")`。

> **多功能可以同时运行**：每个功能独立跟踪状态。你跟 AI 说“开吮吸、振动、拍打三个一起”，它会连调三次 `toy_ble_set`，三个功能都会同时跑、互不覆盖。调 `toy_ble_stop(function="vibrate")` 只会停振动，其他几个继续。调 `toy_ble_stop` 或 `toy_ble_stop(function="all")` 才全部停。

> **工具名都带 `toy_ble_` 前缀**：避免跟其他玩具插件（例如你另装了一个叫 `toy_set` 的插件）同时启用时互相覆盖。两个插件可以并存。

---

## 八、排障

| 现象 | 排查方向 |
|------|----------|
| 浏览器搜不到设备 | 确认玩具已开机、没被别的 APP 占用；浏览器要支持 Web Bluetooth（iOS Safari 不行）；网页必须通过 `http://localhost`、`http://<内网IP>` 或 `https://` 打开（远程 `http://公网IP` 部分浏览器会拒绝，需要 HTTPS 或本地隧道） |
| **连上了但毫无反应** | **第一步检查 `write_without_response` 开关**：如果当前是 false 就改 true 试一次，反之亦然。这是最常见的原因。然后再用 nRF Connect 手发指令验证字节码 |
| 部分功能反应、部分没反应 | 仔细对比对应功能的「启动指令编码」，确认占位符位置和固定字节都正确 |
| 网页打不开 | 防火墙是否放行端口；内网穿透是否配置 |
| 指令延迟约 1 秒 | 当前轮询间隔写死 1 秒。需要更快可改 `main.py` 里 `setInterval(..., 1000)` |
| 全部停止时玩具只停了部分功能 | `functions` 里每个功能必须填正确的「停止指令编码」 |
| AI 调了多个功能，但玩具只开了最后一个 | 老版本（v2.2 及以前）采用单状态覆盖模型，升级到 v2.3.0+ 后多功能会同时运行 |

---

## 九、安全提示

- 这个插件没有任何鉴权。**默认监听 `0.0.0.0`，任何能访问到端口的人都能控制玩具。**
- 不要把端口直接暴露到公网。建议方案：
  - 仅在局域网使用。
  - 用带鉴权的反向代理（Nginx Basic Auth、Cloudflare Access、Tailscale 等）。
  - 用 SSH 隧道转发。

---

## License

MIT
