# Docker 部署后的常用命令

本文档整理容器启动后的常用初始化、导入和单次任务触发命令。

默认示例基于仓库提供的 `compose.yaml`，容器名为 `sakuramedia`。如果你修改了容器名，请把下面命令中的 `sakuramedia` 替换成你自己的容器名。

## 查看容器状态与日志

查看容器状态：

```bash
docker compose ps
```

查看 supervisor 启动日志：

```bash
docker compose logs -f sakuramedia
```

查看 API 持久化日志：

```bash
tail -f ./docker-data/logs/api.log
```
## 媒体库与导入

创建媒体库：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands add-media-library --name <name> --root-path <container_abs_path>
```

说明：

- `--root-path` 必须填写 SakuraMedia 容器内的绝对路径
- 首次作为媒体库使用的目录应为空目录

导入已有媒体到指定媒体库：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands import-media --library-id <id> --source-path <container_abs_path>
```

说明：

- `--library-id` 是目标媒体库 ID
- `--source-path` 是导入源目录在 SakuraMedia 容器内的绝对路径

## 单次执行任务

单次执行订阅演员影片同步：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands aps sync-subscribed-actor-movies
```

单次执行影片热度重算：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands aps update-movie-heat
```

单次执行合集影片同步：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands aps sync-movie-collections
```

单次执行媒体缩略图生成：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands aps generate-media-thumbnails
```

单次执行以图搜图索引：

```bash
docker exec --user app -w /app sakuramedia python -m src.start.commands aps index-image-search-thumbnails
```
