# 子 Issue F：回答模型配置与会话模块

Issue：[#35](https://github.com/vansye/EasyRAG/issues/35)，父 Issue：#1。当前设计：[FastAPI 模块迁移](架构设计.md)。位置：`app/modules/answer_models`。

## 功能与用户故事

用户在现有网页保存 OpenAI 兼容或 DeepSeek 模型设置，下一次提问使用新设置，重启后保留；可恢复启动配置。同一问题的判定、生成使用同一份配置。

本模块独占本机配置文件、地址解析、密钥沿用规则和厂商 SDK。嵌入模型属于 B，不随回答模型切换。

## 数据与接口原型

```python
ConfigUpdate = {provider, model, base_url, api_key}  # api_key 只写
PublicConfig = {configured, provider, model, base_url, api_key_configured, source}
Models.get() -> PublicConfig
Models.save(update: ConfigUpdate) -> PublicConfig
Models.reset() -> PublicConfig
Models.open_session() -> ChatSession
ChatSession.complete(prompt: str) -> str
```

PublicConfig 不含密钥，ChatSession 封装 SDK、不暴露 SDK 对象或密钥。空密钥只有 provider 与实际 endpoint 不变时可沿用。公开地址、沿用判断、SDK 地址采用同一解析规则。

## 边界与失败

- 不 import A/B/C/G，不包含 FastAPI 路由，不操作 MySQL/Chroma。
- 配置原子保存，失败保留上次文件；reset 只移除覆盖，不改 .env。
- 模型缺配置、上游超时和返回非文本为可识别的模块错误，不包含密钥、请求正文或上游正文。
- HTTP 校验及状态码由 G 映射；C 通过 G 注入的 ChatPort 调用会话。

## PR 与验收

- [x] 配置存取和 HTTP 解耦：保存、重读、恢复、非法地址、空密钥沿用、写失败原值保留（PR #44）。
- [x] 会话封装：每次 open_session 固定配置，同一会话调用期间修改配置不影响它；后续会话读取新值（PR #44、#52、#55）。
- [x] 使用隔离配置文件和假客户端运行全部测试，测试不得读写用户配置。
- [x] 模块 import 约束通过；现有网页配置契约不变（PR #53、#55）。

模块实现见 [PR #44](https://github.com/vansye/EasyRAG/pull/44)，真实 HTTP 模型服务与配置切换验收见 [PR #55](https://github.com/vansye/EasyRAG/pull/55)。网页配置保持六个公开字段，密钥不回传；响应含禁止缓存头。
