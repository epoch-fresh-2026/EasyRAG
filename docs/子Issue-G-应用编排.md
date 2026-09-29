# 子 Issue G：FastAPI 接入、应用编排与恢复

Issue：[#36](https://github.com/vansye/EasyRAG/issues/36)，父 Issue：#1。架构：[模块与数据流](架构设计.md)。位置：`app/application`、FastAPI 入口与维护 CLI。

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
POST /api/questions -> {answer, status, sources, trace, history_id, created_at, model, elapsed_ms}
POST /api/questions/stream -> SSE sources / delta / done / error
GET /api/question-history -> {total, items}
GET /api/question-history/{id} -> HistoryDetail
DELETE /api/question-history/{id} -> 204
GET|PUT|DELETE /api/model-config -> PublicConfig
GET /api/runtime -> {state, rag_available, llm, embedding}
POST /api/admin/ready -> ReadinessResult
GET /health -> {status, service, db, retrieval: {status, chroma, embedding, tokenizer}}
```

## 用例与依赖

- 收录索引：A 入库 PENDING 后返回；工作线程调用 B.split → A.begin_indexing → B.replace → A.mark_indexed。
- 更新/重建：先持 MUTATION 许可，再 B 删除旧索引 → A 变更 → 同一许可交给后台直至终态；内容哈希不变不清索引。
- 删除：B 确认删除后才 A 软删；结果不明保持 RECOVERY_REQUIRED。
- 提问：持 QUERY 许可，F 会话和 B 检索适配到 C 自己的端口；结果通过 A 补出处，rank 来自最终 trace。A 保存答案与出处快照成功后，G 才返回正式成功响应。
- 历史：只调用 A 读取或删除快照，不调用模型或检索；重新提问才走当前资料的完整问答流程。
- 恢复：G 获取 A 快照和 B.inspect，核对 ID/归属/正文/元信息。ready 先确认 READY 再扫描 PENDING；恢复期间上传不丢失。
- 启动准备：A 准备数据库，B 准备默认 tokenizer，F 加载本地 SDK；准备不代替手动就绪核验。前端构建存在时由 FastAPI 托管。
- 维护 CLI：建库初始化、受支持旧库接管、数据库升级、停服全量重建；与后端共用进程锁，不公开无门禁的 reset/embed 接口。

只从 `app.modules.<name>.public` 导入业务能力。四个业务模块不反向依赖 G。每个用例独立组织；适配器仅转换数据和错误。

## PR 与验收

- [x] 模块骨架与依赖检查：阻止兄弟模块引用、反向依赖和读取私有实现（PR #39）。
- [x] 收录索引：后台成功/失败、BUSY 保留 PENDING、短事务外模型调用（PR #45）。
- [x] 更新删除：端到端变更许可、哈希未变、索引/数据库失败、提交失败（PR #50）。
- [x] 问答引用：三态结果、引用排序、模型切换、错误形状、查询并发许可（PR #52）。
- [x] 恢复维护：重启遗留 INDEXING、缺失/多余/旧向量、ready/上传竞态、CLI 中断（PR #51、#54、#55）。
- [x] 运行与退出：单进程、单执行者；线程实际结束才释放许可；优雅停机先等待工作再关闭资源（PR #53）。
- [x] CI、真实 MySQL 集成及浏览器回归覆盖运行入口与跨模块链路。

## 流式、并发与资源边界

HTTP 生产线程在实际执行时登记工作，有界队列传送 SSE。断连先标记取消，再等待原线程退出并关闭生成器；不跨线程强制关闭仍运行的生成器。`begin_commit` 与取消通过锁排序，开始提交前取消则不保存，已开始提交可能保存成功但客户端未收到 `done`。前端不自动重试。

多个问答共享查询许可；变更独占许可，并可交给后台任务持有至终态。业务许可不等于跨线程持有 Python 线程锁。停机等待已登记请求、索引任务和许可结束，再关闭 B/A 资源，最后释放进程锁。

阶段计时区分会话创建、embedding、向量查询、判定、生成、出处与历史提交，错误记录失败阶段和安全类别，不记录提示正文或密钥。详细字段及首字口径见 [API 文档](api.md#阶段耗时日志)。

## 恢复与验证

`RECOVERY_REQUIRED` 是不能确认一致时的运行状态，不能用一次健康请求或只比较数量放行。Readiness 比对 A 的资料/切片快照和 B 的索引内容，检查 ID、归属、顺序、正文与元数据；PENDING 在通过后重新排队，INDEXING 遗留需离线恢复。

恢复和维护使用 A/B 的公开能力，具体停服、备份与重建步骤见 [README](../README.md)。原文与当前切片配置完全一致时才复用 chunk ID。任一步失败都不能报告恢复成功。

应用测试覆盖许可、竞态、取消和失败分支；隔离 MySQL 集成测试覆盖 HTTP、历史提交与重建子进程；模块导入测试约束 G 不访问私有实现。流式验证还检查提交前后断连、资源关闭和错误事件，浏览器检查未完成状态与历史回读。
