# 前端资源任务对接说明

本文面向 Web / Flutter 等通用前端客户端，说明活动中心新增的“资源任务”能力。重点是告诉前端：

- 后端现在提供了什么能力
- 前端可以基于这些能力直接完成什么页面
- 当前接入时有哪些边界和注意事项

本文只覆盖新增的资源任务能力，不展开通知中心、活动中心 bootstrap、批次任务列表等已有能力细节。

## 能力总览

后端当前已经提供以下资源任务能力：

- 返回全部已注册资源任务定义，供前端渲染任务入口、Tab 和状态 badge
- 按任务分页查询资源执行记录
- 按状态筛选 `pending` / `running` / `succeeded` / `failed`
- 按关键词搜索影片或媒体
- 返回资源摘要，减少前端二次查详情的需要

对应接口：

- `GET /system/resource-task-states/definitions`
- `GET /system/resource-task-states`

## 当前已注册资源任务

### `movie_desc_sync`

- 资源类型：`movie`
- 展示名称：`影片描述回填`
- 适合展示的资源摘要字段：`movie_number`、`title`

### `movie_interaction_sync`

- 资源类型：`movie`
- 展示名称：`影片互动数同步`
- 适合展示的资源摘要字段：`movie_number`、`title`

### `movie_desc_translation`

- 资源类型：`movie`
- 展示名称：`影片简介翻译`
- 适合展示的资源摘要字段：`movie_number`、`title`

### `movie_title_translation`

- 资源类型：`movie`
- 展示名称：`影片标题翻译`
- 适合展示的资源摘要字段：`movie_number`、`title`

### `media_thumbnail_generation`

- 资源类型：`media`
- 展示名称：`媒体缩略图生成`
- 适合展示的资源摘要字段：`movie_number`、`title`、`path`、`valid`

## 资源任务数据模型

前端在资源任务页面里主要消费两类资源：任务定义和任务记录。

### 任务定义 `ResourceTaskDefinitionResource`

- `task_key`
  - 资源任务稳定标识，适合作为前端路由参数、Tab key、查询条件
- `resource_type`
  - 资源类型，当前支持 `movie`、`media`
- `display_name`
  - 前端直接展示的任务名称
- `default_sort`
  - 当前任务推荐默认排序
- `state_counts`
  - 当前任务下各状态记录数
  - 包含 `pending`、`running`、`succeeded`、`failed`

### 任务记录 `ResourceTaskRecordResource`

- `task_key`
- `resource_type`
- `resource_id`
- `state`
  - `pending`
  - `running`
  - `succeeded`
  - `failed`
- `attempt_count`
  - 当前资源任务记录累计尝试次数
- `last_attempted_at`
  - 最近一次开始执行时间
- `last_succeeded_at`
  - 最近一次成功完成时间
- `last_error`
  - 最近一次失败错误信息
- `last_error_at`
  - 最近一次失败时间
- `last_task_run_id`
  - 最近一次关联的批次任务 ID，可用于联动跳转批次任务详情
- `last_trigger_type`
  - 最近一次触发来源，例如 `scheduled`、`manual`
- `created_at`
- `updated_at`
- `resource`
  - 资源摘要
  - `movie` 任务返回：
    - `resource_id`
    - `movie_number`
    - `title`
  - `media` 任务额外返回：
    - `path`
    - `valid`

## 接口说明

### `GET /system/resource-task-states/definitions`

用途：

- 加载资源任务入口页
- 渲染任务 Tab
- 渲染每个任务的状态 badge

典型返回字段：

- `task_key`
- `display_name`
- `resource_type`
- `default_sort`
- `state_counts`

前端使用建议：

- 用 `task_key` 作为任务切换主键
- 用 `state_counts.failed` 渲染失败数量提醒
- 用 `state_counts.running` 渲染运行中提示

### `GET /system/resource-task-states`

用途：

- 查询某个资源任务下的记录列表
- 支持状态筛选、搜索、排序和分页

查询参数：

- `task_key`
  - 必填
- `page`
- `page_size`
- `state`
  - 可选值：`pending`、`running`、`succeeded`、`failed`
- `search`
  - `movie` 任务按 `movie_number`、`title`、`javdb_id` 搜索
  - `media` 任务按 `movie_number`、`title`、`path` 搜索
- `sort`

允许排序：

- `last_attempted_at:desc`
- `last_attempted_at:asc`
- `last_error_at:desc`
- `attempt_count:desc`
- `updated_at:desc`
- `updated_at:asc`

前端使用建议：

- 首次进入某个任务页时，不传 `sort`，直接使用后端默认排序
- 失败页优先带 `state=failed`
- 搜索框直接绑定 `search`
- 列表项优先展示 `resource.movie_number`、`resource.title`
- `media` 任务额外展示 `resource.path` 和 `resource.valid`

### 1. 资源任务入口页

后端依赖：

- `GET /system/resource-task-states/definitions`

页面能力：

- 展示全部资源任务
- 按任务类型切换 Tab
- 在任务入口上展示 `failed` / `running` badge

推荐展示字段：

- `display_name`
- `resource_type`
- `state_counts.failed`
- `state_counts.running`

### 2. 单任务资源记录列表页

后端依赖：

- `GET /system/resource-task-states`

页面能力：

- 查看某个任务下的影片或媒体记录
- 查看成功、失败、运行中、待处理记录
- 支持分页、搜索、排序、状态筛选

推荐列表列：

- 资源摘要
  - `movie_number`
  - `title`
  - `path`（仅 media）
- `state`
- `attempt_count`
- `last_attempted_at`
- `last_succeeded_at`
- `last_error`

### 3. 失败记录查看页

后端依赖：

- `GET /system/resource-task-states?state=failed`

页面能力：

- 按任务查看失败资源
- 展示失败原因和最近失败时间

### 4. 资源任务详情抽屉 / 详情卡片

后端依赖：

- 当前列表结果即可满足，不要求额外接口

页面能力：

- 展示一条任务记录的完整状态信息
- 展示最近一次成功 / 失败 / 尝试时间
- 展示错误信息
- 展示触发来源

推荐字段：

- `state`
- `attempt_count`
- `last_attempted_at`
- `last_succeeded_at`
- `last_error`
- `last_error_at`
- `last_trigger_type`

## 可选增强页面能力

### 1. 从资源任务记录跳转到批次任务详情

后端依赖：

- `last_task_run_id`
- 现有批次任务接口 `GET /system/task-runs`

页面能力：

- 从某条资源记录回看它最近一次属于哪个批次任务
- 与已有任务中心页面联动

说明：

- 当前资源任务接口不直接返回批次任务详情，只提供 `last_task_run_id`

### `resource-task-states` 只返回已落库记录

- 该接口只返回已经 materialize 到 `resource_task_state` 的记录
- 它不代表系统里全部理论上的 `pending` 资源
- 因此前端不要把 definitions 里的计数理解成“系统总资源数”

### `media_thumbnail_generation` 的资源摘要更丰富

- `movie` 任务一般只展示 `movie_number`、`title`
- `media_thumbnail_generation` 还会返回 `path`、`valid`
- 前端可以直接把它做成更偏运维/资源管理的列表

## 推荐接入顺序

建议前端按以下顺序接入：

1. 先接 `GET /system/resource-task-states/definitions`
   - 完成任务入口页 / Tab
2. 再接 `GET /system/resource-task-states`
   - 完成单任务记录列表页
这样即可完成资源任务只读页面。
