# Cloud115 SDK

SakuraMediaBE 内部维护的 115 网盘极简异步客户端。**仅支持 cookies 认证**，覆盖播放/查找/缩略图三类上层需求所需的 HTTP 接口，完全不依赖第三方 115 SDK。

- 位置：[`src/lib/cloud115/`](../)
- 入口：`Cloud115Client`
- 运行时依赖：`httpx`、`loguru`（都已在 `pyproject.toml`）；**未引入 `cryptography` / `pycryptodome`**，RSA/KDF 全用 Python 内置 `pow()` 手撸

> **重要**：直链下载(`get_download_url`)对非会员账号可用但被官方限速 100KB/s + CDN 多请求拉黑，实测**做不了视频缩略图**。视频类操作(播放、抽帧)必须走 VIP 专属的 `get_video_info` + `get_video_segments`（HLS 分段方案，实测 191 段抽帧顺序跑 6 分钟）。详见 [VIP 视频接口](#vip-视频接口)。

## 目录

- [快速开始](#快速开始)
- [认证与 cookies](#认证与-cookies)
- [接口清单](#接口清单)
- [VIP 视频接口](#vip-视频接口)
- [数据类型](#数据类型)
- [异常层次](#异常层次)
- [关键机制](#关键机制)
- [手动测试 CLI](#手动测试-cli)
- [集成测试](#集成测试)
- [不在本 SDK 范围](#不在本-sdk-范围)

---

## 快速开始

```python
import os
import asyncio
from src.lib.cloud115 import Cloud115Client

async def main():
    async with Cloud115Client(cookies=os.environ["COOKIE_115"]) as client:
        # 探活
        alive = await client.check_cookies_alive()
        # 列根目录一页
        entries, total = await client.list_dir("0", limit=50)
        # 拿一个视频文件的 302 直链
        video = next(e for e in entries if e.is_video)
        du = await client.get_download_url(
            pickcode=video.pickcode,
            user_agent="Mozilla/5.0 SakuraMedia-Player/1.0",
        )
        print(du.url)          # 可直接 302 给播放器
        print(du.expires_at)   # 直链过期 unix 秒
        print(du.user_agent)   # 播放器后续 Range GET 必须复用同一 UA

asyncio.run(main())
```

**要点**：
- 客户端必须用 `async with` 或显式 `await client.close()` 释放 httpx 连接池
- `get_download_url` 的 `user_agent` 参数必填 —— 见[UA 绑定机制](#ua-绑定机制)
- 所有方法都是 `async def`；FastAPI handler 直接 `await`，APScheduler 线程池里包 `asyncio.run(...)` 也可

---

## 认证与 cookies

### cookies 字符串格式

SDK 只接受**逐字节透传的字符串形式**，不接受 dict：

```
UID=<user_id>_A1_<unix_ts>; CID=<hex>; SEID=<hex>; KID=<hex>
```

传更多字段（`GST` / `USERSESSIONID` / `PHPSESSID` / `acw_tc` 等）也无害，会一并作为 `Cookie:` header 发出去。

**核心必须字段**：`UID`（缺失或格式错构造时立即抛 `Cloud115AuthError`，fail fast）。

### 从哪里拿 cookies

- 浏览器 DevTools -> Application -> Cookies -> 115.com，复制全部字段拼成一行
- 或用任意第三方"cookies 导出插件"输出为一行分号分隔格式

### 过期识别

- 调 `check_cookies_alive()` 主动探活
- 或在业务方法调用中捕获 `Cloud115AuthError`（`errno` 属于 `990009 / 990017 / 20130827 / 911` 等）

**SDK 不做自动重登**：cookies 死了怎么办由上层业务决定（通常是通知用户重新填 cookies）。

---

## 接口清单

### 1. `check_cookies_alive() -> bool`

探测当前 cookies 是否仍在登录态。**永不抛异常**，任何异常场景都返 `False`。

```python
alive: bool = await client.check_cookies_alive()
```

**契约**：
- 200 + JSON `state=True` → `True`
- 302 到登录页 / JSON `state=False` / 非 JSON / 网络错 → `False`

**副作用安全**：底层用 `https://my.115.com/?ct=guide&ac=status`，不会触发同设备其它 cookies 失效。**不要用**其它文章推荐的 `login_check_sso` 端点 —— 那个 60 秒后会杀掉同设备其它 cookies。

---

### 2. `list_dir(cid, *, offset=0, limit=1000) -> tuple[list[DirEntry], int]`

列目录一页，返回 `(当前批次条目, 目录总数)`。

```python
entries, total = await client.list_dir("0", offset=0, limit=50)
for e in entries:
    print(e.name, e.entry_id, e.is_dir, e.pickcode)
```

**参数**：
- `cid`：目录 category_id 字符串，根目录用 `"0"`
- `offset`：从第几条开始（0-based）
- `limit`：本页要多少条；**服务端硬上限 1150**，超过抛 `ValueError`

**异常**：
- `state=False` + `errno=990002`（父目录不存在）→ `Cloud115NotFoundError`
- `state=False` + auth 类 errno → `Cloud115AuthError`
- 5xx → 内部重试最多 2 次后 `Cloud115RequestError`
- 429 → `Cloud115RateLimitedError`

**分页遍历示例**（SDK 不提供 `iter_dir`，上层自己写循环）：

```python
entries, total = [], -1
offset = 0
while total == -1 or offset < total:
    batch, total = await client.list_dir(cid, offset=offset, limit=1000)
    if not batch:
        break
    entries.extend(batch)
    offset += len(batch)
```

---

### 3. `file_info(file_id) -> FileMeta`

取单文件元信息。`file_id` 是**整数字符串**（`list_dir` 返回的 `DirEntry.entry_id`）。

```python
meta = await client.file_info("3471260435703924578")
print(meta.name, meta.size, meta.sha1, meta.pickcode)
```

**注意**：**不接受 pickcode 作为参数**。如果你从别处只拿到 pickcode，先 `list_dir` 拿完整 `DirEntry` 用它的 `entry_id`；或者直接 `get_download_url(pickcode, ua)` 也能拿到 `file_id`。

**异常**：
- `data` 数组为空 → `Cloud115NotFoundError`（file_id 不存在）
- 其它异常同 `list_dir`

---

### 4. `get_download_url(pickcode, user_agent) -> DirectUrl`

拿 302 直链。这是 SDK 里逻辑最复杂的一个接口，走 RSA 加密的 `proapi.115.com/app/chrome/downurl`。

```python
du = await client.get_download_url(
    pickcode="bijccwbcsacpi842c",
    user_agent="Mozilla/5.0 SakuraMedia-Player/1.0",
)
print(du.url)          # https://cdnfhnfile.115cdn.net/...?t=...&f=1&...
print(du.expires_at)   # 1783886968
print(du.user_agent)   # 回传给你，用于后续 Range GET
```

**参数**：
- `pickcode`：文件 pickcode（`DirEntry.pickcode`）；空串抛 `ValueError`
- `user_agent`：**必填**。会被 115 绑定进返回 URL 的 `f=` 指纹。空串抛 `ValueError`

**异常**：
- 目录、被封禁、pickcode 无效 → `Cloud115NotFoundError`
- 账号冻结 / 未登录 → `Cloud115AuthError`
- 密文解密错 → `Cloud115CipherError`（正常情况不会出，出了说明 115 改协议了）

**关键**：见下面的 [UA 绑定机制](#ua-绑定机制) 和 [直链生命周期](#直链生命周期)。

---

### 5. `Cloud115Client(cookies, *, user_agent=None, timeout=30.0, http_client=None)`

构造函数。

**参数**：
- `cookies`：见 [认证与 cookies](#认证与-cookies)
- `user_agent`：**客户端 -> 115 的请求 UA**（不是直链绑定的 UA！）。默认写死一个稳定的 Chrome UA。用途：`check_cookies_alive` / `list_dir` / `file_info` / `get_download_url` **POST 请求本身**的 UA
- `timeout`：httpx 请求超时秒
- `http_client`：可选，注入外部 `httpx.AsyncClient`（用于测试或复用连接池）；不注入时 SDK 自建一个，`close()` 时释放

**注意 UA 有两个层次**：
- 构造参数 `user_agent`：客户端 -> 115 的请求 UA
- `get_download_url(pickcode, user_agent=...)` 的参数：**灌进直链**给下游播放器/抽帧用的 UA

这两个是独立的，不要混淆。

---

## VIP 视频接口

**仅 VIP 会员账号可用**。非会员调用会抛 `Cloud115MembershipRequiredError`（errno=406 "需要VIP会员"），可在 UI 上引导升级。

这两个接口用来做视频缩略图/播放，绕开了直链方案的所有问题（限速、CDN 拉黑、UA 绑定、mp4 结构不利 seek）。核心思路：走 **115 服务端已经转码好的 HLS m3u8/ts 分段**，每段是独立可解码的短视频，天然对应"每 N 秒抽一张"。

### `get_video_info(pickcode) -> VideoInfo`

拿视频综合信息 + master m3u8 里的清晰度列表。内部发两次 HTTP：
1. `GET https://webapi.115.com/files/video?pickcode=...` → 视频元数据 + master m3u8 URL
2. `GET <master m3u8 URL>` → 解析出所有 variant 清晰度

```python
info = await client.get_video_info("bijccwbcsacpi842c")
print(info.width, info.height, info.thumb_url)   # 1280 720 https://static.115.com/video/HASH.jpg
for d in info.definitions:
    print(d.bandwidth, d.resolution, d.label, d.m3u8_url)
    # 1800000 1280x720 HD https://cpats01.115.com/.../HASH_1280.m3u8?...
```

**异常**：
- errno=406 → `Cloud115MembershipRequiredError`
- `video_url` 字段为空（非视频 / 未转码）→ `Cloud115NotFoundError`
- 5xx / 网络错 → 内部重试 2 次后 `Cloud115RequestError`

### `get_video_segments(pickcode, *, prefer_bandwidth=None) -> list[VideoSegment]`

拿指定/最高清晰度的 HLS ts 分段列表。

```python
# 默认最高码率
segments = await client.get_video_segments("bijccwbcsacpi842c")
# 或选特定码率
segments = await client.get_video_segments(pickcode, prefer_bandwidth=1800000)

for s in segments:
    print(s.index, f"{s.duration_seconds:.2f}s", s.url)
```

**参数**：
- `prefer_bandwidth`：想要的码率（bit/s）。找不到匹配码率时**回退到最高**（不抛异常，因为清晰度是"偏好"）

**每段的用法**：
```python
# 每段直接抽第一帧，不需要 seek
subprocess.run([
    "ffmpeg", "-y",
    "-user_agent", "Mozilla/5.0 ...",
    "-i", segment.url,
    "-ss", "0", "-vframes", "1",
    "-q:v", "3",
    f"frame_{segment.index:03d}.webp",
])
```

**性能实测**（1.06 GB / 31.6 分钟视频，VIP 账号）：
- 单段下载 ~200-700 KB，抽帧 1-2 秒/段
- 191 段串行抽帧总耗时 **约 6 分钟**
- CDN 参数 `s=4194304` = 4 MB/s（比非会员直链 100 KB/s 快 40 倍）

**注意**：
- 每段的实际时长以 `duration_seconds` 为准 —— 大多数段是 10 秒但边界段会略偏（编码器 GOP 决定）。**上层若要严格"每 10 秒一张"，按 duration 累加找目标段**；若接受"一段一张"的自然精度，直接遍历
- 分段 URL 是 115 转码 CDN 域（`cpats01.115.com`），**不需要额外 UA 绑定**（不是 `f=1` 直链）
- 分段 URL 的过期时间在 URL 里没有 `t=` 参数，但根据实测约 15+ 分钟稳定可用

---

## 数据类型

在 [`types.py`](../types.py) 定义：

### `DirEntry`

`list_dir` 返回的目录条目，文件和目录共用此结构。

| 字段 | 类型 | 说明 |
|------|------|------|
| `entry_id` | `str` | 文件用 `fid`（file_id）；目录用 `cid`（category_id） |
| `parent_id` | `str` | 所在父目录 |
| `name` | `str` | 文件/目录名 |
| `is_dir` | `bool` | `True` 表示目录；由响应中是否含 `fid` 字段判定 |
| `size` | `int` | 文件字节数；目录固定为 0 |
| `sha1` | `str \| None` | 文件 SHA1（大写十六进制）；目录为 None |
| `pickcode` | `str` | 拿直链用；目录也有 pickcode 但一般用不上 |
| `mtime` | `int` | 最后修改 unix 秒 |
| `ctime` | `int` | 创建 unix 秒 |
| `is_video` | `bool` | 115 侧标记为视频类型；目录固定 False |

### `FileMeta`

`file_info` 返回的单文件明细，字段与 `DirEntry` 的文件分支基本一致（区别：`sha1` 不为 None）。

### `VideoInfo`

`get_video_info` 返回的视频综合信息。

| 字段 | 类型 | 说明 |
|------|------|------|
| `pickcode` | `str` | 输入回传 |
| `width` | `int` | 原始视频宽（像素） |
| `height` | `int` | 原始视频高（像素） |
| `thumb_url` | `str` | 封面缩略图 URL |
| `master_m3u8_url` | `str` | HLS master playlist 绝对 URL |
| `definitions` | `list[VideoDefinition]` | 所有可用清晰度 |

### `VideoDefinition`

master m3u8 里的一个清晰度分支。

| 字段 | 类型 | 说明 |
|------|------|------|
| `bandwidth` | `int` | 码率 bit/s（`BANDWIDTH` 属性） |
| `resolution` | `str` | 如 `"1280x720"`；未声明时空串 |
| `label` | `str` | 如 `"HD"`；未声明时空串 |
| `m3u8_url` | `str` | 该清晰度的 variant m3u8 绝对 URL |

### `VideoSegment`

variant m3u8 里的一个 HLS ts 分段。

| 字段 | 类型 | 说明 |
|------|------|------|
| `index` | `int` | 0-based 序号 |
| `url` | `str` | 绝对 URL（相对路径已用 variant m3u8 URL 拼过） |
| `duration_seconds` | `float` | EXTINF 声明的时长，秒 |

### `DirectUrl`

`get_download_url` 返回的直链信息。

| 字段 | 类型 | 说明 |
|------|------|------|
| `file_id` | `str` | 目标文件 file_id（服务端从密文里解出来） |
| `file_name` | `str` | 文件名（同上） |
| `file_size` | `int` | 字节数 |
| `sha1` | `str` | SHA1 |
| `pickcode` | `str` | 回填的 pickcode |
| `url` | `str` | 可直接 302 给播放器的 CDN 直链 |
| `user_agent` | `str` | **调用时传的 UA 原样回传**，调用方后续 Range GET 必须复用 |
| `expires_at` | `int` | 从 URL 的 `t=` 参数解出的过期 unix 秒；`-1` 表示 URL 里未含该字段 |

---

## 异常层次

全部定义在 [`exceptions.py`](../exceptions.py)：

```
Cloud115Error                          # 基类
├── Cloud115AuthError                  # cookies 失效 / UID 缺失 / 账号冻结（errno=99/990009/990017/20130827/...）
├── Cloud115MembershipRequiredError    # 接口需要 VIP 会员（errno=406 "需要VIP会员"）
├── Cloud115NotFoundError              # 文件不存在 / pickcode 无效 / 被封禁
├── Cloud115RequestError               # HTTP 层错、5xx 重试耗尽、非 JSON 响应
├── Cloud115CipherError                # RSA 解密失败（响应结构异常）
└── Cloud115RateLimitedError           # 429 或限流 errno；带 retry_after_seconds
```

上层建议：

```python
from src.lib.cloud115 import (
    Cloud115Error,
    Cloud115AuthError,
    Cloud115NotFoundError,
    Cloud115RateLimitedError,
)

try:
    segments = await client.get_video_segments(pickcode)
except Cloud115MembershipRequiredError:
    # 引导用户升级 VIP；cookies 是好的，重新登录没用
    ...
except Cloud115AuthError:
    # 通知用户 cookies 过期，触发重新填 cookies 流程
    ...
except Cloud115NotFoundError:
    # 视频被 115 封禁或已删；上层把这条 media 标 invalid
    ...
except Cloud115RateLimitedError as e:
    # 上层退避 e.retry_after_seconds 秒后再试
    ...
except Cloud115Error:
    # 兜底：日志 + 重排队
    ...
```

**不要 `except Exception:`** —— SDK 已经把可预期路径映射到具体子类，捕获 `Cloud115Error` 就够；`Exception` 会把 `ValueError`（参数校验错）、`asyncio.CancelledError` 之类的都吞掉。

---

## 关键机制

### UA 绑定机制

`get_download_url` 返回的 URL 里带 `f=1` 参数（或 `f=3`），表示 115 CDN 在校验后续 Range GET 请求的 UA。

**工作流**：

```
1. 播放器/PyAV 请求 SakuraMedia 后端的 /stream 端点
2. 后端读到请求方的 User-Agent（比如浏览器的 UA 或 SakuraMedia 固定 UA）
3. 后端调 Cloud115Client.get_download_url(pickcode, user_agent=<这个 UA>)
4. 115 返回一个绑定了这个 UA 的直链
5. 后端 302 到直链
6. 播放器跟随 302 到 115 CDN，UA 自动一致 → 校验通过 → Range/seek 正常工作
```

**关键**：
- 拿直链时的 UA 和消费直链时的 UA **必须逐字节一致**
- Flutter media_kit 原生端：`Media(url, httpHeaders: {"User-Agent": ...})` 显式带上
- Flutter web：浏览器 UA 由浏览器决定，后端读 request header 传给 SDK 即可
- ffmpeg/PyAV 抽帧：`av.open(url, options={"user_agent": ua})` 显式覆写（默认 UA 是 `Lavf/*`，和拿链接时用的 UA 不一致会 403）

### 直链生命周期

- URL 里的 `t=<unix_ts>` 是**过期时间点**
- 一般 5 分钟内有效（不严格保证）
- 过期后 CDN 返 403 或 EOF
- SDK 只解析过期时间戳，不做主动刷新；**上层的策略**：
  - 播放场景：让客户端 seek 时重新请求 `/stream`（触发拿新直链）
  - 抽帧场景：抽第一帧前拿一次，遇 403 重新拿再继续

### 重试与限流

- **5xx / 网络错 / 超时**：SDK 内部退避重试 2 次（sleep 0.5s / 1.0s），失败抛 `Cloud115RequestError`
- **429**：**不重试**，直接抛 `Cloud115RateLimitedError`（携带 `Retry-After` 到 `retry_after_seconds`）
  - 理由：115 限流是账号信号，SDK 盲重试可能触发更严格风控
  - 上层建议按 `retry_after_seconds` 退避后再排队
- **业务 errno**：按错误映射表分类抛，不重试

---

## 手动测试 CLI

跟 SDK 平级的 `__main__.py` 提供 6 个子命令。cookies 支持三种传入方式：命令行参数、环境变量、交互 prompt。

```fish
# 推荐：环境变量方式，重复执行方便
set -x COOKIE_115 "UID=...; CID=...; SEID=...; KID=..."

uv run python -m src.lib.cloud115 check-alive
uv run python -m src.lib.cloud115 list-dir --cid 0 --limit 20
uv run python -m src.lib.cloud115 list-dir --cid 3428707991046116541  # 子目录
uv run python -m src.lib.cloud115 file-info --file-id 3471260435703924578
uv run python -m src.lib.cloud115 downurl --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 downurl --pickcode xxx --user-agent "Mozilla/5.0 CustomPlayer/1.0"

# VIP 专属：视频信息 + HLS 分段列表
uv run python -m src.lib.cloud115 video-info --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 video-segments --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 video-segments --pickcode xxx --prefer-bandwidth 1800000 --all

# 命令行参数方式（cookies 会进 shell history，注意场合）
uv run python -m src.lib.cloud115 --cookies "UID=..." check-alive

# 交互 prompt（不传 --cookies 也没有 COOKIE_115 时自动进入）
uv run python -m src.lib.cloud115 check-alive
# -> paste cookies:
```

`--help` 查看所有子命令和参数：

```fish
uv run python -m src.lib.cloud115 --help
uv run python -m src.lib.cloud115 list-dir --help
```

---

## 集成测试

`tests/lib/cloud115/test_integration.py` 提供 4 条端到端用例，默认 skip，需显式 flag + `COOKIE_115` 环境变量：

```fish
set -x COOKIE_115 "UID=...; CID=...; SEID=...; KID=..."
uv run pytest tests/lib/cloud115/ --run-cloud115-integration -n0 -v
```

无 flag 时正常 `uv run pytest` 会跳过集成用例（不影响 CI），只跑 67 个单元测试。

**意义**：cipher 单元测试无法端到端验证（`rsa_encode` / `rsa_decode` 不是数学逆变换，见下面协议注解），所以集成测试是 cipher 正确性的唯一权威证据。

---

## 协议实现说明（进阶）

如果你要改 [`cipher.py`](../cipher.py)，先读这段。

### encrypt 和 decrypt 不是数学逆变换

`rsa_encode` 和 `rsa_decode` **都用 `pow(x, e, n)` 同方向的模幂**，不是 RSA 的加密-解密对。

- `rsa_encode`：客户端 → 服务端方向。客户端用公钥 `(n, e)` 加密 payload，服务端用私钥 `d` 解密
- `rsa_decode`：服务端 → 客户端方向。服务端用私钥 `d` "签名" 响应，客户端用公钥 `(n, e)` "验证"恢复
- 数学上依靠 `pow(pow(m, d, n), e, n) == m`（RSA 双向性）

**推论**：**不能做 `rsa_decode(rsa_encode(x)) == x` 的 round-trip 单元测试**（一定失败）。协议正确性只能靠集成测试证明。

### XOR 常量不对称

- `rsa_encode` 里 12 字节 XOR key 用**固定常量** `G_KEY_L`
- `rsa_decode` 里 12 字节 XOR key 用 **`_rsa_gen_key(view[:16], sk_len=12)`** 走 KDF 派生

这是协议约定的不对称，勿改成"对称美"版本。KDF 表 `_G_KTS`（144 字节）是协议常量。

### XOR 语义不是循环 XOR

`_xor_cycle(data, key)` 严格照抄 p115cipher 的 `xor`：

1. 先处理开头 `len(data) % 4` 字节 remainder，与 `key[:remainder]` 逐位 XOR
2. 剩余按 `len(key)` 步长切段，**每段都从 `key[0]` 重新对齐**（不是把 key 循环起来）

只有当 `len(data) % 4 == 0` 且 `len(data) % len(key) == 0` 时才和普通循环 XOR 等价。

### RSA 填充

用"伪 PKCS#1 v1.5"填充，pad 字节固定为 `0x02`（真 PKCS#1 v1.5 要求随机非零）。别改成随机填充，服务端会解不了。

### 与 p115cipher 的关系

本 SDK **不引 p115cipher**，但协议细节严格参考 [`ChenyangGao/p115client`](https://github.com/ChenyangGao/p115client) 的 `p115cipher/util.py` 和 `p115cipher/__init__.py`。如果 115 后续改协议，先看它有没有更新，再对齐我们的 `cipher.py`。

---

## 不在本 SDK 范围

以下能力**明确不实现**，如需要请另起模块：

- 二维码扫码登录（`qrcodeapi.115.com` 三段状态机）
- 离线下载 `offline_add / list / delete`
- 文件上传 `upload_*`
- 分享 `share_*`
- 增量事件订阅（`life_list`）
- pickcode ↔ file_id 数学转换（若需要，从 `list_dir` 返回的 `DirEntry` 里直接读）
- `iter_dir(cid)` 自动翻页（上层写 offset 循环，5 行代码）
- 图片 CDN、字幕、视频转码历史
- 同步接口（如需在阻塞栈里调，用 `asyncio.run(...)` 包一层）
- Netscape cookies 文件导入

上层业务改造（`MediaLibrary` 加 `source_kind` 字段、`/stream` 端点 302 分派、`MediaThumbnailService` 走远程 URL 抽帧等）不在 SDK 范围，由 [`src/service/`](../../../service/) 各域自行接入。
