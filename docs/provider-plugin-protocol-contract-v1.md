# Media Provider Bundle 协议 V2

> 设计草案。实现时作为 Host API 6 的公开类型，插件只能依赖
> `src.plugins.provider_protocol`。

## 1. 注册规则

扩展 key 固定为 `media.provider`。一个插件包最多声明一个 bundle，`provider_key` 全局唯一。
注册阶段只能声明对象，不得联网、校验 Cookie、创建目录或连接下载器。

bundle 必须构造出一个 `StorageProvider`，它必须实现浏览、导入、删除、文件 hash、播放、批量
缩略图和单媒体剪辑。`downloads` 是唯一可选组成。

`JsonObject` 表示 JSON object。`storage_ref`、`source_ref`、导入 `receipt` 和下载完成 ref
都是 opaque `JsonObject`：宿主只保存并原样传回产生它的 bundle，绝不读取、补写、比较或拼接
字段。`provider_config` 在落库和传给 provider 后，对其它宿主业务同样是 opaque；宿主只可
依据 bundle 声明的 `ConfigField` 做机械性表单处理：拒绝未知字段、掩码或保留 secret、处理
`read_only` 字段并执行 patch merge。宿主不得解释字段业务语义、推断存储类型或自行改写值。

## 2. 公开类型

以下为 Python 伪代码；所有 handle 都是只读快照。

~~~python
@dataclass(frozen=True)
class LibraryHandle:
    library_id: int
    provider_key: str
    provider_config: JsonObject
    account_key: str | None


@dataclass(frozen=True)
class MediaHandle:
    media_id: int
    library: LibraryHandle
    storage_ref: JsonObject
    file_name: str
    file_size_bytes: int
    duration_seconds: int


@dataclass(frozen=True)
class DownloadClientHandle:
    client_id: int
    library: LibraryHandle
    provider_config: JsonObject


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    input: Literal["text", "secret", "path"]
    required: bool
    description: str | None = None
    multiline: bool = False
    read_only: bool = False
    hint: str | None = None


@dataclass(frozen=True)
class ProviderDiagnosticCheck:
    key: str
    status: Literal["ok", "warning", "failed", "skipped"]
    code: str
    message: str
    details: JsonObject | None = None


@dataclass(frozen=True)
class ProviderDiagnosticReport:
    status: Literal["ok", "warning", "failed"]
    checks: tuple[ProviderDiagnosticCheck, ...]


@dataclass(frozen=True)
class PreparedLibrary:
    provider_config: JsonObject
    account_key: str | None


@dataclass(frozen=True)
class BrowseEntry:
    source_ref: JsonObject
    name: str
    entry_type: Literal["file", "directory"]
    size_bytes: int | None
    modified_at: datetime | None
    is_video: bool


@dataclass(frozen=True)
class BrowsePage:
    entries: tuple[BrowseEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ImportFile:
    source_ref: JsonObject
    name: str
    relative_path: str
    size_bytes: int
    is_video: bool


@dataclass(frozen=True)
class ImportFileContent:
    content: bytes
    deletion_receipt: JsonObject


@dataclass(frozen=True)
class ImportPlacement:
    relative_path: str


@dataclass(frozen=True)
class StagedMedia:
    storage_ref: JsonObject
    receipt: JsonObject
    size_bytes: int
    duration_seconds: int | None
    video_info: JsonObject | None


@dataclass(frozen=True)
class PlaybackContext:
    request: Request
    resource_path: str
    delivery: Literal["proxy", "redirect"]
    url_for: Callable[[str], str]


@dataclass(frozen=True)
class ThumbnailArtifact:
    offset_seconds: int
    relative_path: str


@dataclass(frozen=True)
class ThumbnailGeneration:
    expected_count: int
    artifacts: tuple[ThumbnailArtifact, ...]


@dataclass(frozen=True)
class ClipArtifact:
    relative_path: str


@dataclass(frozen=True)
class DownloadSubmission:
    source_uri: str
    display_name: str


@dataclass(frozen=True)
class RemoteDownloadTask:
    remote_id: str
    name: str
    state: Literal["queued", "downloading", "completed", "failed"]
    progress: float
    completed_source_ref: JsonObject | None


class ProviderOperationError(RuntimeError):
    provider_key: str
    operation: str
    code: Literal[
        "invalid_config", "authentication_failed", "source_not_found", "source_blacklisted",
        "unsupported", "unavailable",
    ]
    safe_message: str
    retryable: bool
~~~

## 3. Bundle 与操作

~~~python
class DownloadComponent(Protocol):
    config_fields: tuple[ConfigField, ...]

    def prepare_client(
        self,
        *,
        submitted_config: JsonObject,
        library: LibraryHandle,
        previous: DownloadClientHandle | None,
    ) -> JsonObject: ...

    def test_client(
        self,
        *,
        submitted_config: JsonObject,
        library: LibraryHandle,
    ) -> ProviderDiagnosticReport: ...

    def build(self, *, client: DownloadClientHandle) -> "DownloadProvider": ...


class MediaProviderBundle(Protocol):
    provider_key: str
    display_name: str
    library_config_fields: tuple[ConfigField, ...]
    playback_deliveries: tuple[Literal["proxy", "redirect"], ...]
    downloads: DownloadComponent | None

    def prepare_library(
        self,
        *,
        submitted_config: JsonObject,
        previous: LibraryHandle | None,
    ) -> PreparedLibrary: ...

    def build_storage(self, *, library: LibraryHandle) -> "StorageProvider": ...


class StorageProvider(Protocol):
    def browse(
        self, *, parent_ref: JsonObject | None, cursor: str | None, limit: int
    ) -> BrowsePage: ...

    def scan_import_source(self, *, source_ref: JsonObject) -> Iterable[ImportFile]: ...

    def read_import_file(self, *, source: ImportFile) -> ImportFileContent: ...
    def delete_import_file(self, *, receipt: JsonObject) -> None: ...

    def stage_import_file(
        self,
        *,
        source: ImportFile,
        placement: ImportPlacement,
        source_disposition: Literal["keep", "delete_after_commit"],
        operation_key: str,
    ) -> StagedMedia: ...

    def finalize_import(self, *, receipt: JsonObject) -> None: ...
    def abort_import(self, *, receipt: JsonObject) -> None: ...
    def delete_media(self, *, media: MediaHandle) -> None: ...
    def compute_file_hash(self, *, media: MediaHandle) -> str: ...

    async def handle_playback(
        self, *, media: MediaHandle, context: PlaybackContext
    ) -> Response: ...

    def generate_thumbnails(
        self, *, media: MediaHandle, workspace: Path
    ) -> ThumbnailGeneration: ...

    def create_clip(
        self,
        *,
        media: MediaHandle,
        start_offset_seconds: int,
        end_offset_seconds: int,
        workspace: Path,
    ) -> ClipArtifact: ...


class DownloadProvider(Protocol):
    def submit(self, *, submission: DownloadSubmission) -> RemoteDownloadTask: ...
    def list_tasks(self) -> tuple[RemoteDownloadTask, ...]: ...
    def delete_task(self, *, remote_id: str, delete_files: bool) -> None: ...
~~~

## 4. 调用规则

### 配置

- 表单是扁平 object；只支持 `text`、`secret`、`path`。`description` 是给用户看的字段说明，宿主不解释其业务语义。
- 宿主依据 `ConfigField` 拒绝未知可写字段，更新时保留未提交的 secret/read_only，且不在 API
  或日志返回 secret；patch 只做字段级合并。
- `prepare_library` / `prepare_client` 负责 provider 专属的语义验证与规范化；宿主只保存它们
  返回的 object，不自行解释或改写其中值。
- `account_key` 是 provider 返回的上游账号标识；宿主不对其施加唯一性约束。

### 下载器测试

宿主可调用 `DownloadComponent.test_client` 对尚未保存的配置执行主动检查。该方法只返回
`ProviderDiagnosticReport`，不写入下载器配置；`warning` 和 `failed` 都由前端明确展示，
但不会阻止用户保存配置。检查结果中的 `details` 可携带 provider 为解释结果所需的非敏感
结构化信息，但不得携带密码等 secret。

### 浏览与导入

- `parent_ref=None` 是 bundle 自己定义的根；cursor 只能原样回传。宿主限制 limit 为 1–200。
- 所有 `source_ref` 只能由产生它的 bundle 消费；下载完成 ref 也不例外。
- `ImportPlacement.relative_path` 是宿主给出的逻辑相对路径，不能绝对或含 `..`；bundle 映射到
  自己的受管位置。
- `stage_import_file` 必须按 `operation_key` 幂等。宿主先保存 receipt 和业务记录，事务提交后
  `finalize_import`，失败时 `abort_import`；两者必须可安全重试。
- `read_import_file` 仅用于随媒体导入的侧车字幕等小文件；返回的 `deletion_receipt` 只能由同一
  provider 的 `delete_import_file` 消费。删除时必须确认来源未被替换。
- `delete_media` 成功或确认对象已不存在后，宿主才删除 `Media` 记录。

### 文件 hash

`compute_file_hash` 返回媒体原始文件的版本化内容指纹。宿主只负责调用与 Media 所属
`provider_key` 对应的 storage，不读取文件路径、不解释 `storage_ref`，也不提供回退实现。
固定采样区间、短文件处理、摘要拼接和返回值格式见[文件 Hash 约定](./provider-file-hash-protocol-draft.md)。

媒体导入成功时，宿主在创建 `Media` 后计算并保存该值到 `Media.file_hash`。当前只保存，不读取、
查询或按 hash 去重；已有 Media 不做回填。

### 播放

宿主唯一来源媒体路由：

~~~text
GET /media/{media_id}/play/{resource_path:path}?delivery=auto|proxy|redirect
~~~

每次请求由宿主验签、读取 Media、校验 bundle 声明的播放方式后原样返回 `handle_playback` 的 Response。宿主
不解释 `resource_path` 的 provider 业务语义、Range、content type、302 或 HLS；只接受空串或
相对安全路径：不得以 `/` 开头，不得含反斜杠、空段、`.`、`..` 或 NUL 字符。宿主按该语法
验签后原样传给 provider，不做路径拼接或业务解析。`delivery` 默认 `auto`：宿主优先选择 bundle
声明支持的 `redirect`，否则选择 `proxy`，并且只把解析后的方式传给 provider。自动选择 302 后若
provider 返回不支持或可重试错误，宿主只回退一次代理。`delivery` 不属于签名载荷；它仅选择同一
媒体授权下的传输方式。HLS 子资源必须通过 `context.url_for` 继承解析后的 delivery。

初始请求的 `resource_path` 为空。HLS playlist 用 `context.url_for` 生成同一 media 的分片
URL；每个分片再次走同一网关。一次请求只对应一个 Media。

### 缩略图

`generate_thumbnails` 一次处理一个 Media 的全部采样目标；宿主不传 offset，也不循环调用。
插件只写 workspace，返回所有成功产物：offset 非负且不重复，文件必须是非空 WebP；宿主
完成后清理 workspace。

宿主验证后一次性入库。`expected_count > 0` 时至少接受
`max(1, int(expected_count * 0.85))` 个有效产物；为零时至少一个。未达标不入库，按现有重试
状态处理。

### 单媒体剪辑

宿主先从同一 Media 的两个缩略图得到区间，校验 `0 <= start < end`、时长上限和去重。
`create_clip` 只处理一个 Media，并在 workspace 写一个非空 MP4；插件可自行用本地读取、
Range/remux 或厂商 API，不能暴露路径、直链、Cookie 或 HLS 细节。

宿主验证、探测并把 MP4 固化为 `MediaClip` 资产后清理 workspace。片段后续串流、删除、合集
和来源 Media 删除后的保留语义均由宿主负责。

### 下载

`downloads` 缺失时不能创建下载器。存在时，下载器只能绑定同 bundle 的 MediaLibrary；
`completed_source_ref` 只允许该 bundle 的 storage 导入。`list_tasks` 返回该 client 的完整、
权威、未分页快照。宿主只保存 `remote_id`、通用 state/progress；原始状态、速度、ETA、hash、
tag 和保存路径不进入宿主模型。

`completed` 状态必须带 completed ref，其他状态必须为 `None`。`delete_files` 是显式破坏性
选择；远端任务已不存在可视为幂等成功。

### 错误与加载

`safe_message` 可以面向用户和日志；Cookie、token、签名 URL、路径、上游原始响应和请求头不得
进入它。`unavailable` 由 `retryable` 决定是否重试。任意插件错误都不得触发另一个 bundle 或
local 回退。

加载器必须隔离 key 冲突、多个 `media.provider`、缺少必需方法和注册期 I/O 的插件。

## 5. 宿主模型与契约测试

目标字段：

| 模型 | 保留 |
| --- | --- |
| MediaLibrary | provider_key、provider_config、account_key |
| Media | library、storage_ref、file_hash 和通用媒体事实 |
| MediaClip | 宿主独立 MP4 资产和来源/区间事实 |
| DownloadClient | library、provider_config |
| DownloadTask | client、remote_id、state、progress |

`Media.path`、`Media.backend_locator`、`DownloadClient.kind`、provider 专属列和厂商条件索引
不保留为长期兼容层。

每个 bundle 的最小契约测试应覆盖：加载隔离、secret 脱敏、opaque ref 往返、导入 finalize/
abort 重试、Range/302/单媒体 HLS、整批缩略图、单媒体剪辑、下载完成 ref 的同 bundle 导入，
以及 bundle 缺失不回退。
