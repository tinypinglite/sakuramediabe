# Provider 插件化推进记录

本阶段以 [插件化方案](./provider-plugin-protocol.md) 和 [协议 V2](./provider-plugin-protocol-contract-v1.md) 为准。

## 目标与原则

- 不保留兼容层，彻底下线宿主内置 qB、local-media 和 115 能力。
- 采用整体净删，宿主只保留 provider-neutral 的业务编排与公开协议。
- 本阶段只完成边界和宿主改造，暂不实现具体 provider。

## 已完成的删除范围

- 删除 qBittorrent 客户端适配、进度/停滞清理、内容闸门及其专属配置和调度任务。
- 删除 local-media 文件扫描、搬运、写入和本地下载耦合。
- 删除 115 存储、离线下载、HLS、二维码登录、秒传及对应导入/视频导入实现。
- 删除合并播放、合并 HLS、旧媒体流路由及 provider-specific API、schema、模型列。
- 删除旧导入作业模型、旧来源浏览/文件操作路由、旧调度注册和相关测试/文档。

## 当前宿主协议与数据流

- `MediaLibrary` 保存 `provider_key`、opaque `provider_config` 和 `account_key`。宿主仅按
  bundle 的 `ConfigField` 做未知字段拒绝、secret 掩码/保留、`read_only` 处理和 patch merge，
  不解释配置业务语义或存储类型。
- `source_ref`、`storage_ref`、导入 `receipt` 和下载完成 ref 均为 opaque JSON；宿主保存并
  原样传回所属 bundle，普通 API 不返回 `storage_ref` 或下载完成 ref。
- 播放统一经过 `GET /media/{media_id}/play/{resource_path:path}`；宿主验签、定位 bundle，
  provider 直接返回原始 HTTP Response。
- 导入链路为 `browse → scan_import_source → stage_import_file → 宿主事务 →
  finalize_import/abort_import`，receipt 在任务记录中用于恢复。
- 缩略图由 provider 一次生成一个 Media 的完整采样集；剪辑一次只处理一个 Media 并生成独立
  MP4；下载能力是 bundle 的可选组成。

详细类型、错误和调用约束见上述两份协议文档，本记录不重复协议全文。

## 主项目不再做的特判

- 不按 `provider_key` 文本分支，不回退到其它 provider，也不解析 provider ref 或 provider 路径。
- 不在宿主实现 provider 专属扫描、搬运、远端 I/O、HLS/合并播放或下载状态字段。
- 不把 provider 配置、来源/存储引用暴露为普通业务响应字段。

## 数据模型、API、调度、测试与文档

- 数据模型改为 provider 归属、opaque 引用和通用下载状态，移除内置后端列及专属索引。
- API 收敛到 provider 目录、媒体库配置、`/import-sources/browse`、`/imports` 和通用播放网关，
  并删除旧导入、文件、流和后端专属端点。
- 调度仅注册通用 TaskRun、provider 下载同步和媒体处理任务，不再注册厂商清理或本地扫描任务。
- 测试覆盖路由护栏、provider registry/协议、错误映射、receipt 恢复、下载状态和模型/启动边界。
- 领域文档已改写为 provider-neutral；协议边界集中在
  [provider-plugin-protocol.md](./provider-plugin-protocol.md) 与
  [provider-plugin-protocol-contract-v1.md](./provider-plugin-protocol-contract-v1.md)。

## 变更统计

`32ae978` 是第一阶段基线（相对 `c20dcff` 的 tracked 变更），不是最终 worktree 统计：

- Overall：`+3640 / -31396 / net -27756`
- `src/`：`+2339 / -20386 / net -18047`

当前 worktree（tracked + untracked）相对 `c20dcff`：

| 范围 | 新增 | 删除 | 净变化 |
| --- | ---: | ---: | ---: |
| Overall | 4536 | 31506 | -26970 |
| `src/` | 2557 | 20480 | -17923 |
| `tests/` | 1159 | 6440 | -5281 |
| `docs/` | 818 | 4560 | -3742 |

本轮 review 相对 `32ae978`（tracked + untracked）：Overall `+994 / -208 / net +786`；
`src/` `+312 / -188 / net +124`；`tests/` `+543 / -2 / net +541`；`docs/`
`+139 / -18 / net +121`。新增主要是护栏测试和推进记录，不改变第一阶段主项目净删方向。

## Review 修复

- 安全：收紧播放资源路径、片段产物路径、配置 `read_only` 和错误映射。
- 一致性：远端已不存在时清理宿主记录，下载快照和 provider 状态按协议收口。
- 恢复：缩略图 provider 暂不可用退避，片段空占位重生成，导入 receipt 可恢复。
- loader：隔离插件加载失败、冲突和模块残留。
- 契约：补齐 provider 错误、下载任务、播放网关和配置字段校验。
- 死代码：删除无调用状态 helper、旧 facade 和残留专属路径解析。

## 验证结果

- `uv run ruff check src tests`：通过。
- `uv run python -m compileall -q src`：通过。
- `git diff --check`：通过。
- `uv run pytest --collect-only --no-testmon -n0`：收集 400 项，无收集错误。
- 当前无 `SAKURAMEDIA_TEST_DATABASE_URL` 时全套结果：207 passed、193 fixture errors；错误均为数据库 fixture 前置条件，
  无断言失败，不宣称 PostgreSQL 测试通过。

## 未完成项

- 具体 provider 的实现与部署。
- 旧用户升级和数据库迁移。
- 合并主分支前的数据迁移验证。

## 下一步

1. 按 V1 协议实现第一个 provider bundle。
2. 为该 bundle 补齐注册隔离、ConfigField、opaque ref、导入 finalize/abort、播放、整批缩略图、
   单媒体剪辑和可选下载能力的契约测试。
