# 子 Issue G：FastAPI 接入、应用编排与恢复

Issue：[#36](https://github.com/vansye/EasyRAG/issues/36)，父 Issue：#1。当前设计：[FastAPI 模块迁移](架构设计.md)。位置：`app/application`、FastAPI 入口与维护 CLI。

## 职责

G 将独立的 A/B/C/F 公开接口组合成现有业务。它维护 HTTP 契约、模块装配、运行许可、单执行者、生命周期和跨模块恢复，不维护 SQL、切片、提示词、配置密钥或 SDK。

## 数据与接口原型

```python
GateState = RECOVERY_REQUIRED | READY | QUERYING | MUTATING | RECOVERING
DocumentState = PENDING | INDEXING | INDEXED | FAILED  # 状态由 A 维护
ErrorBody = {error: str, state?: GateState}
ReadinessResult = {state: GateState, recovered: int}

POST /api/documents -> 201 DocumentCreated
GET /api/documents -> DocumentPage
GET /api/documents/{id} -> DocumentDetail
GET /api/documents/{id}/chunks -> {items}
PUT /api/documents/{id} -> {id, index_status, reindexed}
DELETE /api/documents/{id} -> 204
POST /api/documents/{id}/reindex -> 202
POST /api/questions -> {answer, status, sources, trace}
GET|PUT|DELETE /api/model-config -> PublicConfig
GET /api/runtime -> {state, rag_available, llm, embedding}
POST /api/admin/ready -> ReadinessResult
GET /health -> {status, service, db, retrieval: {status, chroma, embedding, tokenizer}}
```

## 用例与依赖

- 收录索引：A 入库 PENDING 后返回；工作线程调用 B.split → A.begin_indexing → B.replace → A.mark_indexed。
- 更新/重建：先持 MUTATION 许可，再 B 删除旧索引 → A 变更 → 同一许可交给后台直至终态；内容哈希不变不清索引。
- 删除：B 确认删除后才 A 软删；结果不明保持 RECOVERY_REQUIRED。
- 提问：持 QUERY 许可，F 会话和 B 检索适配到 C 自己的端口；结果通过 A 补出处，rank 来自最终 trace。
- 恢复：G 获取 A 快照和 B.inspect，核对 ID/归属/正文/元信息。ready 先确认 READY 再扫描 PENDING；恢复期间上传不丢失。
- 维护 CLI：显式旧库接管、停服全量重建；不公开无门禁的 reset/embed 接口。

只从 `app.modules.<name>.public` 导入业务能力。四个业务模块不反向依赖 G。每个用例独立组织；适配器仅转换数据和错误。

## PR 与验收

- [x] 模块骨架与依赖检查：阻止兄弟模块引用、反向依赖和读取私有实现（PR #39）。
- [x] 收录索引：后台成功/失败、BUSY 保留 PENDING、短事务外模型调用（PR #45）。
- [x] 更新删除：端到端变更许可、哈希未变、索引/数据库失败、提交失败（PR #50）。
- [x] 问答引用：三态结果、引用排序、模型切换、错误形状、查询并发许可（PR #52）。
- [x] 恢复维护：重启遗留 INDEXING、缺失/多余/旧向量、ready/上传竞态、CLI 中断（PR #51、#54、#55）。
- [x] 运行与退出：单进程、单执行者；线程实际结束才释放许可；优雅停机先等待工作再关闭资源（PR #53）。
- [x] CI + 真实 MySQL 集成 + 浏览器回归；独立备份和切换验证后退出旧 Java（PR #56）。

## 数据切换

先在隔离 MySQL/Chroma/配置上验证，再停止原 Java 和 Python 进程及用户写入，成套备份资料库、索引和配置。A 校验旧 schema 并登记 Alembic 基线，新 FastAPI 占用 8080，验证原资料与问答后才开放写入。失败同步恢复旧版本与同一时间点的数据。#34 保持独立，不自动合并。

2026-09-15 已执行：备份恢复核验通过，旧库接管未修改原行；首次 ready 发现 517 条缺失向量并阻止问答，停服重建后复用全部 623 个切片 ID，最终 READY。13 份原资料保留，真实网页完成配置、资料变更、引用和拒答验证，临时资料清理完成。过程与回退入口见 [切换记录](../README.md)，交付见 [PR #56](https://github.com/vansye/EasyRAG/pull/56)。
