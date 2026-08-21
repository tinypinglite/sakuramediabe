# Cloud115 SDK

SakuraMediaBE 内部维护的 115 网盘极简异步客户端。**仅支持 cookies 认证**，覆盖播放/查找/缩略图/离线下载/导入搬运五类上层需求所需的 HTTP 接口，完全不依赖第三方 115 SDK。

- 位置：[`src/lib/cloud115/`](../)
- 入口：`Cloud115Client`
- 运行时依赖：`httpx`、`loguru`（都已在 `pyproject.toml`）；**未引入 `cryptography` / `pycryptodome`**，RSA/KDF 全用 Python 内置 `pow()` 手撸

> **重要**：115 直链以及 HLS variant/TS 都会校验签发时的 User-Agent。后续播放、Range 读取和 HLS 子请求必须逐字节复用同一 UA；Cookie 不能弥补 UA 不一致。

## 目录

- [快速开始](#快速开始)
- [认证与 cookies](#认证与-cookies)
- [Cookies 保活](#cookies-保活)
- [仅秒传](#仅秒传)
- [接口清单](#接口清单)
- [VIP 视频接口](#vip-视频接口)
- [数据类型](#数据类型)
- [异常层次](#异常层次)
- [关键机制](#关键机制)
- [手动测试 CLI](#手动测试-cli)
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

## 仅秒传

`rapid_upload(path, pid="0")` 只尝试本地文件秒传，不会回退到普通上传。SDK 会从
`UID` Cookie 自动识别 Android（F1）或支付宝小程序（R2）槽，并选择正确的 `userkey` 接口：

```python
result = await client.rapid_upload(
    "/media/movie.mkv",
    pid="0",
)
if result.status.value == "success":
    print(result.file_id, result.pickcode)
elif result.status.value == "not_hit":
    print("115 未命中秒传；SDK 没有上传文件")
```

秒传当前只支持由 `fetch_result(..., app="android")` 或
`fetch_result(..., app="alipaymini")` 取得的 Cookie；其他登录槽调用时会抛出认证异常。
扫码完成后可直接把登录结果交给客户端，业务侧不需要知道底层上传协议：

```python
login = await qr.fetch_result(token.uid, app="alipaymini")
async with Cloud115Client(cookies=login.cookies) as client:
    result = await client.rapid_upload("/media/movie.mkv")
```

SDK 会先计算完整 SHA-1；大文件收到 `status=7` 时，再按服务端返回的 `sign_check` 范围计算一次 SHA-1。最终 `status=2` 才表示已完成秒传。`SUCCESS`、`NOT_HIT` 和 `FILE_CHANGED` 由 `RapidUploadResult` 返回；认证、限流、网络和协议错误继续抛出既有异常。

cookie 上传初始化需要额外获取 `userkey`，该值只缓存在当前客户端实例，不会写入 cookies 或数据库。该功能不会调用 OSS token、分块上传或普通上传接口。

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

## Cookies 保活

SDK 内部把 cookies 存成保序 dict，每次响应都会自动 merge 服务端 `Set-Cookie`（同一个 `Cloud115Client` 实例的多次请求间生效）。这是必要的：

- 阿里云 WAF 的 `acw_tc` 字段是**30 分钟过期** token（`Max-Age=1800`）
- 客户端不带 `acw_tc` 时服务端会立刻塞一个新的（`Set-Cookie: acw_tc=...`）
- 若客户端始终用户初次粘贴那份 cookies 而不吃 `Set-Cookie`，30 分钟后 `acw_tc` 过期，WAF 直接挡门

**实测确认**：`acw_tc` 是账号级、跨 `my.115.com` / `webapi.115.com` / `proapi.115.com` 共享的，只要客户端把新值放进 `Cookie:` header 就没问题（不需要按子域分开管理）。

### 三个保活相关 API

**`snapshot_cookies() -> str`**：拿当前完整 cookies 字符串（含服务端最新推送的 `acw_tc`）。上层业务应**定时落盘**，进程重启时用最新快照初始化 SDK，避免每次首启就撞 `acw_tc` 过期需要重种一次的额外往返。

```python
# 例：APScheduler 定时任务
async def persist_cookies():
    async with Cloud115Client(cookies=load_cookies_from_config()) as client:
        await client.check_cookies_alive()
        save_cookies_to_config(client.snapshot_cookies())
```

**`update_cookies(cookies: str) -> None`**：整体覆盖当前 cookies（管理面板改了 cookies 后热生效）。不合法时抛 `Cloud115AuthError`，**原 cookies 不被破坏**。

```python
try:
    client.update_cookies(new_cookies_from_user)
except Cloud115AuthError:
    # UI 弹错误，客户端仍可继续用旧 cookies
    ...
```

**`user_id -> str`**（只读属性）：从 `UID` cookie 前段解出的数字用户 ID，供上层日志/观测使用。

### 能续、不能续什么

| 字段 | 有效期 | SDK 能自动续吗 |
|------|--------|---------------|
| `acw_tc`（阿里云 WAF） | 30 分钟 | ✅ Set-Cookie 自动 merge |
| `GST` / `USERSESSIONID` / `PHPSESSID` | 服务端不定时刷新 | ✅ 同上 |
| `UID` / `CID` / `SEID` / `KID` | 长效 60 天+ | ❌ 必须用户重登，任何 SDK 都做不了 |
| 账号被踢下线（同 ssoent 别处登录 60 秒后） | 立即失效 | ❌ 上层探活到 fail 后引导重登 |

---

## 接口清单

### 1. `probe_cookies_status()` / `check_cookies_alive()`

`probe_cookies_status()` 区分 `alive`、`expired`、`unavailable`；兼容接口 `check_cookies_alive()` 仍返回 bool。

```python
alive: bool = await client.check_cookies_alive()
```

**契约**：
- 200 + JSON `state=True` → `alive`
- 302 到登录页、401/403、JSON `state=False` → `expired`
- 网络错误、429/5xx、非 JSON 或异常响应 → `unavailable`

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
- 115 将不存在的 `cid` 静默按根目录处理时，SDK 检查响应 `cid` 不一致并抛 `Cloud115NotFoundError`
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

**注意**：`file_info` 只接 `file_id`。**业务侧持久化的 ID 通常是 pickcode**（跨会话稳定，file_id 会因 115 内部存储位置变动而变），这种场景请用下面的 `pickcode_info`。

**异常**：
- `state=False` + `errNo=20018`（文件不存在或已删除）→ `Cloud115NotFoundError`
- `data` 数组为空 → `Cloud115NotFoundError`（file_id 不存在）
- 其它异常同 `list_dir`

---

### 4. `pickcode_info(pickcode) -> FileMeta`

按 pickcode 查单文件元信息。**返回结构与 `file_info` 完全一致**，只是走 `webapi.115.com/files/get_info?pick_code=xxx`（115 官方支持 `pick_code` 参数）。

```python
meta = await client.pickcode_info("bijccwbcsacpi842c")
print(meta.file_id, meta.name, meta.size)
```

**用途**：数据库里持久化的稳定 ID 通常是 pickcode，业务需要"确认这个文件还在不在 / 拿最新元数据"时直接 `pickcode_info(pickcode)`，避免绕道 `list_dir` 或 `get_download_url`。

**异常**：
- `data` 数组为空 → `Cloud115NotFoundError`（pickcode 无效或文件已删）
- 其它异常同 `list_dir`

---

### 5. `dir_info(cid) -> DirMeta`

取目录元信息 + 面包屑路径链。走 `webapi.115.com/category/get?cid=<cid>`。

```python
d = await client.dir_info("3428707991046116541")
print(d.name, d.parent_id, d.file_count, d.folder_count)
for crumb in d.paths:
    print(f"  {crumb.file_id}\t{crumb.name}")
```

**根目录特判**：`cid="0"` 会被 115 服务端拒（`errNo=1001`），SDK 层**直接构造哨兵值返回**（`name="根目录"`, `pickcode=""`, `paths=()`），调用方不必特判 `cid == "0"`。

**参数**：
- `cid`：目录 category_id 字符串；空串抛 `ValueError`

**异常**：
- `state=False` + `errNo=70005`（目录不存在或已删除）→ `Cloud115NotFoundError`
- `state=False` + `errNo=1001`（参数错误）→ `Cloud115Error`
- `state=False` + 未知 `errno` → `Cloud115Error`（基类，未识别的 errno 不吞）
- auth 类 errno → `Cloud115AuthError`

---

### 6. `get_download_url(pickcode, user_agent) -> DirectUrl`

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

### 7. `Cloud115Client(cookies, *, user_agent=None, timeout=30.0, http_client=None)`

构造函数。

**参数**：
- `cookies`：见 [认证与 cookies](#认证与-cookies)
- `user_agent`：客户端请求 115 的固定 UA，也是 `get_video_info` 签发 HLS variant/TS 时绑定的 UA；默认使用稳定 Chrome UA
- `timeout`：httpx 请求超时秒
- `http_client`：可选，注入外部 `httpx.AsyncClient`（用于测试或复用连接池）；不注入时 SDK 自建一个，`close()` 时释放

**注意 UA 有两个层次**：
- 构造参数 `user_agent`：客户端 -> 115 的请求 UA，也是 `get_video_info` 生成的 HLS variant/TS 所绑定的 UA；可通过只读属性 `client.user_agent` 获取
- `get_download_url(pickcode, user_agent=...)` 的参数：**灌进直链**给下游播放器或其它需要 seek 的消费者使用的 UA

这两个是独立的，不要混淆。

---

## 文件管理接口（导入管线用）

### `iter_files_recursive(cid, *, page_size=1000) -> AsyncIterator[DirEntry]`

递归枚举 cid 目录树下**全部文件**（不含目录条目），逐条 yield。

- 触发条件：`/files` 加 `show_dir=0 & cur=0`（p115client 记载的全树递归模式）
- 固定 `o=file_name & asc=1` 排序，保证大目录跨页分页一致
- **每条只带 parent cid，拿不到父目录名** → cid→目录名映射由上层用 `list_dir` 遍历目录结构自行维护（目录数远小于文件数）
- `DirEntry.play_long`（已转码视频时长秒）与 `DirEntry.ic`（违规封禁标记，1=封禁）在本响应白给；未带时为 `None`
- 115 对无效 `cid` 可能返回根目录成功响应；SDK 检查响应 `cid`，不一致时抛 `Cloud115NotFoundError`，不会把根目录内容当作请求目录返回

### `copy_files(fids, *, pid) -> None`

批量复制到 pid 目录（云端零流量搬运）。⚠️ 勿并发、单次 ≤5 万（官方文档）→ 上层串行分批。**复制产生新 fid 和新 pickcode**（仅 sha1 相同）→ 登记必须以复制后 re-list 目标目录的新条目为准。同账号复制**占双倍空间**。

### `move_files(fids, *, pid) -> None`

批量移动，与 copy 协议同型。**fid / pickcode 保持不变**，因此登记可以先于搬运完成。媒体导入的 `cleanup-source` 就是走这个接口：直接把源搬进库子树，不复制、不占双倍空间、也没有"复制完再删源"这一步。

### `batch_rename(renames: dict[fid, 新名]) -> None`

批量改名。⚠️ 文件新名**必须带扩展名**（否则 115 按最后一个 `.` 截断），扩展名本身不可改。改名保持 fid/pickcode 不变。单批上限未见官方文档，上层保守按 30–50/批。

### `rename_file(fid, new_name) -> None`

单文件改名，每个请求只提交一个 fid。导入流程使用该接口，并在成功响应后调用 `file_info(fid)` 核对实际名称。

### `delete_files(fids, *, pid=None) -> None`

批量删除，**进 115 回收站**（有误删缓冲）。pid 可选。

### `download_bytes(pickcode, *, user_agent, max_bytes=10MB) -> bytes`

小文件（字幕等）全量下载。内部先 `get_download_url` 再用**同一 UA** GET 直链——封装"拿链接与 GET 必须同 UA"这一易错点。超过 max_bytes 抛 `Cloud115RequestError`，视频请走 302 或受控 Range 读。

---

## VIP 视频接口

仅 VIP 账号可用。非会员调用会抛 `Cloud115MembershipRequiredError`（errno=406）；
登录态仍然有效，业务侧应提示会员限制并回落到直链，不能引导用户重新登录。

### `get_video_info(pickcode) -> VideoInfo`

先请求 `webapi.115.com/files/video` 获取 master m3u8 地址，再解析所有 variant：

```python
info = await client.get_video_info("bijccwbcsacpi842c")
print(client.user_agent)  # 播放 variant 与 TS 时必须原样复用
for definition in info.definitions:
    print(
        definition.bandwidth,
        definition.resolution,
        definition.label,
        definition.m3u8_url,
    )
```

- `file_status != 1`：抛 `Cloud115VideoNotReadyError`，并保留 `file_status`；新视频通常需要等待转码。
- `video_url` 为空：抛 `Cloud115NotFoundError`，表示 pickcode 不是可转码视频。
- variant 地址位于 115 转码 CDN，可直接交给播放器，不需要携带 Cookie，但必须使用 `client.user_agent`。

### `get_video_segments(pickcode, *, prefer_bandwidth=None) -> list[VideoSegment]`

读取指定码率 variant 的 TS 分段；未指定或指定码率不存在时选择最高码率：

```python
segments = await client.get_video_segments(
    "bijccwbcsacpi842c",
    prefer_bandwidth=1800000,
)
for segment in segments:
    print(segment.index, segment.duration_seconds, segment.url)
```

每个分段是独立签名 URL。2026-07-18 真机复测确认，variant m3u8 与 TS URL
均带 `se=u,ua` 并严格校验签发 UA：匹配 UA 返回 `200`，httpx/mpv/ffmpeg
默认 UA 均返回 `403`，即使额外携带 Cookie 也不会放行。播放器必须把同一
`User-Agent` 应用于 variant 及其所有 TS 子请求。

### `get_video_segments_for_definition(definition) -> list[VideoSegment]`

读取已经由 `get_video_info` 解析出的清晰度分支，避免业务层为了获取同一 variant
的分片再次请求 master playlist。缩略图任务用它选择最低分辨率后解析 TS 时间轴。

### `Cloud115HlsSegmentReader(url, *, user_agent, chunk_size=64KiB)`

单个 TS 的同步、前向、不可 seek file-like。读取器使用绑定 UA 发起惰性流式 GET，
只在 PyAV 调用 `read()` 时继续消费响应；解出首个完整帧并关闭后，不再下载分片余下内容。
它不使用整文件直链，也不发送 HTTP Range，专供 HLS 分片首帧抽取。

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

`file_info` / `pickcode_info` 返回的单文件明细，字段与 `DirEntry` 的文件分支基本一致（区别：`sha1` 不为 None）。

### `DirMeta`

`dir_info` 返回的目录元信息。

| 字段 | 类型 | 说明 |
|------|------|------|
| `cid` | `str` | 目录 category_id（输入回传） |
| `name` | `str` | 目录名 |
| `pickcode` | `str` | 目录自身的 pickcode；根目录哨兵为 `""` |
| `parent_id` | `str` | 父目录 cid，从 `paths` 末尾解析；根目录哨兵为 `""` |
| `file_count` | `int` | 目录直接内容总数（含子目录）；根目录哨兵为 0 |
| `folder_count` | `int` | 直接子目录数；根目录哨兵为 0 |
| `play_long_seconds` | `int` | 目录内视频总时长秒（115 服务端聚合） |
| `mtime` | `int` | 最后修改 unix 秒 |
| `ctime` | `int` | 创建 unix 秒 |
| `paths` | `tuple[DirBreadcrumb, ...]` | 面包屑链：从根到当前目录父级；根目录哨兵为 `()` |

### `DirBreadcrumb`

面包屑一节。

| 字段 | 类型 | 说明 |
|------|------|------|
| `file_id` | `str` | 该级 file_id/cid；根目录是 `"0"` |
| `name` | `str` | 该级名字，如 `"根目录"` |

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

### `VideoInfo` / `VideoDefinition` / `VideoSegment`

- `VideoInfo`：原视频尺寸、缩略图、master m3u8 与全部清晰度分支。
- `VideoDefinition`：variant 的 `bandwidth`、`resolution`、`label` 与绝对 m3u8 地址。
- `VideoSegment`：分段序号、绝对 TS 地址与 `EXTINF` 时长。

---

## 异常层次

全部定义在 [`exceptions.py`](../exceptions.py)：

```
Cloud115Error                          # 基类
├── Cloud115AuthError                  # cookies 失效 / UID 缺失 / 账号冻结（errno=99/990009/990017/20130827/...）
├── Cloud115MembershipRequiredError    # 接口需要 VIP 会员（errno=406 "需要VIP会员"）
├── Cloud115VideoNotReadyError          # 视频尚未完成 HLS 转码；带 file_status
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
    direct = await client.get_download_url(pickcode, user_agent="Mozilla/5.0 ...")
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
- SakuraMedia 前端只需打开既有 `/stream` 地址；后端读取播放器自然 UA，优先签发
  最高码率 HLS，暂不可用时自动降级到原画直链。前端只需正常跟随 302 且不要改写 UA
- 只有绕过 SakuraMediaBE、直接消费 SDK 返回的 115 原始 URL 时，调用方才需要自行保证 UA 一致
- 直接把 HLS URL 交给 ffmpeg/PyAV 时需要显式覆写 UA；后端缩略图任务则用 `Cloud115HlsSegmentReader` 保证 UA 一致并在首个完整帧后停止读取

HLS 同样受 UA 绑定约束。`get_video_info` 使用 `Cloud115Client.user_agent` 签发
master、variant 和 TS 地址；消费方无需 Cookie，但必须对 m3u8 及其所有子请求使用
这一 UA。SakuraMediaBE 的 `/media/{media_id}/stream` 以请求里的真实 UA 调
`get_video_info`，再 `302` 到最高码率 115 variant；播放器跟随重定向及请求 TS 时会
自然沿用自己的 UA，因此可通过 115 的校验。后端不代理 m3u8 或 TS 字节流，也不再
对外暴露独立 HLS 清晰度端点。

### 直链生命周期

- URL 里的 `t=<unix_ts>` 是**过期时间点**
- 真机观察常为十几小时，但不假定固定寿命，始终以 URL 中的 `t` 为准
- 过期后 CDN 返 403 或 EOF
- SDK 只解析过期时间戳，不做主动刷新；**上层的策略**：
  - 播放场景：让客户端 seek 时重新请求 `/stream`（触发拿新直链）
  - HLS 缩略图场景：每个媒体开始时获取一次 playlist；当前任务不在处理中刷新 URL

### 重试与限流

- **5xx / 网络错 / 超时**：SDK 内部退避重试 2 次（sleep 0.5s / 1.0s），失败抛 `Cloud115RequestError`
- **429**：**不重试**，直接抛 `Cloud115RateLimitedError`（携带 `Retry-After` 到 `retry_after_seconds`）
  - 理由：115 限流是账号信号，SDK 盲重试可能触发更严格风控
  - 上层建议按 `retry_after_seconds` 退避后再排队
- **业务 errno**：按错误映射表分类抛，不重试

---

## 手动测试 CLI

跟 SDK 平级的 `__main__.py` 提供多个子命令。cookies 支持三种传入方式：命令行参数、环境变量、交互 prompt。

```fish
# 推荐：环境变量方式，重复执行方便
set -x COOKIE_115 "UID=...; CID=...; SEID=...; KID=..."

uv run python -m src.lib.cloud115 check-alive
uv run python -m src.lib.cloud115 list-dir --cid 0 --limit 20
uv run python -m src.lib.cloud115 list-dir --cid 3428707991046116541  # 子目录
uv run python -m src.lib.cloud115 file-info --file-id 3471260435703924578
uv run python -m src.lib.cloud115 pickcode-info --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 dir-info --cid 0                    # 根目录哨兵
uv run python -m src.lib.cloud115 dir-info --cid 3428707991046116541  # 子目录面包屑
uv run python -m src.lib.cloud115 downurl --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 downurl --pickcode xxx --user-agent "Mozilla/5.0 CustomPlayer/1.0"
uv run python -m src.lib.cloud115 video-info --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 video-segments --pickcode bijccwbcsacpi842c
uv run python -m src.lib.cloud115 video-segments --pickcode xxx --prefer-bandwidth 1800000 --all
uv run python -m src.lib.cloud115 snapshot-cookies   # 打印 Set-Cookie merge 后的完整 cookies

# 文件管理（导入管线用）
uv run python -m src.lib.cloud115 list-recursive --cid 3428707991046116541 --max-files 50
uv run python -m src.lib.cloud115 copy --fid 111 --fid 222 --pid 3000000
uv run python -m src.lib.cloud115 move --fid 111 --pid 3000000
uv run python -m src.lib.cloud115 rename --fid 111 --new-name "ABP-123＿CD1＿movie.mp4"
uv run python -m src.lib.cloud115 delete --fid 111 --fid 222
uv run python -m src.lib.cloud115 download-bytes --pickcode xxx --output /tmp/sub.srt

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

## 协议实现说明（进阶）

如果你要改 [`cipher.py`](../cipher.py)，先读这段。

### encrypt 和 decrypt 不是数学逆变换

`rsa_encode` 和 `rsa_decode` **都用 `pow(x, e, n)` 同方向的模幂**，不是 RSA 的加密-解密对。

- `rsa_encode`：客户端 → 服务端方向。客户端用公钥 `(n, e)` 加密 payload，服务端用私钥 `d` 解密
- `rsa_decode`：服务端 → 客户端方向。服务端用私钥 `d` "签名" 响应，客户端用公钥 `(n, e)` "验证"恢复
- 数学上依靠 `pow(pow(m, d, n), e, n) == m`（RSA 双向性）

**推论**：**不能做 `rsa_decode(rsa_encode(x)) == x` 的 round-trip 单元测试**（一定失败）。仓库中的协议回归使用固定样本；需要验证真实服务时，请通过上面的手动测试 CLI 操作。

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

- 普通文件上传 `upload_*`（当前仅提供 `rapid_upload` 秒传初始化）
- 分享 `share_*`
- 增量事件订阅（`life_list`）
- pickcode ↔ file_id 数学转换（若需要，从 `list_dir` 返回的 `DirEntry` 里直接读）
- `iter_dir(cid)` 单层自动翻页（上层写 offset 循环，5 行代码；全树枚举用 `iter_files_recursive`）
- 图片 CDN、视频转码历史
- 同步接口（如需在阻塞栈里调，用 `asyncio.run(...)` 包一层）
- Netscape cookies 文件导入

> 历史注：早期版本"明确不做"清单还包含扫码登录、离线下载、`files/delete`、递归列文件——
> 均已随业务需要陆续落地（`qrlogin.py` / 离线三组能力 / `delete_files` / `iter_files_recursive`）。

上层业务改造（`MediaLibrary` 加 `source_kind` 字段、`/stream` 端点 302 分派、`MediaThumbnailService` 走远程 URL 抽帧等）不在 SDK 范围，由 [`src/service/`](../../../service/) 各域自行接入。
