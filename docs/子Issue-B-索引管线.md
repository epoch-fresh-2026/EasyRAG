# 子 Issue B：索引管线模块

本文供维护切片、embedding 和检索算法的开发者使用。阅读后应能正确接入整篇索引替换、解释默认检索策略，并找到每项取舍的验证依据。总体边界见[架构设计](架构设计.md)，原文与切片的数据契约见[资料管理](子Issue-A-资料管理.md)。

## 一、职责

索引模块 `retrieval` 独占切片算法、tokenizer、embedding 协议、Chroma 持久化索引和内存 BM25。它只接受数据快照，不读取 MySQL、不解释文档状态、不包含 FastAPI，也不调用 A/C/F/G。

MySQL 的原文和切片是业务真相源；向量和词法索引都是派生数据。G 将 A 的切片转成 B 的输入，负责完整工作流、业务门禁和恢复；C 通过 G 注入的检索端口使用搜索结果。Chroma 客户端、锁和模型传输对象留在 B 内部。

## 二、当前行为

### 标题感知切片

```text
完整原文
  → 按一至三级 Markdown ATX 标题划分小节，记录 heading_path
  → 超容量的小节递归二分：优先段落边界，再句子边界，最后 Unicode 码点硬切
  → 相邻小块在不超容量时合并，跨小节只保留共同标题路径
  → 精确原文子串 + UTF-8 字节区间
```

解析器识别反引号和波浪线代码栅栏，结束栅栏须同种字符且长度不短于开始栅栏。栅栏内的标题、空行和标点不当作结构边界；代码块本身过长时允许硬切。技术资料中的代码注释经常含 `#`，忽略栅栏会把代码误切成章节。

默认 `max_tokens=512`、`min_tokens=64`。**上限约束完整 embedding 输入：正文、非空标题路径和模型特殊 token**，不是正文单独的预算；min 是合并目标，不能为了满足下限突破上限。正文另有 65,535 个 UTF-8 字节的存储上限，拆分和合并都检查这两种容量。

`heading_path` 取完整标题路径的前 512 个 Unicode 码点，再参与 token 计数；原文中的标题不截断。若单个正文字符加路径和特殊 token 仍放不进预算，明确失败，不按 token 数继续裁短标题。`split(content, title)` 的文档标题参数用于输入校验，不拼到正文或替代 Markdown 标题路径。

B 不产生纯空白切片，首尾或片间可有纯空白间隙，所有非空白字符必须覆盖。每片正文都等于原文在 `[byte_start, byte_end)` 的 UTF-8 解码结果。偏移在切分时从原文位置映射，不通过字符串查找补算，重复段落也不会误定位；直接拼接切片不保证恢复被跳过的空白。

### Tokenizer 与 embedding

运行时使用本地 Hugging Face tokenizer JSON，禁用 truncation/padding，计数包含特殊 token；不回退成字符计数。默认 tokenizer 固定到 BGE-M3 的 `5617a9f61b028005a4858fdac845db406aefb181` revision。启动准备可下载缺失的默认文件；自定义 tokenizer 由部署者提供。切片加载后按路径缓存，同路径替换文件需要重启。

默认 embedding 为 Ollama 的 `bge-m3`、1,024 维。B 通过原生 HTTP 调 Ollama `/api/embed`，显式 `truncate=false`；`openai` 和 `deepseek` 配置值均使用 OpenAI 兼容 embedding 协议。兼容分支的存在不代表对应厂商必然提供 embedding 服务。

完整索引输入为 `text + "\n" + heading_path`，路径为空时仅用正文。内部按 `EMBED_BATCH_SIZE` 分批调用，先生成并校验所有向量：数量和顺序对应输入、维度等于配置、转换为 float32 后仍为有限数值。全部通过前不清旧、不写新；任一模型批次失败则保留旧向量。单次 HTTP 超时默认 60 秒，无隐式重试，超时不是整篇文档的总截止时间。

模型或维度变动必须显式重建。collection 名包含规范化模型名和维度，metadata 记录同样信息；已存在的模型或维度标记与配置冲突时索引不可用。切换 tokenizer 或切片参数还需要从原文重切，不能仅把旧切片重新 embed。当前不自动证明 tokenizer 与模型相匹配。

### 整篇索引维护与去重

G 按文档顺序取得全部切片，调用 B 的 `index_representatives`，只保留同一文档中 **embedding 输入完全相同的第一个片段**。MySQL 仍保存全部切片；跨文档相同内容仍分别保留出处。`replace(document_id, chunks)` 接收该文档完整的代表片段列表，调用方不能拆成多次替换。

全部向量准备好后，B 在写锁内检查请求中的 chunk ID 是否属于另一文档，再清除当前文档的旧记录、内部分批 upsert，并重建 BM25。ID 冲突在清旧前报错；正常替换只影响当前文档，但不是原子事务。

写入阶段失败时，B 在同一锁内尝试删除该文档残留并重新构建 BM25，抛出 `IndexWriteError(cause, cleanup_error)`。`cleanup_error=None` 表示已确认清理，不能解释为旧向量已恢复。G 决定 FAILED 状态与业务门禁是否可以重新开放。

删除按 `document_id` 执行；reset 只供维护流程使用。B 的锁覆盖索引读写临界区，不覆盖先前的模型计算，也不是跨进程锁；应用级串行化、进程锁和离线重建由 [G](子Issue-G-应用编排.md) 负责。

### 默认混合检索

当前 `RETRIEVAL_STRATEGY=hybrid`，可切换 `dense` 做对照。问题先生成向量；向量与 BM25 各取前 20 个候选，按倒数排名融合（RRF）后返回 top-k，默认 k=5。请求 top-k 更大时，候选深度至少达到 top-k；结果受实际索引规模限制。

```text
RRF(chunk) = Σ 1 / (60 + 该通道中的排名)
```

BM25 语料由 Chroma 中的正文和标题路径派生，打开持久化 collection 时构建，每次替换、删除或 reset 后在同一写锁内重建。分词采用 jieba 搜索粒度和最小虚词表；英文标识符如 redis-check-aof、sync_threshold、chroma.sqlite3 同时保留完整词与拆分词。分词结果按记录 ID 和实际输入缓存，不另存一套词法数据真相源。

融合只决定顺序。每个 `SearchHit.score` 仍是真实余弦相似度；仅词法召回的候选补算余弦。不能把这个分数当作 RRF 得分，也不能要求返回顺序按 score 递减。词法没有命中时退回向量顺序，当前没有相似度阈值或交叉编码重排。`tags` 存在索引元数据中，但不用于检索过滤或排序。

### 健康与运行信息

`runtime_info()` 只读取本地索引状态和公开配置，不发模型请求、不加载 tokenizer。`health()` 额外检查 embedding 模型列表和 tokenizer 能否加载；Chroma、embedding、tokenizer 任一不可用，B 的汇总状态即为 DOWN。模型列表探测通过不等于实际推理一定成功。

Windows 下 `CHROMA_DIR` 必须为纯 ASCII 路径，否则在打开前报告 `UnsafePersistencePath`；reset 同样受此约束。具体原因见 B-16。进程级 `/health` 和模块依赖状态是不同层级，公开格式见[公共 API](api.md)。

## 三、数据与接口

```python
ChunkDraft = {text, byte_start, byte_end, heading_path, token_count}
IndexChunk = {chunk_id, text, heading_path, tags}
SearchHit = {chunk_id, document_id, text, heading_path, score}
IndexEntry = {chunk_id, document_id, seq, text, heading_path, tags}

split(content, title) -> tuple[ChunkDraft]
index_representatives(chunks: Iterable[IndexChunk]) -> tuple[IndexChunk]
replace(document_id, chunks: Sequence[IndexChunk]) -> int  # 写入的代表片段数
search(query, top_k=5, *, record_timing=None) -> tuple[SearchHit]
delete_document(document_id) -> int
inspect() -> tuple[IndexEntry]
reset() -> None
prepare() -> None
runtime_info() -> dict
health() -> dict
close() -> None
```

`document_id` / `chunk_id` 是正的有符号 64 位整数；一次 replace 内 chunk ID 唯一。`IndexChunk.text` 非空白，`heading_path` 为字符串，tags 为字符串序列。`ChunkDraft` 没有数据库 ID，A 落库后才分配 ID，G 负责适配。依赖失败以 `RetrievalUnavailable(component, cause)` 返回安全的分类信息，不把上游响应体或模型凭据传出去。

```text
Chroma collection = easyrag_<规范化模型名>_<维度>
  id        = 字符串化 chunk_id
  document  = chunk.text                       # 纯正文
  metadata  = {document_id, seq, heading_path, tags?}
  embedding = embed(text + 非空 heading_path)   # 拼接串不写回正文
  collection metadata = {hnsw:space: cosine, embedding_model, embedding_dim}

BM25
  内存语料 = tokenize(Chroma 正文 + 非空 heading_path)
  记录 ID 与向量通道相同，使用同一份代表片段
```

非空 tags 保存字符串数组；空 tags 写入时显式传 `None`，清除复用 ID 上可能残留的旧标签。`seq` 记录输入顺序，来源定位依赖 A 的 chunk ID 与字节区间。

| 配置 | 默认值 | 语义 |
|---|---|---|
| `CHUNK_MAX_TOKENS` / `CHUNK_MIN_TOKENS` | 512 / 64 | 完整输入上限 / 合并软下限 |
| `CHUNK_TOKENIZER_PATH` | 固定 revision 的本地 BGE-M3 JSON | 相对路径按后端服务目录解析 |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | ollama / bge-m3 / 1024 | 独立于回答模型 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | 本地 Ollama / 空 | 兼容地址可带 `/v1`，健康探测共用认证 |
| `EMBEDDING_TIMEOUT_SECONDS` | 60 | 每次 HTTP 请求超时 |
| `EMBED_BATCH_SIZE` | 64 | 内部模型批次和索引写入批次的配置上限 |
| `CHROMA_DIR` | 服务数据目录下的 chroma | Windows 必须使用 ASCII 路径 |
| `RETRIEVAL_STRATEGY` / `RETRIEVAL_CANDIDATES` | hybrid / 20 | 默认融合策略和每通道候选深度 |

## 四、关键取舍与有效裁决

| 编号 | 当前结论 | 依据与代价 |
|---|---|---|
| B-1 | 不预留 `parent_chunk_id` | 当前使用标题感知切片；父子分段未实现，需要实际收益证据后再调整数据模型。 |
| B-2 | 不做自动关键词或自动标签提取 | jieba 当前只服务 BM25 分词，不生成文档元数据；tags 只展示，核心链路不依赖用户标注。 |
| B-3 | 使用 Chroma | 原生支持按 document_id 删除和持久化；当前规模下比自行维护 FAISS 元数据反查更直接。 |
| B-4 | embedding 的 provider、model、dim 可配置，变更后显式重建 | 避免把不同向量空间混用；tokenizer 匹配由部署者保证。 |
| B-6 | 重建采用 G 推送快照 | B 不反向读取 MySQL，正常收录和恢复共用整篇替换契约。 |
| B-8 | embedding 不走 LangChain `init_embeddings` | 原生 HTTP 足以表达所需能力，也能控制模型列表探测、认证、批次和向量校验；回答模型由 F 单独管理。 |
| B-9 | 模型探测检查配置模型是否在列表中 | 端点可达不足以证明模型已安装；列表检查仍不替代推理验证。 |
| B-11 | 标题路径进入 embedding 输入 | 让孤立小节携带层级语义，纯正文仍保存在载荷和 MySQL；拼接可能稀释正文信号，改变此选择需要黄金集对照。 |
| B-12 | 使用 cosine 距离 | 与当前 BGE-M3 评估口径一致，融合后也保留真实余弦分数。 |
| B-13 | 统一 UTF-8 字节、左闭右开、同版本原文 | 规范化哈希不代表偏移版本；所有非空白字符被覆盖，允许纯空白间隙。 |
| B-14 | 一次 replace 是一篇文档的完整代表片段列表 | 全部向量先校验、再整篇替换，内部 batching 不改变文档边界；调用方拆批会覆盖此前批次。 |
| B-15 | 同文档按 embedding 输入去重，保留首次出处 | 避免大量零距离重复向量破坏 HNSW 召回；完整切片仍留在 MySQL。 |
| B-16 | Windows 非 ASCII 持久化路径拒绝打开 | 已复现 Chroma 1.5.9 重启后向量丢失，配置错误必须在写入前暴露。 |
| B-17 | 默认向量 + BM25，使用 RRF | 兼顾主题语义和精确措辞；不引入两种异量纲分数的加权系数。 |

### B-15：为什么在索引层去重

重复内容压测中，约 1 MiB 单字符资料产生 512 个相同片段，占当时索引的 81%。精确余弦能区分正确片段约 0.80 与重复噪声约 0.32，但隔离 Chroma 实验中，512 个零距离重复点使 HNSW 无论插入顺序都找不回正确片段；50 个重复点时正常。索引去重后 512 片段只需 1 个向量；若首片另有标题正文，则可能为 2 个。

在切片层丢弃重复段落会破坏 A 对非空白原文覆盖的校验；跨文档去重又会丢失独立出处。因此规则只在 `index_representatives` 定义，G 的收录、离线重建和一致性核验共同使用。代价是重复段落的引用定位到首次出现处。改成精确搜索虽能绕过 HNSW 症状，却未消除无价值的重复向量。

### B-16：为什么拒绝不安全路径

Windows 隔离实验比较两块磁盘、ASCII/中文目录和 8/1,024 维向量，触发差异的是路径中的非 ASCII 字符：Chroma 1.5.9 的 HNSW `.bin` 文件未写出，但 SQLite、metadata pickle、段序号和日志清理仍继续。超过持久化同步阈值后重启会丢失向量；阈值降至 10、写入 20 条即可复现。窄字符文件 API 是机制推测，尚未由上游源码确认。

调低阈值只会提早触发，警告也无法防止无声丢数据。因此 B 在打开客户端时拒绝该路径，Windows 专用哨兵测试跟踪上游是否修复；修复后再重新评估限制。索引可通过既有重建流程恢复，无需为此增加另一套持久化实现。

### B-17：为什么采用 RRF

v2 的 Q21 标注片段逐字包含“写入缓冲区即返回成功”“断电易丢数据”等信息，却被两篇 Redis 专题的十个片段压出 dense 前十。在线判定器仍能对这些主题相近的片段给出 SUFFICIENT，生成的引用编号合法却答错出处；仅检查引用编号或重排已召回片段不能补回遗漏。

同一语料和 BGE-M3 的对照中，v1 hit@1 从 dense 21/22 提升到 hybrid 22/22；v2 hit@5 从 7/8 提升到 8/8，Q21 在融合后排第 4。BM25 单独使用时 v1 只有 19/22@1、21/22@5，不能作为默认。候选深度 10/20/50 在这些样本上结果相同，当前取 20。

曾比较 dense、RRF、dense 末位补 1 个词法候选、补 2 个四种规则：30 道 Q/R 的标注证据覆盖总数分别为 88/87/86/87，只有 RRF 同时取得 hit@1 29/30 和 hit@5 30/30。融合的已知代价是 Q7、R6 在线判定从 SUFFICIENT 变成 PARTIAL，虽然 top-1 仍来自正确文档；答案因此多了覆盖边界声明。不针对小样本继续调权重来隐藏这种取舍。

BM25 每次写后全量重建，使词法语料直接等于 Chroma 当前载荷，不维护两份增量状态；当前数百片段规模下重建成本较小。该策略的分词、候选打分和余弦补算仍有开销，规模扩大后应重新测量。

## 五、验证依据

| 验收对象 | 现有依据 |
|---|---|
| 标题、代码栅栏、超长小节、完整输入 token 预算、原文字节定位 | `test_chunking`、`test_chunk_storage_contract` |
| 模型批次、数量/顺序/维度、float32 有限值、模型探测和认证 | `test_embedding`、`test_retrieval_module` |
| 同文档去重、跨文档保留、索引/恢复使用相同规则 | `test_index_representatives`、`mysql_rebuild_integration` |
| 纯正文载荷、整篇替换、失败清理、词法生命周期、持久化重开 | `test_retrieval`、`test_retrieval_module` |
| Windows 不安全路径拒绝与上游哨兵 | `test_retrieval` 中的路径及 Chroma 持久化用例 |
| 模块不读取业务数据库、不依赖其他业务模块 | `test_module_boundaries` |

既有[切片与元数据调研](参考调研-切片与元数据.md)提供设计依据；[检索质量评估方法](评估检索质量.md)定义计分口径。可复核报告包括 [v1 hybrid](eval/retrieval-v1-hybrid-2026-09-22.md)、[v2 dense](eval/retrieval-v2-dense-2026-09-22.md)、[v2 hybrid](eval/retrieval-v2-hybrid-2026-09-22.md)和 [v3 hybrid](eval/retrieval-v3-hybrid-2026-09-22.md)。

这些报告只证明给定语料和模型下的离线召回。命中文档不等于片段足以回答，不能据此推断拒答正确率、引用支持率或业务更新同步；在线结论见[离线评估模块](子Issue-D-离线评估.md)。父子分段、自动关键词、查询改写和重排均未进入当前实现，后续方向不构成已发布能力。
