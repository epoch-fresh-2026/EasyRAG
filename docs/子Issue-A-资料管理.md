# 子 Issue A：资料管理模块

本文供维护资料收录、更新、索引状态与出处的开发者使用。阅读后应能判断哪些规则由资料模块保证，哪些操作必须经应用编排完成。总体边界见[架构设计](架构设计.md)，完整 HTTP 契约见[公共 API](api.md)。

## 一、职责

资料模块 `knowledge` 是 MySQL 业务数据的唯一拥有者，负责原文、元数据、切片、索引状态、问答历史快照，以及数据库结构校验和事务。SQLAlchemy Core、PyMySQL 和 Alembic 的对象均留在模块内部；跨模块只传递不可变数据记录。

应用编排模块 G 调用 A 的公开入口，并连接索引模块 B、问答模块 C 和回答模型模块 F。A 不调用这些模块，不拥有 FastAPI 路由、后台执行者或 embedding 计算。上传、更新、删除和恢复的跨模块次序见[应用编排](子Issue-G-应用编排.md)。

## 二、当前行为

### 收录与元数据

当前入口为上传 `.md` / `.txt` 文件，扩展名大小写不敏感。文件按严格 UTF-8 解码，移除开头一个 BOM 后保存原文，不改换行、不裁剪正文。单篇文件和更新正文上限均为 **1 MiB（1,048,576 字节）**；不支持的类型、无效编码、空内容和超限均返回明确原因，超限提示包含实际字节数与上限。

标题与标签只依赖可选元数据和常见正文结构，不要求用户改变记笔记习惯：

```text
title = 非空 frontmatter.title → 正文首个一级标题 → 文件名去扩展名
tags  = frontmatter.tags 的行内数组或块列表 → 空数组
source_uri = 去掉目录部分的原始文件名
```

解析器支持有限的 frontmatter 字段语法，不等同于完整 YAML 解释器；正文仍保留 frontmatter。标题最多 512 个 Unicode 码点，来源文件名最多 1,024 个码点。`tags` 当前只保存、展示和随索引快照传递，不参与检索过滤、查询扩展或排序；没有标签不影响收录和问答。URL 抓取、目录监听、分类树和自动关键词提取尚未实现。

### 变更检测与资料查询

```text
content_hash = SHA-256(统一 CRLF/CR 为 LF，再去首尾约定空白的正文)
```

哈希规范化的空白集合不包含 NBSP、NEL 和窄不换行空格，不能直接用默认 `str.strip()` 替换。此规则只用于判断是否需要重索引，不用于改写原文。

- 哈希相同：仅触碰 `updated_at`，保留原正文、元数据、切片和索引状态，返回 `changed=False`。哈希相同不等于字节位置相同，保存格式不同的新正文却复用旧偏移会破坏出处。
- 哈希不同：更新正文、标题、标签和哈希，清空切片，进入 `PENDING`。G 先清除该文档的派生索引，再调用 A 保存新版本和排队。
- 手动重索引：清空当前切片并置为 `PENDING`，由 G 重新切片和索引。
- 删除：G 先清索引，A 再软删 document、硬删 chunk。当前没有恢复软删资料的业务入口。

资料列表排除已删除文档，按 `updated_at DESC, id DESC` 排序；`q` 是标题字面量子串查询，`%`、`_` 不具有通配符含义，字符比较遵循数据库排序规则。`status` 按大写状态过滤；页码从 0 开始，每页默认 20、范围 1–100。

### 索引状态与切片持久化

```text
PENDING → INDEXING → INDEXED
    └─────────┴──→ FAILED
```

上传落库后返回 `PENDING`，不等待模型计算。G 的单执行者调用 B 切片，将结果转为 A 的 `ChunkWrite`；A 在短事务内锁定资料、校验当前正文、替换全部切片并置为 `INDEXING`。G 完成整篇索引并核对返回数量后调用 `mark_indexed`。失败时由 G 尽力清索引并调用 `mark_failed`，保留已存切片与失败原因；未确认清理或终态写入的操作会使业务门禁进入恢复状态。

A 不跨模型调用持有事务。切片写入、状态变更或计数更新失败时共同回滚；数据变更事务使用 READ COMMITTED。`begin_indexing` 仅接受 `PENDING`，`mark_indexed` 仅接受 `INDEXING`，`mark_failed` 仅接受 `PENDING` / `INDEXING`。普通更新、删除、重新排队拒绝仍处于 `INDEXING` 的资料。

切片契约同时约束正文与出处：

- `seq=0..n-1`，区间有序、非空且不重叠；`byte_start/byte_end` 是完整 `document.content` 的 UTF-8 字节偏移，左闭右开。
- `chunk.text == decode_utf8(utf8(document.content)[byte_start:byte_end])`，不能切开一个 UTF-8 字符。
- 首部、切片间和尾部允许纯空白间隙，必须覆盖所有非空白字符；空白判断包含 NBSP、NEL 和全角空格。纯空白切片拒绝入库。
- 正文不超过 65,535 个 UTF-8 字节，`heading_path` 不超过 512 个 Unicode 码点，`token_count` 是非负整数。A 不替上游截断或过滤。

`snapshots()` 同时返回资料、切片和 A 自己判断的 `chunks_valid`，G 无需读取 SQL 结构。离线重建先按当前 tokenizer 和切片参数重切；`begin_rebuild` 仅在当前正文与快照精确相同、所有切片字段也与重切结果一致时复用 ID，否则重新生成切片 ID。

### 出处与问答历史

`sources(chunk_ids)` 从未删除资料及其切片补全标题、正文、标题路径和字节位置。返回顺序不代表检索排名；G 按 C 的最终 trace rank 排序，并核对来源是否齐全。

问答成功后，由 G 交给 A 保存问题、答案、状态、模型摘要、耗时、来源快照和 trace。历史来源使用保存时的快照，不依赖资料日后仍存在，也不会因更新资料重新定位到新正文。技术失败和未完成的流式回答不保存为成功记录。历史查询和删除属于 A 的数据职责，问答流程不属于 A。

## 三、数据与接口

### 数据原型

```text
document
  id             BIGINT PK, auto
  source_type    ENUM(UPLOAD, URL)  # 当前公开入口只创建 UPLOAD
  source_uri     VARCHAR(1024)
  title          VARCHAR(512)
  content        LONGTEXT          # 完整原文，业务真相源
  content_hash   CHAR(64)           # 不设唯一约束
  tags           JSON              # 展示元数据，允许 []
  index_status   ENUM(PENDING, INDEXING, INDEXED, FAILED)
  index_error    VARCHAR(1024), nullable
  chunk_count    INT
  created_at, updated_at, indexed_at?, deleted_at?
  INDEX(index_status), INDEX(deleted_at)

chunk
  id             BIGINT PK, auto   # A 生成 chunk_id
  document_id    BIGINT FK → document.id
  seq            INT
  text           TEXT
  char_start     INT               # 物理列名；公开字段 byte_start，单位为字节
  char_end       INT               # 物理列名；公开字段 byte_end
  heading_path   VARCHAR(512), nullable
  token_count    INT
  created_at
  UNIQUE(document_id, seq)

question_history
  id             BIGINT PK, auto
  question       TEXT
  answer         LONGTEXT
  status         ENUM(ANSWERED, PARTIAL, REFUSED)
  created_at, elapsed_ms
  model, sources, trace  JSON       # 完成时的快照
  INDEX(created_at, id)
```

`chunk_count` 对外始终读取 MySQL 中实际存在的 chunk 行数。初次 `PENDING` 为 0，`INDEXING` 为本轮已完整落库的数量，`FAILED` 可能保留本轮切片。`INDEXED` 也不要求该值等于向量数：B-15 会对同一文档的相同 embedding 输入去重，而 MySQL 保留全部切片。派生索引数量单独报告。

### 模块公开入口（伪代码）

```python
create(filename, content_bytes) -> Document
list(page=0, size=20, status=None, q=None) -> DocumentPage
get(document_id) -> Document
chunks(document_id) -> tuple[StoredChunk]
update(document_id, content) -> UpdateResult(document, changed)
prepare_reindex(document_id) -> Document
delete(document_id) -> None

begin_indexing(document_id, chunks, *, expected_content=None) -> tuple[StoredChunk]
mark_indexed(document_id) -> None
mark_failed(document_id, error) -> None
pending_ids() -> tuple[int]
snapshots() -> tuple[DocumentSnapshot]
begin_rebuild(document_id, chunks, *, expected_content) -> tuple[StoredChunk]
sources(chunk_ids) -> tuple[Source]

save_history(entry: HistoryWrite) -> HistoryRecord
list_history(page=0, size=20) -> HistoryPage
get_history(history_id) -> HistoryRecord
delete_history(history_id) -> None
```

输入错误、资料不存在、索引状态冲突和数据库不可用通过具名异常跨模块返回。A 不决定 HTTP 状态码。前端使用 FastAPI 暴露的下列入口，详情见[公共 API](api.md)：

```text
POST   /api/documents                    multipart file → 201，PENDING
GET    /api/documents?page=&size=&status=&q= → {total, items}
GET    /api/documents/{id}               → 资料与完整正文
GET    /api/documents/{id}/chunks        → {items: [...]}，按 seq 排序
PUT    /api/documents/{id}               JSON {content} → {id, index_status, reindexed}
POST   /api/documents/{id}/reindex       → 202 {id, index_status, reindexed}
DELETE /api/documents/{id}               → 204
GET    /api/question-history            → {total, items}
GET    /api/question-history/{id}        → 完整问答快照
DELETE /api/question-history/{id}        → 204
```

## 四、关键取舍与有效裁决

| 编号 | 当前结论 | 依据与代价 |
|---|---|---|
| A-1 | G 维护单个后台索引执行者和业务门禁 | 当前规模下串行执行完整索引流程，避免引入 MQ 和额外持久化任务状态；耗时索引期间问答受门禁限制。 |
| A-2 | 启动默认需要就绪核验；孤立 `INDEXING` 不自动视为可重试 | 就绪检查核对依赖与资料/索引快照，一致后重提 `PENDING`；孤立状态或不一致需要离线重建。 |
| A-3 | 单篇上限 1 MiB，超限明确报错 | 避免静默截断；需要更大资料时由用户拆分。 |
| A-4 | 全量重建采用 G 推送快照的模式 | G 持维护进程锁，重切、落库、逐篇替换并核验；B 不读取 MySQL，数据拥有权保持单一。 |
| A-5 | 已确认进入索引流程的资料失败后，尽力清索引并保存 FAILED | 清理结果不确定时不能继续放行；原始失败与清理失败均保留，清理不等于恢复旧向量。 |
| A-6 | `chunk_count` 是库内实际行数 | 与代表片段数量、向量数量分开，避免去重后产生口径歧义。 |
| A-7 / B-2 | 不自动生成标签或关键词，tags 只展示 | 核心问答依赖正文和标题结构；BM25 分词不等于自动元数据提取。 |
| A-8 / B-1 | 当前 chunk 结构够用，不预留 `parent_chunk_id` | 标题路径、正文和字节位置能支撑当前溯源；父子分段没有进入当前实现。 |

同内容不同来源允许成为两份资料，所以哈希没有唯一约束。`tags` 用 JSON 保存，无标签聚合或过滤需求时不引入独立标签关系表。原文与切片、索引之间的完整性靠明确契约和恢复核验维持，不将规范化哈希当作偏移版本号。

## 五、验证依据

| 验收对象 | 应验证的行为 | 现有依据 |
|---|---|---|
| 收录 | UTF-8/BOM、大小上限、标题降级、frontmatter 数组/块列表、哈希空白语义 | 资料输入测试 `test_knowledge_intake` |
| 切片存储 | 非空白覆盖、精确 UTF-8 范围、重复正文定位、字段上限、拒绝重叠 | `test_knowledge_chunks`、A/B 共用的 `test_chunk_storage_contract` |
| SQL 事务与状态 | 同哈希保留原文、实际 chunk_count、状态转换、SQL 故障共同回滚 | 真实 MySQL 的 `mysql_knowledge_integration`、`mysql_schema_integration` |
| 应用流程 | 上传排队、更新/删除先清索引、失败门禁、出处和历史快照 | 应用资料/索引/问答测试及 `mysql_http_integration` |
| 恢复 | 孤立状态拒绝就绪、快照一致性、按当前切片结果复用 ID | `test_application_recovery`、`mysql_recovery_integration`、`mysql_rebuild_integration` |
| 模块边界 | A 不依赖 B/C/F/G，SQL 和数据库对象不越界 | `test_module_boundaries` |

这些测试定义当前可复验的验收条件；真实 MySQL 事务不能用纯替身测试替代，离线召回报告也不能证明资料更新和删除已同步。检索质量与在线回答质量分别见[索引管线](子Issue-B-索引管线.md)、[离线评估](子Issue-D-离线评估.md)。
