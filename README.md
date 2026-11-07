# iMessage 全量导出（macOS）

把 Mac「信息」App 的聊天记录完整导出成本地可浏览的 HTML / CSV / JSON，**包括手写笔迹、数码点触、贴纸、图片、视频**。另附带一个无边记（Freeform）看板清单导出脚本。

全部在本地运行，不联网、不上传任何数据。

```
python3 export_imessage.py
```

输出到 `~/Desktop/iMessage导出_YYYYMMDD/`：

| 文件 | 内容 |
| --- | --- |
| `index.html` | 单文件浏览界面：左侧会话列表、搜索、气泡流，图片内联（可直接单独分享） |
| `messages.csv` / `messages.json` | 结构化数据，便于二次处理 |
| `attachments/<会话名>/` | 图片、视频、语音、贴纸等原始附件 |
| `payloads/` | 手写 / 数码点触的原始载荷与还原出的 `.svg` |

## 为什么需要它

macOS 从 Big Sur 起，`chat.db` 里 `message.text` 经常是空的 —— 正文被塞进了 `attributedBody`（typedstream 二进制归档）。很多导出工具只读 `text`，导出来是一堆空白；更常见的问题是消息数量对不上，因为 `chat_message_join` 会被系统清理。

这个脚本处理了这些坑，并且把 Apple 没有公开文档的两种富媒体消息（手写、数码点触）还原成了矢量图。

## 功能

- **全量**：会话、消息、方向、时间、已撤回/编辑、表情回应
- **正文解码**：从 `attributedBody` 的 typedstream 里取回字符串，过滤 `__kIM*` 元数据，避免二进制垃圾冒充正文
- **归属兜底**：`chat_message_join` 行数被系统清空时，用 `ck_chat_id` 反查 GUID / 号码，避免静默丢消息
- **附件**：复制到导出目录并按文件名去重；`.pluginPayloadAttachment` 这类没有扩展名的文件按魔数识别真实类型（JPEG / PNG / HEIC / MOV / MP3 / …）
- **手写消息**：XZ + protobuf 解码，逐点笔宽还原成 SVG（笔压起伏保留）
- **数码点触**：protobuf 解码，按原始颜色还原涂鸦
- **检索**：HTML 里按会话名或消息内容搜索

## 用法

```bash
# 直接运行（需要「完全磁盘访问权限」）
python3 export_imessage.py

# macOS 上也可以双击
open 导出iMessage.command

# 更多参数
python3 export_imessage.py -o ~/somewhere      # 指定输出目录
python3 export_imessage.py --no-inline         # 图片不内联（HTML 更小）
python3 export_imessage.py --no-copy-attachments
python3 export_imessage.py --db /path/to/chat.db   # 读 Time Machine 备份里的库
```

### 权限

首次运行会因权限被拒。到 **系统设置 → 隐私与安全性 → 完全磁盘访问权限**，把「终端」（或你用的 IDE、WorkBuddy）加进去并打开开关，然后**完全退出并重开**该程序。

数据库不会被修改：脚本用 SQLite 备份 API 把 `chat.db` 连同 `-wal` 快照到临时目录，只读副本。

### 无边记

```bash
python3 export_freeform.py      # 导出到桌面的 无边记导出_日期
```

只能导出看板标题、时间、共享状态和缩略图。看板内容本身是私有 CRDT（`boards.data` / `board_items.specific_data`），没有公开格式，无法还原成可编辑文件 —— 要完整保真请在 App 内「文件 → 导出为 PDF」。

## 技术细节

### 时间

Apple 纪元是 2001-01-01，且不同版本单位不同：新系统是纳秒、老系统是秒。按 `abs(value) > 1e11` 区分。

### 正文（attributedBody）

是 NeXT typedstream 归档，形如 `streamtyped` + `NSString` + 长度 + UTF-8。脚本扫描 `NSString\x01\x94\x84\x01+` 等标记定位字符串，并用「可读字符占比 ≥ 75%」做二次校验，防止把归档里的二进制块当成正文。

### 手写消息（`com.apple.Handwriting.HandwritingProvider`）

载荷是 protobuf：

```
field 2  fixed64   创建时间（Apple 纪元毫秒）
field 3  string    消息 ID
field 4  message   Handwriting {
  field 3  bytes     Frame：Origin(x,y) + Size(w,h)，4×int16 LE
  field 4  int64     笔画数
  field 5  enum      压缩方式（4 = XZ）
  field 8  bytes     笔画数据（XZ 压缩）
}
```

解压后是紧凑二进制：`[笔画数] { uint16 点数; [点数] { int16 x; int16 y; int16 笔宽; int16 保留 } }`。

三个容易踩的坑，都在这份实现里处理了：

1. **坐标要 XOR `0x8000`**，不是当补码解释；
2. **y 轴方向已经是自上而下**，再做翻转会得到镜像乱线；
3. **x、y 两轴单位不相等**（点坐标跨约 32000，Frame 只有几百），必须按 Frame 宽高分别归一，否则笔迹会被纵向拉长。

笔宽是逐点的（实测 1–24 共 24 档），按点宽分档合并成段、每段单独设 `stroke-width`，才能还原起笔粗、收笔尖的笔感。

参考：imessage-exporter 的 [`handwriting.proto`](https://github.com/ReagentX/imessage-exporter/blob/develop/imessage-database/src/message_types/handwriting/handwriting.proto) 与 [handwriting2svg](https://github.com/bobcatalyst/handwriting2svg)。

### 数码点触（`com.apple.DigitalTouchBalloonProvider`）

同为 protobuf，`field 1` 是效果类型（4 = 涂鸦 Sketch），`field 3` 是载荷：笔数、`uint16` 坐标对（0–65535，原点左下、y 向上）、RGBA 颜色表。与手写的坐标系**不同**，不要共用同一套归一逻辑。

### 易被忽略的点

- `chat_message_join` 不是可靠的数据源。它会被系统压缩/清空，直接 JOIN 会静默丢掉大量消息，因此需要 `ck_chat_id` 兜底，并在导出后校验「归属解析失败数 = 0」。
- 通讯录库（`AddressBook`）可能为空，此时只能显示原始号码，不是导出错误。

## 已知限制

- 只能在 macOS 上运行，且需要用户授予完全磁盘访问权限；
- 本机数据库通常只保存**这台 Mac 上同步下来的**消息。更早的历史可能只在 iPhone / iPad 上，需要先在设备上开启「iCloud 云端信息」并等待同步；
- 数码点触导出的是静态帧，动效（点触、心跳、火球）需专用播放器才能还原；
- 手写消息还原的是笔迹本身，不含动画时序。

## 隐私

导出的内容包含完整的私人聊天记录和附件。工具本身不上传任何数据，但请自行妥善保管导出目录 —— 不要把它提交到 Git 仓库或公开位置。仓库中的 `.gitignore` 已排除常见的导出目录。

## License

MIT
