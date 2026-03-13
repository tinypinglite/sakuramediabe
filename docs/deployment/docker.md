# SakuraMediaBE Docker 使用教程

SakuraMediaBE 是 SakuraMedia 的服务端，提供媒体库、影片元数据、下载任务、缩略图和以图搜图等能力。

这份文档覆盖一条最直接的使用路径：使用仓库内的 `Dockerfile` 和 `compose.yaml` 在本机部署并开始使用。

## 1. 准备条件

- 已安装 Docker 和 Docker Compose
- 准备好宿主机上的媒体目录
- 准备好宿主机上的下载目录
- 如果要使用以图搜图，准备好 JoyTag 模型文件 `model_vit_768.onnx`，可以在`release`中下载.

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

这一部分主要涉及两类目录：

- 媒体库目录：给 SakuraMedia 管理和扫描本地媒体文件使用
- 导入源/下载目录：给 SakuraMedia 导入已有文件，或给 SakuraMedia 和 qBittorrent 共同访问下载中的影片文件使用

先说明媒体库的概念：

- 媒体库不是自动扫描宿主机所有目录，而是你在 SakuraMedia App 中手动创建的目录配置
- 每个媒体库只管理一个指定目录
- 创建媒体库时填写的 `root_path` 必须是 SakuraMedia 容器内路径，不是宿主机路径
- 如果你有多块硬盘，可以把每块硬盘上的媒体目录分别挂载到 SakuraMedia 容器中，然后在 App 里创建多个媒体库，一一对应
- 首次挂载给 SakuraMedia 作为媒体库的目录，必须是空目录

媒体库目录是系统接管后的目标落库目录。当前导入实现会在媒体库目录下按影片编号和版本创建目录结构，因此不要把一批已有的杂乱媒体文件直接挂成媒体库目录，不要误以为系统会自动接管这些旧文件。已有文件应通过后面的显式导入流程进入媒体库。

例如，你有两块硬盘，并且都准备了空目录给 SakuraMedia 管理：

```yaml
services:
  sakuramedia:
    volumes:
      - /volume1/media:/media/library/volume1
      - /volume2/media:/media/library/volume2
```

那么在 App 中创建媒体库时可以这样填写：

- 上面两个宿主机目录都应事先准备为空目录
- `A片库` 的 `root_path` 填 `/media/library/volume1`
- `B片库` 的 `root_path` 填 `/media/library/volume2`

也就是说，一个挂载目录通常对应一个媒体库。

如果你已经有一批历史媒体文件，不要直接把这批已有文件所在目录当作媒体库目录。正确做法是：

- 先给 SakuraMedia 挂载一个空媒体库目录，作为系统管理目录
- 再额外挂载一个已有数据源目录，挂载到容器内任意位置都可以，例如 `/mnt/source`
- 然后手动执行导入命令，把源目录中的文件导入到目标媒体库

例如：

```yaml
services:
  sakuramedia:
    volumes:
      - /volume1/media-empty:/media/library/volume1
      - /volume-old/media:/mnt/source
```

假设你在 App 中创建好的目标媒体库 ID 是 `1`，那么可以执行：

```bash
docker exec -w /app sakuramedia python -m src.start.commands import-media --library-id 1 --source-path /mnt/source
```

说明：

- `--library-id` 是目标媒体库 ID
- `--source-path` 是导入源目录在 SakuraMedia 容器内的路径
- 导入源目录不要求挂载到固定位置，只要 SakuraMedia 容器能访问即可

当前导入实现会优先尝试硬链接；只有硬链接失败时才回退为复制。因此建议尽量让下载目录与目标媒体库目录位于同一块硬盘。如果你是从已有数据源目录导入，也尽量让源目录和目标媒体库目录位于同一块硬盘。这样通常可以直接建立硬链接，导入更快，也能减少额外 IO；跨盘时通常无法建立硬链接，系统会回退为复制，因此导入会更慢。

然后是下载目录的路径映射。

本项目支持订阅影片后自动从 PT 和 BT 发起下载，默认部署时通常会接入两个 qBittorrent 下载器。这里要注意，下载目录不是只挂载到 SakuraMedia 容器里就够了，对应的 qBittorrent 容器也必须能访问同一份宿主机目录。

后续你在 App 中添加下载器时，会填写两个路径字段：

- `local_root_path`：SakuraMedia 容器内部看到的路径
- `client_save_path`：qBittorrent 容器内部看到的路径

系统在提交种子到 qBittorrent 时，会把保存目录直接设置为 `client_save_path`。后续同步下载任务、识别实际文件以及导入媒体库时，SakuraMedia 会使用 `local_root_path` 去访问同一份文件。为了提高导入速度，建议下载目录和对应目标媒体库目录位于同一块硬盘。

例如，你准备给两个 qBittorrent 下载器分别使用两个宿主机目录：

- 下载器 1 使用宿主机 `/downloads/qb1`
- 下载器 2 使用宿主机 `/downloads/qb2`

那么 SakuraMedia 容器里至少要能看到这两个目录：

```yaml
services:
  sakuramedia:
    volumes:
      - /downloads/qb1:/downloads/qb1
      - /downloads/qb2:/downloads/qb2
```

如果：

- `qb1` 容器里把宿主机 `/downloads/qb1` 挂载成 `/data/downloads`
- `qb2` 容器里把宿主机 `/downloads/qb2` 挂载成 `/data/downloads`

那么在 App 中添加下载器时应填写：

- 下载器 1
  - `local_root_path` 填 `/downloads/qb1`
  - `client_save_path` 填 `/data/downloads`
- 下载器 2
  - `local_root_path` 填 `/downloads/qb2`
  - `client_save_path` 填 `/data/downloads`

这里的关键点是：`local_root_path` 和 `client_save_path` 指向的是同一份宿主机文件，只是 SakuraMedia 容器和 qBittorrent 容器看到的路径不同。如果路径映射填错，种子虽然可能可以正常提交，但后续同步状态和导入文件时会找不到实际文件。

填写时建议始终按下面的原则检查：

- 创建媒体库时，`root_path` 填 SakuraMedia 容器内路径
- 首次挂载为媒体库的目录应为空目录
- 已有历史媒体文件应作为单独的导入源目录挂载，不要直接当媒体库目录
- 创建下载器时，`local_root_path` 填 SakuraMedia 容器内路径
- 创建下载器时，`client_save_path` 填 qBittorrent 容器内路径
- 所有路径都应填写绝对路径
- 为了尽量走硬链接，提高导入速度，媒体库目录和下载目录最好位于同一块硬盘

这一节只说明路径映射原则，不展开 qBittorrent 容器本身的完整部署方式。

## 3. 修改配置文件

编辑 `docker-data/config/config.toml`，这是启动容器前的必需步骤。至少检查这些配置项：

- `auth.username`
- `auth.password`
- `auth.secret_key`
- `metadata.proxy`

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

查看日志：

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

## 7. JoyTag 与 Intel GPU

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
docker compose logs -f sakuramedia
```

或者直接查看持久化日志目录中的文件。
