# Media Provider Bundle 插件化方案

> 设计草案；只定义目标边界和公开协议，不含迁移步骤或代码改动。
>
> 类型和硬规则见 [provider-plugin-protocol-contract-v1.md](./provider-plugin-protocol-contract-v1.md)。

## 1. 目标

主仓库不应知道媒体来自哪一种存储，也不应知道它通过 Range、302 或 HLS 播放。
它只按 `MediaLibrary.provider_key` 找到 `media.provider` bundle 并分派请求。

~~~text
宿主：鉴权、验签、业务记录、任务状态
              │
              ▼
media.provider bundle：全部外部 I/O 与 HTTP Response
~~~

`provider_key` 只用于注册表查找。宿主不得按其文本做厂商分支。

## 2. Bundle 边界

一个插件包最多注册一个 `media.provider`。它必须提供一个 `StorageProvider`，其中包含：

| 能力 | 规则 |
| --- | --- |
| 媒体库配置、浏览、导入、删除 | 必选 |
| 文件 hash | 必选；按统一采样约定生成媒体文件内容指纹 |
| 播放 | 必选；插件直接返回 HTTP Response |
| 缩略图 | 必选；一次生成一个 Media 的完整缩略图集 |
| 单媒体剪辑 | 必选；生成独立 MP4 片段 |
| 下载 | 可选；仅服务同一 bundle 的媒体库 |

存储和下载不能由宿主自由拼装。下载完成物只能交给同一 bundle 的 storage 消费；bundle
可以按自身能力提供下载，也可以不提供：

| provider_key | bundle 内部 |
| --- | --- |
| `provider-a` | storage、缩略图、剪辑；可选择提供下载 |
| `provider-b` | storage、缩略图、剪辑；可选择提供下载 |
| 未来 provider | 自己的 storage、缩略图、剪辑；可选择不提供下载 |

插件内部可以拆模块；对宿主始终只有一个不透明边界。

## 3. 所有权

| 宿主 | bundle |
| --- | --- |
| 账号、鉴权、签名、通用路由 | Cookie 校验、厂商 SDK、远端 API |
| MediaLibrary、Media、DownloadTask、MediaClip 记录 | 所有媒体源 I/O、受管目录、来源/存储 ref |
| 电影/视频业务规则、导入事务、任务状态 | 播放响应、缩略图和片段临时产物 |
| 缩略图与片段产物最终入库 | provider 内部交接 |

`storage_ref`、`source_ref`、导入 `receipt` 和下载完成物均为不透明 JSON object，宿主只能
保存和原样传回产生它的 bundle，不能读写其中字段。`provider_config` 落库和传给 provider
后对其它宿主业务同样是不透明 JSON；宿主只依据 bundle 声明的 `ConfigField` 做机械性表单
处理：拒绝未知字段、secret 掩码/保留、`read_only` 处理和 patch merge。宿主不得解释字段业务
语义、推断存储类型或自行改写值。

## 4. 六条链路

| 链路 | 宿主 | bundle |
| --- | --- | --- |
| 配置媒体库 | 渲染平面字段、脱敏、保存返回配置 | `prepare_library` 验证 Cookie、识别账号、确认受管根目录 |
| 浏览与导入 | 原样传回 ref；在数据库提交后 finalize，失败时 abort | 浏览、扫描、搬运或远端落位，返回 storage ref 与 receipt |
| 播放 | 验签后调用通用网关 | `handle_playback` 返回 Range、302 或单媒体 HLS Response |
| 缩略图 | 一次验证并入库整批产物 | `generate_thumbnails` 在一次调用中生成一个 Media 的完整采样集 |
| 剪辑 | 校验缩略图区间、时长和去重；固化 MP4 | `create_clip` 从一个 Media 生成临时 MP4 |
| 下载 | 保存通用任务状态；把完成 ref 原样交给同 bundle 导入 | 提交、同步、删除任务 |

播放的唯一来源媒体网关为：

~~~text
GET /media/{media_id}/play/{resource_path:path}?delivery=proxy|redirect
~~~

初始请求、HLS playlist 和 HLS segment 都走此路由；`resource_path` 只由插件解释。`delivery`
默认 `proxy`，由 bundle 声明支持的方式；它不改变媒体授权，故不进入 URL 签名。宿主只
接受空串或相对安全路径：不得以 `/` 开头，不得含反斜杠、空段、`.`、`..` 或 NUL 字符，
不拼接或解析其业务含义。HLS playlist 用网关 URL 生成分片地址。一次请求只对应一个 `Media`。

片段是宿主的独立资产：插件只把 MP4 写入 workspace，宿主验证、探测、移动并记录它；来源
Media 删除后片段仍按现有业务规则保留。

## 5. 目标状态

迁移完成后：

- `MediaLibrary` 只保留 `provider_key`、`provider_config`、`account_key`；
- `Media` 只保留一个不透明 `storage_ref`、文件 hash 和通用媒体事实；
- `DownloadClient` 只保留 provider 归属和 provider_config；
- `DownloadTask` 只保留 `remote_id`、通用状态和进度；
- 主仓库没有厂商 SDK、schema、router、模型列、HLS 专用路由或厂商 `if` 分支。

bundle 缺失或停用时返回 `provider_not_installed`，不回退到其它 provider。

## 6. V1 不含

- 二维码登录；Cookie 是唯一授权输入。
- 跨 bundle 复制或自动导入。
- 下载暂停/恢复、文件清单、死种清理。
- 异步剪辑或把片段留在 provider 外部。

现有媒体有效性扫描是否保留，迁移前另行决定；若保留，只能是 bundle 的高层请求。
