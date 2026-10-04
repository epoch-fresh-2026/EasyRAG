# 子 Issue C：问答 Agent 模块

本文供维护知识库问答流程、模型提示词与引用契约的开发者使用。阅读后应能追踪一次回答为何直答、部分回答或拒答，并判断技术失败应在哪里暴露。总体边界见[架构设计](架构设计.md)，浏览器入口见[公共 API](api.md)。

## 一、职责

问答模块 `qa` 负责证据判定、生成与拒答、引用编号校验和 trace。它只依赖自己声明的 `SearchPort`、`ChatPort` / `StreamChatPort`，不直接导入 A/B/F/G、FastAPI、Chroma 或模型 SDK。

G 注入 B 的检索适配器和 F 打开的模型会话，获取 C 的结果后调用 A 补全出处、保存问答快照。问题向量化和混合检索属于 B；C 决定如何使用返回证据。模型配置与探测属于 F，HTTP、门禁、取消控制和历史提交属于 G。

当前是固定的一轮检索流程，没有 `create_agent`、自主工具选择、查询改写或重查。多步骤来自“检索 → 判定 → 分流 → 生成或拒答”，不能把 trace 的结构扩展能力写成已经执行多轮。

## 二、当前行为

### 单轮检索与三态判定

```text
问题
  → B 的默认混合检索，top_k=5
  → 无候选：直接 NONE，不调用判定或生成
  → 有候选：同一回答模型会话完成结构化判定
      ├─ SUFFICIENT → 按 relevant 筛选 → 生成 → 引用校验 → ANSWERED
      ├─ PARTIAL    → 按 relevant 筛选 → 带覆盖边界生成 → 引用校验 → PARTIAL
      └─ NONE       → 固定拒答，不生成 → REFUSED
```

问题必须非空白，最多 2,000 个 Unicode 码点，验证后保留原文本。`top_k` 在模块入口可配置且必须为正整数，当前应用调用使用默认 5。每次回答只调用一次 search，trace 中 `round_index=1`。

| 判定 | 含义 | 行为 |
|---|---|---|
| `SUFFICIENT` | 有与主题直接相关的片段，足以支撑完整回答 | 只依据选中片段作答 |
| `PARTIAL` | 有直接相关内容，但缺失的是问题主体而非边角 | 基于已有内容作答，并声明库中覆盖范围和无法回答的部分 |
| `NONE` | 没有与主题直接相关的片段，词面相似不算直接相关 | 返回“知识库中没有找到能回答这个问题的内容。” |

这个判定不由相似度阈值决定。分数可以表达检索相似程度，却不能说明片段是否覆盖问题主体；相关专题也可能不足以回答具体问题。PARTIAL 必须同时满足“直接相关”“不足以完整回答”“缺失主体”，避免把任意边角缺失都判为部分覆盖。

### relevant 与原始编号

判定提示要求模型在同一次调用中给出状态和相关片段编号：

```json
{"verdict": "SUFFICIENT", "relevant": [1, 3]}
```

候选按检索顺序编号 `1..k`。`relevant` 表示支撑判定的候选 rank，不是 chunk ID。生成提示只包含被选中的证据，编号仍沿用原 rank，**不压缩为新的连续编号**；例如选中 `[1, 3]` 时，生成器只能引用 `[1]` 和 `[3]`。

- `SUFFICIENT` / `PARTIAL` 的 relevant 若出现，必须是非空整数数组，编号处于 `1..k`；布尔值、越界值、非数组和空数组均是 `INVALID_JUDGE_OUTPUT`，不进入生成。
- 重复编号去重，最终选择顺序按原候选顺序，而非模型列出的顺序。
- 缺少 relevant 时保留全部候选；格式遗漏与模型明确输出错误指令采用不同处理。评估按实际送入生成的证据计算压缩比例。
- `NONE` 忽略 relevant，trace 记空数组，不调用生成。

`trace.retrieved` 保留全部候选及原排名，`trace.relevant` 保留被选中的 rank。`AnswerDraft.chunk_ids` 包含全部交给生成器的证据 ID，不缩为正文实际引用的子集。前端按 relevant 展示“采用 / 未进生成”，并单独标注正文引用；旧记录缺少该字段时明确提示未保存采用情况。

### 生成与引用校验

生成提示要求只使用片段中的信息，并在事实性结论后用 `[n]` 标注来源。PARTIAL 额外要求说明未覆盖的范围。判定和生成是两次独立调用，使用 G 为本次问题打开的同一个模型会话；运行中更改配置不会更换正在回答的模型。

答案返回前，C 检查至少存在一个正文引用，且全部引用编号属于选中证据集合。支持单号、列表和范围；如只选中 `{1, 3}`，`[1,3]` 合法，`[1-3]` 因包含 2 而非法。代码、链接、图片、HTML 和转义文本中的数字不当作正文引用。前后端使用共享引用样例保持 Markdown 解析口径一致。

无引用、全部越界或部分越界均抛出 `QaError("generate", "INVALID_CITATIONS")`，不自动重试，也不保存为成功历史。这个检查只证明引用编号能映射到允许的来源，不能证明每句话都受来源支持；语义忠实度需要独立在线评估。

### 流式回答与失败

流式入口复用同一套检索、判定和 relevant 筛选。可生成时依次产生 `sources`、零到多段 `delta`，累积答案通过引用校验后才产生 `done`。拒答直接产生完成结果，不调用生成。消费者取消后，在下一个阶段和读取模型分块前后检查取消状态，并关闭嵌套生成器；已开始的同步模型调用不因此保证被立即中断。

部分文本到达不代表回答成功。最终引用校验失败时，流不会产生成功 `done`；G 负责把取消、模型故障和历史提交结果映射到 HTTP/SSE 行为。完整事件契约见[公共 API](api.md)，提交边界见[应用编排](子Issue-G-应用编排.md)。

模型输出无法解析、非法判定、空模型内容、检索失败或生成失败都是技术错误。拒答是有依据的业务结果，不能拿技术错误代替拒答，否则拒答正确率会失去意义。C 只暴露 `QaError(stage, cause)` 的阶段与安全原因分类，原始上游响应体不越过模块边界。

`record_timing(stage, elapsed_ms)` 分别记录 `judge`、`generate`，失败调用也记录耗时；未执行的阶段不生成计时。回调不包含问题、提示词和凭据。B 的 embedding/索引耗时以及 G 的模型会话、出处、历史与总耗时单独记录。

## 三、数据与接口

### 能力端口与结果（伪代码）

```python
Evidence = {chunk_id, document_id, text, heading_path, score}

SearchPort.search(query, top_k=5) -> tuple[Evidence]
ChatPort.complete(prompt) -> str
StreamChatPort.stream(prompt) -> Generator[str]

TraceHit = {chunk_id, document_id, score, rank}
QaTraceEntry = {
    round_index: 1,
    query: 原始问题,
    retrieved: tuple[TraceHit],    # 全部候选，rank 从 1 起
    decision: SUFFICIENT | PARTIAL | NONE,
    relevant: tuple[int],         # 选中候选的原 rank
}
AnswerDraft = {
    answer: str,
    status: ANSWERED | PARTIAL | REFUSED,
    chunk_ids: tuple[int],        # 所有选中证据；不表达排名
    trace: tuple[QaTraceEntry],   # 当前只有一条
}

Qa.answer(question, search, chat, top_k=5, *, record_timing=None) -> AnswerDraft
Qa.stream(question, search, chat, top_k=5, *, record_timing=None,
          cancelled=lambda: False) -> Generator[QaStreamEvent]
QaStreamEvent = sources(chunk_ids, trace) | delta(text) | done(AnswerDraft)
validate_question(question) -> None
```

C 无新增数据库表，也不读取 A 的文档标题或原文字节位置。B 返回纯正文及标题路径；G 依据 chunk ID 调 A 获取完整 `Source`，再按 trace rank 对齐。若来源缺失或无法匹配，G 将其视为索引一致性错误，需要恢复，不把缺少出处的答案当作成功结果。

```text
POST /api/questions  {question}
  → {answer, status, sources, trace, history_id, created_at, model, elapsed_ms}

POST /api/questions/stream  {question}
  → SSE：来源、文本增量、最终结果或错误
```

普通问答的输入错误返回 400，业务门禁未就绪返回 503，判定/生成技术失败由 G 映射为 502。流式开始后的错误由 SSE 事件表达，不复用已经发出的 HTTP 状态。详细状态码、事件名和快照格式以[公共 API](api.md)为准。

### 一次编号对齐示例

```text
检索：rank 1 → chunk 101，rank 2 → chunk 205，rank 3 → chunk 309
判定：{"verdict":"PARTIAL", "relevant":[3, 1, 3]}
选中：rank 1 → chunk 101，rank 3 → chunk 309
生成：只看 [1] 与 [3]，要求声明覆盖边界
答案：正文含 [3]，状态 PARTIAL
校验：允许集合 {1, 3}，通过
输出：chunk_ids = [101, 309]；trace 仍含三个候选，relevant = [1, 3]
G：补全两个 Source，按 rank 1、3 排序，保存完成快照
```

即使正文只引用 `[3]`，返回的 sources 仍包含全部交给生成器的两个片段。客户端必须按 trace 的原 rank 映射引用，不能用 sources 数组下标重新编号。

## 四、关键取舍与有效裁决

| 编号 | 当前结论 | 依据与代价 |
|---|---|---|
| C-1 | 使用自有固定步骤流程，不使用 `create_agent` | 检索、三态判定、分流和 trace 都能独立验证；当前没有需要模型自主规划工具调用的证据。 |
| C-2 | 不做查询改写重查 | 原问题在当前混合检索评估中已经召回目标文档，增加一轮没有已证实的可挽救样本；触发重新评估的条件是出现 hybrid top-5 仍遗漏标注来源的 Q/R 题。 |
| C-3 | 当前只执行一轮；若后续验证重查，预算方向为总计最多两轮 | “初始检索 + 至多一次改写”是尚未启用的方向，不是现有 `max_rounds` 参数或第二轮实现。额外轮次同时增加模型和检索成本。 |
| C-4 | 判定和生成共用同一模型会话，但保持两次独立调用 | 判定结果可单独测试和统计；省去第二套模型配置与探测，不把判断隐藏在最终答案里。 |
| C-5 | 回答模型支持 API 为主、本地可切 | 由 F 管理 provider/model/base_url 与会话快照，C 不分 provider，也不把某次评估模型写成代码默认值。 |
| C-6 | 判定同时选出 relevant，生成和引用只使用选中片段 | 减少无关上下文和生成提示长度，同时保留原编号；错误编号显式失败，字段缺失回退全部候选。 |

### C-2：为什么暂不增加检索轮次

v1 的 R 类六题虽然标为“需改写”，原问题已全部进入 top-5。补充 v2 后，dense 的 Q21 漏召回由 B-17 的混合检索解决；[v3 难例报告](eval/retrieval-v3-hybrid-2026-09-22.md)包含错别字、缩写、中英混用和专题压制短卡，hybrid 的 Q/R hit@1 为 15/15。当前 v1/v2/v3 中，没有 hybrid top-5 遗漏标注来源的 Q/R 题。

这些样本支持暂不增加轮次，不代表重查永远无效，也不代表检索命中后一定答得完整。部分覆盖和引用不忠实可能发生在候选已正确召回之后，需要先区分检索与生成问题。V2 只保留有界重查方向；分类树、全库统计和资料目录/概览问答均未实现，此处不扩展新的工具设计。

### C-6：证据筛选的收益与限制

[relevant 在线报告](eval/answers-relevant-2026-09-22.md)中，32 道已作答问题的 160 个候选只将 76 个送入生成，比例 47.5%；没有出现标注出处进入候选却被判定器剔除的题。相对[全部候选对照](eval/answers-hybrid-2026-09-22.md)，生成提示字符数中位数从 1,881 降至 1,096，完整请求中位数从 8,927 ms 降至 6,116 ms。

同一批报告的错源率均为 0/30，库外拒答正确率均为 10/10；引用支持率从 85.7% 到 87.3%，仍有不被来源支持的句子。模型输出、上游延迟和样本规模限制了这些数字的外推范围；它们是已有对照证据，不是稳定延迟承诺，也不能将编号合法当成答案忠实。

## 五、验证依据

| 验收对象 | 应验证的行为 | 现有依据 |
|---|---|---|
| 三态流程 | 无候选不调用模型，NONE 不生成，PARTIAL 一轮并声明边界 | `test_qa_module`、`test_qa_answer_contract` |
| relevant | 子集使用原 rank、缺失回退、错误字段失败、NONE 忽略、并发状态不串扰 | `test_qa_module` |
| 引用 | 至少一个正文引用、允许集合校验、范围展开、Markdown 非正文排除 | `test_qa_answer_contract` 和共享 `answer-citations.json` 样例 |
| 流式 | 来源先于增量，最终校验后才 done，取消阻止后续阶段，关闭模型流 | `test_qa_stream`、应用/HTTP 流式测试 |
| 业务出口 | 同一模型会话、原 rank 对齐、来源完整、历史提交失败不报成功 | `test_application_questions`、`test_question_stream`、真实 MySQL HTTP 集成 |
| 可替换性 | 使用端口替身即可测试；C 不导入其他业务模块或 SDK | `test_module_boundaries` |

离线召回与在线问答验收必须分开。[评估模块](子Issue-D-离线评估.md)说明拒答、部分覆盖、错源和引用支持率的口径；既有报告保留模型、语料、计时和适用范围，不能用某次命中率替代端到端回答质量。
