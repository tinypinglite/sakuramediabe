# 统一配置 API

## 概述

新增统一配置接口 `GET /config` 与 `PATCH /config`，可一次性读取和局部修改全部 `config.toml` 配置项，不再需要为每个配置节单独造接口。

## 变更内容

- **新增 `GET /config`**：返回全部配置节的明文快照，以及每项的生效方式（`hot` / `restart_api` / `restart_scheduler`）。
- **新增 `PATCH /config`**：嵌套 dict partial，仅更新显式给出的字段；校验通过后落盘并热更新 api 进程内存；响应回显本次「已即时生效」与「需重启才生效」的字段清单。
- **配置校验下沉**：`scheduler.*_cron`（cron 可解析）与若干 URL 字段（http/https，proxy 额外允许 socks5(h)）的语义校验移入 `Settings` 子模型，对启动加载与所有配置接口统一生效。
- 与既有 `/indexer-settings`（DB 表联动）、`/movie-desc-translation-settings/test`（翻译连通性探测）并存，后者继续承载非纯字段能力；`media.others_number_features` 走 `PATCH /config`（规范化在 `Media` 模型层）。

## 生效方式说明

系统为 api / aps 双进程部署，配置写只作用于 api 进程内存，故：

- `scheduler.*_cron` 等由调度进程消费的配置，修改后**需重启 aps 进程**才对定时任务生效。
- `database.url`、`logging.level` 等在进程启动期读取一次，修改后**需重启 api 进程**。
- 其余大多为 `hot`，api 进程内即时生效。

## 注意事项

- 敏感字段（密钥、密码、`database.url`）随 `GET /config` 明文返回，接口须经鉴权且走 HTTPS。
- 热改 `auth.secret_key` 会使已签发的 access token 立即失效。
- 升级后若现有 `config.toml` 中存在非法 cron 或 URL，进程启动会因新增校验而失败——请确保存量配置合法。
