# 子 Issue B：索引管线模块

## FastAPI 迁移设计（2026-09-15，当前实施范围）

Issue [#3](https://github.com/vansye/EasyRAG/issues/3)，父 Issue #1。位置：`app/modules/retrieval`，总体见 [模块设计](架构设计.md)。旧内部 HTTP 章节作为历史契约和测试证据保留。

B 独占切片、tokenizer、embedding 和 Chroma。只接受数据快照，不读取 MySQL、不调用 A/C/F/G，不解释文档业务状态、不包含 FastAPI。所有公开能力由 public 入口提供。

```python
ChunkDraft = {text, byte_start, byte_end, heading_path, token_count}
IndexChunk = {chunk_id, text, heading_path, tags}
SearchHit = {chunk_id, document_id, text, heading_path, score}
IndexEntry = {chunk_id, document_id, seq, text, heading_path, tags}
split(content, title) -> tuple[ChunkDraft]
replace(document_id, chunks) -> int
delete_document(document_id) -> int
search(query, top_k=5) -> tuple[SearchHit]
inspect() -> tuple[IndexEntry]
reset() -> None
runtime_info() -> RuntimeReport
health() -> DependencyReport
close() -> None
```

先完成整篇 embedding 校验再变更索引；局部写入失败在模块内部尝试清理，并明确暴露清理是否已确认，交由 G 决定恢复。索引/SDK 对象不出模块。保留向量检索和现有元数据守门；不在迁移时启用 BM25 或更换算法。reset 只由维护用例调用，不暴露公开 HTTP。

- [x] 抽出模块配置与切片公开入口，保留全部 Unicode/token 预算回归（PR #41、#48）。
- [x] 索引维护与检索公开入口，隔离 Chroma 测试成功、部分失败、清理失败及索引快照（PR #41）。
- [x] 原评估工具仅通过公开切片入口使用算法；模块 import 约束通过（PR #47）。
- [x] 真实 Chroma 重建、重开和元数据核验；空标签显式传 `None` 清除复用 ID 上的残留标签（PR #54）。

实现见 [PR #41](https://github.com/vansye/EasyRAG/pull/41)、[保留回归 #48](https://github.com/vansye/EasyRAG/pull/48)、[真实恢复 #54](https://github.com/vansye/EasyRAG/pull/54)。collection 的 document 载荷为原文正文，标题只影响 embedding 输入并保留在 metadata；非空 tags 使用字符串数组。本次没有切换检索算法或重新声称召回提升。

### 当前切片产出契约（2026-09-15 修订）

B 的切分和相邻小片段合并同时检查两种容量：完整 embedding 输入的 token 预算，以及切片正文 65,535 个 UTF-8 字节的存储上限。`heading_path` 元数据取标题路径的前 512 个 Unicode 码点，再参与 token 计数；原文中的完整标题、正文和 UTF-8 定位不变。预算仍包含特殊 token；若单个正文字符与标题路径已经无法放入 token 预算，继续明确失败，不按 token 数再次裁短标题。

B 不产生纯空白切片。因此首尾或切片间可能存在纯空白间隙，但每个非空白字符必须保留，每个切片仍是原文中的精确子串。`document.content` 是完整原文入口；直接拼接切片可能缺少这些空白间隙。A 按相同约定验证范围、文本和字段上限，完整格式见 [公共 API](api.md#资料与切片)。

本节取代下方历史记录中“切片连续覆盖每个原文字节”的要求。没有改变索引真相源、查询算法或数据库列类型；模块之间仍只由 G 适配公开数据。跨模块回归同时覆盖超长标题、稀疏 token 的超长正文、Unicode 空白、实际入库及重建 ID 复用。

## 历史实现与契约：内部 HTTP 阶段

> 父 Issue：#1 总功能文档
> 位置：Python / FastAPI（`rag-service/`）
> 对应考察点：RAG 与信息检索（设计文档必答题 1 后半、2 前半）
> 前置：子 Issue A 已定数据契约（chunk 字段、编排次序）；`docs/参考调研-切片与元数据.md` 已定方向（B-1 不预留 parent 字段、B-2 传统关键词先行）

## 一、功能描述

索引管线是派生物的制造者：输入是 Java 送来的原文与 chunk，输出是可检索的向量索引；词法索引到 M4 才实现，当前保持空壳。它不持久化任何业务真相——一切可从 MySQL 重算（架构不变量二）。

Python 已接入 `/chunk`、整篇 `/embed` 及相关索引维护；Java 已实现切片结果校验、MySQL 事务替换及 `/chunk`、`/embed`、`DELETE /index/{document_id}` 三个独立 HTTP 客户端，B-13 使用显式的 UTF-8 字节偏移契约。历史验证记录见 §八至 §十二，后续 P1/P2 整改见子 Issue A §八、§九，删除客户端与最新回归见本文件 §十三和子 Issue A §十二。上传接口、状态机、两跳业务编排与失败清理编排仍待接入，不代表子 Issue B 或 M2 整体验收通过。既有离线召回基线使用真实 Ollama，但召回评估、HTTP 客户端测试、完整业务集成与云端联调是不同层次的证据，不能互相替代。

### 1. 切片（chunk 端点）

**策略：标题层级感知切分，超长小节二次切分。**

```
第一刀：按 Markdown 标题层级（#/##/###，不更深）
        每个小节 = 候选 chunk，继承标题路径（heading_path）
第二刀：小节的完整 embedding 输入超过 max_tokens（默认 512）时，在小节内部按段落递归二分，
        二分出的块共享同一个 heading_path
        首选段落边界（空行），段落边界不命中时回退句子边界，再不命中回退硬切
```

**两条从实践中来的硬约束：**

- **代码栅栏识别**：解析器必须先识别 ``` / ~~~ 栅栏（结束栅栏同种字符且不短于开始栅栏），栅栏内部的 `#`、空行、疑似标题一概不当结构。知识库实测代码块 1173 个，技术笔记的代码注释里 `#` 极常见——不识别栅栏，切片会在代码中间乱切。
- **byte_start/byte_end 是切片的输出**：使用原文 UTF-8 字节偏移，左闭右开。核心在切片时记录范围，再映射到字节位置，不靠对重复正文做字符串查找来补算。HTTP 同样显式使用 byte_* 名称，不混用 Python 码点或 Java/JS UTF-16 下标，详见 §三 B-13。

**参数**：`max_tokens=512`、`min_tokens=64`，要求 `max > 0` 且 `0 <= min <= max`。预算包含正文、非空标题路径及模型特殊 token；min 是软下限，只在合并不超过 max 时合并相邻块。参数变动仍须用黄金集验证，不在接线时顺便修改切分策略。

**本地 tokenizer**：`CHUNK_TOKENIZER_PATH` 指向 Hugging Face tokenizer JSON，默认使用离线基线相同 revision 的 BGE-M3 文件；相对路径以 `rag-service/` 为基准。首次 `/chunk` 懒加载并缓存，禁用 truncation/padding，不联网下载、不回退为字符计数；同路径替换文件后须重启服务。文件缺失、损坏或无法编码输入时返回 `503 TOKENIZER_UNAVAILABLE`，不阻止进程启动。切换 embedding 模型时，部署方须同时提供匹配的 tokenizer；当前不自动识别或证明模型与文件匹配。tokenizer 或切片参数变化后，Java 必须重新切片并更新 chunk 真相源，不能仅把旧 chunks 再 embed。

模型文件不入库。首次运行可按[既有召回基线的复现命令](eval/retrieval-baseline-v1.md)准备默认 tokenizer；来源是 BAAI/bge-m3 的 `5617a9f61b028005a4858fdac845db406aefb181` revision，文件校验值见 §九。

### 2. Embedding（embed 端点）

- 可配置：`EMBEDDING_PROVIDER`（ollama / openai / deepseek）+ `EMBEDDING_MODEL` + `EMBEDDING_DIM`，启动时读配置
- 默认 `bge-m3`（Ollama 本地）；`nomic-embed-text` 作为对照组，跑同一黄金集出对比数字
- **原生 HTTP，不经 LangChain**：Ollama 调 `/api/embed`，显式传 `truncate=false`；`openai` 与 `deepseek` 均走 OpenAI 兼容 `/v1/embeddings` 协议。保留 `deepseek` 配置值不代表 DeepSeek 官方提供 embedding，也不代表已完成云端实测
- **认证、地址与超时**：请求头使用可选 `EMBEDDING_API_KEY`；兼容服务的 `EMBEDDING_BASE_URL` 支持根地址或以 `/v1` 结尾，不重复拼接 `/v1`。`EMBEDDING_TIMEOUT_SECONDS` 默认 60 秒，约束每次 HTTP 请求，不是整篇文档的总截止时间；不做隐式 HTTP 重试
- **全部向量先生成、后写入**：Python 按 `EMBED_BATCH_SIZE` 内部分批，校验返回数量与输入一致、每个向量维度 == `EMBEDDING_DIM`、所有坐标转为 Chroma 使用的 float32 后仍为有限数值；全部批次通过前不清旧、不写新。任一模型请求或批次校验失败均保留旧向量，不能只验证第一批或写入后才发现维度、数值溢出错误
- **换模型或换维度 = 全量重建**：Chroma collection 名为 `easyrag_<模型>_<维度>`（如 `easyrag_bge-m3_1024`），任一变化即换 collection，旧 collection 留作对照后删。collection 的 metadata 记录 `embedding_model` 与 `embedding_dim`，启动时比对，不符即拒绝启动并提示重建——把"换模型"变成显式动作而不是隐性事故。维度进名字是必要的：同一模型改成截断输出（如 bge-m3 从 1024 降到 512）时模型名不变，只按模型名命名会让旧 collection 被静默复用
- **模型可用性探测**：`/health` 不只探端点可达，还校验**配置的模型确实在可用列表里**（Ollama 查 `/api/tags`，OpenAI 兼容端点查 `/v1/models`），认证与地址拼接沿用 embedding 的相同规则。端点在线但模型没拉时，`embedding` 子项判 DOWN 并列出实际可用模型，顶层仍为 UP；模型列表探测不是实际推理检查，不保证第一次 embed 成功

### 3. 索引维护（Chroma + BM25）

**向量索引（Chroma）**：
- 持久化目录 `rag-service/data/chroma/`（.gitignore 已挡）
- 写入：`chunk_id` 作记录 ID；`heading_path` 非空时 embedding 输入为正文 + 换行 + 标题路径，否则仅正文。Chroma 的 document 字段保留该输入，绝不回写 Java 或真相源。metadata 存 `document_id / seq / heading_path`；tags 非空时存原生字符串列表，不编码成字符串，为空时省略该键（Chroma 拒绝空数组）。`heading_path` 同时参与 embedding 与出处层级展示
- 整篇替换：一次 `/embed` 接收一篇完整文档。全部向量生成与校验成功后进入索引变更临界区，先核对当前 collection 内所有请求 `chunk_id` 的归属，再按 `document_id` 清旧一次、内部分批 upsert。ID 属于另一文档则返回 `409 CHUNK_ID_CONFLICT`，该次调用不删除任一文档；正常替换只影响当前文档，**不是原子事务**
- 替换阶段失败：首次清旧或任一 upsert 异常后，仍在同一变更临界区内尝试清除本篇残留，返回 `503 INDEX_WRITE_FAILED`，`cause` 为原异常类名；清理成功则 `cleanup_error=null`，清理也失败则为清理异常类名，不能声称没有部分索引。清理不是恢复旧向量；Java 标记 FAILED，保留失败信息，可手动重试完整文档，不做隐式 HTTP 重试
- 并发边界：写入、删除、reset 在**单个 Python 进程**的索引变更期间串行化，不是跨进程或分布式事务。M2 已选择由单 Java 实例全局串行化完整变更工作流，并与问答互斥；索引变更结果不明时暂停，reset/rebuild 仅在维护门禁内执行。该业务门禁尚未实现，Python 现有锁不覆盖 embedding 计算，`/reset` 也不取消计算；具体恢复与体验约束见 §十四、子 Issue A §十三
- 删除：按 `document_id` 的 where 过滤批量删（Chroma 原生支持——这也是选它不选 FAISS 的主因：FAISS 无元数据过滤，按文档删除要自建 id 反查表）
- 全量重建：由 Java 编排（见 §三「全量重建的编排」）——Python 只暴露 `POST /reset` 清空与幂等 `/embed`，**不反向拉取 chunk、不读 MySQL**。这是不变量二的落地，也是换 embedding 模型的入口

**词法索引（BM25，rank-bm25 库，内存态）—— M4 实现，M1/M2 只留空壳**：
- 中文分词用 jieba；索引内容 = chunk 正文 + 标题路径
- 进程内存构建，**空启动**（0 条），不依赖 Java 即可起进程
- **M4 启用混合检索（RRF 融合）时才真正实现**：原计划 M2 就建（B-5），后推翻——M2→M4 之间没有任何功能会读它，失效逻辑不会被验证，提前写等于纯投入（见 B-10）
- 实现时与向量索引同生命周期：写入/删除/重建同步操作两份

### 4. 健康检查

`GET /health`：Python 进程、Chroma 可读写、embedding 端点可达且**配置的模型确实在模型列表中**（含当前模型名与实际可用模型列表）、BM25 条数（M4 前为 0）。探测复用 embedding 的可选密钥与地址规则，不执行真实推理。Java 侧的状态机与 FAILED 判定依赖此端点，但不能把模型列表探测通过当成 embedding 成功保证。

`/health` 不加载或验证 tokenizer，不能作为 `/chunk` 已就绪的证明；切片资源错误由实际 `/chunk` 调用报告。

**顶层 `status` 恒为 UP**，任何子项 DOWN 都不改它——进程活着是独立于依赖的事实。若依赖挂了就整包 503，Java 便无法区分"Python 挂了"和"模型服务挂了"，FAILED 的归因会错。这条与 Java 侧 `/health` 的分层语义对称。

## 二、数据原型（伪代码）

本模块不拥有数据库表。派生数据原型如下，当前只维护 Chroma，BM25 为 M4 设计、尚未实现：

```
Chroma collection "easyrag_<embedding_model>_<embedding_dim>"
  id       = chunk_id (BIGINT 字符串化)
  document = text + ("\n" + heading_path if heading_path else "")
  metadata = { document_id, seq, heading_path, tags?: string[] }
  # collection 自身的 metadata 另记 embedding_model / embedding_dim，
  #   启动时与配置比对，不符即拒绝启动（维度守门的落地）。
  #   模型名中的非法字符（如 bge-m3:latest 的冒号、HF 风格名的斜杠）
  #   会被替换为 '-'：Chroma 只接受 [a-zA-Z0-9._-]。

  # 重要：document 保留 embedding 输入；tags 仅非空时存原生列表，空数组省略。
  #   返回给 Java 的 chunks[].text 与落库的 chunk.text 仍是原文子串
  #   （text == decode_utf8(utf8(原文)[byte_start:byte_end])），拼接结果不回写 Java 或任何真相源。

BM25 (rank-bm25, 进程内存；M4 设计，当前为空)
  语料 = [jieba 分词(正文 + 标题路径)] × chunk_id 索引表
  # 同一 chunk 在两份索引中用同一 chunk_id 关联

配置（环境变量，.env.example 入库）
  EMBEDDING_PROVIDER = ollama | openai | deepseek
  EMBEDDING_MODEL    = bge-m3
  EMBEDDING_DIM      = 1024
  EMBEDDING_BASE_URL = http://localhost:11434  # Ollama 或兼容服务地址；兼容服务可带 /v1
  EMBEDDING_API_KEY  = ""                     # 可选，embedding 与 /health 使用相同认证规则
  EMBEDDING_TIMEOUT_SECONDS = 60              # 每次 HTTP 请求，不是整篇文档总截止时间
  CHUNK_MAX_TOKENS   = 512
  CHUNK_MIN_TOKENS   = 64
  CHUNK_TOKENIZER_PATH = data/tokenizers/bge-m3-5617a9f61b028005a4858fdac845db406aefb181.json
  EMBED_BATCH_SIZE   = 64                      # Python 内部模型调用/写入的批大小，非 /embed 请求上限
  # 无 JAVA_BASE_URL：推模式下 Python 不知道 Java 在哪，也不需要知道
```

## 三、对外接口（伪代码）

### Java → Python（对 A 的契约实现）

```
POST /chunk
  { document_id, text, title }
  → 200 { chunks: [{ seq, text, byte_start, byte_end, heading_path, token_count }] }
     # text == decode_utf8(utf8(原文)[byte_start:byte_end])，左闭右开
  → 422 字段校验失败 / INVALID_UTF8_TEXT / CHUNK_TOKEN_LIMIT_EXCEEDED
  → 503 TOKENIZER_UNAVAILABLE（cause=异常类名）

POST /embed
  { document_id, chunks: [{ chunk_id, text, heading_path, tags }] }
  → 200 { indexed: int }
  → 422 空列表 / 格式或字段校验失败 / 重复 chunk_id
  → 409 CHUNK_ID_CONFLICT
  → 503 EMBEDDING_UNAVAILABLE（模型调用或向量校验失败，保留旧向量）
  → 503 INDEX_UNAVAILABLE（索引预检或清旧前的准备失败）
  → 503 INDEX_WRITE_FAILED（清旧或 upsert 失败；cause=异常类名，cleanup_error=null 或清理异常类名）

DELETE /index/{document_id}
  → 200 { removed: int }    # 清该文档向量；BM25 当前为空，M4 才同步维护
  → 422 document_id 非正 / 超 BIGINT 范围 / 路径参数无法解析
  → 503 INDEX_UNAVAILABLE（cause=异常类名）

POST /reset
  → { reset: bool }         # 清空当前 collection，供全量重建用；BM25 到 M4 前始终为空

GET /health
  → { status, chroma, embedding: { model, reachable }, bm25: { chunks } }
```

`409`/`503` 的应用错误码位于 `detail.error`，不是响应顶层。例如：`{"detail":{"error":"INDEX_WRITE_FAILED","cause":"RuntimeError","cleanup_error":null}}`。这些 `503` 的 `cause` 仅含异常类名，不回显上游响应或原始异常消息。普通字段校验和 `/embed` 的 `422` 使用 FastAPI 标准的 `detail` 错误列表；`/chunk` 的非法 UTF-8 文本、无法满足 token 预算则返回 `{"detail":{"error":"INVALID_UTF8_TEXT"}}` 或 `{"detail":{"error":"CHUNK_TOKEN_LIMIT_EXCEEDED"}}`。

**DELETE 请求与结果**：`document_id` 为正有符号 BIGINT，范围 `1..9223372036854775807`，请求不带正文。`removed` 是本次实际删除量，重复删除或不存在文档返回 **200 且 removed=0**，不是 404；不要求它等于 MySQL `chunk_count`。Java `IndexClient.deleteDocument(long)` 返回 `int`，严格接受 `0..2147483647` 的 JSON 整数，越界、负数、缺失/null 或强制转换形式均拒绝。删除客户端不更新 MySQL、不接状态机、不自动补偿；即使本次删除成功，在途 `/embed` 仍可能写回向量。详细客户端及并发边界见 §十三和子 Issue A §十二。

**`/chunk` 请求与行为**：三个字段均必填。`document_id` 是正的有符号 BIGINT JSON 整数（拒绝布尔值与字符串）；`text` 是非空白字符串；`title` 是字符串、可为空。`text/title` 必须能严格编码为 UTF-8，未配对 surrogate 在 Pydantic 常规字符串校验前拒绝，不回显非法文本。`title` 当前仅接收并校验，不改写正文或 Markdown 标题路径；`token_count` 统计完整 embedding 输入和特殊 token。接口不创建 chunk ID、不调用 embedding、不修改 Chroma/BM25，也不依赖索引可用。预算不足以容纳单个字符及其标题路径和特殊 token 时明确返回 422，不静默截断。

**`/embed` 请求字段与整篇约束**：

| 字段 | 约束 |
|---|---|
| `document_id` | 正的有符号 BIGINT，范围 `1..9223372036854775807` |
| `chunks` | 非空数组，只包含这一篇文档的完整 chunks；完整性与 `seq` 顺序由 Java 保证 |
| `chunks[].chunk_id` | 同为正的有符号 BIGINT，且本请求内唯一；ID 由 Java 生成 |
| `chunks[].text` | 非空白字符串 |
| `chunks[].heading_path` | 必填字符串，允许空字符串，不接受 null |
| `chunks[].tags` | 字符串数组，省略时默认 `[]` |

Java 按 `seq` 排序发送，不额外传 seq 字段；Python 按数组位置派生 `seq=0..n-1`。一个 HTTP 请求就是一篇完整文档，`EMBED_BATCH_SIZE=64` 仅用于 Python 内部分批，不限制请求的 chunk 数，也不要求 Java 分批。成功时 `indexed` 等于请求的 chunk 数；重复相同整篇请求不产生重复 ID，新列表更短时清除旧的剩余 chunks。清空使用 DELETE，不发送空列表。当前 collection 的跨文档 ID 冲突在清旧前拒绝，`/embed` 本身不删除任一文档；Java 后续独立的失败清理策略见 A §一.3。

### UTF-8 定位契约（B-13）

- **单位与来源**：`byte_start` 包含起点，`byte_end` 不含终点；相对于请求原文、也就是同一版本 `document.content` 的 UTF-8 编码。不是上传文件原始字节、HTTP JSON 报文字节或拼接后的 embedding 输入。
- **原样保留**：Python 不 trim、不统一 CR/LF、不去 BOM、不做 NFC/NFD 归一化。字节边界不截断 UTF-8 编码序列，但不承诺硬切位置总在用户可见字形（组合字符、ZWJ emoji）的整体边界。
- **消费方式**：Python 按 UTF-8 bytes 切片后严格 decode；Java 对 UTF-8 字节数组截取后用严格 `CharsetDecoder` 解码，不能直接 `String.substring(byte_start, byte_end)`。需要 Java/JS 高亮下标时，由对应原文前缀解码后的 UTF-16 长度转换。
- **浏览器 BOM**：`TextDecoder` 默认可能吞掉开头的 BOM；精确还原使用 `new TextDecoder("utf-8", {fatal: true, ignoreBOM: true})` 解码 `TextEncoder` 生成的字节子数组，不能直接 `String.slice` 字节位置。
- **正文版本**：规范化哈希相同不保证字节相同。跳过重索引就必须保留原文及对应 chunks；若替换原文，即使只改换行或边缘空白，也须重切。URL 正文提取、上传解码等应在 Java 确定并保存原文前完成，之后各层不得单独规范化。
- **完整性与入库门禁**：当前切片完整、连续覆盖原文，seq 按响应顺序从 0 开始；Java `ChunkBatch` 拒绝缺字段、缺段、重叠、非法 UTF-8 边界、text 不匹配或无法供 `/embed` 使用的空白块。也按现有 TEXT/VARCHAR 列约束拒绝超长正文块/标题路径，不改变 Python 切片策略或静默丢弃内容。事务入口和详细限制见子 Issue A §八。
- **旧列与旧数据**：HTTP/Python 统一 byte_*；Java `ChunkRepository` 已显式映射到现有 char_* 物理列，并在锁定文档、核对同版本原文后事务替换 chunks/count；V1/V2 迁移未改。历史数据单位不明时从已保存原文重切重建，不用列名、ASCII 样本或数值大小猜测兼容。

### 全量重建的编排（Java 侧行为，非 Python 接口）

推模式下 Python 保持纯被动，**不反向调用 Java、不读 MySQL**。重建的触发与数据搬运都在 Java；Java 须关闭常规变更和问答入口，先确认旧在途索引变更已结束，再进入以下流程。存在未确认调用时按子 Issue A §十三 完成旧执行者退出确认；不能依靠 `/health` 或 `/reset` 取消 embedding 计算，也不能在重建中途开放问答：

```
触发：手动 rebuild（换模型后 / 怀疑索引与库不一致）或 Java 启动自检发现索引缺失
1. Java → Python: GET /health，检查依赖子项与配置模型（模型列表探测，不是真实推理验证）
2. Java → Python: POST /reset
3. Java:   从 MySQL 分页读未删除文档，逐篇加载该文档的全部 chunks ORDER BY seq
4. Java → Python: 每篇一次 POST /embed，携带完整 chunks，不跨文档、不拆分同一文档
5. Java:   逐篇核对 indexed，汇总并核对 Chroma 与 MySQL chunk 总数；任何失败或计数不符均为重建未完成
```

分页只用于读取文档列表，不是 `/embed` 的外部分批协议。当前 BM25 保持 0 条、不参与非零计数核对；M4 实现后再纳入两份索引的同步验收。任一步失败都不能将 rebuild 报为成功。

上述流程只有在确认已落库 chunks 对应当前原文、tokenizer 与切片参数时才能复用。若原文、tokenizer 或切片参数变化，或者恢复时无法证明 chunks 与当前原文配套，须先逐篇 `/chunk` 并更新 MySQL 中的 chunks，再生成新向量；不能把更新失败后遗留的旧 chunks 直接推送，也不能仅重写向量便认为旧切片满足新的 token 预算。

边界声明（与总 Issue §五、子 Issue A §四一致）：**B 与 C 永不反向调用 A，不读写 MySQL**。全量重建的编排是 Java 的职责，归属与上传索引的状态机一致——跨服务搬运数据本就是"数据权威层"该做的事。


## 四、模块边界

**B 负责**：切片算法与参数、embedding 调用及全部向量校验、Chroma 的构建/删除/重建（BM25 到 M4 才实现）、模型与索引元信息的一致性守门。

**B 不负责**：chunk 落库（A）、何时触发索引（A 的状态机编排）、检索时的查询与融合（C，读 B 的索引）、评估指标计算（D）。B 的索引对 C 是只读消费。

**B 的无状态边界**：索引均为派生缓存，删除 `data/chroma/` 与重启进程不丢任何真相，但需要 Java 重新推送完整文档。离线基线不能证明真实 embedding 重建耗时或长文档性能，不作秒级/分钟级承诺。

## 五、验收

以下保留模块级验收清单；接口层验证结果单列于 §八、§九，不等价于 Java 端到端验收。

- [ ] 一篇含代码块的多级标题笔记：所有代码块未被栅栏外的 `#` 切碎（抽查黄金集语料中代码密度最高的一篇）
- [ ] 每个返回的 chunk：`text == decode_utf8(utf8(原文)[byte_start:byte_end])`，使用同版本原文；覆盖非 BMP、BOM、组合字符、重复正文与 CRLF，A 的验收也依赖此契约
- [ ] `/chunk` 使用本地 tokenizer 的真实计数，包含标题路径与特殊 token；资源缺失/损坏/编码失败返回 503，输入非法或预算无法容纳时返回 422，不截断、不回退字符计数
- [ ] 超长小节被二分且子块共享 heading_path；短小节按 min_tokens 并入
- [ ] 重复相同整篇 `/embed` 无重复 ID；以更少 chunks 替换后无旧剩余 chunks，其他文档不受影响（BM25 到 M4 前为空）
- [ ] 一篇超过 64 个 chunks 仍以一次 `/embed` 接收，Python 内部分批后 `indexed` 为整篇数量，`seq=0..n-1`
- [ ] 空列表、非正或超 BIGINT 范围的 ID、空白 text、字段格式错误、重复 chunk_id 均返回 422；清空索引走 DELETE
- [ ] 请求 ID 在当前 collection 属于另一文档 → 409 CHUNK_ID_CONFLICT，该次 `/embed` 不删除任一文档
- [ ] Chroma document 为正文及非空标题路径拼接的 embedding 输入，不回写 Java；metadata 保留 document_id / seq / heading_path，非空 tags 为原生列表、空 tags 省略
- [ ] 首次清旧或任一 upsert 失败 → 503 INDEX_WRITE_FAILED 且 cause 为异常类名；临界区内清理成功时 cleanup_error=null，清理失败时为异常类名且不保证无残留；不做隐式 HTTP 重试
- [ ] 单 Python 进程的索引写入/删除/reset 串行化；reset 不取消正在计算的 embedding，不能替代 Java 完整工作流串行、问答互斥和结果不明时暂停
- [ ] 确认没有该文档的在途索引变更后，DELETE 成功则该文档在 Chroma 查不到；不把孤立的 DELETE 成功扩展成不存在晚到写入的保证；BM25 当前为空，M4 实现后再验证同步删除
- [ ] 删除 `data/chroma/` + 重启 Python：进程正常起、`/health` 绿、BM25 为 0 条（空启动，不依赖 Java）
- [ ] 上述状态下由 Java 在维护门禁内确认旧索引变更结束，再按 health → reset → 分页读文档 → 每篇完整 chunks 一次 embed → 计数核对触发全量重建；当前 Chroma 条数与 MySQL chunk 总数一致，任何失败均不能报重建成功或开放问答，BM25 到 M4 才纳入
- [ ] Python 代码中不存在指向 Java 的出站调用（边界的可验证形式：grep 无 Java 地址、无 MySQL 驱动依赖）
- [ ] 任一模型请求失败，或任一模型批次的返回数量、维度、坐标数值/float32 有限性校验失败 → `/embed` 不清旧、不写新，原向量保持不变
- [ ] Ollama 使用 /api/embed 且 truncate=false；openai/deepseek 使用兼容 /v1/embeddings，根地址与 /v1 地址、可选密钥及默认 60 秒单请求超时均按契约生效；/health 复用密钥与路径规则但只探模型列表
- [ ] 换 embedding 模型名：服务拒绝在旧 collection 上写入，提示需 rebuild
- [ ] 顶层 `status` 恒为 UP（进程活着是独立事实）；embedding 端点停掉时 `embedding` 子项转 DOWN 并带 error 类型（A 依赖它判 FAILED）
- [ ] embedding 端点返回"200 但结构不对"（网关首页、`{"models": null}` 等）时 `/health` 仍是 200，`embedding.error` 为 `BAD_RESPONSE`——解析异常不得逃逸成 500，否则 Java 会误判成"Python 挂了"
- [ ] 配置 `EMBEDDING_MODEL=bge-m3:latest` 或 HF 风格名（含 `/`）时服务正常启动（collection 名净化）
- [ ] 已有 collection 由 1024 维建立、配置改成 512 → 拒绝启动并说明需重建
- [ ] `data/chroma/` 数据损坏时进程仍起得来，`chroma` 子项如实报 DOWN

## 六、已裁决（汇总）

| # | 问题 | 结论 |
|---|---|---|
| B-1 | chunk 表预留 parent_chunk_id | 不预留；M4 采用父子分段时一次迁移改清楚（用户已定） |
| B-2 | 自动元数据路线 | 传统关键词（jieba/TF-IDF）先行，不够再上 LLM 抽取（用户已定）；M4 候选 |
| B-3 | 向量库 | **Chroma**（用户已定）。理由：原生元数据过滤与按 document_id 删除；持久化开箱即用；几百篇规模下 FAISS 性能优势无意义而元数据能力缺失是实痛 |
| B-4 | embedding 可配置 | 已定。provider + model + dim 三元组配置；换模型走显式 rebuild，不用旧 collection |
| B-5 | BM25 时机 | ~~M2 就建~~ **已被 B-10 推翻**，改为 M4 实现 |
| B-6 | 全量重建的依赖方向 | **推模式**（用户已定）：Java 编排，health → reset → 分页读文档 → 每篇完整 chunks 一次 `/embed` → 计数核对；Python 不反向拉取。理由：边界无例外比带例外好讲；重建是编排行为，归属与上传状态机一致；拉模式省下的 Java 代码在几百篇规模下不构成收益。HTTP 请求边界见 B-14 |
| B-7 | BM25 能否空启动 | **能**。Python 启动零外部依赖，避免两服务互等；M2 只用向量检索，Chroma 持久化重启不空，BM25 空着不影响 |
| B-8 | embedding 是否走 LangChain `init_embeddings` | **不走**，自己维护 provider 分支。理由：本模块只需要"取一个向量"这一个能力，为一次 HTTP 调用引入 langchain 依赖树不划算；且需要自己控制模型可用性探测（`init_embeddings` 不提供）。LangChain 仍用于模块 C 的 agent 循环——README 技术栈已相应更正 |
| B-9 | `/health` 探测到什么程度 | **校验配置的模型确实在列表中**，不止探端点可达，沿用 embedding 的密钥与地址规则；顶层 status 恒为 UP。模型列表探测不执行推理，也不保证 embed 成功（同类问题在 Java 侧也踩过：`SELECT 1` 通过但一张表都没建） |
| B-10 | BM25 何时实现 | **推迟到 M4**（推翻原 B-5）。原判断是"M2 就建以避免 M4 补失效逻辑"，但 M2→M4 之间没有任何测试或功能会读 BM25，其失效逻辑不会被验证，到 M4 启用时该错还是会错——等于纯提前投入。与"反对复杂化"冲突，改为 M4 启用混合检索时一并实现。**已于 2026-09-22 兑现，见 B-17**；当时预想的"失效逻辑"最终不存在——词法索引从 Chroma 载荷派生、每次写后重建，没有独立的持久化状态需要失效 |
| B-13（2026-09-08） | 偏移单位、字段与原文版本 | **对外统一 byte_start/byte_end，UTF-8 字节、左闭右开，对应同版本 document.content**；不直接使用 Python 码点或 Java/JS UTF-16 下标。正文不规范化，规范化哈希不充当偏移版本号；Java 已显式映射到现有 char_* 物理列并实现校验/事务落库，未知单位的历史数据重切重建。Python/Java 共用 Unicode 样例验证，完整边界见 §三和 A §八；Java HTTP/状态机编排尚未接入 |
| B-14（2026-09-08） | `/embed` 的文档与批处理边界 | **一个 HTTP 请求 = 一篇文档的完整、有序 chunks，正常路径整篇替换**；全部向量先生成并校验，再清旧一次、内部分批 upsert，非原子事务。`EMBED_BATCH_SIZE=64` 只控制 Python 内部批大小。按文档清旧时，外部拆批会互相覆盖；当前用内部 batching 表达调用规模即可，不在没有明确需求与实测前引入外部分批、批次会话、断点续传、队列或分布式事务，也不作长文档性能承诺。Java 继续拥有 MySQL、调度、chunk ID 与同文档操作排序 |
| B-15（2026-09-21） | 同一资料内相同片段是否都进索引 | **不都进：embedding 输入（正文 + heading_path）完全相同的片段只索引第一个，MySQL 仍保留全部片段行**。起因是 #71：一份 1MB 单字符压测残留被切成 512 个相同片段，占索引 81%；bge-m3 精确余弦下正确片段 0.80、垃圾 0.32，但隔离 Chroma 复现 512 个零距离重复点让 HNSW 无论插入顺序都找不回正确片段（50 个时正常）。**为什么放索引层不放切片层**：A 的落库校验要求切片覆盖原文每个非空白字符（B-13 偏移契约的守卫），切片阶段丢片段会被拒绝；索引层去重后每个字节仍有出处、`chunk_count` 按 A-6 仍是库内事实。**为什么文档内不全局**：跨资料的相同段落必须各自保留出处。**为什么不改精确搜索**：几百篇规模下可行，但不治根因且放弃了 HNSW 的扩展性。规则只在 B 的 `index_representatives` 定义一处，G 的收录、离线重建与就绪一致性审计共用；旧索引若含重复向量会被审计判 `INDEX_CONTENT_MISMATCH`，走既有 `rebuild-index` 修复。代价：重复区间的引用落到首次出现处。对照：1MB 相同内容 512 片段 → 1 个向量（首片含标题行时 2 个）；真实路径见 `mysql_rebuild_integration.py` 的重复段落用例 |
| B-16（2026-09-21） | Chroma 持久化目录含非 ASCII 字符时怎么办 | **Windows 下拒绝打开，`/health` 报 `UnsafePersistencePath`；部署把 `CHROMA_DIR` 放到纯 ASCII 路径**。起因是 #74：正常关闭的进程重启后 `Error loading hnsw index`，段目录只剩 index_metadata.pickle、写前日志已清理。隔离对照（两盘 × ASCII/中文路径 × 8/1024 维）证明唯一决定因素是路径含非 ASCII：chromadb 1.5.9（Rust 绑定）在 Windows 上写 HNSW 的 .bin 文件静默失败，而 sqlite、pickle、段序号与日志清理照常；阈值以下靠日志回放掩盖，越过 `hnsw:sync_threshold`（默认 1000）即丢。机制推断为内置 C++ hnswlib 走窄字符文件 API，未读上游源码核实；把阈值降到 10 后写 20 条即可复现，已做成 Windows 专用哨兵测试，它一旦失败就说明上游修好了、守门可放开。9-15 的"重启后只剩 106 条向量"同因。**为什么是守门而不是调小阈值**：阈值只决定多早丢，不决定丢不丢；调小反而更早触发。**为什么硬拒绝而不是警告**：风险不对称，一边是启动时的配置错误，一边是无声丢数据。**为什么不换存储或自己持久化**：几百篇规模下没有收益，且已有 `rebuild-index` 作为派生索引的恢复路径。检查只在 B 的 `_open_client` 一处，`reset()` 同样受控。代价：Windows 用户目录含非 ASCII 字符时，默认 `data/chroma` 与 pytest 的 tmp_path 都会被拒绝，需要显式指定 ASCII 路径 / `TEMP`；本机业务索引已迁至纯 ASCII 目录并重建，旧目录保留取证 |
| B-17（2026-09-22） | 混合检索：怎么让措辞精确命中的短卡不被同主题专篇压掉 | **默认 `hybrid`：向量与 BM25 各取前 20 个候选做倒数排名融合（RRF，k=60）；词法索引由 Chroma 载荷派生，每次写后在同一把写锁内全量重建**。靶子是 v2 的 Q21：目标片段逐字含"写入缓冲区即返回成功""断电易丢数据""fsync……强制立即落盘"，却被 Redis 持久化/过期 Key 两篇专篇的 10 个片段（0.60–0.65）压出前十——稠密向量按主题相近排序，精确措辞没有额外权重；在线评估里判定器看着 Redis 片段判 SUFFICIENT，答案引用合法但答错了篇（错源 1/30）。三策略对照（同一语料、同一 tokenizer、bge-m3）：v1 hit@1 dense 21/22 → **hybrid 22/22**（R1 补上），hit@5 均 22/22；v2 hit@5 dense 7/8 → **hybrid 8/8**（Q21 第 4 名）；bm25 单独用 v1 退到 19/22@1、21/22@5，不能作默认。候选深度 10/20/50 结果相同，取 20。**为什么 RRF 不做加权分数**：余弦相似度与 BM25 分数量纲不同，线性加权需要调一个没有理论依据的系数；RRF 只用名次，无参数可调错。**为什么不用"dense 优先 + 词法补位"**：把四种规则（dense / RRF / 末位补 1 个 / 末位补 2 个）放在 30 道 Q/R 上离线对照，证据覆盖（top-5 里来自标注出处的片段数之和）分别为 88 / 87 / 86 / 87，补位规则同样改动 25–26 个候选集且在 Q2/Q3/R1 上丢覆盖，只有 RRF 同时拿到 hit@1 29/30 与 hit@5 30/30。**已知代价（在线复验两次都稳定）**：Q7（PromptTemplate）与 R6（缓存一致性）的判定由 SUFFICIENT 变 PARTIAL——Q7 是 dense 前三的三个同篇片段被"prompt"高频的多轮对话/create_agent 片段挤剩一个，R6 只是尾部候选换了两个；两题 top-1 仍是正确文档，答案多了覆盖边界声明。这是"救回答错篇的 Q21"换来的，不调权重去掩盖：小样本上调系数就是过拟合（判断力记录 #19）。**为什么每次写后全量重建、不做增量簿记**：一致性由构造保证——Chroma 里有什么，词法语料就是什么；百篇规模下重建是毫秒级，分词结果按（id，正文）缓存；增量簿记省不了什么却引入两套状态。**为什么词法通道也用正文 + 标题路径、且受 B-15 去重**：语料与向量通道一致，代表片段一个来源。**分词**：jieba 搜索粒度 + 标识符（`redis-check-aof`、`sync_threshold`、`chroma.sqlite3`）整体保留并附带分段，去掉标点与一份最小虚词表；jieba 会在 `_`/`-`/`.` 处切开标识符，所以标识符先按正则整体提取。**不做**：交叉编码重排（只能重排已召回的候选，救不了 Q21 这种没进候选的）、相似度阈值（#15 已排除）、加权融合、索引期 LLM 生成问句。代价：启动多一次分词（222 片段约 0.2 秒；jieba 首次加载词典约 2 秒）；`search` 的 `vector` 段多一次 BM25 打分与最多 20 个候选的余弦补算；每个命中仍报真实余弦相似度，只是顺序由融合决定。报告：`eval/retrieval-v1-hybrid-2026-09-22.md`、`eval/retrieval-v2-dense-2026-09-22.md`、`eval/retrieval-v2-hybrid-2026-09-22.md`；在线错源对照见子 Issue D |

## 七、待议

| # | 问题 | 背景 |
|---|---|---|
| B-11 | heading_path 是否进 embedding 输入 | 当前设计是进（正文+标题拼接后 embed）。反方观点：拼接会稀释正文语义。可做成开关，用黄金集 A/B。默认进——参考调研中 LlamaIndex 的做法即此类拼接 |
| B-12 | Chroma 的 distance metric | 默认 cosine。bge-m3 官方建议 cosine；无需提前裁决，写死 cosine 起，异常再议 |

## 八、整篇 /embed 切片验证记录（2026-09-08）

本节保留前一轮仅接通整篇 `/embed` 的验证快照；后续 `/chunk` 接线与 B-13 验证见 §九。

### 自动化回归

在 RAG 服务目录使用项目虚拟环境执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

- 全量 **150 passed**，相对本切片前的 93 项新增 **57 项**。仅有 Chroma asyncio 与 Starlette anyio 的两条既有弃用警告；依赖检查返回 `No broken requirements found`。
- 新增覆盖原生 embedding 协议、鉴权与路径、内部批次、超时不重试、输入和向量校验，以及真实临时 Chroma 上的整篇重试、缩短替换、跨文档 ID 冲突、失败清理与写入/删除/reset 的进程内串行化。
- 审查后补充了正负 float32 溢出及最大有限值边界、后续模型批次溢出时保留全部旧向量，以及首次清旧部分生效后报错时的清理成功/失败测试；两处缺陷均先复现失败再修复。

### 本地真实模型冒烟

使用 FastAPI `TestClient` 调正式路由、真实 Ollama `bge-m3:latest`（1024 维），仅发送合成文档。Chroma 使用隔离的 `EphemeralClient`，不打开业务持久目录，也不访问 MySQL 或私人知识库。

- 模型 digest：`7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`。
- 内部 `EMBED_BATCH_SIZE` 设为 2：3 个 chunks 仍只发一次 `/embed`，实际模型调用分成 2 + 1；原样重试同样分批。

| 步骤 | 接口结果 | Chroma 总向量数 |
|---|---|---|
| 写入另一篇 1 块文档 | `indexed=1` | 1 |
| 写入目标整篇 3 块 | `indexed=3` | 4 |
| 原样重试目标文档 | `indexed=3` | 4 |
| 目标缩为 1 块，使用新 chunk ID | `indexed=1`，旧 3 个 ID 均消失 | 2 |
| 删除目标文档 | `removed=1`，另一篇仍保留 | 1 |
| reset | `reset=true`，collection metadata 保留，BM25 为 0 | 0 |

### 尚未验证的边界

该轮 `/embed` 验证不包含 `/chunk` 接线、Java/MySQL 编排、跨语言偏移 B-13、真实云端 embedding 联调、真实模型链路的进程重启持久性或长文性能测试；后续切片接口与 B-13 的证据见 §九。OpenAI 兼容协议仅由 `MockTransport` 测试覆盖。该轮没有重跑黄金集，不产生新的召回分数，也不据此宣称召回率提升；BM25 仍为空壳，按 B-10 留到 M4。

## 九、偏移契约与 /chunk 验证（2026-09-08）

> 本节保留上一切片的验证记录；后续 Java 落库结果见 §十，不用本节的旧测试数量描述最新实现。

### 自动化与跨语言验证

- 在 RAG 服务目录运行 `.\.venv\Scripts\python.exe -m pytest -q`：**190 passed**，比前一切片新增 **40 项**；新增接口/配置测试与原有切片测试的定向集合为 **64 passed**。仍只有 Chroma asyncio、Starlette anyio 的两条既有弃用警告；`pip check` 无损坏依赖。
- 在 Java 服务目录运行 `.\mvnw.cmd -q test`：**11 项单元测试通过**，其中 9 项为新偏移契约测试、2 项为原有健康检查测试。本轮没有运行需要 MySQL 的迁移集成测试。
- 两侧读取同一份 7 组 JSON 样例：混合中文/emoji、LF、CRLF、孤立 CR、BOM、组合字符/ZWJ emoji、重复段落及边缘空白；含 ASCII 对照。Python 正式 `/chunk` 输出对照固定 byte_* 结果，Java 验证严格 UTF-8 解码与转换后的 UTF-16 定位，不拿 ASCII 的偶然相等冒充跨语言通过。
- 两侧均验证了“规范化哈希相同但旧偏移失效”：CRLF 样例为 55 字节，LF 版本为 50 字节，旧结束位置不能复用。Python 另覆盖 token 预算、资源失败、输入校验和端点不依赖 embedding/索引的边界。
- 未配对 surrogate 的回归先复现失败，再将 text/title 的严格 UTF-8 检查前移到 Pydantic 常规字符串校验之前，返回明确 422，避免错误回显再次触发编码异常。
- 使用 Node 的标准 `TextEncoder/TextDecoder` 对相同 7 组样例做只读探针通过；同时复现默认 decoder 吞 BOM，并验证 `ignoreBOM: true` 能保留原文。这不是前端 UI 高亮集成验收。

### 本地真实模型链路

通过正式路由使用真实 BGE-M3 tokenizer 和 Ollama `bge-m3:latest`（1024 维，模型 digest 同 §八），Chroma 使用隔离的 `EphemeralClient`。只发送合成样例，不打开业务持久索引、不访问 MySQL 或私人知识库。

- tokenizer SHA-256：`21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08`。
- 为固定共享样例的章节边界，冒烟设 `max_tokens=512`、`min_tokens=0`，内部 embedding batch 为 2；生产默认值仍为 512/64、batch 64，未修改切分算法。
- 7 组样例经真实 tokenizer 得到 15 个 chunks；所有 byte_* 与共享契约一致，token_count 与完整 embedding 输入的实际 tokenizer 计数一致。

| 步骤 | 结果 | Chroma 总向量数 |
|---|---|---|
| BOM/组合字符样例 `/chunk → /embed` | `indexed=2` | 2 |
| CRLF/emoji 样例 `/chunk → /embed` | `indexed=3` | 5 |
| 原样重试目标文档 | `indexed=3`，无重复 ID | 5 |
| 修改目标正文后重新 `/chunk → /embed` | `indexed=1`，旧三个 ID 消失，新偏移对应新正文 | 3 |
| 删除目标文档 | `removed=1`，另一篇两块仍保留 | 2 |
| reset | `reset=true`，collection metadata 保留，BM25 为 0 | 0 |

### 验证边界

本轮完成 Python 切片接口与跨语言偏移契约，不代表 Java/MySQL 上传、落库、状态机和原文版本协调已实现；未做前端 UI 高亮、真实云端 embedding、真实链路进程重启持久性或长文性能验收。没有重跑召回黄金集，不产生新的召回分数。换 tokenizer/切片参数后的重切编排仍由 Java 后续实现，BM25 继续留到 M4。

## 十、Java 切片校验与事务落库验证（2026-09-08）

本节保留 Java 持久化原语切片的 **48 + 16** 历史验证快照，不是上传/索引状态机验收。生产入口与后续 P1/P2 整改见子 Issue A §八、§九，`/chunk`、`/embed` 客户端的进展见 A §九、§十，传输统一见 A §十一，删除客户端与最新回归以 A §十二为准；本原语切片未修改 Python 实现、tokenizer 参数、V1/V2 迁移或共享 JSON 样例。

### 自动化回归

- Java 干净构建：在 `server/` 运行 `.\mvnw.cmd '-Dit.test=ChunkRepositoryIT' clean verify`，**48 项单元测试 + 16 项 MySQL 集成测试通过**，无失败或测试错误；Jar 打包通过。单元测试含 46 项契约测试及 2 项既有健康测试。
- TDD 记录：可运行的空校验实现先得到 36 项断言失败，再实现校验；空落库实现先得到 16 项断言失败，再实现事务。两轮最终均无测试错误，不把缺少类或数据库配置失败当作业务红灯证据。
- 既有 7 组共享 Unicode 样例现在还会调用实际 `ChunkBatch.validateAgainst`；新增字段缺失、非法范围/顺序、缺口/重叠、文本不符、未配对 surrogate、空白块、集合不可变及数据库长度边界反例。仍验证不把相同规范化哈希视为原文字节版本号。
- Python 全量回归 **190 passed，2 条既有弃用警告**；`pip check` 无损坏依赖。本轮没有新的召回评估分数。

### MySQL 真实事务验证

- 使用 MySQL 8、现有 V1/V2 Flyway 迁移和 Spring 事务代理，不用模拟数据库代替回滚验证。
- 字节偏移确实写入旧 char_* 列；生成 ID 唯一、有序结果与原始 byte_* 一致，替换变短后无旧余块，只更新目标文档的 chunks/count，其他文档和非目标字段不变。
- BOM、组合字符、ZWJ emoji、CRLF 原样存储；65535 字节 TEXT 和 512 个 emoji 的 heading_path 可落库，不把 UTF-16 长度当 MySQL 字符数。
- 拒绝不存在/软删文档和无效 ID；拒绝 CRLF/LF、边缘空白、大小写及 NFC/NFD 不同的原文版本，即使规范化哈希相同或 SQL collation 可能视为相等也不复用结果。
- 在第二次 INSERT 和最终 chunk_count UPDATE 分别通过测试库触发器注入错误，均恢复原 chunks（含原 ID）与 count，无半份切片。
- 双连接验证：持有文档行锁时另一个 replace 等待；正文更新提交后，等待者读取新正文并拒绝旧版本切片，不能在旧快照上继续写入。

### 隔离与复现边界

集成测试每次随机创建 `easyrag_chunk_it_<UUID>` 数据库，测试数据源和 Flyway 都显式指向该库；删除数据及最终 DROP 前核对当前库名，测试结束已删除随机库并关闭连接池。不读写默认业务库、业务向量索引或私人知识库。

复现需要 MySQL 的建库/删库、迁移、读写和创建触发器权限。地址取 `MYSQL_HOST`/`MYSQL_PORT`（默认 localhost/3306），凭据沿用 `local` profile 的 datasource 配置；Flyway 的独立连接显式引用相同凭据，不把凭据写进测试代码。不要把定向命令换成无筛选的 `verify`：原有 `SchemaMigrationIT` 仍直连 local 配置，本轮没有执行它。

本节对应的原语切片未新增或验证 Java → Python HTTP 客户端、上传/更新接口、异步状态机、完整 `/chunk → MySQL → /embed` 联调、前端引用可见性/高亮、长文性能或云端模型；后续 `/chunk`、`/embed` 客户端的进展与验证边界见子 Issue A §九、§十。原文行锁只约束库内替换，不能替代同文档跨 HTTP 排序、rebuild 在途任务协调或跨存储一致性；后续切片继续完成这些编排边界。

## 十一、Java /embed 客户端接线验证（2026-09-09）

本节保留 `/embed` 客户端阶段的 **238 + 24** 历史验证快照；后续 `/chunk` 传输整改与当前统一契约见 §十二及子 Issue A §十一。

Java 新增独立 `EmbedClient`，直接接受 `ChunkRepository.StoredChunk` 与文档级标签；复用已有 RAG 地址/连接和读超时配置，不修改 Python 协议、切片策略、迁移、共享偏移样例或依赖。详细输入、异常、配置与验证记录以子 Issue A §十为准。

为修复实测的 401/407 错误正文丢失，`/embed` 改用内置 JDK HTTP 工厂，并关闭默认自动解压以保留错误字节。配置键不变，read-timeout 约束包含响应头/body 的 HTTP 交换总等待预算，持续分段到达不会重置计时，也不证明 Python 已取消执行。本节记录的阶段中 `/chunk` 曾使用不同的 socket 空闲等待语义，其同类认证错误正文问题及超时差异现已由 §十二修正。

- **整篇而非 HTTP 分批**：按 seq 排序后一次发送，沿用 MySQL ID，不修改原文或调用方列表，不把 heading_path 拼进 text。64、65、129 块的回归均验证只出现一次 `/embed`；Python 继续负责内部 embedding/upsert 分批。
- **调用前与成功校验**：本地拒绝空列表、非法/重复 ID、跨文档、序号不连续和非法字符串；200 必须是精确的 `application/json`，indexed 为严格 JSON 整数且恰等于提交数量，拒绝隐式转换、重复键、尾随或损坏 JSON。序号连续不能证明调用方没有漏掉文档尾部，整篇完整性与正文版本仍由编排层保证。
- **保留失败而不擅自补偿**：非 200 且 body 完整读完时保留状态、headers 和 body，即使 Content-Type/正文损坏也不覆盖该 HTTP 失败；读取中断/超时仍走传输故障边界。不误把 `detail.error` 当作顶层字段，也不假设 422 detail 必为对象。客户端不重试、不跟随 POST 重定向、不自行 DELETE；超时不代表 Python 停止写入，后续状态机须落实失败记录、清理和在途协调。
- **分层验证**：102 项新增客户端测试通过，包含 401/407、gzip 错误正文保真和持续分段响应的整体预算回归；定向干净构建共 **238 项 Java 单测 + 24 项隔离 MySQL 集成测试通过**，Jar 打包成功，随机库已清理；Python 全量 **190 passed，2 条既有弃用警告**。历史 §十及 A §九的计数保留为对应阶段快照。

本节记录的 `/embed` 阶段没有运行真实 Java/Python 双进程链路、完整上传/索引状态机或召回评估；当时尚未实现失败清理客户端、跨 HTTP 排序、状态更新及 reset/rebuild 协调，BM25 继续留到 M4。删除索引客户端的后续进展见 §十三；调用原语就绪不等于失败清理编排、整篇索引业务或 M2 验收完成。

## 十二、/chunk 传输整改与当前验证（2026-09-09）

`ChunkClient` 已改用与 `EmbedClient` 相同的 JDK HTTP 工厂配置：HTTP/1.1、禁止重定向、关闭自动解压。401/407 错误正文丢失已在旧实现复现并修复，gzip 错误体保持原始字节与 Content-Encoding；没有通过跳过严格 JSON 或字节偏移校验来换取通过。详细修改与证据见子 Issue A §十一。

- **超时已统一**：原配置键及默认值不变，两个客户端的 read-timeout 均为包含响应头/body 的 HTTP 交换总等待预算，不是 socket 空闲等待，也不是 Python 取消确认。持续分段响应不会重置预算；长文是否需要调大预算仍待实测。
- **验证增量明确**：ChunkClient 从 88 项增加到 92 项；旧实现出现 2 项认证错误正文失败及 1 项总预算契约差异失败，修复后全部通过，gzip 用例用于守住原有保真行为。
- **当前回归**：定向干净构建 **242 项 Java 单测 + 24 项隔离 MySQL 集成测试通过**，Jar 打包成功，随机库已 DROP、连接池关闭；Python **190 passed，2 条既有弃用警告**。本轮未修改 Python、配置、依赖、迁移、仓库或共享契约样例。

旧工厂风险与两个客户端的超时差异不再是待办。本节保留 **242 + 24** 的传输整改验证快照，当时提出的独立 Java `DELETE /index/{document_id}` 客户端已由 §十三落实；上传/更新接口、状态机、失败清理编排、同文档在途排序及 reset/rebuild 协调仍未完成。该阶段没有真实双进程联调、长文/云端验收或新的召回分数，不代表 M2 完成。

## 十三、Java 删除索引客户端与当前验证（2026-09-09）

`IndexClient.deleteDocument(long documentId)` 已实现，供后续编排调用 `DELETE /index/{document_id}`。本轮仅新增该客户端、对应测试并同步 A/B 文档，没有修改 Python、配置、依赖、数据库仓库或迁移。详细契约与验证过程见子 Issue A §十二。

- **幂等与计数**：正 BIGINT ID、无正文 DELETE、严格非负整数 `removed`；0 是成功，不与 MySQL `chunk_count` 比较。404 与其他非 200 都保留为 HTTP 失败，不能用“可能早已删除”将错误吞掉。
- **传输对齐**：沿用现有 RAG 配置与 JDK HTTP/1.1 工厂，禁止重定向、关闭自动解压、不做应用层重试。完整的非 200 响应保留状态、headers 和原始字节，包括 401/407 与 gzip；读取不完整则按 I/O 失败处理。read-timeout 仍为响应头加正文的总等待预算，不是 Python 取消确认。
- **测试先行与回归**：可编译空壳先得到 **93 项预期断言失败、0 errors**，实现后定向 **93 项全部通过**。定向干净构建 **335 项 Java 单测 + 24 项隔离 MySQL 集成测试通过**，Jar 打包成功，随机库已 DROP、连接池已关闭；未运行 `SchemaMigrationIT`。Python **190 passed，2 条既有弃用警告**。
- **未完成边界**：三个 HTTP 客户端就绪，不代表已经自动记录失败、清理索引或更新状态。DELETE 成功后在途 `/embed` 仍可能写回，DELETE 超时后自身也可能继续执行；跨 HTTP 的同文档排序及 reset/rebuild 协调仍归后续编排处理，BM25 继续留到 M4。

下一阶段先推进已有文档的最小索引编排与状态/并发契约，再接上传、更新和手动重索引入口。本轮没有真实 Java/Python 双进程联调、完整业务链路、长文/云端验收或新召回分数，不代表子 Issue B 或 M2 整体验收完成。

## 十四、M2 轻量协调与运行成本边界（2026-09-09，已确认、待实现）

用户暂选“完整工作流串行 + 索引变更结果不明时暂停 + 维护恢复”，并要求以实际问答体验、内容变更生效时间和运行成本决定是否优化。完整业务契约以子 Issue A §十三 为准；这不是已经修复 Python 任意并发时序的声明。

### 保持的接口与职责

- 不新增持久化操作代次、跨 HTTP 版本字段或 Python SQLite 控制库；不修改既有 `/chunk`、整篇 `/embed`、DELETE/reset 请求与响应，MySQL 仍是唯一真相源。Python Agent 不因此获得直接读取 MySQL 或反向调用 Java 的权限，也不以生成后过滤引用代替生成前的正确性。
- 受支持的业务路径限定为一个 Java 编排实例、一个 Python 写入进程；对当前业务 collection 的变更不能绕过 Java 门禁。Python 的写锁仍只覆盖索引变更，不能把它当作完整请求排序、取消或业务就绪机制。
- 单篇 `/embed` 仍先计算并校验全部向量、再整篇替换；`EMBED_BATCH_SIZE` 只控制 Python 内部模型/写入批次。Java 的失败路径仍尽力调用一次独立 DELETE，且 `removed=0` 不表示已取消旧任务。
- 模型列表探测和 Chroma 健康只反映依赖状态。`/health` 为绿、计数相等、DELETE/reset 返回成功均不能单独证明没有旧请求仍会写入；不能用这些信号自动解除 Java 的不确定状态。

### 在途风险如何处理

| 已知时序 | 轻量方案要求 |
|---|---|
| 旧 `/embed` 计算期间 DELETE 成功，随后旧结果写回 | 写入结果仍不明时，Java 继续关闭变更和问答入口；不把 DELETE 当作取消确认 |
| 旧、新 `/embed` 完成次序倒置 | 正常业务不允许两条变更工作流重叠；旧请求不明时不启动新请求 |
| 旧 DELETE 晚于新 `/embed` 到达 | 清理确认前不允许启动新流程；清理结果不明则维护隔离，而非超时释放门禁 |
| reset 与旧计算交错，或仅 Java 重启 | 维护恢复先确认旧编排执行者与 Python 写入进程退出，再 reset/rebuild；启动默认不开放业务 |

这些是后续跨层时序测试的验收要求，本节没有新运行测试。直接绕过 Java 并发调用 Python 的底层交错仍可能产生残留；方案通过限制可达业务时序、暂停服务和维护修复保证边界，而非宣称索引物理状态时时原子一致。

### 耗时与费用的验收

常规收录按篇完成后释放门禁，维护重建则全程关闭问答；单篇的整篇性不因追求更快响应而改变。先记录每篇 chunks 数、模型批次数、模型计算与索引写入耗时，并由 Java 补齐排队、数据库、HTTP、忙碌反馈和恢复全程；Python 阶段耗时不能冒充用户端到端耗时。模型不提供实际用量时不推算收费金额，重建引起的重复模型请求须单列。

历史 `docs/eval/retrieval-baseline-v1.md` 仅有一次离线召回成本记录，未调用生成 LLM，不含业务门禁与恢复，不据此承诺单次问答延迟或在线同步时间。首次真实基线使用仓库样例，区分空闲/索引中提问、单篇/连续收录与维护恢复，报告样本量、环境、忙碌占比及连续不可用窗口；首轮不以压力测试、私有知识库或长文章替代通常场景。

时间与费用可接受就维持当前方案；不可接受时先根据阶段数据定位主因，再评估参数、重复计算或调度的修改成本。若必须在索引时持续问答、故障后自动继续或在线重建，则重新设计隔离保证并取得确认，不偷改门禁或超时语义。本次仅对齐 A/B 文档，业务门禁、编排、在线性能测量及体验验收仍未完成。
