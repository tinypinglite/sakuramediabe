# SakuraMediaBE

> 警告
>
> 本项目基本属于由 vibing code 快速堆叠出来的实验性产物，整体实现没有经过完善测试，也没有针对真实生产环境做充分验证。它可能存在数据损坏、配置误用、任务异常、误删媒体文件或其他资源的风险。请务必在理解风险的前提下谨慎使用，优先在隔离环境、测试数据或完整备份条件下运行，不建议直接用于重要数据或不可恢复的媒体库。

SakuraMediaBE 是 SakuraMedia 的服务端项目，负责提供媒体库管理、影片元数据、下载任务、视频缩略图生成、精彩时刻标记、以图搜图、播放访问等后端能力。 前端项目仓库在 [text](https://github.com/tinypinglite/sakuramedia)

项目当前基于 Python 3.10、FastAPI、Peewee、Pydantic 2 和 APScheduler 构建，代码结构按 `api -> service -> model` 分层组织，面向单账号场景运行。

## 重要提示

本项目仅用于网络爬虫技术交流与学术研究，不提供任何多媒体资源下载，不存储任何非法内容。用户使用本工具产生的一切后果由使用者自行承担，作者不参与任何资源分发。

请在遵守当地法律法规、版权要求的前提下使用本项目。

## 核心能力

- 媒体库配置、导入、播放资源访问与播放进度维护
- VR 类多文件资源自动合并导入，减少多场景文件的播放割裂
- 基于 `PyAV` 的媒体缩略图生成，按时间点沉淀可回看的画面索引
- 基于缩略图时间点的精彩时刻标记与快速跳播
- 影片、演员、标签、合集等目录数据管理
- 播放列表与收藏整理
- 基于 JoyTag OpenVINO 和 LanceDB 的全库缩略图以图搜图
- 基于 Jackett 与 qBittorrent 的下载任务管理与同步
- APScheduler 定时任务编排

## 亮点功能

### VR 多文件资源自动合并

针对常见的 VR 影片多文件种子场景，系统会在导入阶段自动识别同一影片下的多个视频分片，并按顺序合并为单个媒体资源入库，减少手动整理成本，也避免播放时在多个场景文件之间来回切换。该合并策略只针对识别为 VR 的资源组生效，非 VR 多文件资源默认仍按独立媒体处理。

### 视频时刻缩略图与精彩时刻

系统会通过定时任务调用 `PyAV`，按 10 秒间隔为媒体文件生成缩略图，并保存对应的时间偏移。缩略图文件会直接按秒数命名为如 `120.webp` 这样的形式。前端可以基于这些缩略图把某个位置标记为精彩时刻，之后就能从对应时间点快速进入播放，适合做回看、挑选片段和重点定位。

### 全库缩略图以图搜图

所有已生成的媒体缩略图都可以进入以图搜图索引链路。服务端会使用 JoyTag OpenVINO 生成向量，并写入 LanceDB，支持用户拿一张截图或相似画面去全库检索，直接定位到对应影片、媒体文件和具体时间点。

## 部署软/硬件要求

1. 一台 基于 Linux 系统的NAS，且 CPU 为Intel 系列，核显非必须

   * 影片资源缩略图生成依赖 `PyAV` 和 FFmpeg 运行时库，不再依赖额外的 `mtn` 或 ImageMagick 可执行文件

   * 用于支持以图搜图的模型`joytag`在推理时使用时采用了`openvino`，并且支持核显加速，因此最好是 intel 带核显的 CPU。AMD 系列可能不支持，未经测试

2. `jackett`，用于处理订阅影片，会从`indexer`中自动查找资源并下载

3. `qbittorrent`，用于下载资源，本项目假设你有`pt` `bt`站，你可以在 APP 中填写两个 qb 下载器，具体看部署文档

## 文档导航

- [Docker 部署教程](./docs/deployment/docker.md)
- [Docker 部署后的常用命令](./docs/deployment/commands.md)
- [常见问题](./docs/faq.md)
- [API 设计文档总览](./docs/README.md)
- [播放域 API：媒体、缩略图与精彩时间点](./docs/playback/media.md)
- [发现域 API：以图搜图](./docs/discovery/image-search.md)
- [项目约定](./docs/conventions.md)

## 开发命令

```bash
poetry install
poetry run python -m src.start.commands initdb
poetry run python src/start/startapi.py
poetry run pytest
```

部署后的初始化、导入和单次任务触发命令请参考 [Docker 部署后的常用命令](./docs/deployment/commands.md)。
