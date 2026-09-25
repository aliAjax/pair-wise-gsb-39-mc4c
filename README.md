# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行和无障碍变化发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行和无障碍限制。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入两条示例线路、六个站点和一个 23:50 发车的跨日班次。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更，已发布快照继续保留。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。

## 接驳计划台

打开 <http://127.0.0.1:8010/shuttle.html>。规则、保存和页面分开：纯规则在 `shuttle_rules.py`，存储在 `shuttle_store.py`，页面在 `static/shuttle.html`。

- 草稿版本登记站点人数需求（人数、可选服务时段、备注），并安排接驳班次（班次号、车组、服务时段、单班载客量、停靠站）。
- 覆盖按停运/跳站变更形成的站点缺口计算；同一车组时段重叠的班次、站点没人覆盖或单班容量不够的需求都留在待确认并写明缺口。覆盖与缺口只统计已确认班次。
- 版本发布时把已确认班次写进方案快照（待确认班次与缺口一并记录）；新版本复制草稿后继续编辑只影响新版本。
- 数据落在同一 SQLite 库，重开后仍可按版本查看覆盖情况。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行或无障碍变化。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时和到达时间。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。
- `GET /api/versions/{id}/shuttle`：查看该版本的接驳需求、班次、站点覆盖与缺口。
- `POST /api/versions/{id}/shuttle/demands|trips`：在草稿版本登记需求或安排班次。
- `PUT|DELETE /api/shuttle/demands/{id}`、`PUT|DELETE /api/shuttle/trips/{id}`：修改或删除（仅草稿版本）。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径、版本复制与发布隔离、审批冲突、无障碍路径、跨日时刻和坏数据整批隔离；接驳部分覆盖车组时段重叠、站点没人覆盖、单班容量缺口、发布快照只含已确认班次、草稿隔离和重开后按版本查看。
