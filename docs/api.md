# EasyRAG 公共 API

供前端开发与接口验收使用。统一后端默认地址为 `http://127.0.0.1:8080`，Vue 通过同源 `/api`、`/health` 访问。在线 [Swagger](http://127.0.0.1:8080/docs) 可查看路由和请求参数；当前响应未全部声明为 OpenAPI schema，字段、错误状态和示例以本文为准。启动、配置和停服恢复操作见 [README](../README.md)。

## 路由

除文件上传外，请求体使用 `application/json`。普通成功响应为 JSON，流式问答为 SSE；删除资料或问答历史成功时没有响应体。

| 方法与路径 | 参数 / 请求体 | 成功状态与含义 |
|---|---|---|
| `GET /health` | 无 | `200`，进程与依赖的分层健康状态 |
| `GET /api/runtime` | 无 | `200`，运行门禁状态和模型公开信息 |
| `POST /api/admin/ready` | 无 | `200`，显式检查一致性并重新提交待处理资料 |
| `POST /api/documents` | `multipart/form-data`，必填文件字段 `file` | `201`，资料已入库，返回 `id/title/source_type/index_status` |
| `GET /api/documents` | `page=0`、`size=20`、可选 `status`、`q` | `200`，`{total, items}` |
| `GET /api/documents/{document_id}` | 路径 ID | `200`，资料详情 |
| `GET /api/documents/{document_id}/chunks` | 路径 ID | `200`，`{items}`，按 `seq` 排序 |
| `PUT /api/documents/{document_id}` | `{"content":"完整正文"}` | `200`，`{id, index_status, reindexed}` |
| `POST /api/documents/{document_id}/reindex` | 路径 ID，无请求体 | `202`，`{id, index_status, reindexed}`，重处理已提交 |
| `DELETE /api/documents/{document_id}` | 路径 ID | `204`，撤下向量、软删除资料并删除切片 |
| `POST /api/questions` | `{"question":"问题"}` | `200`，已保存的回答、出处、检索过程与历史元数据 |
| `POST /api/questions/stream` | `{"question":"问题"}` | `200 text/event-stream`，来源、增量和最终已保存结果 |
| `GET /api/question-history` | `page=0`、`size=20` | `200`，`{total, items}` 历史摘要 |
| `GET /api/question-history/{history_id}` | 路径 ID | `200`，历史问题、回答与来源快照 |
| `DELETE /api/question-history/{history_id}` | 路径 ID | `204`，删除该条问答历史 |
| `GET /api/model-config` | 无 | `200`，当前生效的配置，固定六个公开字段 |
| `PUT /api/model-config` | `provider/model/base_url`，可选 `api_key` | `200`，保存后的六字段配置 |
| `DELETE /api/model-config` | 无 | `200`，移除本机覆盖后重新读取启动配置 |

`document_id`、`history_id` 按有符号 64 位整数解析，格式错误或溢出返回 `400`；不存在或已删除的资料、问答历史返回 `404`。

| 列表参数 | 规则 |
|---|---|
| `page` | 从 `0` 开始，非负有符号 32 位整数；缺省或 `page=` 均为 `0` |
| `size` | `1..100` 的整数；缺省或 `size=` 均为 `20` |
| `status` | `PENDING / INDEXING / INDEXED / FAILED`，忽略大小写；缺省或全空白不筛选；非空值不要带首尾空格 |
| `q` | 去掉首尾空白后，在标题中作字面包含搜索；`%`、`_` 不是通配符；缺省或空白不筛选 |

资料列表按 `updated_at` 降序、`id` 降序排列，不包含已删除资料。`status/q` 仅适用于资料列表。`page=1.0` 等小数写法不会转换为整数。

## 资料与切片

单次上传一份 `.md` 或 `.txt`，后缀忽略大小写，内容必须是非空 UTF-8 文本；开头的 UTF-8 BOM 会移除。文件产品上限为 **1 MiB（1,048,576 字节）**，上传请求整体另有 **4 MiB** 硬上限，包含 multipart 开销且对没有 `Content-Length` 的请求同样计数。两种超限都返回 `400`。更新正文也受 1 MiB 上限约束。

标题优先取 frontmatter 的 `title`，再取正文第一个一级标题，最后取文件名；`tags` 来自支持的 frontmatter 标签数组。公开上传只产生 `source_type: "UPLOAD"`，没有 URL 抓取接口。

以下 JSON 是字段示例，ID、时间、分数和 token 数以实际响应为准。上传成功只说明已保存为 `PENDING`，不表示索引完成。

查询、变更或恢复占用门禁时，上传仍可保存为 `PENDING`。队列保留遇到 `BUSY` 的任务，等待门禁回到 `READY` 后自动重新提交；等待不会占用索引工作线程，每次执行仍须取得变更许可。`RECOVERY_REQUIRED` 必须先手动确认就绪，不会因重试而自动放行。索引已进入 `FAILED` 的资料不自动重试。停机时取消尚未重投的内存任务、等待已提交任务退出；未处理资料仍保存在数据库，下次启动并确认就绪后重新提交。资料详情中的 `index_status` 是处理结果，上传响应不承诺完成时间。

```json
{"id":7,"title":"Note","source_type":"UPLOAD","index_status":"PENDING"}
```

列表响应：

```json
{
  "total": 1,
  "items": [{
    "id": 7,
    "title": "Note",
    "source_type": "UPLOAD",
    "tags": [],
    "index_status": "INDEXED",
    "chunk_count": 1,
    "updated_at": "2026-09-15T12:30:00"
  }]
}
```

详情响应：

```json
{
  "id": 7,
  "source_type": "UPLOAD",
  "source_uri": "note.md",
  "title": "Note",
  "content": "# Note\n中文",
  "tags": [],
  "index_status": "INDEXED",
  "index_error": null,
  "chunk_count": 1,
  "created_at": "2026-09-15T12:30:00",
  "updated_at": "2026-09-15T12:30:00"
}
```

`index_status` 为 `PENDING / INDEXING / INDEXED / FAILED`；失败诊断在 `index_error`。`chunk_count` 来自实际保存的切片数。详情不暴露内部 `content_hash`、`indexed_at`、`deleted_at`。

切片响应：

```json
{
  "items": [{
    "id": 11,
    "seq": 0,
    "text": "# Note\n中文",
    "byte_start": 0,
    "byte_end": 13,
    "heading_path": "Note",
    "token_count": 8
  }]
}
```

`seq` 从 `0` 开始；`byte_start/byte_end` 是原文 UTF-8 字节区间 `[start, end)`，不能直接作为 JavaScript 字符串下标。`heading_path` 可为空字符串或 `null`；切片列表不重复返回 `document_id`。


切片正文最多 65,535 个 UTF-8 字节；标题路径元数据最多 512 个 Unicode 码点，超长路径在切片时取前 512 个码点，完整标题仍在原文中。切分和合并均满足正文的字节上限及配置的 token 预算；token 数包含正文、标题路径和模型特殊 token。若标题路径与单个正文字符已超过 token 预算，索引会明确失败。

切片范围有序且不重叠，`text` 必须是对应范围的精确原文。首尾和切片之间允许省略纯 Unicode 空白（包括 NBSP、NEL、全角空格），纯空白切片不进入索引；所有非空白字符必须被覆盖。`document.content` 保存完整原文，拼接切片可能缺少这些空白间隙。

更新或重处理提交后的示例：

```json
{"id":7,"index_status":"PENDING","reindexed":true}
```

更新先统一换行并去除正文首尾空白后比较哈希；哈希未变时仅更新修改时间，保留原正文和切片，返回当前状态及 `reindexed: false`。正文改变或显式重处理会撤下旧向量并异步建立新索引，最终状态要从资料接口读取。

## 问答

问答使用单轮检索的正文片段。资料列表的 `total` 是数据库查询结果，但问答流程没有接入该统计或完整目录，不能将问答接口用于可靠的全库计数、完整清单或全库概览。

`question` 必须是非空字符串，最多 2,000 个 Unicode 码点，保留原始问题文本。当前每次问题检索一次，默认 `top_k=5`；HTTP 不开放 `top_k` 或检索轮数参数。检索默认为混合策略：向量与 BM25（同一份片段正文 + 标题路径）各取前 20 个候选做倒数排名融合，再截取 `top_k`；`trace.retrieved[].score` 仍是该片段与问题的向量余弦相似度，只用于展示，`rank` 才是融合后的顺序。`RETRIEVAL_STRATEGY=dense` 时退回纯向量排序。每个问题固定一个回答模型会话，配置变更从下一次问题生效。

```json
{
  "answer": "资料中记录了中文内容。[1]",
  "status": "ANSWERED",
  "history_id": 41,
  "created_at": "2026-09-15T12:30:00",
  "model": {"provider": "openai", "model": "qwen2.5:7b-instruct-q4_K_M"},
  "elapsed_ms": 6400,
  "sources": [{
    "chunk_id": 11,
    "document_id": 7,
    "title": "Note",
    "text": "# Note\n中文",
    "byte_start": 0,
    "byte_end": 13,
    "heading_path": "Note"
  }],
  "trace": [{
    "round_index": 1,
    "query": "资料记录了什么？",
    "retrieved": [{"chunk_id":11,"document_id":7,"score":0.88,"rank":1}],
    "decision": "SUFFICIENT",
    "relevant": [1]
  }]
}
```

`status` 为 `ANSWERED / PARTIAL / REFUSED`，对应判定 `SUFFICIENT / PARTIAL / NONE`。依据不足仍是正常 `200`，`answer` 为拒答说明、`sources` 为空，但 `trace` 可以保留检索结果。检索、模型调用、判定输出、出处查询或历史保存发生技术失败时返回 `502`，不会伪装成拒答。只有历史事务提交成功后才返回 `200`；失败不自动重试模型调用。

`trace[].relevant` 是判定器标为支撑判断的片段 `rank`（C-6）：生成只看到这些片段，`sources` 也只包含它们；`retrieved` 仍列出全部候选。判定器未标出时等于全部候选；拒答时为空数组。旧历史记录没有该字段。

检索结果为空时直接 `REFUSED`，不调用判定或生成模型；非空但无关的结果仍经过判定。新生成的 `ANSWERED / PARTIAL` 答案至少包含一处正文引用，且所有正文编号都必须落在本轮 `relevant` 内（未标出时即 `1..k`）。支持 `[1]`、`[1,2]`、`[1，2]`、`[1-3]` 和 `[1–3]`；区间必须递增且区间内每个编号都可引用。代码、链接、图片、HTML 与转义文本中的数字不作为正文引用。前后端共享引用样例随测试执行。

缺少引用、越界或有效与无效编号混用均为内部 `QaError(generate, INVALID_CITATIONS)`，接口返回通用 `502` 问答失败，不查出处、不保存成功历史、不自动重试。这是编号与来源映射校验，不能证明每个事实都得到来源支持；既有历史快照保持原样。

`sources` 按最终检索排名排列，以 `chunk_id` 关联 `trace.retrieved`；引用编号对应从 `1` 开始的 `rank`。来源列表表示提供给回答的依据，正文实际使用了哪些引用需另行统计。若出处无法与切片 ID 对齐，服务要求恢复并返回 `503`。

`history_id` 标识本次保存的独立记录。`model` 只包含实际执行本次问答的 `provider/model`，来自同一个冻结会话，不含地址或凭据。`elapsed_ms` 从服务端创建会话前计时，到回答与来源准备完成为止，不包含历史写库和网络传输。`created_at` 是数据库保存时间，沿用资料时间的 Asia/Shanghai 无偏移 ISO 格式。

### 阶段耗时日志

通过 `python -m app` 启动时，每次获准执行的问答在结束时输出一条 `INFO app.application.questions question_timing {...}`。日志独立于 HTTP 响应和历史 schema，用于比较实际瓶颈；参数校验失败或门禁拒绝的请求没有执行阶段日志。

| 字段 | 计时边界 |
|---|---|
| `session_ms` | 创建本问冻结模型会话 |
| `embedding_ms` | 问题 embedding 调用 |
| `vector_ms` | embedding 前的索引探测与随后的向量查询，累加且不包含 embedding |
| `judge_ms` / `generate_ms` | 各自模型调用 |
| `sources_ms` | 查找当次来源 |
| `history_ms` | 保存完成历史，包括提交 |
| `total_ms` | 创建会话前至历史提交或失败处理结束，包含应用编排 |
| `status` / `failed_stage` | `ANSWERED / PARTIAL / REFUSED / FAILED`；成功时失败阶段为 `null` |

未执行的阶段为 `null`，异常调用仍计入已消耗时间。`failed_stage` 区分 `session/search/judge/generate/sources/history`，检索内部耗时由 embedding/vector 两字段定位。日志不含问题、答案、来源正文、服务地址或凭据；`total_ms` 不包含网络传输与浏览器渲染，不能当作用户看到完整答案的时间。原 `elapsed_ms` 仍截止历史保存前。

## 流式问答

网页通过 fetch POST 读取 SSE，不使用自动重连。事件格式为 `event: 类型`、`data: JSON` 与空行；JSON 中的换行经过转义。响应禁止缓存，代理应关闭响应缓冲。

| 事件 | data | 语义 |
|---|---|---|
| `sources` | 来源数组 | 按生成时固定的检索 rank 排列，先核实来源完整性 |
| `delta` | 文本字符串 | 尚未经过最终引用校验的预览，可能包含不完整 Markdown |
| `done` | 与 JSON 问答相同的完整结果 | 引用校验通过且 MySQL 历史提交成功，包含 history_id |
| `error` | `{error, state?}` | 本次技术失败或门禁拒绝，停止消费；不伪装成 REFUSED |

生成型答案按 sources → delta → done/error 输出；拒答不进入生成，可以直接 done。参数错误在响应前返回 400；已开始 SSE 后的门禁、模型、引用和保存错误使用 error，HTTP 状态仍为 200。EOF 不等于成功。客户端不自动重试，因为提交开始后断连可能已保存成功但客户端没有收到 done；应查看历史再决定是否重问。

断连与历史提交开始通过锁串行化：取消先发生则不提交，提交先发生则允许一次原子提交完成。同步 SDK 调用不能强行中断，取消后不开始下一阶段，已有调用结束或超时后由原工作线程关闭生成器并释放 QUERY/request_work；停机等待它实际结束。部署使用当前单 worker Uvicorn（ASGI HTTP 2.3），断连由响应监听处理。

流式日志增加 `stream=true`、`first_text_ms`（从取得 QUERY 许可后至首段非空文本，不含 HTTP 解析与线程调度）和 CANCELLED 状态；`total_ms` 包含提交及队列等待，不含浏览器渲染，`generate_ms` 包含生成迭代期间的背压等待。前端首字时间从提交到收到首个非空增量单独计算，不能与 SDK 首段时间混用。未完成预览不作为历史记录或已核验引用展示。

## 问答历史

`GET /api/question-history` 使用上面的 `page/size` 规则，按 `created_at` 降序、`id` 降序返回摘要。列表不加载答案、来源和 trace，示例：

```json
{
  "total": 1,
  "items": [{
    "id": 41,
    "question": "资料记录了什么？",
    "status": "ANSWERED",
    "created_at": "2026-09-15T12:30:00",
    "model": {"provider": "openai", "model": "qwen2.5:7b-instruct-q4_K_M"},
    "elapsed_ms": 6400
  }]
}
```

`GET /api/question-history/41` 返回 `id/question/answer/status/created_at/model/elapsed_ms/sources/trace`。其中 `id` 等于问答响应的 `history_id`，其余同名字段与保存时一致；没有 `history_id` 重复字段。`ANSWERED / PARTIAL / REFUSED` 都可保存，拒答仍可能有 trace 而没有 sources。

`sources` 保存完整来源文本、标题和字节范围；它们不再依赖当前资料和切片。历史引用应直接显示这份快照，不能用保存的 `document_id/chunk_id` 跳转到当前原文。trace 中能匹配 sources 的条目可展示对应快照；没有保存来源的条目只展示检索记录。更新或删除资料、重建索引不改写历史。

`DELETE /api/question-history/41` 成功返回 `204`；不存在或重复删除返回 `404`。删除只影响这条历史记录，不删除资料、切片或向量。历史列表、详情与删除不经过问答门禁，不调用 embedding 或 LLM；数据库不可用时返回 `503`。

重新提问直接调用 `POST /api/questions`，只发送原问题文本，使用当前知识库和模型并创建新历史。没有会话或轮次，也不会将旧问答自动加入上下文或检索索引。

## 模型配置

保存请求示例使用本地 Ollama 的占位 key，不是真实凭据：

```json
{
  "provider": "openai",
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "base_url": "http://localhost:11434/v1",
  "api_key": "ollama"
}
```

`provider` 只接受 `openai / deepseek`，Ollama 使用 `openai` 兼容协议。`model` 去首尾空白后须为 `1..200` 字符；`base_url` 最多 2,048 字符且必须是 HTTP(S)，不能包含用户信息、查询参数或片段；`api_key` 最多 4,096 字符。请求不接受额外字段。

`api_key` 可省略、为 `null` 或留空，但只有服务类型和规范化后的接口地址与当前配置一致时才能沿用密钥；首次配置或更换目标地址必须提供密钥。保存只校验字段与持久化，能否回答需实际提问验证。

GET、PUT、DELETE 成功时都只返回以下六个字段：

```json
{
  "configured": true,
  "provider": "openai",
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "base_url": "http://localhost:11434/v1",
  "api_key_configured": true,
  "source": "local"
}
```

`source` 为 `local` 或 `environment`。没有可用启动配置时返回 `configured: false`、`api_key_configured: false`、空 `model`、默认 `openai` 地址及 `source: "environment"`。覆盖文件损坏或不可读则返回 `503`。

F 模块负责原子保存和移除本机覆盖文件。所有模型配置响应（包括错误与尾斜杠重定向）都带 `Cache-Control: no-store`。API 不回传密钥，错误不回显提交的凭据或上游响应正文；浏览器不应缓存密钥。切换回答模型不重建向量。

HTTP 开始接收请求前，G 调用 F 的 `Models.prepare()` 加载 SDK 依赖及消息类，不读取项目模型配置、创建客户端或发送预热请求。准备失败只记录安全错误分类，配置与资料管理仍可用，提问时沿用原来的按需加载和错误映射。模型配置不成为启动条件；每问仍创建一份冻结配置的会话，`READY` 继续只由原有一致性检查确认。

## 健康与运行门禁

`GET /health` 的顶层 `status: "UP"` 只代表进程能响应。`db.status` 单独报告 MySQL/schema 状态；`retrieval` 包含整体 `status` 和 `chroma`、`embedding`、`tokenizer` 的分层状态，以及当前检索策略 `strategy`（`hybrid` 或 `dense`）。依赖 DOWN 时健康接口仍返回 `200`，不能只看 HTTP 状态。

`GET /api/runtime` 示例：

```json
{
  "state": "RECOVERY_REQUIRED",
  "rag_available": true,
  "llm": {"configured":true,"provider":"openai","model":"qwen2.5:7b-instruct-q4_K_M"},
  "embedding": {"provider":"ollama","model":"bge-m3","dim":1024}
}
```

`rag_available` 保留前端兼容语义，表示统一服务可达；检索依赖失败时也为 `true`。此接口只读，不探测模型网络、不加载 tokenizer、不确认恢复。回答模型未配置或无法读取时，`llm` 为 `{"configured":false,"provider":null,"model":null}`。

| `state` | 含义 |
|---|---|
| `RECOVERY_REQUIRED` | 启动或变更未确认一致；需人工检查并确认恢复 |
| `READY` | 门禁允许开始操作 |
| `QUERYING` | 问答进行中，暂停资料变更 |
| `MUTATING` | 资料变更或索引进行中，暂停问答与其他变更 |
| `RECOVERING` | 正在核对资料、切片和向量 |

`POST /api/admin/ready` 检查依赖，以及所有已索引资料的切片 ID、内容和元数据是否与向量一致；遗留 `INDEXING`、缺失或多余向量等会拒绝就绪。通过后开放门禁并重新提交 `PENDING`，示例为 `{"state":"READY","recovered":0}`。`recovered` 是重新提交数，不是已完成数；后台处理已开始时响应状态也可能是 `MUTATING`。页面必须由用户点击「确认就绪」，不会自动放行。

## 错误

普通错误结构为 `{"error":"可展示的说明"}`；门禁与恢复错误额外携带 `state`。参数校验统一返回 **400，不返回默认 422**；包括 JSON 格式错误、字段类型错误、缺少必填项与整数溢出。

| 状态码 | 触发条件 |
|---|---|
| `400` | 请求不合法、文件类型/大小/编码不支持、模型配置被拒绝 |
| `404` | 资料、问答历史不存在或已删除；路由不存在 |
| `405` | 路由不支持该请求方法 |
| `409` | 更新、重处理、删除或确认就绪遇到运行门禁忙 |
| `502` | 问答技术失败，包括未配置回答模型、上游调用或历史保存失败 |
| `503` | 数据库/模型配置不可用、需恢复、变更未确认完成；问答遇到门禁忙也为 `503` |
| `500` | 未预期的服务异常，返回脱敏通用说明 |

例如未就绪时提问：

```json
{"error":"当前操作暂不可用","state":"RECOVERY_REQUIRED"}
```

不存在公开的 `/chunk`、`/embed`、`/query`、`/reset`、`/index/reset` 等内部操作路由；索引重建只通过停服维护命令执行。
