# SakuraMediaBE

SakuraMediaBE 是 SakuraMedia 的服务端项目，负责提供媒体库管理、影片元数据、下载任务、缩略图生成、以图搜图、播放访问等后端能力。

项目当前基于 Python 3.10、FastAPI、Peewee、Pydantic 2 和 APScheduler 构建，代码结构按 `api -> service -> model` 分层组织，面向单账号场景运行。

## 重要提示

本项目仅用于网络爬虫技术交流与学术研究，不提供任何多媒体资源下载，不存储任何非法内容。用户使用本工具产生的一切后果由使用者自行承担，作者不参与任何资源分发。

请在遵守当地法律法规、版权要求的前提下使用本项目。

## 核心能力

- 媒体库配置、导入、播放资源访问与缩略图生成
- 影片、演员、标签、合集等目录数据管理
- 播放列表与收藏整理
- 基于 JoyTag OpenVINO 和 LanceDB 的以图搜图
- 基于 Jackett 与 qBittorrent 的下载任务管理与同步
- APScheduler 定时任务编排

## 文档导航

- [Docker 部署教程](./docs/deployment/docker.md)
- [Docker 部署后的常用命令](./docs/deployment/commands.md)
- [API 设计文档总览](./docs/README.md)
- [项目约定](./docs/conventions.md)

## 开发命令

```bash
poetry install
poetry run python -m src.start.commands initdb
poetry run python src/start/startapi.py
poetry run pytest
```

部署后的初始化、导入和单次任务触发命令请参考 [Docker 部署后的常用命令](./docs/deployment/commands.md)。
