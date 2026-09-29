# 前端设计与实施记录：两页知识工作台

2026-09-14：采用用户确认的方案一，实现「资料库 / 知识问答」工作台及网页回答模型配置。对应前端子 Issue [#33](https://github.com/vansye/EasyRAG/issues/33)，归属总功能 Issue [#1](https://github.com/vansye/EasyRAG/issues/1)。

## FastAPI 迁移兼容验收（2026-09-15）

后端已统一为 FastAPI 8080，现有 Vue 外观、路由、交互和模型配置保持兼容，没有增加轮询。当前 HTTP 由 G 维护，回答模型配置由 F 维护，接口与启动方式见 [API 文档](api.md) 和 [前端 README](../frontend/README.md)。下文旧 Java 文件与 2026-09-14 测试数量作为当时的实施记录保留。

[PR #56](https://github.com/vansye/EasyRAG/pull/56) 的 Vue 15 项测试与构建通过；真实浏览器完成 10 个验收步骤，包括模型保存/回读/恢复、上传与索引、问答引用、更新与重处理、删除后拒答。3 次实际模型请求均返回 200，原有 13 份资料与 623 个切片在浏览器验收前后哈希一致，临时资料已清理。完整切换证据见 [切换记录](../README.md)。

## 用户故事与验收

- [x] U1：上传 Markdown/TXT 后可看到收录与处理状态，支持标题搜索、状态过滤和分页。
- [x] U2/U3：可以查看、修改、重新处理和删除资料；变更后答案使用最新内容。
- [x] U4：回答中的编号引用能定位到实际原文片段，切页保留本次回答。
- [x] U5：无依据时明确拒答；部分覆盖与服务失败分别呈现。
- [x] U7：展示真实检索轮次和片段，不把未引用的片段描述为已被模型丢弃。
- [x] 模型配置：网页填写回答模型的 provider、base_url、model、api_key；保存后下次提问生效，重启后保留，可恢复启动配置。
- [x] 配置边界：API Key 不回传、不写浏览器持久存储；更换接口不得沿用旧密钥；嵌入模型只读。
- [x] 交互：390 px 窄屏可用，抽屉支持键盘与焦点恢复，失败保留输入。

## 数据原型与模块边界

```ts
type Workspace = {
  documents: { total: number; items: DocumentSummary[] }
  question: { draft: string; answer: AnsweredQuestion | null; usedModel: string }
  runtime: { state: string; rag_available: boolean; llm: PublicModel; embedding: PublicModel }
}
type AnsweredQuestion = {
  answer: string
  status: 'ANSWERED' | 'PARTIAL' | 'REFUSED'
  sources: { chunk_id: number; document_id: number; title: string; text: string }[]
  trace: { round_index: number; query: string; decision: string;
    retrieved: { rank: number; chunk_id: number; document_id: number }[] }[]
}
type ModelConfig = {
  configured: boolean
  provider: 'openai' | 'deepseek'
  model: string
  base_url: string
  api_key_configured: boolean
  source: 'environment' | 'local'
}
// PUT 中 api_key 为仅写入字段；留空只在接口及 provider 不变时沿用。
type ModelConfigUpdate = Pick<ModelConfig, 'provider' | 'model' | 'base_url'> & { api_key?: string }
```

前端只调用 G 的统一 FastAPI REST API，负责交互、阅读排版和临时页面状态；不直连模型服务或数据库。A 维护 MySQL 资料真相，B 维护检索索引，C 维护问答流程，F 维护回答模型配置与会话，G 编排跨模块业务。配置保存在被 Git 忽略的本机文件，恢复启动配置不修改 `.env`。资料与切片仍以 MySQL 为准。

本子 Issue 覆盖资料管理、问答与出处和模型配置；URL 抓取、评估看板 U8、会话历史、流式输出、嵌入模型切换不在本次交付范围。

## 页面与设计

资料库 `/` 以标题和可用状态为主，顶部提供添加与拖放入口，表格支持标题搜索、状态筛选和分页。原文与引用片段共用右侧抽屉；正文修改、重新处理、删除都在资料上下文中完成。

问答页 `/ask` 在空态展示输入框与来自实际资料标题的建议。完成后按问题、答案、出处、可展开的检索过程组织阅读。Pinia 保留本次问题和回答，切页不会清空；刷新网页不保留历史。

视觉采用暖灰画布、白色内容区、深绿色强调色。中文衬线标题配合系统无衬线正文，辅助文字保持可读对比度。窄屏隐藏次要表格列，出处卡片转为单列。

## 行为边界

| 行为 | 当前实现 |
|---|---|
| 上传 | 单份 Markdown/TXT，UTF-8，1 MB；轮询索引终态 |
| 详情 | 原文、切片；引用按 chunk_id 定位，失效时提示查看当前内容 |
| 更新 | 哈希相同保留原文和切片；否则清旧索引、保存正文并异步重建 |
| 删除 | 明确确认；先清 Python 索引，再软删文档和清切片 |
| 重新处理 | 可查询资料在抽屉底部操作；失败及待处理状态提供重试入口 |
| 回答 | 正常、部分覆盖、依据不足分别呈现；失败保留问题，等待显示实际时间 |
| 引用 | 依据生成回答那轮 trace 的 rank → chunk_id → source 关联，不使用 sources 数组位置 |
| 检索过程 | 真实轮次、query、判定、检索片段；未在正文引用的条目不叫「被丢弃」 |
| 排版 | 标题、列表、粗体、代码和表格；HTML 为文字，外部图片不自动加载，代码下标不算引用 |
| 就绪 | 只读探测；需要恢复时由用户明确点击按钮，不自动调用 ready |
| 模型 | 显示实际回答与检索模型；原生 dialog 配置回答模型，保存后下次提问生效，可恢复启动配置；嵌入模型只读 |

问答或资料处理时暂停新的资料变更；本页面更新、重建或删除资料后，旧回答提示重新提问。共享抽屉关闭后恢复焦点，方向键和 Home/End 可切换页签。

浏览器仅对接 Java；Python 不访问 Java/MySQL。更新和重建从撤旧索引到新索引终态持续持有同一变更租约，结果不确定时保持恢复状态。

原有后端上传在并发问答时可能遗留 PENDING。本页面在问答或忙碌时禁用上传；跨窗口并发仍沿用后端边界，可以在就绪后通过「重新处理」推进。

回答模型配置保存在 Python 的 `config/llm.json`（可用 `LLM_CONFIG_FILE` 指定路径），优先于环境变量和 `.env`，通过临时文件及原子替换避免半写入。API Key 为仅写字段；留空只在 provider 和实际接口地址一致时沿用，换地址须填新密钥。浏览器不持久化密钥，地址禁止包含用户信息、查询参数和片段，旧环境配置也受此限制。

公开地址、密钥沿用判断和模型构造共享同一解析，兼容 SDK 的 OpenAI / DeepSeek / LangSmith 网关环境变量。并发状态刷新共享在途 Promise，保存后不会再被旧模型信息覆盖。面板在打开和提交时读写配置，不增加轮询；保存成功表示配置已持久化，模型服务可用性由实际提问验证。

## 接口

| 接口 | 用途 |
|---|---|
| GET /health、GET /api/runtime | 数据库健康、RAG 可用性、当前操作状态与公开模型信息 |
| POST /api/admin/ready | 用户明确触发的就绪确认 |
| GET /api/documents?page=&size=&status=&q= | 列表、状态过滤、标题包含搜索 |
| POST /api/documents | multipart 上传 |
| GET /api/documents/{id}、GET /api/documents/{id}/chunks | 详情和当前片段 |
| PUT /api/documents/{id} | JSON {content} 更新正文 |
| POST /api/documents/{id}/reindex | 异步重新处理，202 |
| DELETE /api/documents/{id} | 删除，204 |
| POST /api/questions | JSON {question}，答案、来源和 trace |
| GET /api/model-config | 当前回答模型公开配置与 api_key_configured，不含密钥 |
| PUT /api/model-config | JSON ModelConfigUpdate，保存后用于下一次问答 |
| DELETE /api/model-config | 移除本机覆盖配置，恢复环境变量 / .env 中的启动配置 |

不存在返回 404；忙碌冲突为 409，保留 state；变更结果不确定为 503 / RECOVERY_REQUIRED。Python GET /runtime 只返回 provider、model、embedding dim 与 configured，Java 补充运行状态，不传密钥或模型服务地址。

## 改动文件、原因与影响范围

前端路径相对 `frontend/`；Java 主目录为 `server/src/main/java/com/easyrag/server/`，测试目录为 `server/src/test/java/com/easyrag/server/`。

| 文件 | 原因与影响范围 |
|---|---|
| index.html、src/main.ts、src/app/router.ts、tsconfig*.json、vite.config.ts | Vue 入口、两页路由、TypeScript / Vite 与 Java 开发代理 |
| src/app/App.vue | 两页外壳、顶栏、服务提示、共享抽屉与 query 参数 |
| src/shared/components/AppIcon.vue | 统一 SVG 图标 |
| src/shared/api/client.ts、types.ts | runtime 契约，保留冲突和恢复状态 |
| src/shared/gate.ts、gate.test.ts | 只读状态刷新、在途请求共享、问答可用条件、手动恢复及状态冲突回归 |
| src/features/documents/api.ts、model.ts、store.ts | 接口、状态文案、上传校验、搜索分页、请求顺序与忙碌限制 |
| src/features/documents/DocumentListView.vue | 资料列表、拖放、轮询生命周期、空态与错误提示 |
| src/features/documents/components/DocumentDrawer.vue | 原文、片段、编辑、重建、删除、键盘和焦点 |
| src/features/qa/model.ts、model.test.ts | 编号引用、实际引用计数、Markdown 结构与顺序回归 |
| src/features/qa/store.ts、AskView.vue | 当前问答、实际耗时、失败保留输入、旧回答提醒和空态 |
| src/features/qa/api.ts、components/QuestionBox.vue | 问答请求、多行输入、快捷键、真实模型信息及配置入口 |
| src/features/qa/model-config.ts、components/ModelSettings.vue | 模型配置契约、密钥沿用边界、读取 / 保存 / 恢复、错误提示与焦点恢复 |
| src/features/qa/components/AnswerView.vue | 回答三态、出处、检索过程、复制与重新提问 |
| src/features/qa/components/AnswerBody.ts、AnswerBody.test.ts | 安全排版与引用按钮；HTML、代码、外部链接边界回归 |
| src/styles/main.css、documents.css、qa.css、drawer.css | 视觉、阅读排版、响应式布局、减少动效偏好 |
| tests/browser-smoke.mjs、browser-model-settings.mjs、browser-live.mjs | 隔离界面及配置回归，真实配置保存 / 问答 / 资料变更验证和清理 |
| package.json、package-lock.json、.gitignore | 锁定依赖、验证命令、本地运行产物忽略规则 |
| Java document/DocumentManagementController.java | 更新、删除、reindex 三个入口及错误契约；详情与 chunks 沿用 main 的 DocumentController |
| Java document/DocumentManagementService.java | scheduleIndexing()、delete() 的清索引及租约编排 |
| Java document/DocumentManagementRepository.java | 原文与切片短事务、软删除 |
| Java document/DocumentController.java、DocumentQueryRepository.java | 标题包含搜索，% 和 _ 作为普通文字 |
| Java document/DocumentIntakeService.java | 复用标题、front matter 和规范化内容哈希 |
| Java document/IndexingTrigger.java、AsyncIndexingTrigger.java、DocumentIndexingService.java | 变更租约交给异步任务并持有到终态 |
| Java rag/AsyncIndexingConfig.java | 关闭执行器时明确拒绝，避免静默丢弃租约 |
| Java runtime/RuntimeController.java、ModelConfigController.java | 公开运行信息、配置读写转发、白名单响应与错误脱敏；沿用 HTTP/1.1，避免 Uvicorn 在 h2c 升级请求中丢失正文 |
| Java document/QuestionController.java | 非敏感的异常类别、根因、耗时、上游状态日志 |
| Java 测试 document/DocumentManagementControllerTest.java、DocumentManagementServiceTest.java、DocumentManagementRepositoryIT.java | 管理契约、失败/并发边界和隔离 MySQL 事务 |
| Java 测试 document/AsyncIndexingTriggerTest.java、DocumentControllerTest.java、DocumentIndexingServiceTest.java、ReadinessRaceReproTest.java、RagQueryClientTest.java、QuestionControllerTest.java | 租约、搜索、兼容性、超时装配和日志脱敏回归 |
| Java 测试 runtime/RuntimeControllerTest.java、ModelConfigControllerTest.java | 公开字段、三个配置方法、错误映射、密钥脱敏、HTTP/1.1 及只读运行状态 |
| rag-service/app/runtime.py、main.py、tests/test_runtime.py | 注册运行 / 配置路由、公开模型信息和配置不可用响应 |
| rag-service/app/model_config.py、llm.py、tests/test_model_config.py、tests/conftest.py | 本机配置读写、统一地址解析、每次问答创建配置快照；真实 SDK 地址、凭据边界、原子写失败和测试隔离 |
| rag-service/tests/test_qa.py | 补充 SDK 异常及异步离线回归；生产异常处理沿用 main |
| .github/workflows/ci.yml、根 .gitignore | Node 24 前端测试 / 构建任务；忽略本机模型配置 |
| 根 README.md、frontend/README.md、本文、参考调研-前端.md、实施计划 | 启动方式、模块边界、配置使用与验证证据 |

## 验证

命令见 [frontend/README.md](../frontend/README.md)。隔离浏览器回归和真实联调分别运行；真实联调用独立临时资料，比较原有文档及切片哈希。模型异常保留真实状态与耗时，失败不当作通过。最新本地报告与截图位于 `frontend/.verification/`。

## 2026-09-14 PR 验证

- 前端 15 项测试与生产构建通过；工作台和模型配置两套隔离浏览器回归通过，覆盖 390 px 布局、上传/问答互斥、密钥处理、保存重试和焦点恢复。
- Java 全量单元 569 项通过；四个指定隔离 MySQL 集成测试类合计 102 项通过，测试数据库已清理；HTTP/1.1 修复后的两个配置 Controller 共 28 项回归通过。
- Python 全量 276 项通过，包含配置保存、恢复、错误脱敏、SDK 地址优先级及真实模型工厂构造；两条依赖弃用提示不影响结果。
- 审查发现的 URL 凭据泄露、SDK 地址不一致、旧刷新覆盖新模型问题，均已通过回归与独立复审。

- 21:56 完成真实浏览器联调：网页保存当前回答模型 → 刷新回读 → 上传并索引 → 问答及引用定位 → 修改正文并重建 → 新内容问答 → 删除 → 再问拒答 → 恢复启动配置。
- 真实回答模型为 agnes-3.0-flash，检索模型为 bge-m3；三次问答均为 HTTP 200，分别耗时约 10.2、4.9、11.1 秒。DeepSeek 路径以离线 SDK 和接口回归验证。
- 临时文档 15 已删除；联调开始时已有的 12 份资料及切片哈希全部保持一致，浏览器无页面错误。模型配置已恢复到原环境来源，临时覆盖文件已移除。

真实联调报告与截图位于已忽略的 `frontend/.verification/`；服务预览为 http://127.0.0.1:5173/ 。GitHub 交付与 CI 状态见子 Issue #33 关联的 PR。
