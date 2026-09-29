# 子 Issue C：问答 Agent 模块

## M4 第三项：判定器标出相关片段，生成只看子集（2026-09-22，C-6）

继续关联 #27 / #1。判定器的 JSON 由 `{"verdict"}` 扩为 `{"verdict", "relevant": [n, …]}`：`relevant` 是与问题主题直接相关、支撑该判定的片段编号；生成提示只列这些片段，**编号沿用检索 rank 不重编**，正文引用必须落在其中，`AnswerDraft.chunk_ids` 也只含这些片段。`QaTraceEntry` 新增 `relevant`（选中的 rank，按候选顺序去重）；`retrieved` 仍记录全部候选，G 与前端按 rank 解析出处的逻辑不变。

契约细则：

- SUFFICIENT / PARTIAL 时 `relevant` 必须是非空整数数组、编号落在 `1..k`；空数组、越界、非整数、非数组都是 `QaError("judge", "INVALID_JUDGE_OUTPUT")`，不进入生成。
- NONE 时忽略 `relevant`，trace 记为空。
- **缺失 `relevant` 视为全部候选**（与 C-6 之前的行为一致）。没输出字段和输出了错误字段是两件事：前者退回旧契约、可被评估脚本按"送入生成的比例"度量；后者是模型给出了错误指令，必须响亮失败。这样既不把格式遗漏升级成用户可见的 502，也不让错误编号静默通过。
- `citations_are_valid(markdown, allowed)` 接受允许的 rank 集合（旧的整数 k 仍表示 `1..k`），区间 `[a-b]` 展开后逐个检查。

| # | 裁决 | 理由 |
|---|---|---|
| C-6 | 判定器在同一次调用里多输出"哪些片段支撑判断"，生成与引用只看这些片段 | 不违背 C-4——仍是两次独立的结构化调用，判定输出仍是结构化判断，只是从一个枚举值变成枚举值加编号列表；不把判定并入生成。收益来自两处：送入生成的证据 token 减少（"效率 = 端到端延迟"唯一有实质空间的杠杆，生成占八成以上），以及不相关片段不再进入生成上下文（跨文档串扰）。数字见子 Issue D 的 relevant 对照 |
| C-2 复核（2026-09-22） | 有界改写重查继续不做 | 难例集 v3（错别字、缩写、中英混用、专篇压制短卡）在混合检索下离线 hit@1 15/15；v1/v2/v3 三集都没有漏召回的题，改写重查没有可救的对象。钩子保留，触发条件写明：某集出现 hybrid 仍未进 top-5 的 Q/R 题 |

## M4 第一项：回答约束与计时（2026-09-16）

继续关联 #27 / #1，按 [M4 方案](子Issue-C-问答Agent.md) 实施。检索为空时直接 REFUSED，不调用判定/生成；非空候选保持原有三态判断及一轮检索。

生成结果返回前校验正文来源编号：必须有引用，且所有编号在本轮 `1..k` 内。未引用、全部越界或混合越界均为 `QaError("generate", "INVALID_CITATIONS")`，由 G 返回技术失败，不自动重试或保存历史。代码、链接、图片、HTML 与转义文本中的数字不是正文引用；前后端以 `tests/contracts/answer-citations.json` 的共享样例核对。该校验确认编号可映射到来源，不声称能自动证明每个事实都由来源支持。

```python
answer(question, search, chat, top_k=5, *, record_timing=None) -> AnswerDraft
# record_timing("judge" | "generate", elapsed_ms)
# 回调只承载本次模型调用耗时，不包含问题、提示词或模型凭据。
```

`chunk_ids` 保留所有交给生成器的证据，不缩为正文实际引用的子集。G 继续按最终 trace rank 补全出处与保存快照。未执行的模型阶段不产生计时；失败调用也报告耗时，之后仍抛出原有安全异常。SDK 准备、流式、有界重查按后续独立任务推进。

## FastAPI 迁移设计（2026-09-15，当前实施范围）

Issue [#27](https://github.com/vansye/EasyRAG/issues/27)，父 Issue #1。位置：`app/modules/qa`，总体见 [模块设计](fastapi-modules.md)。旧 Java 入口与具体 IndexStore 调用作为历史记录保留。

C 自己维护提示词、判断、生成、拒答、引用编号和 trace，只依赖自己声明的能力端口。检索适配器和模型会话由 G 注入；不得 import A/B/F/G、FastAPI、Chroma 或模型 SDK。无需数据库或模型服务即可单独测试。

```python
Evidence = {chunk_id, document_id, text, heading_path, score}
SearchPort.search(query: str, top_k: int) -> tuple[Evidence]
ChatPort.complete(prompt: str) -> str
AnswerDraft = {answer, status, chunk_ids, trace}
answer(question, search: SearchPort, chat: ChatPort, top_k=5) -> AnswerDraft
```

保留现有三态判断、提示词、轮次和 trace 字段；不启用此前尚未实现的改写或额外模型调用。NONE 不生成；模型输出无法解析是技术失败，不伪装成库外拒答。每次问答的临时 trace 独立。C 输出 chunk ID，最终业务出处由 G 调 A 补充。

- [x] 能力端口与问答引擎，注入检索/模型替身完成三态、引用、解析失败及请求状态隔离回归（PR #46）。
- [x] 集成验证判定与生成使用同一模型会话；模型更换不影响在途问答（PR #52、#55）。
- [x] 模块 import 约束通过；G 的适配器变化不修改 C 的算法实现。

实现见 [PR #46](https://github.com/vansye/EasyRAG/pull/46)，问答装配与最终 trace rank 对齐见 [PR #52](https://github.com/vansye/EasyRAG/pull/52)，真实 SQL/Chroma/HTTP 模型集成见 [PR #55](https://github.com/vansye/EasyRAG/pull/55)。改写重查保留原规划状态，本次不增加调用轮数。

## 历史实现与契约：旧调用入口

> 父 Issue：#1 总功能文档
> 位置：Python（`rag-service/app/qa.py`）+ Java 入口（`server`）
> 对应考察点：检索链路与不准的兜底（必答题 2）、agent 处理"一步答不好"（必答题 3）、拒答（追问 1）
> 目标用户故事：**U4 答案 + 可点开的出处**、**U5 库外拒答**、U7 检索过程可见（trace 供给前端）

## 一、为什么需要这个模块

收录链路（A-1）已把资料变成可检索的向量索引，但没有任何入口能"问一句"。三条兜底功能中的第二条——基于知识库内容问答、答案能溯源——是本项目的核心，也是评审最想看的部分。

## 二、功能描述

```
问题 → 检索 top-k 片段 → 三态判定 → 分流
                                    ├─ SUFFICIENT → 生成带 [n] 引用的答案
                                    ├─ PARTIAL    → 带边界声明生成（改写钩子位，M2 不启用）
                                    └─ NONE       → 拒答，不进入生成
```

**三态判定标准**（判断力记录 #15，实测排除相似度阈值与子方面覆盖数后确定）：

1. top-k 中至少一个片段与主题直接相关（非词面巧合）
2. 该片段无法支撑完整回答（把片段单独交给 LLM 问"仅凭这段能否完整回答"）
3. 缺失部分是问题主体而非边角

第 2 条是核心，1、3 是护栏。判定输出结构化三值 `SUFFICIENT | PARTIAL | NONE`。

**拒答是一等行为**（架构不变量 4）：判定为 NONE 不调用生成，返回固定的"库里没有相关内容"语义——不编造、不用低分片段凑数。

**trace 结构**（供 U7 前端展示检索过程）：

```
trace = {
  rounds: 1,                        # 检索轮数（改写启用后可为 2）
  retrieved: [                      # 本轮检索的 top-k，含分数
    {chunk_id, document_id, score, rank}
  ],
  decision: "SUFFICIENT",           # 三态判定结果
  # 改写钩子：PARTIAL 时 rewrite → 重查 → 仍 PARTIAL 才带边界生成（M2 留位）
}
```

## 三、内部逻辑拆分（用接口表示）

现有组件（复用，不改）：

```
embed_texts(client, [question], settings) → [[float]]     # app/embedding.py，问题向量化
create_chat_model(settings) → BaseChatModel               # app/llm.py，判定与生成共用
IndexStore（新增 query，见子 Issue B 侧改动）
```

本模块新增：

```
app/retrieval.py
  retrieve(question, settings, index) → RetrievedChunk[]
    1. 问题 embedding（embed_texts，单条）
    2. IndexStore.query(vector, top_k)
  RetrievedChunk = {chunk_id, document_id, text, heading_path, score}

app/qa.py
  judge(question, chunks, model) → "SUFFICIENT"|"PARTIAL"|"NONE"
    LLM 结构化输出：严格 JSON 解析，失败是错误不是静默拒答
  generate(question, chunks, model) → answer
    prompt 给片段编号 [1..k]，要求引用标记 [n]；拒绝编造未给出的内容
  answer_question(question) → {answer, status, chunk_ids, trace}
    单轮编排（rounds=1）；PARTIAL → 带边界声明生成（钩子注释位）
    status: "ANSWERED"|"PARTIAL"|"REFUSED"

app/main.py 新端点
  POST /query {question: string}
    → 200 {answer, status, chunk_ids[], trace}
    → 422 空/超长问题
    → 503 EMBEDDING_UNAVAILABLE / INDEX_UNAVAILABLE / LLM_UNAVAILABLE
```

Java 侧新增：

```
QuestionController（POST /api/questions）
  1. gate.tryAcquire(QUERY)——RECOVERY_REQUIRED/MUTATING 态 → 503 明确反馈，
     不调 LLM，不伪装成"检索无结果"
  2. RagQueryClient → Python POST /query
  3. chunk_ids → MySQL 补出处（标题、byte_start/byte_end、heading_path）
  4. 响应 {answer, status, sources[], trace}
     sources = [{chunk_id, document_id, title, byte_start, byte_end,
                 heading_path, text}]
```

## 四、用示例把接口串起来

**直答路径（Q1：什么是 ACID？）**

```
① POST /api/questions {question: "什么是 ACID？"}
   Java: QUERY 租约（READY 态，与其他 QUERY 并发不互斥）
② Python /query
   embed_texts(["什么是 ACID？"]) → 1024 维向量
   IndexStore.query(vec, k=5) → ACID.md 的 3 个片段 + Redis 事务 2 个
   judge("什么是 ACID？", 5 片段) → SUFFICIENT
     （第 1 片即"ACID 指原子性/一致性/隔离性/持久性"，可完整回答）
   generate → "ACID 是……[1]。事务隔离级别……[3]"
③ Java 用 chunk_ids 查 MySQL → sources 带 title="数据库事务 ACID 特性"、
   byte 偏移、heading_path="详细 > 概念"
④ 前端：答案 + 出处可点开（定位到文档与字节区间）+ trace（5 片段、判定结果）
```

**拒答路径（N1：Redis 的 GEO 命令怎么用？）**

```
② IndexStore.query → Redis 事务/过期处理等片段（词面相似，主题不相关）
   judge → NONE（判据 1 不满足：无片段与 GEO 直接相关）
   不调 generate，直接返回
③ {answer: "知识库中没有找到能回答这个问题的内容。", status: "REFUSED",
    chunk_ids: [], trace: {rounds:1, retrieved:[...], decision:"NONE"}}
```

**为什么出处由 Java 补而不是 Python 返回**：Python 只认识 chunk_id（派生索引里没有文档标题等业务数据）。标题、路径是 MySQL 的真相，去查就违反"B/C 不反向调用 A"。出处补全是"业务出口"的职责，归模块 A。

**为什么判定失败是错误而不是拒答**：拒答是有依据的业务判断（"库里没有"），LLM 输出解析失败是技术故障（"没判出来"）。把后者伪装成前者，会让"拒答正确率"这个评估指标失去意义——错误和正确拒答混在一个分母里。

## 五、数据原型

无新表。检索读 Chroma（chunk_id 作 id、正文作 documents 载荷、metadata 含 document_id/seq/heading_path）；出处查 MySQL `chunk JOIN document`。

**模块 B 侧一处前置修正**：Chroma 的 documents 载荷现存的 `embedding_text`（正文+标题路径拼接串）改为存 `chunk.text`（纯正文）。向量是显式传入的，改载荷不影响向量；不改的话检索回来拿不到干净正文，而"为拿原文反向查 Java"违反边界。已有索引重灌即可（reset + 重新收录）。

## 六、模块边界

**C 负责**：问题向量化、检索编排、三态判定、生成与拒答、trace。
**C 不负责**：怎么切片、索引维护（B）；对外 REST 与出处补全（A）；评估指标计算（D）。
**依赖方向**：A → C（HTTP 调用），C → B（进程内函数调用，读本地派生索引）。C 无任何出站调用（除 LLM/embedding API）——**不存在依赖循环**。

## 七、验收

- [ ] Q1 类问题（答案明确在库）→ 答案含 [n] 引用，sources 的 title/byte 偏移能定位到正确文档与区间
- [ ] N1 类问题（主题不在库）→ REFUSED，明确说库里没有，不编造，不调用生成
- [ ] 判定器返回非法输出（JSON 解析失败）→ 503 错误，不是 REFUSED
- [ ] 闸门未就绪/索引变更中提问 → 503 明确反馈，不调 LLM，不伪装成无结果
- [ ] trace 结构完整：rounds、retrieved（含分数与排名）、decision
- [ ] PARTIAL 类问题（P1 多头注意力）→ 带边界声明的回答（"库中仅有……的简要提及"）
- [ ] 检索 top-k 可配置（默认 5）
- [ ] LLM/模型配置可切换（API 与本地），行为一致

## 八、已裁决（2026-09-12，两圈讨论）

| # | 问题 | 结论 | 理由 |
|---|---|---|---|
| C-1 | 循环形态 | **自研有界循环，不用 create_agent** | 评审标准"只看跑通了什么、想清楚了什么"；固定步骤可测、trace 结构完整（U7 需要）；基线数据（6 道需改写题原问题全部命中 top-5）是"多步无增益"的现成证据。多步≠必须 tools：三态判定+分流本身就是多步流程。后续可将检索注册为真 tool 做对照实验（自主检索词 vs 原问题的命中率）——若补难例后有增益，即带数字的创新点 |
| C-2 | 改写重查 | **M2 不做，留钩子** | 同上数据依据；不为未被证明的收益加复杂度 |
| C-3 | max_rounds | **2**（初始 + 至多 1 次改写） | 30 题规模无证据支持更多轮；每多一轮是 LLM+检索双重成本 |
| C-4 | 判定器模型 | **与生成器同一个 LLM** | 判定与生成需要的语言能力相同；省一套配置与探测；API 模型结构化遵循度高 |
| C-5 | LLM 默认 | **API 为主、本地可切** | 真实使用以 API（deepseek 等）为主，Ollama 作离线备选。provider/model/base_url 三元组配置即切，代码零改动 |

## 九、与其他模块的接口边界

- 对 A：`POST /query` 只被 Java 调用（内部端点，不暴露给前端）；错误码风格与 /chunk、/embed 一致（`detail.error` + cause）
- 对 B：C 读 IndexStore 的 query 方法（进程内），不直接碰 Chroma 客户端
- 对 D：trace 结构是评估的原始材料（改写轮数、判定分布、检索分数——评估平台可直接消费）
- 对 E：`{answer, status, sources[], trace}` 即前端问答页的完整数据契约
