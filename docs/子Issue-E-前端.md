# 子 Issue E：前端知识工作台

对应 [#33](https://github.com/vansye/EasyRAG/issues/33)，父 Issue [#1](https://github.com/vansye/EasyRAG/issues/1)。前端使用 Vue 3、TypeScript、Pinia 与 Vite，通过 G 的 FastAPI 接口工作。启动和测试命令见 [前端 README](../frontend/README.md)。

## 一、职责与页面

资料库 `/` 负责上传、标题搜索、状态筛选与分页。预览、原文、切片和正文编辑共用资料抽屉，重新处理与删除保留在资料上下文中。

知识问答 `/ask` 按问题、答案、出处和可展开的检索过程组织阅读，并提供历史记录与模型配置入口。Pinia 保留切页前的当前问答；刷新后的临时状态清空，已保存的回答可以从服务端历史打开。

前端负责交互、排版、焦点与临时状态，不直连 MySQL、索引或模型服务，不代替后端判断证据、数据一致性或是否提交历史。模块关系见 [架构设计](架构设计.md)。

## 二、数据与接口原型

以下为展示层原型，完整字段与错误状态见 [API 文档](api.md)。

```ts
type AnswerStatus = 'ANSWERED' | 'PARTIAL' | 'REFUSED'
type Trace = {
  round_index: number
  query: string
  decision: 'SUFFICIENT' | 'PARTIAL' | 'NONE'
  retrieved: { rank: number; chunk_id: number; document_id: number; score: number }[]
  relevant?: number[]
}
type SavedAnswer = {
  history_id: number
  answer: string
  status: AnswerStatus
  sources: Source[]
  trace: Trace[]
  created_at: string
  model: { provider: string; model: string }
  elapsed_ms: number
}
type Source = {
  chunk_id: number; document_id: number; title: string; text: string
  byte_start: number; byte_end: number; heading_path: string
}
type PublicModelConfig = {
  configured: boolean; provider: 'openai' | 'deepseek'; model: string
  base_url: string; api_key_configured: boolean; source: 'environment' | 'local'
}
```

| 接口组 | 页面用途 |
|---|---|
| `/health`、`/api/runtime` | 依赖状态、运行许可与模型信息 |
| `POST /api/admin/ready` | 用户明确触发的就绪检查 |
| `/api/documents` 及详情、chunks、reindex | 上传、查询、预览、编辑、删除与重处理 |
| `POST /api/questions/stream` | 页面流式提问；同步 `/api/questions` 仍可用 |
| `/api/question-history` 及详情和删除 | 历史分页、快照查看与删除 |
| `GET/PUT/DELETE /api/model-config` | 读取、保存和恢复回答模型配置 |

## 三、关键交互与取舍

| 行为 | 当前规则与理由 |
|---|---|
| 两页工作台 | 资料表格利于扫描，问答页控制阅读宽度；共享抽屉让核验原文时不丢失问题上下文 |
| 上传 | 单份 md/txt、UTF-8、1 MiB；显示真实索引状态，在需要时轮询处理终态 |
| 资料预览 | 默认呈现 Markdown，隐藏开头 frontmatter；HTTP(S) 链接新标签页打开，不抓网页 |
| 编辑与删除 | 失败保留编辑草稿；关闭有未保存内容时确认，删除单独确认；原文、切片与预览分开 |
| 流式显示 | 文本增量仅作临时纯文本；收到已保存的 `done` 后再渲染正式答案、出处和历史 |
| 未完成回答 | 失败保留问题；HTTP/SSE 错误展示服务端信息，网络、断流或格式异常提示先检查历史；均不自动重试，提交阶段断连可能已经保存 |
| 引用 | 使用生成那轮 trace 的 rank → chunk_id → source 关联，不把 sources 数组下标当引用编号 |
| 历史 | 来源展示回答时的快照；原文更新或删除不改变快照，重新提问才使用当前资料 |
| 并发结果 | 历史选择与请求使用修订号/请求序号隔离迟到结果，避免旧请求覆盖新选择 |
| 运行门禁 | 忙碌时暂停新的资料变更；只读探测与手动确认就绪分开，页面不自动恢复 |
| 模型配置 | 原生 dialog；只在打开及提交时读写配置，嵌入模型只读，回答模型更换从下一问生效 |

页面主线已有检索轮次、query、判定、候选及正文引用展示；后端 `relevant` 表示送入生成的片段编号。生成采用标签的 U7 前端增强尚未发布，不能用“正文未引用”推断“未提供给模型”。当前后端只有一轮检索，界面不虚构重查过程。

## 四、排版、可访问性与配置边界

Markdown 由 marked 解析，再由 Vue 创建受控节点。HTML 作为文本，外部图片不自动加载，代码中的编号不识别为引用。资料预览和回答复用相同的安全排版规则，保留标题、列表、表格和代码等阅读结构。

视觉采用暖灰、纸白和深绿，系统字体避免远程资源依赖。窄屏隐藏次要表格列并将出处改为单列；抽屉关闭恢复焦点，方向键及 Home/End 切换页签，多行问答支持 Ctrl/Command + Enter。支持减少动效偏好。

API Key 仅写入请求，不回填、不进入浏览器持久存储。空密钥只有 provider 与实际接口地址不变时可沿用，改变地址必须重填；配置响应禁止缓存。地址解析、原子保存和单问题会话由 F 负责，前端不重新实现另一套地址规则。保存成功表示配置已持久化，实际可用性通过提问验证。

## 五、能力与验收边界

界面支持资料管理、带引用问答、部分回答/拒答、历史和模型配置。没有网页评估看板、独立链接收藏库、网页抓取、富文本编辑或自动多轮会话记忆。问答尚未接入全库统计与完整目录，界面展示引用不构成全库覆盖保证。

单元测试检查 Markdown、引用编号、资料预览、门禁及模型配置；隔离浏览器测试覆盖上传搜索、编辑草稿、引用定位、历史选择/删除、流式失败、密钥边界和窄屏布局。真实联调与隔离测试区分运行：前者会调用实际模型，不能用模拟接口结果冒充真实问答质量。测试入口与本地产物说明见前端 README。
