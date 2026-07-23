# Aegis REST / SSE API 文档

## 1. 约定

- Base URL：`/api/v1`
- JSON：UTF-8，时间为 ISO 8601 UTC 时间
- 认证：`Authorization: Bearer <access_token>`
- 错误：`{"detail":"可读错误说明"}`；参数错误的 `detail` 为 FastAPI 校验数组
- 分页：`page` 从 1 开始，`page_size` 最大 100
- 每个响应包含 `X-Request-ID`，排障时应一并提供
- Web 用户端为 `/user`、管理端为 `/admin`；两者使用独立浏览器 Token，但所有权限仍由下列 API 的服务端角色与资源归属校验决定

状态码：`201` 创建成功，`202` 异步受理，`204` 删除成功，`401` 未登录，`403` 无权限，`404` 资源不存在或不属于当前用户，`409` 重复资源/同会话已有回答在生成，`413/415/422` 文件或输入非法，`429` 登录限流或今日额度耗尽，`503` 依赖不可用。

## 2. 接口列表

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/auth/register` | 公开 | 邮箱或手机号注册 |
| POST | `/auth/login` | 公开 | 登录 |
| GET | `/auth/me` | 用户 | 当前用户 |
| POST | `/sessions` | 用户 | 新建独立会话 |
| GET | `/sessions` | 用户 | 自己的历史会话 |
| GET | `/sessions/{id}` | 用户 | 会话详情与完整消息 |
| DELETE | `/sessions/{id}` | 用户 | 删除会话 |
| GET | `/chat/usage` | 用户 | 今日额度 |
| POST | `/chat/stream` | 用户 | RAG 流式问答 |
| PUT | `/messages/{id}/feedback` | 用户 | 点赞/踩与文字反馈 |
| POST | `/knowledge/bases` | 管理员 | 新建知识库 |
| GET | `/knowledge/bases` | 管理员 | 知识库列表 |
| POST | `/knowledge/documents` | 管理员 | 上传并异步索引文档 |
| GET | `/knowledge/documents` | 管理员 | 文档与处理状态 |
| POST | `/knowledge/documents/{id}/retry` | 管理员 | 重试失败文档入库 |
| DELETE | `/knowledge/documents/{id}` | 管理员 | 删除文档与向量 |
| GET/PUT | `/profile` | 用户 | 画像、同意状态、事实与服务摘要 |
| POST | `/profile/facts` | 用户 | 新增有来源的显式画像事实 |
| DELETE | `/profile/facts/{id}` | 用户 | 软撤销画像事实 |
| DELETE | `/profile/memory` | 用户 | 撤回并清除长期会话摘要 |
| POST/GET | `/sessions/{id}/tasks` | 用户 | 创建/读取会话任务记忆 |
| PATCH | `/sessions/{id}/tasks/{task_id}` | 用户 | 乐观锁更新任务 |
| POST | `/handoffs` | 用户 | 创建人工接管工单 |
| GET | `/handoffs/mine` | 用户 | 查看自己的接管工单 |
| GET | `/admin/overview` | 管理员 | 用户、会话、反馈与 7 日问答量 |
| GET | `/admin/sessions[/{id}]` | 管理员 | 审计式读取会话列表/详情 |
| GET/PATCH | `/admin/handoffs[/{id}]` | 管理员 | 排队与乐观锁处置工单 |
| GET/PATCH | `/admin/experiments/control` | 管理员 | 读取/原子更新五轴灰度与 Shadow |
| GET | `/admin/experiments/audits` | 管理员 | 灰度变更与自动回滚审计 |
| GET/PATCH | `/admin/providers[/{id}]` | 管理员 | 加密 Provider 配置与在线能力验证 |
| GET | `/admin/providers/audits` | 管理员 | Provider 变更审计 |
| GET | `/admin/data-access-audits` | 管理员 | 特权会话读取审计 |
| GET | `/health/live` | 公开 | 进程存活 |
| GET | `/health/ready` | 公开 | 数据库与向量库就绪 |
| GET | `/health/capacity` | 公开 | 请求、模型与入库队列容量快照 |

## 3. 认证

### 注册 `POST /auth/register`

邮箱和手机号必须且只能填写一个，密码 8—128 位且同时含字母和数字。

当前题目范围把邮箱或手机号作为登录标识，注册成功后直接签发访问令牌，**不会发送或校验邮件/短信验证码**。这不代表联系方式已经核验。生产接入时应增加独立 OTP challenge：验证码只保存带服务端密钥的摘要，设置用途、短有效期、一次性消费、发送/校验频控和错误次数上限；供应商超时不能创建未验证账号，日志不得记录验证码或完整手机号。该扩展不改变现有密码哈希与登录接口，但应给账号增加 `contact_verified_at`，并由通知服务负责邮件/短信供应商降级和送达审计。

```json
{
  "email": "alice@example.com",
  "password": "Secure123",
  "display_name": "Alice"
}
```

手机号方式：

```json
{
  "phone": "+85255556666",
  "password": "Secure123",
  "display_name": "Alice"
}
```

成功 `201`：

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 7200,
  "user": {
    "id": "3d8b5d75-...",
    "email": "alice@example.com",
    "phone": null,
    "display_name": "Alice",
    "role": "user",
    "tenant_id": "default",
    "created_at": "2026-07-20T08:00:00Z"
  }
}
```

### 登录 `POST /auth/login`

```json
{"account":"alice@example.com","password":"Secure123"}
```

响应结构与注册相同。账号不存在、停用或密码错误统一返回 `401 账号或密码错误`，并执行等价的密码哈希校验，避免暴露账号枚举信号。默认 15 分钟窗口内同一账号失败 5 次后返回：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 900
Content-Type: application/json

{"detail":"登录尝试过于频繁，请稍后再试"}
```

计数键是服务端密钥 HMAC，不保存原始账号，且由 MySQL 在多个 API Pod 间共享。生产网关还需叠加 IP/设备维度限流。

## 4. 会话与历史

### 新建 `POST /sessions`

```json
{"title":"新对话"}
```

```json
{
  "id":"9ace...",
  "title":"新对话",
  "status":"active",
  "created_at":"2026-07-20T08:00:00Z",
  "updated_at":"2026-07-20T08:00:00Z"
}
```

首个问题会把默认标题自动更新成问题前 40 字。

同一会话只允许一个回答生成链路。并发提问返回 `409` 和 `Retry-After: 2`，且不消耗每日额度。回答生成期间删除该会话同样返回 409；租约会随 SSE 事件续期，服务异常退出后最多等待 `CHAT_TURN_LEASE_SECONDS` 即可恢复。

### 列表 `GET /sessions?page=1&page_size=20`

```json
{"items":[{"id":"9ace...","title":"退款条件","status":"active","created_at":"...","updated_at":"..."}],"total":1,"page":1,"page_size":20}
```

### 详情 `GET /sessions/{session_id}`

```json
{
  "id":"9ace...",
  "title":"退款条件",
  "status":"active",
  "created_at":"...",
  "updated_at":"...",
  "messages":[
    {"id":"m1","role":"user","content":"手机充电无反应怎么办？","intent":"售后问题","retrieval_status":null,"model_name":null,"created_at":"...","citations":[],"feedback":null},
    {"id":"m2","role":"assistant","content":"…… [S1]","intent":"售后问题","retrieval_status":"matched","model_name":"gpt-4o-mini","created_at":"...","citations":[{"id":"c1","document_id":"d1","chunk_id":"d1:0","source_name":"小米产品使用与故障排查.md","excerpt":"至少充电 30 分钟……","score":8240,"display_order":1}],"feedback":null}
  ]
}
```

删除文档后历史引用中的 `document_id` 可能为 `null`，但 `source_name` 和 `excerpt` 快照仍保留。

## 5. 流式问答

### 请求 `POST /chat/stream`

`Content-Type: application/json`，问题不能为空且最多 500 个 Unicode 字符。`knowledge_base_id` 可选；不传时在所有活跃知识库中检索，等价于自动路由到得分最高的知识片段。

```json
{
  "session_id": "9ace...",
  "question": "小米手机完全无法开机，充电也没有反应，应该先做什么？",
  "knowledge_base_id": "kb-id-optional"
}
```

响应 `Content-Type: text/event-stream`。这是 **POST SSE**，浏览器应使用 `fetch + ReadableStream`，不能直接使用只支持 GET 的 `EventSource`。

### SSE 帧格式

每帧以空行结束，`data` 始终是单行 JSON。网络分片不保证和事件边界一致，客户端必须缓存到 `\n\n` 再解析。

```text
event: ack
data: {"user_message_id":"m1","session_id":"s1","usage":{"used":3,"limit":100}}

event: meta
data: {"intent":"售后问题","model":"gpt-4o-mini"}

event: sources
data: {"items":[{"source_id":"S1","document_id":"d1","document_name":"退款政策.txt","excerpt":"客户在首次购买……","score":0.824,"is_critical_rule":true,"section_path":"退款规则","parent_expanded":true}]}

event: token
data: {"delta":"首次购买"}

event: token
data: {"delta":"至少充电 30 分钟后…… [S1]"}

event: done
data: {"message_id":"m2","grounded":true,"evidence_available":true,"handoff_recommended":false,"guardrail_filtered_segments":0,"answerability":"supported","input_guardrail":null,"claims":[{"text":"至少充电 30 分钟后长按电源键 10 秒以上。","evidence_ids":["S1"],"supporting_spans":[{"source_id":"S1","quote":"至少充电 30 分钟，再长按电源键 10 秒以上。"}]}],"reranking":{"attempted":false,"applied":false,"reason":"confident_deterministic_rank"},"knowledge_route":{"mode":"global","confidence":0.0,"knowledge_base_id":null},"suggestions":["仍然无法开机怎么办？"],"timings":{"retrieval_ms":82,"generation_ms":410,"total_ms":506}}

```

事件语义：

| event | 语义 | 客户端动作 |
|---|---|---|
| `ack` | 用户消息已落库、额度已扣 | 用真实消息 ID 替换临时 ID |
| `meta` | 意图与模型 | 展示意图标签 |
| `sources` | 本轮证据，可能为空 | 立即展示引用卡片 |
| `token` | 一个增量字符串，不保证是完整字或词 | 原样追加，不能覆盖 |
| `ping` | 12 秒无业务事件时的心跳 | 忽略或刷新连接存活时间 |
| `done` | 回答已持久化 | 结束 loading、展示追问；`answerability` 区分完整支持、部分支持和无支持，`input_guardrail` 标记显式提示词注入（正常请求为 `null`），`claims` 只列带有效证据 ID 的结论，`reranking` 与 `knowledge_route` 提供可观测决策；`timings` 给出服务端 retrieval/generation/total 毫秒数；`evidence_available` 区分有证据回答与安全兜底，`handoff_recommended` 提示转人工，`guardrail_filtered_segments` 给出被过滤的无引用片段数。发布的最终回答已通过引用护栏或抽取式降级，因此当前协议中 `grounded` 为 `true` |
| `error` | 检索或生成阶段失败 | 展示 `message`，按 `code` 决定是否重试；`SESSION_TURN_LOST` 表示当前 Pod 已失去会话租约，必须停止追加并刷新 |

连接建立前的校验错误仍使用普通 HTTP 4xx JSON。连接建立后的错误只能用 `event: error` 表达，HTTP 状态已经是 200。`done` 与 `error` 都是终止事件；客户端若遇到 EOF 却从未收到二者之一，必须把本轮标记为异常中断，保留部分文本但提示其可能不完整，不能让界面永久停在生成中或把半截回答当作成功。

### JavaScript 调用示例

```ts
const response = await fetch('/api/v1/chat/stream', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token}`},
  body: JSON.stringify({session_id: sessionId, question}),
})

const reader = response.body!.getReader()
const decoder = new TextDecoder()
let buffer = ''
while (true) {
  const {value, done} = await reader.read()
  buffer += decoder.decode(value, {stream: !done}).replace(/\r\n/g, '\n')
  // 循环取出 buffer 中全部以 \n\n 结束的帧，剩余半帧留给下次。
  if (done) break
}
```

完整且经过构建检查的解析器在 `frontend/src/api.ts`。

### 今日额度 `GET /chat/usage`

```json
{"used":3,"limit":100,"remaining":97}
```

额度按 `BUSINESS_TIMEZONE` 自然日统计。

## 6. 知识库

### 新建知识库 `POST /knowledge/bases`

```json
{"name":"售后政策","description":"退款、退订与服务补偿规则"}
```

### 上传 `POST /knowledge/documents`

使用 `multipart/form-data`：

```bash
curl -X POST http://localhost:8000/api/v1/knowledge/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "knowledge_base_id=$KB_ID" \
  -F "file=@退款政策.txt"
```

成功返回 `202`，初始 `status=processing`。后台成功后变为 `ready` 并填写 `chunk_count`，失败变为 `failed` 和安全的 `error_message`。同一知识库按 SHA-256 由数据库唯一约束去重，包括并发上传；已有失败记录应调用 retry，不重复上传。

### 文档列表 `GET /knowledge/documents?knowledge_base_id={id}&page=1&page_size=20`

```json
{
  "items":[{
    "id":"d1","knowledge_base_id":"kb1","original_name":"退款政策.txt","content_type":"text/plain",
    "size_bytes":2048,"status":"ready","chunk_count":5,"error_message":null,"created_at":"...","updated_at":"..."
  }],
  "total":1,"page":1,"page_size":20
}
```

### 删除 `DELETE /knowledge/documents/{id}`

成功 `204`。系统先将状态改为 `deleting`，删除该文档所有 Qdrant points，再删除 MySQL 元数据，最后清理上传文件。向量删除失败时返回 `503` 且保留失败状态，避免出现“界面已删除但向量仍能召回”的静默不一致。

## 7. 反馈

`PUT /messages/{assistant_message_id}/feedback`

```json
{"rating":-1,"comment":"引用正确，但没有说明到账时间"}
```

`rating` 只能是 `1` 或 `-1`；同一回答再次提交是更新，不会产生重复统计。只能评价自己会话中的 assistant 消息。

## 8. 管理统计

`GET /admin/overview`

```json
{
  "users":20,"sessions":84,"questions_today":36,
  "positive_feedback":31,"negative_feedback":4,
  "daily_questions":[{"date":"2026-07-14","count":18},{"date":"2026-07-15","count":22}]
}
```

`daily_questions` 始终包含最近 7 个自然日，没有数据的日期补 0。

## 9. 用户画像与记忆

- `GET /profile`：读取显式偏好、两类同意状态、未撤销事实与服务交互摘要。
- `PUT /profile`：更新 `preferred_name/locale/timezone/response_style/memory_consent/consent_version/profile_learning_consent/profile_consent_version`。启用任一学习能力都必须带对应 consent version。
- `POST /profile/facts`：显式保存 `device_model/purchase_channel/preferred_contact_channel/service_region` 之一；事实保留来源、置信度、确认时间与可选有效期。
- `DELETE /profile/facts/{fact_id}`：软撤销本人的事实；历史来源仍可审计，但不再进入画像或检索提示。
- `DELETE /profile/memory`：撤回同意并删除当前用户的所有 session summary，返回 204。

画像和长期摘要只调整表达与连续性；画像检索提示仅在问题未明确型号/渠道时补充限定，不能当作知识库事实、授权或跨用户共享依据。

## 10. 任务记忆

- `POST /sessions/{session_id}/tasks`：创建 `{ "task_type":"refund_request", "slots":{"order_id":"A-1"} }`。
- `GET /sessions/{session_id}/tasks`：列出本用户的任务。
- `PATCH /sessions/{session_id}/tasks/{task_id}`：传 `expected_version`与新 status/slots。版本落后返回 409。

slots 仅允许有界的扁平 JSON 标量，不接受任意嵌套对象。

## 11. 人工接管

`POST /handoffs`（用户）：

```json
{"session_id":"s1","trigger":"manual","reason":"需要人工确认退款","priority":4}
```

同会话已有 open/in_progress 工单时幂等返回原工单。用户不能伪造系统 trigger。`GET /handoffs/mine` 查看自己的请求。

管理员：

- `GET /admin/handoffs?status=open`：按优先级和时间排队；
- `PATCH /admin/handoffs/{id}`：更新 `assigned_to/status/resolution`，必须携带当前 `expected_version`。

```json
{"expected_version":1,"status":"in_progress","assigned_to":"admin-user-id"}
```

合法状态流为 `open → in_progress → resolved`，任一非关闭状态可转 `closed`，`resolved` 可重新进入 `in_progress`；接管必须指定有效管理员，解决必须填写结论。每次成功更新 `version + 1`，并发写入旧版本返回 `409`，后台会提示并自动刷新，避免两个坐席互相覆盖处置结果。

## 12. 管理员会话审计

- `GET /admin/sessions?page=1&page_size=20`：全量会话分页，含 owner 和消息数。
- `GET /admin/sessions/{session_id}`：完整消息、引用和反馈。

两个接口均是特权读取，会在 `admin_data_access_audits` 追加管理员、目标、Request ID、用途和时间，服务日志仅写 ID。普通用户返回 403。`GET /admin/data-access-audits?limit=100` 可查看最近审计；应用没有修改或删除该表记录的接口。

## 13. Provider 与灰度控制

- `GET /admin/providers`：返回 `llm_stable` 与 `dashscope_ai` 的非秘密配置、验证状态、密钥是否存在和不可逆指纹；永不回显 Key 前后缀。
- `PATCH /admin/providers/{credential_id}`：必须携带 `expected_version`、协议、受白名单约束的 HTTPS/内网 URL、模型、启停、是否验证和变更原因；Key 留空表示保留。启用前必须在线验证，失败返回 422 且原配置不变。百炼套件会同时探测 Embedding 输出维数、原生 Rerank 完整性和带真实图片的视觉响应。
- `GET /admin/providers/audits?limit=50`：只返回字段集合、操作者、版本、Request ID 和结果，不返回配置或密钥明文。

灰度控制读取示例：

```json
{
  "id":"global","canary_enabled":true,"stage":1,
  "model_canary_percent":0,"embedding_canary_percent":0,
  "prompt_canary_percent":0,"rag_canary_percent":0,
  "service_canary_percent":0,"shadow_canary_percent":100,
  "version":4
}
```

更新使用 `PATCH /admin/experiments/control`：

```json
{
  "expected_version":4,"canary_enabled":true,"stage":3,
  "model_canary_percent":1,"embedding_canary_percent":1,
  "prompt_canary_percent":1,"rag_canary_percent":1,
  "service_canary_percent":1,"shadow_canary_percent":0,
  "reason":"离线与 Shadow 质量门通过，进入 1%"
}
```

旧 `expected_version` 返回 409。缺少 Canary 模型、服务地址或独立 Embedding collection 时返回 422。一键回滚将 `canary_enabled=false`；自动门也会执行相同 Kill Switch 并写入 `/admin/experiments/audits`。

## 14. 健康与容量

- `GET /health/live`：进程存活，不检查依赖。
- `GET /health/ready`：MySQL + Qdrant collection 可用才返回 200。
- `GET /health/capacity`：在途请求、拒绝计数、LLM/embedding/reranker 各自的 bulkhead/circuit、query embedding 缓存 hit/miss/coalesced，以及入库 queued/running/retry/dead、过期租约和最老待处理年龄。

SSE `done` 包含 `evidence_available`、`handoff_recommended`、`input_guardrail`、`guardrail_filtered_segments` 与分阶段 `timings`；客户端应依这些状态显示人工入口和耗时，不应自行猜测模型是否可信。
