# 子 Issue A：资料管理模块

## FastAPI 迁移设计（2026-09-15，当前实施范围）

Issue [#2](https://github.com/vansye/EasyRAG/issues/2)，父 Issue #1。位置迁移至 `app/modules/knowledge`。总体边界见 [模块设计](架构设计.md)。下面旧 Spring Boot 章节保留为历史契约和验证记录。

A 独占 document/chunk、输入解析、哈希、数据库迁移和文档状态；不调用 B/C/F，不包含 HTTP 路由、后台调度或索引计算。G 通过公开入口调用 A，数据库连接和行对象不跨模块。

```python
Document = {id, source_type, source_uri, title, content, content_hash,
            tags, index_status, index_error, created_at, updated_at}
Chunk = {id, document_id, seq, text, byte_start, byte_end, heading_path, token_count}
create(filename, content_bytes) -> Document
list(page, size, status, q) -> {total, items}
get(id) -> Document
update(id, content) -> {document, changed}
prepare_reindex(id) -> Document
delete(id) -> None
begin_indexing(id, chunk_drafts) -> tuple[Chunk]
mark_indexed(id) -> None
mark_failed(id, error) -> None
pending_ids() -> tuple[int]
sources(chunk_ids) -> tuple[Source]
snapshots() -> tuple[DocumentSnapshot]  # A 维护 chunks_valid，G 不读取 SQL 私有结构
begin_rebuild(id, chunk_drafts, *, expected_content) -> tuple[Chunk]
```

内部采用 SQLAlchemy Core + PyMySQL 与 Alembic。空库建立 V2 等价结构；旧库校验 Flyway V2 和真实 schema 后显式登记基线，保留 IDs、原迁移记录及业务数据。保留 UTF-8/BOM、1 MiB、标题/来源限制、Java 哈希空白语义、字面量搜索、实际 chunk_count、updated_at 与短事务回滚规则。

- [x] 数据库结构、迁移和旧库接管；真实 MySQL 8 验证，无 SQLite 替代（PR #42）。
- [x] 输入、查询与资料变更；保留已知 Unicode、元数据和同哈希行为（PR #40、#43）。
- [x] 切片、状态与出处；失败共同回滚，切片替换使用 READ COMMITTED，状态转换由具名方法维护（PR #43）。
- [x] 恢复快照与切片 ID 复用：仅当正文和当前切片参数的结果完全一致时保留 ID（PR #49、#54）。
- [x] 单独测试本模块，无 Chroma、模型和 FastAPI 依赖；模块 import 约束通过。

实现 PR：[输入 #40](https://github.com/vansye/EasyRAG/pull/40)、[接管 #42](https://github.com/vansye/EasyRAG/pull/42)、[CRUD #43](https://github.com/vansye/EasyRAG/pull/43)、[恢复 #49](https://github.com/vansye/EasyRAG/pull/49)。本次接管本机数据的实际结果另见 [切换记录](../README.md)，不以隔离测试代替实际迁移结果。

### 当前切片持久化契约（2026-09-15 修订）

A 保存完整的 `document.content`。切片按 `seq=0..n-1` 排序，UTF-8 字节区间必须有序、非空且不重叠；每个 `text` 必须逐字节对应原文。首部、切片之间和尾部允许留出纯空白间隙，所有非空白字符必须被覆盖。空白采用与 B 相同的 Unicode 语义，包括 NBSP、NEL 和全角空格；纯空白切片仍拒绝入库。

存储上限保持 `text <= 65,535` 个 UTF-8 字节、`heading_path <= 512` 个 Unicode 码点。A 不替上游截断或过滤；B 在产出时满足这两项规格，并将标题路径元数据限制为前 512 个码点，完整标题仍保留在原文中。正文、偏移、重建复用和出处查询使用同一份原文。公开格式见 [API 的资料与切片约定](api.md#资料与切片)。

本节取代下方历史契约中“切片连续覆盖每个原文字节”的要求。合法的纯空白间隙可通过恢复核验；缺失非空白内容、重叠、错误 UTF-8 边界、正文不匹配仍然拒绝。跨模块约定由 `test_chunk_storage_contract.py` 与真实 MySQL 索引/重建回归共同验证，A/B 的生产代码继续互不 import。

## 历史实现与契约：Spring Boot 阶段

> 父 Issue：#1 总功能文档
> 位置：Spring Boot（`server/`）
> 对应考察点：知识组织与管理（设计文档必答题 1 的前半）

## 当前实施进度（2026-09-10）

本表按可验证里程碑管理进度，不用文件数或测试数量折算 M2 总体完成百分比。**M2 尚未完成用户端收录/问答闭环**。

| 能力 | 当前进度 | 验证边界/下一检查点 |
|---|---|---|
| 切片、三个 HTTP 客户端、切片/状态落库、业务门禁 | 已实现并分层验证 | 底层契约、隔离 MySQL 与进程内时序证据见历史各节，不等于完整业务已接通 |
| 已有 PENDING 文档的同步单篇编排 | 已实现，见 §十六、§十七 | 原 53 项分层测试保留；本批补上真实 Python、MySQL、Chroma 与本地模型同链路证据 |
| Java/Python/MySQL 同一条索引链路 | 本检查点已通过，见 §十七 | 4 项真实集成测试覆盖成功、明确失败、超时迟到写入与夹具关闭失败；不等于业务入口或生产恢复已完成 |
| 首次安全就绪、维护恢复 | 待实现 | 启动仍默认关闭，确认旧执行者退出与修复/重建验证之前不能开放 |
| 单执行者、收录/重排队、问答入口 | 待实现 | 本批同步原语没有任务队列、业务 REST 接口或前端；入口必须统一走门禁 |
| 真实更新/删除同步、体验与运行成本 | 有首批索引成本样本，在线验收未完成 | §十七记录单篇与模型加载耗时；业务更新/删除、生效时间、问答体验及恢复时间仍待验收 |

## 一、功能描述

资料管理是系统的入口与数据权威层。它负责把外部资料收进来、判断是否变化、持久化原文与切片元数据、编排索引流程，并对外提供全部 REST 接口。

### 1. 收录

两种来源，统一落到同一张 `document` 表：

- **上传**：md / txt 文件，multipart 提交
- **URL**：提交链接，服务端抓取正文

收录时提取标题与可选元数据。**设计前提：不要求用户改变记笔记的习惯**——核心链路只依赖普遍存在的结构信号（文档标题、Markdown 标题层级），frontmatter 属于可选增强。

```
title:  frontmatter.title → 正文第一个 # 一级标题 → 文件名（去扩展名）/ URL 的 <title>
tags:   frontmatter.tags → 空数组（无 frontmatter 的用户不受影响，只是少一种过滤维度）
```

frontmatter（`title / created / updated / tags / type`）在 Obsidian、Hugo、Jekyll 用户的笔记里常见，但不是普遍习惯。因此：

- **有 frontmatter**：`tags` 进入结构化过滤，检索多一个维度
- **无 frontmatter**：系统功能完整，检索依赖正文语义与 `heading_path` 的层级结构

单篇正文上限 **1 MB**。超限时返回明确提示（告知实际大小与上限，建议拆分），而不是静默截断或泛化的 500。常规笔记远达不到这个量级，触发上限本身就是一个值得提醒用户的信号。

**URL 抓取失败不入库**，直接返回 4xx。理由：没有正文的 document 对检索毫无意义，只会在列表里留下空壳，而空壳的清理成本高于重试成本。

### 2. 变更检测

`content_hash = SHA-256(规范化后的正文)`。规范化 = 统一换行符 + 去首尾空白（避免编辑器差异造成的假变更）。

- 更新时哈希不变 → 跳过重索引，只更新 `updated_at`
- 哈希变化 → 旧 chunk 全删、重新切片、重建索引

**哈希不是原文字节的版本号**：统一换行和去首尾空白后哈希相同，并不代表 UTF-8 字节位置相同。跳过重索引时须保留原有 `document.content` 和对应 chunks，不能悄悄替换正文；若选择保存仅格式不同的新正文，也必须重切并重建索引。Python 接收的 `text`、落库的 `document.content` 与引用展示必须是同一版本的原文。

### 3. 索引编排

上传接口不等待索引完成，立即返回 `PENDING`，后台异步推进状态机：

```
PENDING ──► INDEXING ──► INDEXED
                   └───► FAILED（记 index_error，满足恢复条件后可手动重试）
```

选异步而非同步的理由：U1 的体验是"丢进去立刻出现在列表里"，而 embedding 依赖外部模型服务、耗时不可控。状态机同时给"更新重索引"和"手动重索引"提供统一入口。

编排次序（与模块 B 的两跳调用；Python `/chunk`、`/embed` 与 Java 切片校验/事务落库已实现，Java `/chunk`、`/embed` 客户端分别见 §九、§十，统一传输契约见 §十一，独立删除索引客户端见 §十二，业务门禁原语见 §十四，文档索引状态仓库见 §十五，同步单篇编排与失败清理见 §十六；**上传接口、异步单执行者与就绪恢复入口已落地（子 Issue A-1，#15）**，问答入口属模块 C、URL 收录与更新/删除属 A-2，均未接通）：

```
1. Java: 存原文 + 哈希，status = PENDING
2. Java → Python: POST /chunk  {document_id, text, title}
3. Python → Java: chunks[]（无 id，带 seq / byte_start / byte_end / heading_path）
4. Java: 落库 chunk（生成 chunk_id），status = INDEXING
5. Java → Python: POST /embed  {document_id, chunks: [{chunk_id, text, heading_path, tags}]}（一篇完整 chunks，按 seq 排序）
6. Python: 生成并校验全部向量后，整篇替换 Chroma 索引（派生；BM25 到 M4 才实现）
7. Java: 收到 200 且 indexed == 本篇 chunk 数后，status = INDEXED, indexed_at = now
```

字段以 §三 的契约为准：一次 `/embed` 必须携带**一篇文档的全部 chunks**，正常路径按 `document_id` 整篇替换。`document_id` 与 `heading_path` 必填，后者允许空字符串；仅非空时将正文 + 换行 + 标题路径作为 embedding 输入，拼接结果不回写 Java 或原文。`EMBED_BATCH_SIZE=64` 只控制 Python 内部分批，不是 HTTP 请求上限，也不指导 Java 拆分文档。

任一步失败 → status = FAILED，index_error 记具体原因（含 Python 返回的 `cause` / `cleanup_error`），不做隐式 HTTP 重试；只有确认不存在未结束的索引变更、清理成功且业务终态已落库，才允许手动重试整篇文档（见 §十三）。失败路径上**无条件尽力调用一次 `DELETE /index/{document_id}`**，清理调用失败也必须记录，不能掩盖原始错误。超时或 Python 不可达只说明结果不确定，可能已写入或部分写入，不能断言索引为空或清理必定成功。已落库的 chunk 保留，供排查与下次重索引前清空。Python 的模型失败保护与 `409 CHUNK_ID_CONFLICT` 不改动旧向量，仅约束该次 `/embed`；Java 随后的 DELETE 是针对当前文档的独立清理调用。

M2 采用**单 Java 实例、单 Python 写入进程、全部索引变更仅经 Java 编排**的轻量边界：完整变更工作流全局串行，并与问答互斥；索引变更结果不明时暂停后续变更和问答，不用超时换取继续执行的许可。业务级门禁不等于跨 HTTP 持有数据库事务，数据库操作仍使用短事务。Python 只在单进程的索引变更临界区内串行化写入、删除与 reset，**整篇替换不是原子事务**，也不会取消正在计算的 embedding。门禁和状态仓库分别见 §十四、§十五，接入它们的同步单篇编排见 §十六；**单执行者与就绪恢复入口已落地（A-1，#16 #17）**，问答入口与维护式 reset/rebuild 的编排见 §十三。

**全量重建**：换 embedding 模型或怀疑索引与库不一致时，由 Java 编排。先关闭常规变更和问答入口、确认旧在途索引变更已结束，再执行 `GET /health` 校验依赖子项 → `POST /reset` → 分页读取 MySQL 未删除文档 → 逐篇加载完整 chunks（`ORDER BY seq`）→ 每篇一次 `POST /embed` → 核对逐篇及总计数。存在未确认调用时须先完成 §十三 的维护隔离，不能只靠 health 或 reset 排除旧任务。禁止跨文档混装或将一篇拆成多次 `/embed`；任何失败或计数不符都表示重建未完成，不能报成功或开放问答。当前只核对 Chroma，BM25 到 M4 前保持空壳。Python 不反向拉取（见子 Issue B §三、B-6 与 B-14）。

### 4. 删除

`document` 软删（`deleted_at`），`chunk` 硬删，并通知 Python 清除该文档的索引。

- 软删 document：删了什么可追溯、可恢复，且历史问答记录里的文档引用不会变成悬空 id
- 硬删 chunk：chunk 是派生物，没有保留价值；删掉后检索自然引用不到它 —— 这就是 U2 的实现

## 二、数据原型（伪代码）

```
document
  id            BIGINT        PK, auto
  source_type   ENUM          UPLOAD | URL          -- 预留 FOLDER（见 #1 收录入口裁决）
  source_uri    VARCHAR(1024) 上传=原始文件名，URL=链接
  title         VARCHAR(512)  见降级顺序
  content       LONGTEXT      原文（真相源，溯源展示与全量重建都依赖它）
  content_hash  CHAR(64)      SHA-256(规范化正文)
  tags          JSON          frontmatter.tags，用于结构化过滤
  index_status  ENUM          PENDING | INDEXING | INDEXED | FAILED
  index_error   VARCHAR(1024) NULL
  chunk_count   INT           default 0     -- MySQL 中该文档现存 chunk 行数（库内事实，非索引事实）
  created_at    DATETIME
  updated_at    DATETIME
  indexed_at    DATETIME      NULL
  deleted_at    DATETIME      NULL              -- 软删标记

  INDEX (index_status)
  INDEX (deleted_at)
  -- 不对 content_hash 加唯一约束：同内容不同来源是两份资料，
  --   去重会让"上传成功但列表里没有"变成难以解释的行为。重复内容仅提示，不阻止。

chunk
  id            BIGINT        PK, auto            -- 即 chunk_id，由 Java 生成（唯一写入者）
  document_id   BIGINT        FK → document.id
  seq           INT           文档内序号，从 0 起
  text          TEXT          切片正文
  char_start    INT           旧物理列名，存 byte_start：document.content 的 UTF-8 字节起始偏移
  char_end      INT           旧物理列名，存 byte_end：UTF-8 字节结束偏移，左闭右开
  heading_path  VARCHAR(512)  NULL  所属标题路径，如 "三、工作机制 > 1. 推理阶段划分"
  token_count   INT
  created_at    DATETIME

  UNIQUE (document_id, seq)
  -- 对外 byte_start/byte_end 映射到现有 char_start/char_end 列，与 heading_path 共同支持溯源：
  --   前者让引用能定位到原文那一段，后者给切片带上层级语义。
```

**B-13 的落库兼容边界**：Python 与切片响应 DTO 统一使用 `byte_start/byte_end`；Java `ChunkRepository` 已显式映射到 V1 的 `char_start/char_end` 物理列，不把这些数值传给 `String.substring`。V1/V2 迁移保持不变，当前实现和事务边界见 §八。已有数据若偏移单位不明，须按保存的正文重新切片、重新索引；不能靠改列名或数值大小猜测单位。

`tags` 用 JSON 列而不是 `tag` / `document_tag` 两张表：当前只需要"按标签过滤"，反范式够用；等真的要做标签聚合再规范化，避免一开始就维护两个真相源。

`tags` 允许为空数组，且空数组不影响任何核心功能——见 §一.1 的设计前提。`heading_path` 才是核心链路依赖的结构信号，它来自 Markdown 标题层级，对所有笔记普遍成立。

## 三、对外接口（伪代码）

### 前端 ↔ Spring Boot

```
POST   /api/documents
       multipart: file                       # 上传 md/txt
       或 json:   { url: string }            # 提交链接
  → 201 { id, title, source_type, index_status: "PENDING" }
  → 400 { "error": "<面向用户的原因>" }       # 抓取失败 / 不支持的类型 / 空内容 /
                                             #   非 UTF-8 / 超 1MB（附实际字节数）

GET    /api/documents?status=&page=&size=
  → 200 { total, items: [{ id, title, source_type, tags,
                           index_status, chunk_count, updated_at }] }
     # status 大小写不敏感（归一为大写后过滤）；page 从 0 起，size 默认 20、上限 100
     # chunk_count 语义：MySQL 中现存的 chunk 行数，不是索引里的条数。
     #   PENDING  —— 0（首次）或上一轮的遗留数（重索引前不清）
     #   INDEXING —— 本轮切片的最终条数（切片原子返回、一次落库，不会中途增长）
     #   INDEXED  —— 与 Chroma 条数一致（BM25 到 M4 才纳入核对）
     #   FAILED   —— 与索引侧可能不一致，以本字段为准（库内是真相）

GET    /api/documents/{id}
  → 200 { id, title, content, tags, source_type, source_uri,
          index_status, index_error, chunk_count, created_at, updated_at }
  → 404 不存在或已删除

PUT    /api/documents/{id}
       multipart: file  或  json: { content: string }
  → 200 { id, index_status }                 # 哈希不变则 status 保持 INDEXED
  → 404

DELETE /api/documents/{id}
  → 204                                      # 软删 document + 硬删 chunk + 清索引
  → 404

POST   /api/documents/{id}/reindex
  → 202 { id, index_status: "PENDING" }      # 手动重索引，受 §十三 的恢复门禁约束

GET    /api/documents/{id}/chunks
  → 200 { items: [{ id, seq, text, byte_start, byte_end, heading_path, token_count }] }
       # 透明度接口：让"切成什么样"可见，也是调试切分策略的入口

POST   /api/admin/ready                      # 就绪恢复（A1-1；已落地）
  → 200 { state: "READY", recovered: int }   # recovered = 重提的 PENDING 篇数
  → 409 { state: "MUTATING"|"RECOVERING" }   # 有未结束的变更，不夺取闸门

# 状态标注（2026-09-12）：POST/GET /api/documents 与 POST /api/admin/ready 已落地（A-1）；
#   其余（详情/PUT/DELETE/reindex/chunks）属 A-2，尚未实现
```

### Spring Boot → Python（Python 端实现见子 Issue B；Java `/chunk` 客户端见 §九）

```
POST /chunk   { document_id, text, title }
  → 200 { chunks: [{ seq, text, byte_start, byte_end, heading_path, token_count }] }
     # text == decode_utf8(utf8(document.content)[byte_start:byte_end])，左闭右开
  → 422 字段校验失败 / INVALID_UTF8_TEXT / CHUNK_TOKEN_LIMIT_EXCEEDED
  → 503 TOKENIZER_UNAVAILABLE（cause=异常类名）

POST /embed   { document_id, chunks: [{ chunk_id, text, heading_path, tags }] }
  → 200 { indexed: int }
  → 422 空列表 / 格式或字段校验失败 / 重复 chunk_id
  → 409 CHUNK_ID_CONFLICT（当前 collection 中 chunk_id 属于另一文档；本次 /embed 不删除任一文档）
  → 503 EMBEDDING_UNAVAILABLE（模型调用或向量校验失败，保留旧向量）
  → 503 INDEX_UNAVAILABLE（索引预检或清旧前的准备失败）
  → 503 INDEX_WRITE_FAILED（清旧或 upsert 失败；cause=异常类名，cleanup_error=null 或清理异常类名）

DELETE /index/{document_id}
  → 200 { removed: int }
  → 422 document_id 非正 / 超 BIGINT 范围 / 路径参数无法解析
  → 503 INDEX_UNAVAILABLE（cause=异常类名）
```

`409`/`503` 的应用错误码位于 `detail.error`，不是响应顶层。例如：`{"detail":{"error":"INDEX_WRITE_FAILED","cause":"RuntimeError","cleanup_error":null}}`。这些 `503` 的 `cause` 仅含异常类名，不回显上游响应或原始异常消息。普通字段校验和 `/embed` 的 `422` 使用 FastAPI 标准的 `detail` 错误列表；`/chunk` 的非法 UTF-8 文本、无法满足 token 预算则返回 `{"detail":{"error":"INVALID_UTF8_TEXT"}}` 或 `{"detail":{"error":"CHUNK_TOKEN_LIMIT_EXCEEDED"}}`。

DELETE 请求不带正文，`document_id` 为正有符号 BIGINT。`removed` 是非负的实际删除量，重复删除或不存在文档返回 200 且 `removed=0`，不与 MySQL `chunk_count` 比较；404 不是幂等成功。Java 返回值范围、严格响应校验和传输失败边界见 §十二。

`/chunk` 的三个请求字段均必填：`document_id` 为正的有符号 BIGINT JSON 整数，拒绝布尔值和字符串 ID；`text` 为非空白字符串；`title` 为字符串，允许为空。`text/title` 必须能严格编码为 UTF-8。`title` 由 Java 管理，当前只接收并校验，不插入正文、不替换 Markdown 的 `heading_path`。返回 `seq=0..n-1`，不创建 chunk ID；`token_count` 计入正文、非空标题路径与模型特殊 token。

偏移只定位入库原文的 UTF-8 编码，不定位上传文件原始字节、JSON 报文字节或拼接后的 embedding 输入。Python 不 trim、不统一 CR/LF、不去 BOM、不做 Unicode NFC/NFD 归一化。Java/JS 必须先按字节截取并严格 UTF-8 解码；需要字符串高亮下标时，再把解码后的前缀长度转换成 UTF-16 码元位置。浏览器使用 `TextDecoder` 时须设置 `fatal: true, ignoreBOM: true`，避免原文中的 BOM 被默认吞掉。完整示例见子 Issue B 的 B-13 契约。

`/embed` 请求约束：`document_id`、`chunk_id` 均为正的有符号 BIGINT（`1..9223372036854775807`）；`chunks` 为非空完整列表，列表内 `chunk_id` 唯一。`text` 为非空白字符串，`heading_path` 为必填字符串（可为空），`tags` 为字符串数组（省略时默认 `[]`）。库内 `heading_path` 为 NULL 时 Java 发空字符串，不发 null；完整性与顺序由 Java 保证，按 `seq` 排序后发送，Python 按数组位置派生 `seq=0..n-1`。

成功时 `indexed` 等于本次整篇 chunk 数；重复相同整篇请求不产生重复 ID，新列表更短会删除旧的剩余 chunks。清空文档索引用 DELETE，不用空 `/embed`。Python 必须先生成并校验全部向量（数量、配置维度、坐标转为 float32 后仍为有限数值），任一模型调用或向量校验失败时不动旧向量；随后在正常路径下清旧一次并内部分批 upsert。首次清旧或任一 upsert 失败时，仍在变更临界区内尝试清除本篇残留：清理成功才返回 `cleanup_error=null`，清理也失败则返回其异常类名，不能保证没有残留。这不是原子事务，清理也不恢复旧向量。详见子 Issue B §一.2–3。

当前 Python 接口与跨语言契约的验证记录见子 Issue B §八、§九；Java `/chunk`、`/embed`、删除索引客户端分别见本模块 §九、§十、§十二，完整业务编排仍待接入。仅索引丢失时可复用既有 chunks；tokenizer 或切片参数变化时，须重新 `/chunk`、重新落库后再 `/embed`，不能只重写向量便认为新 token 预算已满足。

## 四、模块边界

**A 负责**

- 全部对前端的 HTTP 入口（Python 不暴露给前端）
- 原文与 chunk 元数据的持久化，MySQL 的唯一写入者
- 内容哈希与变更判定
- 索引状态机与调度（M2 完整变更工作流全局串行、与问答互斥；含启动就绪、失败恢复入口及维护式 reset/rebuild）
- 为模块 C 提供 `chunk_id → 原文定位` 的补全能力（问答响应里的出处）

**A 不负责**

- 怎么切、切多大、按什么切（模块 B）
- 检索、改写、拒答、生成（模块 C）
- 评估指标的计算（模块 D）

**依赖方向**：A → B（HTTP 调用），A → C（HTTP 调用）。B 与 C 不反向调用 A，不读写 MySQL。

## 五、验收

- [ ] 上传一篇 md，列表中出现该条目，状态从 PENDING 变为 INDEXED（U1）
- [ ] 该文档的 `/chunks` 返回切片，每条 `byte_start/byte_end` 按 UTF-8 字节截取并严格解码后与 text 相等，包含 emoji、BOM、组合字符与 CRLF
- [ ] 重新上传内容完全相同的文件 → 不触发重索引（`indexed_at` 不变）
- [ ] 规范化哈希相同而跳过重索引时，原有 content 与 chunks 保持配套；若保存格式不同的新正文，则重新切片，不复用旧字节偏移
- [ ] HTTP 的 byte_start/byte_end 显式映射到现有物理列；Java/前端不把字节值直接当 UTF-16 字符串下标
- [ ] 修改正文后更新 → 旧 chunk 全部消失、新 chunk 生成、`chunk_count` 变化（U3 的前半）
- [ ] 删除文档 → 列表不再返回、`/chunks` 返回 404、索引侧该文档的向量被清除（U2 的前半）
- [ ] 抓取失败的 URL → 返回 4xx，`document` 表不新增行
- [ ] frontmatter 有 title 时用它；无 frontmatter 时回落到一级标题；都没有时用文件名
- [ ] **一篇完全没有 frontmatter 的 md 能正常收录、索引、被检索命中**（不写 frontmatter 的用户不受影响）
- [ ] 超过 1 MB 的文件 → 返回提示，含实际大小与上限说明
- [ ] 启动默认不开放问答和常规变更；遗留 INDEXING 不直接重置排队，须先确认旧执行者结束并完成维护恢复，不能用仅重启 Java 或 health 为绿替代确认
- [ ] Python 不可用、请求超时或 `/embed` 失败 → status = FAILED，记录原始原因；无条件尽力 DELETE，清理失败也记录；只有索引变更/清理和业务终态均确认后才允许继续或手动重试
- [ ] Java 一次发送按 `seq` 排序的完整文档，即使超过 64 个 chunks 也不拆成多个 `/embed`；M2 完整变更工作流全局串行且与问答互斥，reset/rebuild 全程处于维护门禁内
- [ ] 全量重建逐篇核对 `indexed`、汇总核对 Chroma 与 MySQL chunk 数；任一步失败或计数不符都标为未完成，BM25 到 M4 前不参与非零计数验收
- [ ] 旧 `/embed` 结果不明但随后 DELETE 成功，或 DELETE 自身结果不明 → 不发起下一条索引变更、不开放问答；普通异常退出不能意外解除暂停
- [ ] 新增资料返回 PENDING 不等待 embedding；正常批量收录按篇释放门禁；问答忙碌/维护明确反馈且不调用 LLM，不伪装成检索无结果
- [ ] 按 §十三 交付正常问答、索引期间问答、内容生效及维护恢复的真实耗时与模型调用成本，经用户判断可接受后再完成体验验收

## 六、已裁决

| # | 问题 | 结论 | 理由 |
|---|---|---|---|
| A-1 | 异步索引实现 | **Spring `@Async` + 单个索引执行者 + 业务门禁**，不引入 MQ、持久化代次或 Python SQLite 控制库 | M2 先限制完整工作流的并发，用明确的暂停与维护恢复换取较低实现复杂度；时间成本按 §十三 实测 |
| A-2 | 重启时卡在 INDEXING 的文档 | **启动默认不就绪，先维护恢复，再允许重新排队**；取代原“启动即重排队”裁决 | 仅重启 Java 不会终止旧 Python 请求；不能因内存门禁重置而放行，须向用户显示需恢复的状态与原因 |
| A-3 | 单篇正文体积上限 | **1 MB，超限返回明确提示** | 常规笔记远达不到；触发上限本身是值得提醒用户的信号（建议拆分），不静默截断 |
| A-4 | 全量重建的依赖方向 | **推模式**：维护门禁内确认旧变更结束，再由 Java 编排 health → reset → 分页读文档 → 每篇完整 chunks 一次 embed → 计数核对，Python 不反向调用 | 边界"B/C 不反向调用 A"保持无例外；重建是编排行为，与上传状态机同归属；不是对一篇文档做 HTTP 分批。详见子 Issue B B-6、B-14 |
| A-5 | FAILED 前是否清索引 | **无条件尽力调一次 `DELETE /index/{id}`**，记录清理失败，不承诺清理成功 | 超时或不可达可能留下不确定结果或半成品索引；不能推断"索引本就没写进去"。保留原始失败原因，供手动整篇重试 |
| A-6 | `chunk_count` 的语义 | **MySQL 现存 chunk 行数**（库内事实，非索引事实），四态含义写进接口说明 | 两个来源都叫"chunk 数"必然产生歧义；固定为库内口径，索引条数由 `/health` 单独暴露 |

## 七、原待议项的去向（子 Issue B 已给出结论）

| # | 问题 | 结论 |
|---|---|---|
| A-7 | 无 frontmatter 用户的结构化过滤如何生效 | 归入 **B-2**：先走传统关键词抽取（jieba / TF-IDF，零成本、确定性）自动填充主题词，不够再上 LLM 抽取，届时有实测支撑。列为 M4 候选，不进 M2 基线 |
| A-8 | chunk 结构是否需要调整 | **够用，不调整**。B 的标题层级切分天然产出 `heading_path`，二次切分的子块共享同一路径；切片输出 `byte_start/byte_end`，Java 校验后写入旧 `char_start/char_end` 列，由共享 UTF-8 样例和落库测试兜底。B-1 已定不预留 `parent_chunk_id`，等 M4 真采用父子分段时一次迁移 |

## 八、Java 切片落库现状（2026-09-08）

已实现 `ChunkBatch` 和 `ChunkRepository`；后续新增的 Java `/chunk` HTTP 客户端见 §九，上传接口与异步状态机仍未接入；§五涉及完整业务流程的验收项仍不勾选。

### 结果校验

`ChunkBatch` 对应 `/chunk` 的完整响应，复制并冻结 chunks 列表。调用 `validateAgainst(content)` 时：

- 六个结果字段均必填；`seq` 必须按响应数组顺序从 0 连续，不能默默把乱序或缺段结果当作完整文档。
- chunks 从字节 0 连续覆盖至原文 UTF-8 长度，无空范围、重叠或缺口；严格编码原文和文本、严格解码每个范围，并与 `text` 精确比较。不规范化正文，也不强制组合字形/ZWJ emoji 整体不可分。
- `text` 不得为空白，包含 Python 会视为空白的 NBSP、NEL 等；否则无法进入 `/embed`。若上游产生纯空白块，拒绝整批，不靠过滤或 trim 改变原文覆盖关系。
- 按现有列约束校验：`text` 最多 65535 个 UTF-8 字节，`heading_path` 最多 512 个 Unicode 码点（不是 Java UTF-16 长度），允许空标题路径。超限拒绝，不静默截断，不在此轮改变表结构。
- `token_count` 必须是非负 INT；Java 不持有 tokenizer，不重新计数或证明模型/预算匹配，仍沿用 Python 的结果。

### 事务入口

通过 Spring 管理的 bean 调用 `ChunkRepository.replace(documentId, expectedContent, batch)`；`expectedContent` 必须是送给 `/chunk` 的那份原文，HTTP 调用应在数据库事务外完成。

1. 校验正 document ID 和完整 batch，再读取当前 JDBC 连接的实际隔离级别；必须为 `READ_COMMITTED`，否则在文档查询和删除旧行前明确拒绝。
2. 在 `@Transactional(isolation = Isolation.READ_COMMITTED)` 事务中读取未软删文档并 `SELECT ... FOR UPDATE`，随后用 Java 精确字符串相等核对当前 content 与 expectedContent。既不依赖规范化哈希，也不依赖 MySQL 忽略大小写/重音的比较规则。
3. 只删除当前文档的旧 chunks，按 seq 插入新行。Java 是唯一写入者，ID 由本次 MySQL `AUTO_INCREMENT` 插入分配；byte_* 显式写入 char_* 物理列。
4. 同事务更新 `document.chunk_count`，返回有序、不可修改的 `StoredChunk` 列表（带 chunkId 和偏移）。删除、任一次插入或 count 更新失败，全部回滚，保留原来的 rows/count；不承诺自增 ID 连续。

**外层事务契约**：保留默认 `REQUIRED` 传播，不使用 `REQUIRES_NEW` 独立提交。没有外层事务时，仓库创建 RC 事务；已有外层事务时，实际隔离级别也必须为 RC。Spring 加入已有事务不会用本方法的注解覆盖外层隔离级别，因此直接检查 `Connection.getTransactionIsolation()`，不能只看可能为 null 的线程事务元数据；外层声明 `DEFAULT` 也以连接实际值为准。后续要把状态更新与切片一起提交，调用者必须建立相容的 RC 外层事务，两者仍可共同回滚。

非法结果抛 `IllegalArgumentException`；事务隔离级别不兼容抛 `IllegalStateException`，提示 `chunk replacement requires READ_COMMITTED transaction isolation`；文档不存在或已软删抛 `EmptyResultDataAccessException`；原文已变化抛 `IllegalStateException`；SQL 失败保留 Spring `DataAccessException`。运行时异常仍遵循 Spring 事务回滚规则，参与外层事务时会标记回滚，不能捕获后假设外层仍可正常提交。这些是内部 Java 边界，不是已实现的 HTTP 状态码映射，不做隐式重试。

RC 只用于这个调用边界，不改 MySQL 或生产连接池的默认隔离级别；普通读取不再保证 RR 的可重复读语义，当前替换依靠文档行锁和原文精确比较保证一致性。调用者若把更多业务放进同一外层事务，需要遵守这个读取语义。

**当前原语只改变 chunks 与 chunk_count**：不保存/规范化 content，不修改 content_hash、index_status、index_error、updated_at 或 indexed_at，也不调用 `/embed`。后续编排仍须协调正文更新、旧切片的可见性、状态转换，以及同文档更新/删除/重试和 rebuild 的在途任务；行锁和原文比较不是跨 HTTP 任务排序，更不是 MySQL/Chroma 的分布式事务。

### P1 并发整改与验证

对抗审查发现：默认 RR 下，两篇不同的新文档都完成空范围 `DELETE` 后再 `INSERT`，即使各自持有 document 行锁，仍会因 chunk 索引的间隙锁而死锁。此次改用 RC 消除这个已复现的交错，并拒绝不兼容外层事务，避免它静默恢复 RR；不通过独立提交、重试或全局串行化掩盖问题，也不宣称能消除所有可能的数据库死锁。

- TDD 红灯：新增 8 项回归先在原实现运行，得到 **7 项断言失败、0 项测试错误**。其中跨文档并发重复 3 轮均出现死锁；`DEFAULT`（实际 RR）、RU、RR、SERIALIZABLE 四种外层配置都执行到了 DELETE，触发测试库的拒绝删除触发器；RC 外层共同回滚的对照原本通过。
- 修复后新增 8 项全部通过。并发用真实 `JdbcTemplate` 在两次真实 DELETE 完成后加屏障，不替换 SQL 或数据库行为；3 轮共 6 笔写入全部提交，分别核对两篇文档的切片正文和 count。这是受控交错回归，不是自然死锁概率或吞吐压测。
- 不兼容外层事务在触发 DELETE 前拒绝；RC 外层先完成切片替换和状态更新，再人为抛错，旧 chunks（含原 ID）、count 与文档状态均恢复，守住共同回滚边界。
- P1 整改时的干净构建：在 `server/` 运行 `.\mvnw.cmd '-Dit.test=ChunkRepositoryIT' clean verify`，**48 项单元测试 + 24 项 MySQL 集成测试通过**，无失败、测试错误或跳过，Jar 打包成功。24 项包含原有 16 项及本次新增 8 项；原有同文档等锁后核对新正文、软删拒绝和 SQL 故障回滚仍通过。
- 测试专用连接池固定默认 RR，验证仓库不能依赖环境恰好为 RC；生产配置未改。测试和 Flyway 指向随机独立库，清理前核对库名，结束已 DROP 测试库并关闭连接池；未运行直连 local 配置的 `SchemaMigrationIT`，不要把上述命令换成无筛选 `verify`。

子 Issue B §十保留的是原语切片 **48 + 16** 及 Python **190 passed** 的验证快照；本节记录 P1 时点的 **48 + 24**，后续客户端的最新验证见 §九。Python 与偏移契约在 P1 阶段未改、未重跑，也没有新的召回评估分数。

**P2 接续**：`ChunkBatch` 校验的是反序列化后的 Java 值，不能单独证明原始 JSON 类型正确。默认 Jackson 的隐式类型转换问题由 §九中实际 HTTP 调用使用的专用 reader 处理；没有修改全局 mapper，也不能把任意默认 mapper 读取 `ChunkBatch` 的路径视为严格入口。

## 九、Java /chunk HTTP 客户端与 P2 整改（2026-09-09）

本节保留 `/chunk` 客户端切片的 **136 + 24** 历史验证快照；后续 `/embed` 客户端见 §十，当前传输整改与最新回归结果见 §十一。本节中的未完成项描述的是当时的切片边界。

**后续复核整改**：§十在初版 `/embed` 上发现的 401/407 错误正文丢失，也已在旧 `/chunk` 客户端上实测复现，并由 §十一修正。该问题不再是待处理风险；修复后的认证错误保真与统一超时证据以 §十一为准，不追溯修改本节初版的测试计数。

已实现 Spring bean `ChunkClient`，入口为 `chunk(documentId, text, title)`，返回经过完整校验的 `ChunkBatch`。它是可独立调用的内部 HTTP 客户端，尚无上传/索引编排服务把它与 `ChunkRepository` 串成业务链路；不据此勾选 §五的端到端验收。

### 请求与结果边界

1. 在发出请求前拒绝非正 ID、null/空白正文、null 标题和不能严格编码为 UTF-8 的 text/title；标题允许为空。与 Python 一致识别 NBSP、NEL 等空白，不 trim、不改换行，不插入文档标题。
2. 该历史切片初版用 Spring `RestClient` 和 `SimpleClientHttpRequestFactory` 发送 `POST /chunk`，后续工厂替换见 §十一；字段仍为 `document_id`、`text`、`title`，请求和 Accept 均为 `application/json`。正文与标题经 JSON 编码后保持原值，支持有符号 BIGINT 最大 ID。
3. 通过 `RestClient.exchange` 先取得状态码和原始 body 字节，非 200 不解析媒体类型。成功必须为 HTTP 200、非空 body，媒体类型的 type/subtype 精确为 `application/json`（允许 charset 参数，不接受 `application/*` 或 `*/*`）。随后交给专用 `ObjectReader`，不先用宽松字符串解码替换损坏的 UTF-8 字节。
4. reader 拒绝小数、数字字符串或布尔值冒充 Integer，拒绝数值或布尔值冒充 String；即使 `0.0` 能无损变为整数，也不接受。重复 JSON 字段和尾随第二份 JSON 同样拒绝，不能静默取最后一个值或只读第一份对象。
5. 解析后立即调用 `ChunkBatch.validateAgainst(text)`，复用 §八的必填字段、连续序号/字节覆盖、精确原文比较和存储长度约束。任何一项失败都不返回可落库结果；不过滤坏块，不重算 token_count，INT 上限内的合法值仍可透传。

### 配置与异常

配置集中在 `server/src/main/resources/application.yml`，支持对应环境变量覆盖：

| 配置键 | 环境变量 | 默认值 |
|---|---|---|
| `rag.base-url` | `RAG_BASE_URL` | `http://localhost:8000` |
| `rag.connect-timeout-ms` | `RAG_CONNECT_TIMEOUT_MS` | `3000` |
| `rag.read-timeout-ms` | `RAG_READ_TIMEOUT_MS` | `60000` |

超时值必须为正毫秒数，0 和负数在构造客户端时拒绝，避免意外禁用超时。base URL 可包含路径前缀和尾斜杠，最终追加 `/chunk`。bean 初始化只配置客户端，不探测 Python；Python 不可用不会因本客户端的启动探针而阻止 Java 启动。

该历史切片初版的读超时为 socket 读取等待超时，持续缓慢发送数据可能延长总耗时。§十一已将 `/chunk` 改为与 `/embed` 相同的 HTTP 交换总等待预算；本段不再描述当前超时行为，仍不承诺长文吞吐或 Python 取消确认。

| 情况 | Java 边界（传输整改见 §十一） |
|---|---|
| 本地请求参数或超时配置无效 | `IllegalArgumentException`；无 HTTP 请求 |
| 能完整读取响应时的任意非 200，包括 201/202/204、重定向、422、503 | `RestClientResponseException`；即使 Content-Type 畸形，也保留状态码、响应头和原始 body 字节，不把错误体交给切片 reader |
| 连接拒绝、响应头/body 读取超时等 I/O 错误 | 由 Spring 包装为 `ResourceAccessException` 并保留底层原因；不能完整读取错误响应时也作为传输失败，而非假装已有完整错误体 |
| 200 但媒体类型/body/JSON/切片契约无效 | `RestClientException("Invalid /chunk response", cause)`；保留解析或校验原因 |

不自动跟随 POST 重定向，不做应用层重试，不记录请求正文或错误响应日志；调用方可从 HTTP 异常读取 Python 的 `detail.error` 或标准 422 `detail` 列表，而不是假设错误码在顶层。这里尚未将异常映射成前端状态码或 `index_status=FAILED`。

**事务约束保持不变**：客户端不创建数据库事务、不读写 MySQL/Chroma。调用方必须在数据库事务外完成 HTTP，再把相同 text 和返回 batch 交给 §八的 RC 仓库入口；当前没有业务编排层替调用方保证这一顺序，不能持有库锁等待切片，也没有额外的事务注解自动禁止这种误用。

### 验证与尚未覆盖

- TDD：可运行的空客户端先得到 7 项 Unicode 用例断言失败；基础 HTTP 实现先通过这 7 项。扩展到 82 项回归后得到 **62 项断言失败、0 项测试错误**，其中 15 组错误 JSON 类型仍被默认解析接纳，其余失败还包含未统一的异常边界和待加约束，不代表发现了 62 个独立漏洞；实现严格入口后 **82 项全部通过**。
- 收尾复核另补媒体类型反例：9 项定向用例先得到 **6 项断言失败、0 项测试错误**，复现畸形 Content-Type 掩盖 503 状态、200 畸形头错误边界不一致及通配媒体类型被接受。改用 `exchange` 避免消息转换器先解析响应类型，再精确匹配成功媒体类型，最终 **88 项客户端回归全部通过**。
- 客户端测试使用随机端口的真实本机 HTTP 服务，不 mock `RestClient`；覆盖 7 组共享 Unicode 样例、请求字段/headers、P2 类型反例、必填/错误范围、空/截断/重复/尾随 JSON、非法 UTF-8 字节、异常状态、配置、连接拒绝及响应头/body 的真实读超时。服务、连接和线程均在测试后清理，不依赖在线 Python、Ollama 或业务数据库。
- 该阶段 Java 干净构建：在 `server/` 运行 `.\mvnw.cmd '-Dit.test=ChunkRepositoryIT' clean verify`，**136 项单元测试（88 客户端 + 46 契约 + 2 健康）及 24 项 MySQL 集成测试全部通过**，无失败、错误或跳过，Jar 打包成功。P1 的并发/外层事务/回滚回归保持通过；随机 MySQL 测试库已 DROP、连接池已关闭。不要改用无筛选 `verify`，本轮未运行直连 local 库的 `SchemaMigrationIT`。
- Python 源码及共享样例未改；在 `rag-service/` 运行 `.\.venv\Scripts\python.exe -m pytest -q`，**190 passed，2 条既有弃用警告**。这包括 Python 端的共享偏移与 `/chunk` 契约回归，不是 Java 进程直接调用真实 Python 进程的联调证据。

本轮仅改客户端、客户端测试、application 配置及 A/B 两份文档；没有修改 `ChunkBatch`、仓库、迁移或 Python。未完成 Java `/embed` 客户端、上传接口、异步状态机、`/chunk → MySQL → /embed` 全链路、前端引用可见性、真实双进程/云端联调或长文性能评估，也没有新的召回分数。

## 十、Java /embed HTTP 客户端（2026-09-09）

本节保留 `/embed` 客户端切片的 **238 + 24** 历史验证快照；该阶段 `/chunk` 尚未完成传输整改。两客户端当前统一的契约与最新验证见 §十一。

已实现 Spring bean `EmbedClient`，入口为 `embed(long documentId, List<ChunkRepository.StoredChunk> chunks, List<String> tags)`，返回经核对的 `int indexed`。它与 `ChunkClient` 一样是独立的同步 HTTP 客户端，不是上传接口、异步索引服务或跨存储事务。

### 请求与整篇边界

- 接收仓库生成 ID 的 `StoredChunk` 列表，按 `seq` 排序后发出**一次** `POST /embed`；不修改调用方列表，不生成 ID，64、65、129 块均不拆成多个 HTTP 请求。序号由列表顺序传达，只发送 `chunk_id / text / heading_path / tags`，不发送偏移或 token_count。
- 发请求前拒绝非正 document_id、空/null chunks、null 元素、跨文档 chunk、非正或重复 chunk_id；排序后 seq 必须从 0 连续，重复或缺号均失败，不静默丢块或重编号。
- 正文必须非空白，空白判定与 Python 对齐（包含 NBSP/NEL）；正文、heading_path、tags 中的字符串必须非 null 且可严格编码为 UTF-8。heading_path、标签列表和单个标签允许为空；标签列表本身不能为 null。文档级 tags 取快照后随每块发送，不 trim、不改换行、不把标题路径拼进原文。
- **连续序号不等于已证明整篇完整**：客户端不查询 MySQL，无法发现调用方只给了从 seq=0 开始的完整前缀，也无法核对正文版本、chunk_count 或 ID 的真实归属。调用方必须传入同一版本的全部已落库 chunks 和对应标签；字节范围与 token_count 仍由前置切片校验/落库负责，客户端不重算。

### 成功响应与错误边界

- 通过 `RestClient.exchange` 读取状态和原始 body。只有 **HTTP 200**、非空且媒体类型精确为 `application/json` 的响应才进入成功解析；允许合法 charset 参数，拒绝 `application/*`、`*/*` 和其他 JSON 子类型。
- 使用客户端私有 Jackson reader：`indexed` 必须是非 null 的 JSON 整数，拒绝字符串、小数、指数形式和布尔值的隐式转换，拒绝重复键、尾随 JSON/垃圾及损坏 JSON。`indexed` 必须恰好等于本次提交的 chunk 数；缺失、0、负数、溢出或数量不符均不能报成功。没有修改全局 mapper 或 `/chunk` 的 reader。
- 非 200 且 body 完整读取后返回 `RestClientResponseException`，保留状态、headers 和原始 body 字节，不先解析 Content-Type 或假定错误 JSON 的形状。因此损坏的媒体类型/错误正文不会覆盖完整响应的 HTTP 错误；读取途中中断或超时仍属于传输故障，不能声称已保留完整错误响应。Python 应用错误在 **`detail.error`**，`cause` / `cleanup_error` 同样在 detail 内；422 的 detail 也可能是 FastAPI 校验列表，留给后续编排层按实际响应处理。
- 成功响应非法时抛 `RestClientException("Invalid /embed response", cause)`；输入/超时配置非法时抛 `IllegalArgumentException` 且不发送 HTTP；传输故障由 Spring 包装并保留原因，当前 exchange 路径为 `ResourceAccessException`。后续失败记录不能只保存最外层消息而丢掉响应 body 或 cause。

### 配置与调用责任

复用 §九已有的 `rag.base-url / rag.connect-timeout-ms / rag.read-timeout-ms`，对应 `RAG_BASE_URL / RAG_CONNECT_TIMEOUT_MS / RAG_READ_TIMEOUT_MS`，默认仍为 `http://localhost:8000 / 3000 / 60000`。支持路径前缀和尾斜杠；两个超时必须为正毫秒。初始化不探测 Python，本轮没有修改 application 配置或增加依赖。

为保留 401/407 错误正文，`/embed` 使用 `JdkClientHttpRequestFactory` 与 Java 17 内置 `HttpClient`，显式禁用重定向并使用 HTTP/1.1。关闭 Spring JDK 工厂默认的自动解压，保留错误响应的 Content-Encoding 与原始 body 字节；gzip 错误正文也不在此层转换。

`/embed` 的 read-timeout 是 Spring JDK 工厂的 **HTTP 交换总等待预算，覆盖响应头和正文**，持续少量到达的正文不会重置计时；它不是每次 socket read 的空闲超时，也不是覆盖本地参数处理/JSON 解析的整个 Java 方法硬 deadline。本节记录的阶段中 `/chunk` 曾沿用 socket 读取等待超时，后续已由 §十一统一。超时由 Spring 包装为 I/O 故障，不能仅用 `SocketTimeoutException` 判断：响应头等待与正文被超时关闭时的底层异常类型可能不同。

无论哪种超时，**都不是 Python 取消确认**。Python 可能在 Java 结束等待后继续完成索引写入；长文的多批 embedding 可能超过默认预算，应据实调整 `RAG_READ_TIMEOUT_MS`，不能靠把一篇拆成多次 `/embed` 规避。

客户端不做应用层重试、不跟随 POST 重定向，也不自行调用 DELETE。它不打开数据库事务，不保证调用方没有外层事务；编排层须先提交 chunk 落库，再在事务外调用 `/embed`，最后另开短事务更新状态。§一的失败清理、原始错误记录、同文档串行、reset/rebuild 在途协调仍待编排层实现；不能把一次超时、DELETE 或单进程索引锁当作取消在途任务的保证。

### 验证记录

- TDD：可运行空客户端先得到 **5 项预期断言失败**，基础发送实现后 5 项通过；扩展到 98 项时得到 **86 项断言失败、0 errors**，覆盖待加校验、异常包装和配置接线，不把这个数量当作漏洞数。严格初版 98 项通过后继续处理独立复核发现；最终 **102 项客户端测试全部通过**。
- 复核整改：401/407 的非空错误体回归在旧工厂下确实变成空数组（17 项状态测试中 2 项失败），更换 JDK 工厂后通过，包含认证 challenge。持续分段返回正文的用例先证明旧 socket 超时不会限制整体等待，再验证新工厂的总预算；另用 gzip 反例验证默认自动解压会改写错误字节，关闭后通过。这些新增反例都观察过预期失败。
- 测试使用真实 loopback `HttpServer` 随机端口，不 mock `RestClient`；覆盖 BIGINT 上界、Unicode 原样传输、排序不改输入、整篇大于 64 块、请求反例、严格 JSON/计数、状态/媒体类型、路径前缀、Spring 配置，以及真实连接拒绝、响应头/body 超时和持续分段响应总预算；测试服务、执行线程及测试进程均已退出。
- 在 `server/` 运行 `.\mvnw.cmd '-Dit.test=ChunkRepositoryIT' clean verify`，**238 项单元测试 + 24 项 MySQL 集成测试通过**，无失败、错误或跳过，Jar 打包成功。单测组成：102 项 EmbedClient、88 项 ChunkClient、46 项偏移契约、2 项健康测试；保留既有编译器/JVM 提示，未顺手修改其他测试。
- MySQL 集成测试使用随机 `easyrag_chunk_it_<UUID>` 库，完成后已 DROP 并关闭连接池。未运行直连 local 业务库的 `SchemaMigrationIT`，不要把定向命令换成无筛选 `verify`。
- 在 `rag-service/` 运行 `.\.venv\Scripts\python.exe -m pytest -q`，**190 passed，2 条既有弃用警告**。Python 实现和共享 Unicode 样例未改。

本节记录的 `/embed` 阶段仅新增 `EmbedClient.java`、`EmbedClientTest.java` 并同步 A/B 文档。HTTP 客户端测试与 Python 回归是分层证据，该阶段未完成真实 Java/Python 双进程联调、`/chunk → MySQL → /embed` 编排、失败清理客户端、上传/更新接口、状态机、前端引用或长文/云端验收；没有新的召回分数，不代表 M2 完成。删除索引客户端的后续实现见 §十二，不能把调用原语就绪等同于失败清理编排已接入。

## 十一、/chunk 认证错误保真与传输统一（2026-09-09）

本轮修正 §九遗留的 401/407 错误正文丢失，并把 `/chunk` 的传输行为对齐到 §十的 `/embed`。只修改 `ChunkClient.java`、`ChunkClientTest.java` 和 A/B 两份文档；未修改 `EmbedClient`、配置文件、依赖、`ChunkBatch`、仓库、迁移、Python 或共享 Unicode 样例。

### 修改与行为影响

- `ChunkClient` 构造器改用内置 JDK HTTP 工厂，HTTP/1.1、禁止重定向、关闭自动解压；连接超时仍来自原配置。不通过放宽响应校验、忽略错误正文或自动重发请求来绕过问题。
- 非 200 且 body 完整读完时，状态、响应头和原始 body 字节保持配套；已验证带认证 challenge 的 401/407，以及 gzip 错误体的 Content-Encoding 和编码后字节。读取中断或超时仍按 I/O 失败处理，不假装拿到了完整响应。
- **两个客户端现在统一使用 HTTP 交换总等待预算**：`RAG_READ_TIMEOUT_MS` 默认仍为 60000，覆盖响应头与正文，分段到达不重置计时。原来靠持续小段响应延长总耗时的 `/chunk` 调用，现在会受该预算限制；长文是否需要调大预算，仍需实测，不在本轮修改切片参数。
- `chunk(documentId, text, title)` 签名、请求字段、原文/标题原样发送、严格 JSON 类型与重复/尾随校验、`ChunkBatch.validateAgainst(text)`、UTF-8 字节偏移和 token_count 透传均不变。仍不创建数据库事务、不做应用层重试、不记录正文、不探测启动依赖。
- I/O 异常仍由 Spring 包装并保留 cause。测试不再把旧实现的最底层 `SocketTimeoutException` 形状当作通用契约；JDK 的连接拒绝 cause 还可能继续嵌套其他异常。业务编排应处理传输失败，不能仅靠一种最底层异常类型判断失败。

### 验证证据

- 先在旧实现运行 `ChunkClientTest`：**92 项中 3 项预期断言失败、0 errors**。其中 401/407 的非空正文实际变成空数组；另一项证明旧 socket 超时不会约束持续分段响应的总时间，是超时契约差异的证据，不把它另算成旧框架缺陷。gzip 保真在旧实现下已通过，用于防止迁移引入自动解压回归。
- 替换工厂后 **92 项 ChunkClient 测试全部通过**，比原 88 项新增 4 项；7 组共享 Unicode 样例、类型/媒体类型反例、非法输入、原文覆盖、路径前缀、配置和真实 I/O 失败测试保持通过。全部 HTTP 用例走本机随机端口服务，不 mock `RestClient`。
- 在 `server/` 运行 `.\mvnw.cmd '-Dit.test=ChunkRepositoryIT' clean verify`，**242 项单元测试 + 24 项隔离 MySQL 集成测试通过**，无失败、错误或跳过，Jar 打包成功。单测组成：92 项 ChunkClient、102 项 EmbedClient、46 项偏移契约、2 项健康测试；既有编译器/JVM 提示未作为无关改动处理。
- 集成测试仅使用随机 `easyrag_chunk_it_<UUID>` 库，结束后已 DROP 并关闭连接池。没有运行直连 local 业务库的 `SchemaMigrationIT`，不要改用无筛选 `verify`。
- 在 `rag-service/` 运行 `.\.venv\Scripts\python.exe -m pytest -q`，**190 passed，2 条既有弃用警告**。本机 HTTP 服务及测试线程均已关闭。

### 剩余边界与下一步

总等待预算仍不等于 Python 取消确认：`/chunk` 超时后 Python 可能继续切片计算，`/embed` 超时后可能继续写索引。客户端没有替调用方实现失败记录、清理或同文档在途协调。

该阶段没有真实 Java/Python 双进程联调、完整上传/索引状态机、长文性能或云端验收，没有新的召回分数。当时提出的 Java `DELETE /index/{document_id}` 客户端已由 §十二落实；本节保留 **242 + 24** 的传输整改验证快照，后续删除客户端验证见 §十二，门禁组件进展见 §十四。

## 十二、删除索引客户端与失败清理边界（2026-09-09）

本轮新增 `IndexClient.java`、`IndexClientTest.java` 并同步 A/B 文档，共四个文件。仅补齐删除索引的 HTTP 调用原语；没有修改既有两个客户端、Python、配置、依赖、数据库仓库或 V1/V2 迁移。

### 请求与成功契约

- Spring 组件 `IndexClient.deleteDocument(long documentId)` 返回 `int`。ID 必须是 `1..9223372036854775807`，非正数在发送前拒绝；请求为无正文的 `DELETE /index/{document_id}`，带 `Accept: application/json`，保留配置中的路径前缀，构造组件时不探测 Python。
- 只接受 HTTP **200**、精确的 `application/json` 媒体类型（允许合法参数）和非空 JSON 正文。`removed` 必须是非负 JSON 整数；Java 接受 `0..2147483647`，超出 `int` 范围视为无效响应，不截断或溢出。这个 Java 返回类型约束没有改变 Python 的 `int` 响应模型。
- **`removed=0` 是幂等成功**：文档原本没有索引或重复删除都可返回 0。返回量是 Python 本次实际删除的向量数，不要求与 MySQL 的 `chunk_count` 相等，也不能据此修改 MySQL 中的 chunk 行数。
- 私有 Jackson reader 拒绝缺失/null、负数、数字字符串、小数/指数形式、布尔值、重复键、尾随 token、损坏 JSON 等响应，不改变全局 mapper。非法成功响应统一抛 `RestClientException("Invalid /index response", cause)`。

### 失败与传输契约

- **404 不是删除成功**：Python 对不存在文档的正常返回是 `200 {"removed":0}`，404 可能说明部署路径有误。其他非 200（包括 201/202/204、重定向、401/407、422、503）也不会被解释为成功。
- 非 200 且正文完整读取时，抛 `RestClientResponseException`，保留 HTTP 状态、响应头与原始字节；不先解析损坏的 Content-Type 或假设 `detail` 必为对象。401/407 的认证 challenge、gzip 错误体及 Content-Encoding 均保真。
- 连接拒绝、响应头/body 超时、正文读到一半断开仍按 `ResourceAccessException` 处理并保留 I/O cause；即使已经收到 503 响应头，也不把不完整正文伪装成完整的 HTTP 错误响应。
- 沿用现有 `RAG_BASE_URL`、`RAG_CONNECT_TIMEOUT_MS`、`RAG_READ_TIMEOUT_MS`，默认值仍为 `http://localhost:8000`、3000、60000；两个超时必须为正毫秒。JDK HTTP/1.1、禁止重定向、关闭自动解压、不做应用层重试。read-timeout 与另两个客户端一致，是响应头加正文的 HTTP 交换总等待预算；持续分段到达不重置计时，不是整个方法的硬 deadline。

### 验证证据

- 测试先行：新增测试最初因客户端尚不存在无法编译；补可运行空壳后，**93 项均出现预期断言失败、0 errors**。这是缺失功能的红灯，不是 93 个既有缺陷；完成实现后，定向运行 `IndexClientTest` **93 项全部通过**。
- HTTP 用例使用真实 loopback `HttpServer` 随机端口，不 mock `RestClient`；覆盖 BIGINT 上界、无正文请求、实际删除量和重复删除、严格 JSON/计数、媒体类型、HTTP 错误保真、配置接线、路径前缀、真实连接拒绝与超时。响应头前断开、200/503 正文截断、持续分段响应也有用例，已验证这些失败路径不重复提交 DELETE；本机服务和执行线程随测试关闭。
- 在 `server/` 运行 `.\mvnw.cmd '-Dit.test=ChunkRepositoryIT' clean verify`，**335 项 Java 单元测试 + 24 项隔离 MySQL 集成测试通过**，无失败、错误或跳过，Jar 打包成功。单测组成：93 项 IndexClient、92 项 ChunkClient、102 项 EmbedClient、46 项偏移契约、2 项健康测试。
- 清理构建前核对 `server/target` 为工作区内预期目录且非链接。集成测试只使用随机 `easyrag_chunk_it_<UUID>` 库，结束后已 DROP 并关闭连接池；未运行直连 local 业务库的 `SchemaMigrationIT`，不要改为无筛选 `verify`。
- 在 `rag-service/` 运行 `.\.venv\Scripts\python.exe -m pytest -q`，**190 passed，2 条既有弃用警告**；保留既有 Java 编译器/JVM 提示，没有为消除无关警告扩大改动范围。
- 四文件 UTF-8、空白和控制字符检查通过，Jar 已确认包含 `IndexClient` 及其响应 record。本轮独立只读审查两次因模型服务不可用中止，未取得独立审查结论；已做本地契约逐项复核，并明确断言认证 challenge 头部保真，不能将这记作独立审查通过。

### 已完成与剩余边界

现在 Java 已有 `/chunk`、`/embed`、`DELETE /index/{document_id}` 三个独立 HTTP 客户端，但没有自动触发失败清理、记录业务失败、更新状态、排队或协调同文档在途任务，也没有打开数据库事务。后续编排仍须按 §一.3 保留原始失败并尽力清理一次，清理异常不能覆盖原始错误；已落库 chunks 的保留策略不变。

**DELETE 成功不是取消确认，也不是索引持续为空的保证**：在途 `/embed` 可能随后写回向量；DELETE 自身超时或断开时也可能已执行或仍在执行，不能断言什么都没删。Python 的进程内写锁不保证不同 HTTP 请求按 Java 期望排序，后续仍须解决同文档排序及 reset/rebuild 的在途协调。

下一阶段建议先明确并实现已有文档的最小索引编排与状态/并发契约，再接上传、更新和手动重索引入口。本轮没有真实 Java/Python 双进程联调、完整状态机、长文/云端验收或新召回分数；上述分层测试不代表子 Issue A/B 或 M2 整体验收完成。

## 十三、M2 轻量编排与体验约束（2026-09-09，已确认、业务接入待实现）

用户选择“完整工作流串行 + 索引变更结果不明时暂停 + 维护恢复”，暂不采用持久化代次/写入栅栏，也不改为不可变索引加生成前 MySQL 校验。这个取舍以单实例、低频收录为前提，不承诺多实例、超时后自动继续或在线无停顿重建。保留既有四态、整篇 `/embed`、失败尽力 DELETE 一次、UTF-8 定位及 MySQL 唯一真相源契约；本节不是实现完成记录。

### 执行与恢复边界

- 全局业务门禁覆盖已有资料的正文/删除变更、切片落库、Python 索引调用、业务终态及失败清理。排队中的更新不能提前修改当前正文；不能在收到 Python 响应后、业务状态尚未提交时先释放门禁。数据库事务不跨模型计算或 HTTP 等待。
- 新增资料可以先保存原文并返回 PENDING，再等待索引执行；PENDING 只表示已收录，不表示已经可用于回答。列表、资料详情和状态查看不因 RAG 门禁关闭而一并停用。
- 问答与索引变更不重叠：多个纯只读问答可以共享，全部已有问答结束后才能开始变更；变更占用或维护期间，新问答明确返回忙碌/需恢复，不让 HTTP 请求无限排队，也不调用 LLM 后再丢弃结果。暂不新增问答任务队列或自动重试调度器；忙碌不是“库里没有”。

| 失败情形 | 后续是否允许继续 |
|---|---|
| `/chunk` 纯计算失败，尚未发送 `/embed`，随后独立 DELETE 成功且 FAILED 落库 | 可以；切片调用本身不写索引，不因它尚在计算就升级为全库维护 |
| `/embed` 已明确结束并失败，随后 DELETE 成功且 FAILED 落库 | 可以；保留原始错误和 chunks，允许手动整篇重试 |
| `/embed` 超时/断连等导致写入结果不明，即使随后 DELETE 成功 | 不可以；旧任务仍可能写回，保持全局暂停 |
| DELETE/reset 结果不明、清理未成功，或无法确认 MySQL 业务终态 | 不可以；保留可取得的故障信息，进入维护恢复，不能仅因文档状态为 FAILED 就放行 |

启动默认关闭服务门禁；遗留 INDEXING/FAILED 不构成安全重试的证明。对未确认的索引变更，恢复前须确认旧 Java 编排执行者与 Python 写入进程均已退出，防止旧 embed 或清理请求在恢复后到达。之后才允许在维护门禁内按 MySQL 修复/重建并验证，再开放服务。复用 chunks 须确认其对应当前原文、tokenizer 与切片参数；无法证明时先重切，不能把更新失败后遗留的旧 chunks 重新推送就算恢复成功。等待固定秒数、health 为绿、一次 DELETE 成功或单纯计数相等，都不是旧任务已结束的证明。

维护恢复优先考虑能够证明的受影响范围，但**未验证局部恢复流程前仍采用保守的全量重建**；范围不明或 reset 中断不能猜测只修一篇。正常单篇收录/重试不触发全量 reset，明确失败且清理完成也不自动升级为全库重算。启动默认不就绪不等于已经实现了快速启动检查或低成本恢复。

### 用户体验与运行成本

- 正常批量收录按“每篇完整流程”占用并释放门禁，不把整个待处理队列包在一次占用中；这不保证问答一定能抢到空档，连续忙碌时长仍须实测。全量维护重建不同，必须验证完成后统一开放，不能中途暴露半成品库。
- 用现有状态区分“已收录待处理”“索引中”“已生效”“失败/需恢复”；索引完成的反馈以 INDEXED 已落库且服务门禁允许查询为准，不用上传成功冒充同步完成。不承诺未经测量的完成倒计时。
- 首轮先记录阶段耗时和调用数量，不引入新的监控平台、缓存层或复杂优先级调度。记录标识、模型配置、阶段和结果，不记录原文、向量或 API 密钥。

| 验收维度 | 必须报告的口径 |
|---|---|
| 正常问答 | 可用时的端到端回答耗时；拆分接入等待、检索与生成，不能只测向量查询 |
| 索引期间问答 | 忙碌反馈耗时、忙碌/维护请求占比、最长连续不可提问窗口；不能只统计成功请求来掩盖阻塞 |
| 内容生效 | 新增收录返回 PENDING 到 INDEXED 落库并可查询的总时间，包含排队、切片、落库和 embedding；更新/删除入口接入后再补对应同步时间 |
| 故障恢复 | 从暂停到重新可查询的全程，包括人工介入/等待、旧进程退出确认、清理/重建与验证，而非只测 reset |
| 模型成本 | embedding 批次/请求数、问答模型调用数、重复计算量；上游提供用量时记录实际用量，否则标明未取得，不把本地 token 估算当作计费数据 |

已有 [离线基线](eval/retrieval-baseline-v1.md) 的一次历史记录为：29 篇、222 chunks，文档 embedding **26.616 秒**，总计 **29.923 秒**；问题 embedding 为批量处理，未调用生成 LLM，也不含 Java 编排、业务排队与故障恢复。这只能说明该样本上的离线成本，不能作为单次问答延迟、在线同步时间或服务承诺。

真实链路接入后，先用仓库样例测单篇收录、连续收录、空闲问答、索引期间提问和一次受控维护恢复；说明语料、模型/硬件、冷暖启动、样本数及调用量。有足够重复样本时报告 p50/p95/最大值，样本不足时报告逐次结果，不制造稳定分位数结论。由用户根据实测确认可接受预算，才完成体验验收；当前不臆定秒级 SLA，也不把尚未测量写成“性能可接受”。

### 实施顺序与优化门槛

先实现共享门禁及确定性时序测试，再接现有文档的状态与三个 HTTP 客户端编排，最后接上传/问答入口并采集真实耗时。门禁原语见 §十四，现有文档状态落库原语见 §十五，同步单篇 HTTP 编排见 §十六；下一检查点是隔离环境的真实链路联调，首次就绪/维护恢复及业务入口仍待实现。门禁测试须覆盖晚到 embed、晚到 DELETE、异常退出和启动默认关闭；清理成功不得误解为取消成功。每批默认不超过三个文件，超出先列范围确认。

若实测成本可接受，保持第一条路线，不提前升级；若问答不可用窗口、内容生效或恢复成本不可接受，先定位排队/模型/重复计算的主要开销，评估现有参数与不必要重建，再决定是否需要更细调度或改变隔离方案。不得通过提前释放安全门禁、把一篇拆成多次 `/embed`、缩短 HTTP 超时后直接继续，换取表面上的响应更快。

## 十四、业务门禁原语第一批（2026-09-10）

本批执行 §十三 的“共享门禁及确定性时序测试”，只涉及 `server/src/main/java/com/easyrag/server/rag/RagOperationGate.java`、对应 `RagOperationGateTest.java` 和本文档。不接入已有 HTTP 客户端、业务接口、数据库状态机或恢复执行器，不修改 B 的底层索引契约。

### 接口与安全边界

| 接口/边界 | 本批要求 |
|---|---|
| `state()` | 启动为 RECOVERY_REQUIRED；暴露当前进程内状态，不是 MySQL 文档状态，也不是执行许可，不能凭 READY 跳过获取 Lease |
| `tryAcquire(Operation)` | 返回同一临界区内的状态快照和可选 Lease；冲突直接拒绝，不等待持有者完成，不新增任务队列 |
| QUERY | 多个纯只读问答可以共享；最后一个 Lease 结束才允许写入，异常退出不把索引标为不确定；不能用此许可执行写工具 |
| MUTATION / RECOVERY | 独占；READY 允许写入或维护，RECOVERY_REQUIRED 只允许进入维护准备；不能越过已有问答/写入 |
| `Lease.confirmCompletion()` | 调用方确认完整业务终态后才调用；首次有效完成返回 true，已经关闭/完成的旧句柄返回 false，不能释放后继任务 |
| `Lease.close()` | 查询释放自身读取占用；写入/恢复未经确认就退出则保持 RECOVERY_REQUIRED；重复关闭不改变后继状态 |

`confirmCompletion()` 是调用方对结果已确认的声明，不会替调用方验证 Python 已退出、清理成功或 MySQL 已提交。恢复许可只允许进入维护准备，不能据此立即 reset 或开放业务；旧执行者退出确认、修复/重建与验证仍须由后续编排完成。Lease 是进程内所有权凭据，不是跨 HTTP 代次、取消令牌或持久化写入栅栏。

实现只用短同步区维护读取 Lease 集合、独占 Lease 和状态，不在业务执行期间持有 Java 监视器，不新增线程池、网络/模型调用、数据库表、定时解锁或持久化协调状态。Spring 扫描注册为一个默认关闭的单例；创建新实例仍从 RECOVERY_REQUIRED 开始，不继承另一实例的就绪状态。

### 验证结果与剩余边界

- **测试先行**：可编译空壳得到 **48 项预期断言失败、0 errors**；实现后原 48 项通过，审查补充共享读取的有效确认路径后，定向 **49 项全部通过**。命令为 `./mvnw.cmd -o -Dtest=RagOperationGateTest test`，在 `server/` 执行。
- **本轮回归**：`./mvnw.cmd -o test` 得到 **384 项单测通过，0 failures / errors / skipped**，即原有 335 项加门禁 49 项。没有运行 `verify`、MySQL 集成测试、Python 测试或模型调用，没有重新打包 Jar；原有阶段的 335 + 24、190 passed 等记录保留为历史证据。
- **具体覆盖**：启动关闭、允许维护准备、只读查询共享、最后一个查询释放、写入/恢复独占、异常退出保持暂停、重复和旧句柄不释放后继任务、迟到结果不结束维护，以及 Spring 单例扫描。受控线程测试覆盖 8 个同时争用者只能有一个独占者、8 个查询同时持有读取许可、忙碌判断在原业务尚未结束时已返回。
- **证据边界**：线程测试用 CountDownLatch 固定次序，5 秒等待仅作死锁保护，不是性能预算；这是门禁内的所有权时序，不是 Java/Python 双进程取消或真实索引清理测试。查询被定义为纯只读，查询失败退出本身不会触发索引维护。
- **独立复核**：只读代码/时序审查未发现阻断项，提出饥饿风险披露和共享读取有效确认路径两项非阻断建议；已补充说明，并将读取退出按 close/confirm 参数化验证。审查者未运行测试，测试证据来自本轮实际执行。
- **运行成本边界**：当前不建立公平队列或记住写入等待意图；拒绝不保留优先权，也不阻止后续查询，持续查询可能让写入或维护长期得不到许可，连续收录也可能让问答长期忙碌。后续编排不能靠忙等改善这些指标，须分别测 PENDING 排队时间与问答不可用窗口，再判断是否值得增加有限调度；本批不声称已满足同步时间或问答效率目标。

## 十五、文档索引状态仓库（2026-09-10）

本批接续 §十三 的“现有文档状态与客户端编排”，先补齐数据库边界，不把持久化、HTTP 编排和入口混在一次改动里。仅新增 `server/src/main/java/com/easyrag/server/document/DocumentIndexRepository.java`、`server/src/test/java/com/easyrag/server/document/DocumentIndexRepositoryIT.java`，并修改本文档；复用既有 `ChunkRepository`、四态与 V1/V2，不新增表、迁移、依赖或后台执行者。

### 目标与接口契约

目标是让已有 PENDING 文档可以被后续编排读取，并确保“整篇 chunks 已落库”和 INDEXING 同时提交；本批不实现新增资料、手动重排队、HTTP 调用或门禁接入。实现采用 Spring JDBC 与短事务，Python 调用不得位于这些事务内。

| 接口 | 本批契约 |
|---|---|
| `findPending(documentId)` | 只读取未删除的 PENDING 文档，返回原样 content/title 和不可变 tags 快照；不存在、已删除或其他状态返回空，不擅自把 FAILED/INDEXING 重排队。SQL NULL / JSON null 标签按空列表处理，其他非字符串数组拒绝，不把数字等强转成标签 |
| `saveChunksAndMarkIndexing(document, batch)` | 在 READ_COMMITTED 短事务中条件更新 PENDING → INDEXING、清除旧 index_error，并调用既有 `ChunkRepository.replace` 校验原文及替换完整 chunks；任一步失败整体回滚，不提前暴露 INDEXING 或新 chunk_count |
| `markIndexed(documentId)` | 只允许未删除的 INDEXING → INDEXED，同时清除 index_error、设置 indexed_at；不修改正文、updated_at 或 chunk_count。调用方负责事先验证 `/embed` 成功及计数 |
| `markFailed(documentId, error)` | 只允许未删除的 PENDING/INDEXING → FAILED，保留 chunks、chunk_count、正文及历史 indexed_at；失败原因必须非空白、合法 UTF-8 且不超过 1024 个 Unicode 码点，不静默截断 |

所有写入必须确认恰好更新一行；重复结束、错误前态、文档已删除/不存在均显式失败，不能把零行更新当成功。INDEXING/FAILED 保留的 indexed_at 仅是历史成功时间，不能证明当前内容已生效。错误摘要由后续编排在字段上限内分别保留原始失败与清理结果，不把原始响应正文、原文或密钥直接写入日志。

仓库不获取/释放业务门禁，也没有持久化代次；状态条件不能识别跨轮次迟到执行者，不能代替 §十三 的单执行者与维护恢复。`markIndexed` 不是 Python 终止证明，`markFailed` 也不是索引清理证明。调用方仍须先取得变更许可，完整流程不包数据库事务；仓库参与已有事务时，方法返回不等于外层已提交，禁止提前确认 Lease。

### 实施步骤与验证结果

1. 编写可编译空壳与隔离 MySQL 反例 → 验证：**55 项中 50 项预期断言失败、0 errors**；另 5 项是空壳也满足的“不读取非 PENDING/已删/不存在文档”筛选用例，不把它们计为失败证据。
2. 实现最小 JDBC 仓库 → 验证：在 `server/` 执行 `./mvnw.cmd -o -Dit.test=DocumentIndexRepositoryIT test-compile failsafe:integration-test failsafe:verify`，**55 项全部通过**。覆盖快照只读/不可变、标签类型、状态前置条件、零行更新拒绝、原文变更、插入/状态 SQL 故障的原子回滚、历史字段保留、错误长度与 emoji、外层事务回滚、错误隔离级别及两个并发 PENDING 尝试仅一份提交。
3. 回归并记录结果 → 验证：在 `server/` 执行 `./mvnw.cmd -o '-Dit.test=DocumentIndexRepositoryIT,ChunkRepositoryIT' verify`，**384 项单元测试 + 79 项隔离 MySQL 集成测试通过，0 failures / errors / skipped**；79 项为既有切片仓库 24 项和本批状态仓库 55 项，Jar 打包成功。没有执行无筛选 `verify` 或直连 local 业务库的 `SchemaMigrationIT`。

测试只在本轮随机 `easyrag_index_it_<UUID>` / `easyrag_chunk_it_<UUID>` 库执行 V1/V2、插入合成资料和注入触发器故障；每次清理前核对实际库名与固定前缀规则。已确认最终回归的两个测试库均 DROP、Hikari 连接池均关闭。并发测试用 CountDownLatch 对齐启动，5 秒等待仅作死锁保护，不是性能 SLA。原有历史测试快照保持不变。

独立只读审查核对了事务传播、实际隔离级别、条件更新、字段保护和文档边界，未发现 Critical / Important 或需要本批修正的 Minor 问题，可作为独立持久化原语交付。审查者未运行测试，测试与清理证据均来自本轮主执行流程。

### 剩余边界与下一步

本批没有运行 Python 测试、真实模型调用或 Java/Python 双进程联调；SQL 故障/外层回滚测试不等于已经验证数据库提交响应丢失，也不证明跨 HTTP 的旧任务已停止。数据库往返和短事务是本批新增成本，没有新增模型调用；测试套件耗时不能当作内容同步或问答性能证据。

本节提出的三个 HTTP 客户端与门禁接入、一次失败清理、结果不明时暂停、终态返回后释放门禁及 Java 阶段记录，已由 §十六 的同步原语落实并分层测试。启动恢复、手动重排队、上传/问答入口、真实链路和耗时验收仍未完成，不因状态仓库已就绪而自动开放服务。

业务入口、物理在途任务的结束证明、故障恢复、问答忙碌率/内容生效时间和模型成本均不由这些原语测试覆盖。三个 HTTP 客户端仍可独立调用，只有经 §十六 同步服务的执行才受其门禁保护；不得绕过编排调用客户端，也不能仅因 Lease 或编排测试通过就勾选完整业务验收。

## 十六、已有 PENDING 文档的最小索引编排（2026-09-10）

本批仅新增 `server/src/main/java/com/easyrag/server/document/DocumentIndexingService.java`、`server/src/test/java/com/easyrag/server/document/DocumentIndexingServiceTest.java`，并更新本文档。复用现有门禁、状态仓库和三个 HTTP 客户端，不新增迁移、依赖、配置、线程池、自动重试或业务入口。

### 执行契约

- `index(documentId)` 是同步单篇执行原语，不是 `@Async` 调度器；正整数 ID 和“当前线程无活动数据库事务”在获取门禁、访问数据库/HTTP 前检查。后续单执行者可调用它，但本批不接队列、上传、重排队或问答入口。
- 获取 MUTATION 许可后读取未删除的 PENDING 文档；拒绝准入返回 BUSY，不查询数据库、不调用 Python。非 PENDING/不存在/已删除返回 SKIPPED，释放本次无副作用占用，不清理别的状态的索引。
- **资格读取失败还没有进入索引工作流**：不能证明该 ID 是待处理文档，因此不发送 DELETE、不盲写 FAILED；返回故障并保守保持维护暂停，留下安全的阶段错误信息。这是“确认 PENDING 后，任一步失败尽力清理一次”的前置边界，不把任意调用 ID 当作删除许可。
- 确认 PENDING 后，按 `/chunk` → chunks 与 INDEXING 同事务落库 → 整篇 `/embed` → INDEXED 落库执行；业务终态提交返回前一直持有门禁。禁止跨 HTTP 持有数据库事务，不在本服务内部重新实现切片、HTTP 重试或索引计数协议。
- 工作流失败时，独立尽力 DELETE **一次**，随后尽力记录 FAILED；清理或失败记录的异常不得掩盖最初失败。`/chunk`/切片落库失败未发送 embed；如果 DELETE 与 FAILED 均确认，可以释放门禁。`/embed` 只在完整、严格匹配当前 Python 错误协议的响应下视为明确结束，清理与落库确认后可以继续；不凭任意 4xx/5xx 放行。
- `/embed` 超时、断连、未知/无效响应，即使 DELETE 成功仍保持暂停；DELETE 失败、FAILED 落库失败或 INDEXED 提交出错也保持暂停，不因后续记录成功就替此前未确认提交背书。启动恢复与旧执行者退出证明仍不在本批实现。

明确结束的 `/embed` 错误只识别下列当前协议，不把任意“收到错误响应”解释成取消确认：

| HTTP | `detail.error` | 必需字段 |
|---|---|---|
| 409 | CHUNK_ID_CONFLICT | 不附其他 detail 字段 |
| 503 | INDEX_UNAVAILABLE / EMBEDDING_UNAVAILABLE | cause 为 ASCII 异常类型标识符 |
| 503 | INDEX_WRITE_FAILED | cause 为类型标识符，cleanup_error 为 null 或类型标识符 |

还必须满足 `application/json`、无 Content-Encoding 或仅 identity、顶层只含 detail 对象、字段集合匹配且无重复键/尾随 JSON。未知错误、额外字段、压缩错误体或不完整形状均保守暂停；后续 Python 错误协议扩展须同步更新分类与测试，不能猜测新响应的完成语义。

`IndexingResult` 返回 INDEXED / SKIPPED / BUSY / FAILED、当前执行是否要求恢复、安全错误摘要和不可变阶段耗时记录；它不是获取问答许可的替代品，也不承诺返回之后系统不会被下一篇变更占用。错误摘要仅保留阶段、异常类型、HTTP 状态及符合协议的 Python error/cause/cleanup_error，不保存原始响应、正文、向量、异常 message 或认证信息。原始失败、独立清理、失败落库分别预留 400/300/200 码点摘要空间，超长标识符/摘要用可见省略号收尾，不让一项长错误挤掉另一项；连同阶段和分隔符保持在现有 1024 码点上限内。

### 检查点与进度

1. **范围与失败分类已完成** → 验证：三文件边界；资格未确认不误删，已确认工作流普通失败清理一次；未知 embed 结果不放行。致命 Error 会向外传播，未确认的 Lease 仍在退出时关闭为维护状态，不承诺此时能完成清理/记录。
2. **测试先行与正常/失败流程已完成** → 验证：可编译空壳的原 **50 项全部预期断言失败、0 errors**。首轮实现唯一失败实证为测试的 IntNode/LongNode 比较差异，请求内容相同，仅修改按线上 JSON 表示比较的测试对照，没有放宽业务协议。追加迟到 DELETE 和额外协议字段反例后，在 `server/` 执行 `./mvnw.cmd -o -Dtest=DocumentIndexingServiceTest test`，**53 项全部通过**，含超过 64 chunks 仍只发一次 embed。
3. **受控时序与事务边界已完成** → 验证：7 个阶段逐一阻塞时仍拒绝问答和第二篇执行；真实 Java 客户端超时后，受控 HTTP handler 的 embed 或 DELETE 迟到完成仍不能解除维护。活动数据库事务在任何副作用前拒绝；测试用屏障固定次序，250ms HTTP 预算和 5 秒等待只是测试配置，不是线上 SLA。
4. **Java 回归与构建已完成** → 验证：在 `server/` 执行 `./mvnw.cmd -o '-Dit.test=DocumentIndexRepositoryIT,ChunkRepositoryIT' verify`，**437 项单测 + 79 项隔离 MySQL 集成测试通过，0 failures / errors / skipped**，Jar 打包成功。437 为原 384 加本批 53；79 为切片仓库 24 和状态仓库 55。两个随机测试库均 DROP、Hikari 连接池关闭，没有运行无筛选 `verify` 或业务库 `SchemaMigrationIT`。历史各节的验证数字保持不变。

独立只读复核在已读同步实现中未发现已证实的 Critical / Important / Minor 问题，可作为本批同步原语交付。其范围不覆盖逐项测试审读、IndexClient 或 Python 路由，不能称为完整跨服务协议审计；测试证据来自主执行流程，Python 当前错误返回由主执行对照源码，真实链路验证仍待下一检查点。

### 运行成本口径与剩余工作

正常单篇只调用一次 chunk、一次整篇 embed；已确认 PENDING 的普通失败路径额外尽力调用一次 DELETE，不增加 health 探测、自动 HTTP 重试或模型调用。阶段记录在本次调用返回时提供，用于统计 Java 调用数量/耗时，不是实时任务百分比，也不含外部排队时间、Python 内部 embedding 批次、真实模型配置/计费量、问答不可用窗口或人工恢复时间；本批不新增探测调用来填补这些未知项。

本批编排测试使用真实三个 HTTP 客户端、随机本机端口服务和模拟仓库；真实仓库由独立隔离 MySQL 测试覆盖，**两者不能合并宣称为真实 Java/Python/MySQL 同链路已打通**。未运行 Python 测试、真实模型调用、启动恢复或用户端收录/问答；没有新召回成绩和在线性能结论。

下一检查点优先用隔离库/隔离索引和一篇合成资料完成真实链路联调，暴露实际跨服务问题，再推进可操作的首次安全就绪/维护流程、单执行者与业务入口。启动默认关闭不变，不能把测试夹具里直接确认恢复 Lease 的做法当作生产恢复实现；M2 整体仍按顶部进度表保持未完成。

## 十七、真实索引链路联调检查点（2026-09-10）

### 本批范围与隔离

只新增 `DocumentIndexingIT`、`rag-service/tests/indexing_integration_server.py` 并更新本文，共三个文件；不修改生产服务、数据库迁移、业务入口或恢复契约。每个用例独立启动真实 Spring 上下文和单进程 Uvicorn，使用随机 `easyrag_chain_it_<32位随机值>` MySQL 库及 `server/target/indexing-it-<同一随机值>/chroma`。Spring 启动参数同时覆盖 datasource/Flyway URL、Flyway 凭据引用和 `rag.base-url`，不回落到业务库或默认 8000 端口；Python 启动夹具拒绝非测试目录或非空目录，不读取 `.env`，显式指定所有索引/切片设置。

真实运行范围为 Spring 管理的编排器、门禁、事务仓库和三个 HTTP 客户端，以及 Python 原有 `/chunk`、`/embed`、`DELETE /index/{id}`、Chroma 和本地 Ollama `bge-m3`（1024 维）。沿用本地 bge-m3 tokenizer、512/64 token 切片预算和 64 条内部批量；语料只有合成的中文、emoji、组合字符与 CRLF，没有读取个人知识库。测试代理只向 `127.0.0.1:11434` 转发模型请求，不调用付费云端 API，不用假向量代替成功结果。

测试控制/观测路由只由独立测试启动文件注册，生产 `python -m app` 不加载它。控制端点用于注入模型 503、阻塞旧请求、释放旧请求和退出；观测端点读取真实 Chroma，验证 ID、embedding 输入正文、metadata 和有限非零的 1024 维向量。启动阶段的健康检查和观测轮询属于测试成本，不是生产编排器新增的调用。

测试夹具先核对新进程身份、空索引和空测试库，再确认自己新建门禁的 RECOVERY Lease。这只是建立测试前置条件，**没有实现生产首次就绪或维护恢复**。最终四个用例的收尾均先终止自有 Python 写入进程及 Java 执行者，再 DROP 自有数据库、核对 schema 已消失，并在校验绝对路径后删除自有索引目录；不会清空业务索引。初始化中断或 JVM 被强杀后的自动资源回收不在这四项验收范围内。

### 检查目标与执行记录

1. **成功索引**：MySQL 为 INDEXED，原文/标签/业务时间不变；切片覆盖原文全部 UTF-8 字节，未规范化 emoji、组合字符与 CRLF；Chroma ID、正文、标题路径与标签逐项对齐，门禁回到 READY，重复调用为 SKIPPED 且没有新 HTTP 副作用。
2. **明确结束的 embedding 失败**：先真实索引目标文档与邻居，再由测试 SQL 将目标重置为 PENDING（仅准备前置条件，不代表重排队入口已实现）；上游模型 503 经真实 Python 转为 EMBEDDING_UNAVAILABLE，Java 独立清掉目标旧向量并标 FAILED，保留新 chunks 和历史 indexed_at，邻居向量不受影响，门禁允许后续任务。
3. **Java 超时后迟到写入**：模型请求用事件屏障暂挂，另一数据库连接能读取已提交的 INDEXING/chunks；3 秒测试 HTTP 预算到期后独立 DELETE 已确认、MySQL 已 FAILED，旧请求仍未结束。释放屏障后真实 Python 写回了 3 条向量，但门禁保持 RECOVERY_REQUIRED，QUERY 许可和下一篇索引均被拒绝。该例验证暂停策略，不承诺把超时变为取消，也不把残留向量直接算成可查询状态；实际问答 REST 入口仍未实现。
4. **夹具关闭失败**：单独注入关闭接口 503，仍须终止自有进程、等待 Java 执行者退出、DROP 测试库并移除测试索引；不能因为清理请求本身失败而跳过其余收尾。

首轮 Java 编译、真实三服务启动和成功索引主体断言已通过，但三个用例整体失败：测试控制客户端默认尝试 HTTP/2 升级，Uvicorn 记录 `Unsupported upgrade request` / `Invalid HTTP request received`，POST 控制/关闭请求返回 400；关闭请求断言失败还会跳过后续清理。只修测试客户端为与生产客户端一致的 HTTP/1.1，并让退出失败仍执行进程终止，追加第 4 项反例。第二轮三项通过，超时用例的轮询错误地把尚未完成的 null 状态转成整数，被 Jackson 3 拒绝；修正测试等待条件后，迟到写入用例单独通过，最终四项及完整回归全部通过。上述调整均在测试夹具内，生产实现未改变。

首轮失败留下三组隔离资源，手工清理命令被执行环境策略拒绝、未执行，当前保留待显式处置。最终复核仍仅有这三个历史测试目录，未发现仍运行的测试 Python 进程；不能把旧资源保留与后续用例自动清理成功混为一谈。三个随机值为 `41a3901eaca641a5b3f8a7e05d75b6e3`、`f234aee37ffd4875aa5b981225d9871d`、`9555eca65a324dfcad2f6d90c0818d35`，对应上述测试库名和目录，不含业务数据。最终 Java 回归新建的 4 组链路测试库/索引及另外 2 个仓库测试库均已自动清理，Hikari 连接池全部关闭。

### 验证方式与口径

依赖本地 MySQL、Python `.venv` 及本地 tokenizer、Ollama 已安装的 bge-m3。IT 标记 `requires-mysql` / `requires-ollama`，不会进入普通 `mvn test`；不因依赖缺失而静默跳过。不运行无筛选 `verify`，避免连到业务库的 `SchemaMigrationIT`。

在 `server/` 单独验证本批：`./mvnw.cmd '-Dit.test=DocumentIndexingIT' test-compile failsafe:integration-test failsafe:verify`。最终 Java 回归执行 `./mvnw.cmd '-Dit.test=DocumentIndexingIT,DocumentIndexRepositoryIT,ChunkRepositoryIT' verify`，**437 项单测 + 83 项隔离集成测试通过，0 failures / errors / skipped，Jar 构建成功**，完成于 **2026-09-10 23:16:29 +08:00**。83 为原仓库 79 加本批 4。四项链路 IT 合计 74.962 秒，包含四次服务/数据库启动、模型加载、故障注入等待与收尾，不是单篇业务延迟。运行日志为 `server/target/document-indexing-chain-regression.log`。

在 `rag-service/` 执行 `.venv/Scripts/python.exe -m pytest -q`，**190 项通过，2 条第三方弃用警告**（Starlette / Chroma），耗时 53.28 秒，没有为本批修改依赖。日志为 `server/target/document-indexing-python-regression.log`；这些回归测试不是新的黄金集召回评估。

独立审查尝试因模型服务限流中断，未获得审查结论；本批仅记录主执行的隔离/生命周期/协议范围自检及上述实际运行证据，不宣称独立审查通过。

成本记录区分 Java 同步调用总耗时、各阶段耗时、模型真实请求数、Ollama 返回的计算/加载耗时与 `prompt_eval_count`。当前不是问答生成测试，没有新召回得分，也没有长文档、排队、公平调度或人工恢复时长的验收。模型代理的额外本机 HTTP 开销、启动冷暖差异及故障注入等待不得冒充生产 SLA；本地调用没有云 API 账单，但仍消耗本机计算资源。

最终同一次回归的实测样本：

| 样本 | 结果 | 解释边界 |
|---|---|---|
| 正常合成文档，3 chunks | Java 单篇 4.0301 秒 | LOAD_DOCUMENT 0.0024、CHUNK 1.9546、SAVE_CHUNKS 0.0298、EMBED 2.0290、MARK_INDEXED 0.0142 秒；包含该 Python 进程首次加载 tokenizer |
| 正常文档的真实模型调用 | Ollama total 1.7409 秒、load 0.6598 秒、516 tokens | 一次模型请求处理整篇 3 chunks；不是收费 token 账单 |
| 已明确结束的模型失败 | Java 0.1778 秒 | 该次为注入 503，没有调用真实模型；此前准备目标/邻居各有一次真实模型调用，不算在 0.1778 秒内 |
| 超时与迟到写入 | Java 先在 5.2730 秒返回；释放后模型 total 13.6492 秒，其中 load 11.1961 秒 | Java 耗时包含人为 3 秒 HTTP 等待；晚到模型耗时不包含在返回耗时内，期间始终没有重新开放门禁 |
| 全部四项链路测试的模型用量 | 4 次真实 embedding 请求，累计 2064 个 Ollama 报告的 prompt tokens | 包括失败测试的索引前置条件；不含健康探测、注入失败或历史调试重跑，LLM 生成调用为 0 |

此样本已提示**首次 tokenizer 加载与模型加载是当前值得继续测量的等待来源**，不能只看后续暖调用就认定体验已达标；也没有证据需要为这几篇资料增加并发写入、队列或版本代际机制。本批不做性能优化。下一阶段优先把首次安全就绪/维护恢复变成可操作流程，再接单执行者与最小收录入口；后续用真实入口分别测冷/暖启动和问答忙碌窗口，依据用户可接受的等待时间决定是否预加载或保持模型常驻。M2 整体仍未完成。
