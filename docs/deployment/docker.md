# SakuraMediaBE Docker 使用教程

SakuraMediaBE 是 SakuraMedia 的服务端，提供媒体库、影片元数据、下载任务、缩略图和以图搜图等能力。

这份文档覆盖一条最直接的使用路径：使用仓库内的 `Dockerfile` 和 `compose.yaml` 在本机部署并开始使用。

## 1. 准备条件

- 已安装 Docker 和 Docker Compose
- 准备好宿主机上的媒体目录
- 准备好宿主机上的下载目录
- 如果要使用以图搜图，准备好 JoyTag 模型文件 `model_vit_768.onnx`，可以在`release`中下载.
- 镜像运行时需要 FFmpeg 动态库，仓库内 `Dockerfile` 已通过 `apt install ffmpeg` 处理，缩略图生成不再依赖 `mtn` 或 ImageMagick。

建议先在仓库根目录执行：

```bash
mkdir -p docker-data/config docker-data/db docker-data/cache/assets docker-data/cache/gfriends docker-data/image-search-index docker-data/logs docker-data/joytag
cp config.example.toml docker-data/config/config.toml
```

说明：

- 容器会把配置、数据库、缓存、索引和日志拆开挂载
- `config.toml` 是必需项
- 建议从轻量版 `config.example.toml` 复制后按需补配置

## 2. 修改 Compose 挂载

仓库已经提供了 `compose.example.yaml`，把它重命名为 `compose.yaml`。

### 2.1 元数据类挂载目录设置

各路径用途说明：

- `/data/db`
  如果用的是默认的 `sqlite`，这里会存放数据库文件，建议放在 SSD 硬盘
- `/data/config`
  这里存放 `config.toml` 配置文件
- `/data/cache/assets`
  这里存放影片的各类图片和字幕信息
- `/data/cache/gfriends`
  这里存放 `gfriends` 的缓存文件
- `/data/indexes`
  这里存放 LanceDB 数据文件
- `/data/logs`
  日志保存位置
- `/data/lib/joytag`
  这里存放 JoyTag 模型文件

```yaml
services:
  sakuramedia:
    volumes:
      - ./docker-data/config:/data/config
      - ./docker-data/db:/data/db
      - ./docker-data/cache/assets:/data/cache/assets
      - ./docker-data/cache/gfriends:/data/cache/gfriends
      - ./docker-data/image-search-index:/data/indexes
      - ./docker-data/logs:/data/logs
      - ./docker-data/joytag:/data/lib/joytag
```

### 2.2 媒体文件挂载

这里以单硬盘举例直接把整块硬盘根目录(或者是已有媒体路径、qb下载路径的共同父目录，这一步是为了确保硬链接能成功)挂载到 SakuraMedia 容器，而不是分别挂很多个子目录。例如：

```yaml
services:
  sakuramedia:
    volumes:
      - /mnt/volume1:/volume1
```

这一部分主要涉及三类路径：

- 媒体库目录：给 SakuraMedia 管理和扫描本地媒体文件使用
- 下载目录：给 SakuraMedia 和 qBittorrent 共同访问下载中的影片文件使用
- 已有媒体目录：给 SakuraMedia 导入历史文件使用

媒体库不是自动扫描宿主机所有目录，而是你在 SakuraMedia App 中手动创建的目录配置。每个媒体库只管理一个指定目录，创建媒体库时填写的 `root_path` 必须是 SakuraMedia 容器内绝对路径，不是宿主机路径。首次作为媒体库使用的目录应是一个新的专用目录，不要直接把历史媒体目录当成媒体库根目录。

下面用一个完整的单硬盘示例说明。

假设：

| 项目 | 宿主机路径 | 容器内路径 |
| --- | --- | --- |
| 硬盘根目录 | `/mnt/volume1` | SakuraMedia 容器内 `/volume1` |
| qB 下载目录 | `/mnt/volume1/qb-downloads` | 挂载到qBittorrent 容器内对应的 `/downloads` |
| qB 下载目录在 SakuraMedia 中的对应路径 | `/mnt/volume1/qb-downloads` | SakuraMedia 容器内 `/volume1/qb-downloads` |
| 已有媒体目录 | `/mnt/volume1/old-media` | SakuraMedia 容器内 `/volume1/old-media` |
| SakuraMedia 管理的媒体库目录 | `/mnt/volume1/sakuramedia` | SakuraMedia 容器内 `/volume1/sakuramedia` |

对应的关键挂载示例可以这样写：

```yaml
services:
  sakuramedia:
    volumes:
      - /mnt/volume1:/volume1
```

这一套设计里，SakuraMedia 统一通过 `/volume1/...` 访问这块硬盘上的所有目录；qBittorrent 只需要把自己的下载目录挂载为 `/downloads`。下面按实际使用顺序说明。

#### 1. 在 App 中创建媒体库

建议在宿主机上先准备一个新的专用目录 `/mnt/volume1/sakuramedia`，然后在 App 中创建媒体库时填写：

| App 字段 | 应填写的值 | 说明 |
| --- | --- | --- |
| 根路径   | `/volume1/sakuramedia` | SakuraMedia 容器内的绝对路径 |

说明：

- 根路径必须填 SakuraMedia 容器内路径，不是宿主机路径
- 不要直接填 `/volume1`，因为根目录下还会包含下载目录、旧媒体目录和其他文件
- 不要把 `/volume1/old-media` 直接当成媒体库目录，那是历史媒体导入源，不是系统接管后的目标目录
- `/volume1/sakuramedia` 应是给 SakuraMedia 新建的专用目录

#### 2. 在 App 中添加 qBittorrent 下载器

下载目录不是只挂载给 SakuraMedia 就够了，对应的 qBittorrent 容器也必须能访问同一份宿主机目录。后续你在 App 中添加下载器时，会填写两个路径字段：

- `client_save_path`：qBittorrent 容器内部看到的路径
- `local_root_path`：SakuraMedia 容器内部看到的路径

在这个示例中应填写：

| App 字段 | 应填写的值 | 路径属于谁 |
| --- | --- | --- |
| qBittorrent 保存路径 | `/downloads` | qBittorrent 容器 |
| 本地访问路径 | `/volume1/qb-downloads` | SakuraMedia 容器 |

系统在提交种子到 qBittorrent 时，会把保存目录直接设置为 `qBittorrent 保存路径`。后续同步下载任务、识别实际文件以及导入媒体库时，SakuraMedia 会使用 `本地访问路径`去访问同一份文件。这里的关键点是：`qBittorrent 保存路径` 和 `本地访问路径` 指向的是同一份宿主机目录 `/mnt/volume1/qb-downloads`，只是两个容器看到的路径不同。如果路径映射填错，种子虽然可能可以正常提交，但后续同步状态和导入文件时会找不到实际文件。

#### 3. 导入已有媒体

如果你已经有一批历史媒体位于宿主机 `/mnt/volume1/old-media`，不要直接把它当成媒体库目录，而是作为单独的导入源目录使用。先在 App 中创建好目标媒体库，然后参考 [Docker 部署后的常用命令](./commands.md) 执行导入。

假设你创建好的目标媒体库 ID 是 `1`，可以执行：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands import-media --library-id 1 --source-path /volume1/old-media
```

说明：

- `--library-id` 是目标媒体库 ID
- `--source-path` 必须填写 SakuraMedia 容器内的绝对路径
- 这个例子里应填 `/volume1/old-media`，不是宿主机路径 `/mnt/volume1/old-media`
- 导入后的目标媒体库则是前面创建好的 `/volume1/sakuramedia`

当前导入实现会优先尝试硬链接；只有硬链接失败时才回退为复制。因此建议尽量让下载目录、导入源目录和目标媒体库目录位于同一块硬盘。这样通常可以直接建立硬链接，导入更快，也能减少额外 IO；跨盘时通常无法建立硬链接，系统会回退为复制，因此导入会更慢。

#### 为什么推荐这样挂载

- 单硬盘用户只需要记住一套 SakuraMedia 容器内路径前缀 `/volume1`
- SakuraMedia 可以同时访问媒体库、下载目录、已有媒体目录，不需要为每种用途单独设计一套挂载路径
- qBittorrent 和 SakuraMedia 访问的是同一份宿主机文件，只是容器内路径不同
- 下载目录、导入源和媒体库都在同一块硬盘时，更容易走硬链接，导入更快
- 后续如果要新增目录，也只是在 `/volume1` 下继续组织

填写时建议始终按下面的原则检查：

- 创建媒体库时，`root_path` 填 SakuraMedia 容器内绝对路径
- 首次作为媒体库使用的目录应是专用目录，不要直接用历史杂乱目录
- 创建下载器时，`local_root_path` 填 SakuraMedia 容器内路径
- 创建下载器时，`client_save_path` 填 qBittorrent 容器内路径
- `local_root_path` 和 `client_save_path` 必须指向同一份宿主机目录
- 所有路径都应填写绝对路径

如果你有多块硬盘，可以按相同模式分别挂载为 `/volume1`、`/volume2`，并为每块盘创建独立媒体库。

这一节只说明路径映射原则，不展开 qBittorrent 容器本身的完整部署方式。

## 3. 修改配置文件

编辑 `docker-data/config/config.toml`，这是启动容器前的必需步骤。至少检查这些配置项：

- `auth.username`
- `auth.password`
- `auth.secret_key`
- `metadata.proxy`
- `indexer_settings.type`
- `indexer_settings.api_key`

默认 SQLite 配置已经指向容器内数据库路径：

```toml
[database]
engine = "sqlite"
path = "/data/db/sakuramedia.db"
```

如果你使用默认的 Docker 挂载方式，通常不需要改这个路径。

如果你希望打开 Swagger 文档，可改成：

```toml
enable_docs = true
```

文档页面启动后可访问 `/docs`。



## 4. 启动服务

在仓库根目录执行：

```bash
docker compose up --build -d
```

查看容器状态：

```bash
docker compose ps
```

查看 supervisor 启动日志：

```bash
docker compose logs -f sakuramedia
```

容器启动后会由 `supervisord` 拉起两个进程：

- API 服务，监听容器内 `8000`
- APScheduler 任务进程，负责定时任务

同时应用启动时会自动执行 `initdb()`，创建表、默认账号和系统播放列表。

## 5. 访问地址

默认端口映射来自 `compose.yaml`：

- API: `http://localhost:38000`
- Supervisor: `http://localhost:39001`

如果启用了 `enable_docs = true`，还可以访问：

- Swagger: `http://localhost:38000/docs`

## 6. 首次登录

默认账号来自你的 `config.toml`：

```toml
[auth]
username = "account"
password = "account"
```

## 7. 首次完成下载配置

完成登录后，建议按下面顺序配置下载链路：

1. 先在 App 中创建媒体库，确认目标 `root_path`、下载目录挂载和硬链接策略已经准备好。
2. 再创建一个或多个 `DownloadClient`，填写正确的 `local_root_path` 与 `client_save_path`。
3. 然后到 App 或 `/indexer-settings` 配置 Jackett `indexer`。
4. 为每个 `indexer` 指定对应的 `download_client`。
5. 完成后再去搜索候选资源；此时系统会按 `candidate.indexer_name` 自动解析目标下载器。

如果先配置 `indexer`，但还没有创建 `DownloadClient`，后续下载提交流程无法完成自动路由。

## 8. JoyTag 与 Intel GPU

如果你要启用以图搜图：

- 确保容器内有 `/data/lib/joytag/model_vit_768.onnx`
- 默认配置下 `image_search.joytag_model_dir = "/data/lib/joytag"`

如果你希望 OpenVINO 尝试使用 Intel 核显，可以在 `compose.yaml` 里追加：

```yaml
devices:
  - /dev/dri:/dev/dri
```

不挂载 `/dev/dri` 也可以启动，只是相关推理会回退到 CPU。

## 8. 日志与数据目录

默认建议把运行数据拆成下面几类目录：

- `./docker-data/config/config.toml`：运行配置
- `./docker-data/db/`：数据库文件
- `./docker-data/cache/assets/`：导入图片缓存
- `./docker-data/cache/gfriends/`：GFriends 文件树缓存
- `./docker-data/image-search-index/`：图像搜索索引
- `./docker-data/logs/`：持久化日志

常见日志查看方式：

```bash
tail -f ./docker-data/logs/api.stdout.log
```

常见日志文件包括：

- `./docker-data/logs/supervisord.log`
- `./docker-data/logs/api.stdout.log`
- `./docker-data/logs/api.stderr.log`
- `./docker-data/logs/aps.stdout.log`
- `./docker-data/logs/aps.stderr.log`

如果需要查看 supervisor 本身的启动日志，仍然可以使用：

```bash
docker compose logs -f sakuramedia
```
