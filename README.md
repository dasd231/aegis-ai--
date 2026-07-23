# Aegis AI 智能客服系统

> 项目仓库：[@dasd231/aegis-ai--](https://github.com/dasd231/aegis-ai--)

Aegis 是一套可本地复现、也有分布式参考部署的企业级 AI 智能客服。它覆盖认证、隔离会话、多模态知识入库、RAG 检索与流式引用、反馈、可撤回记忆、任务状态、人工接管与管理审计。默认离线演示不需要 API Key；生产模型密钥只能位于后端或密钥系统。

## 一键运行与快速验收

仓库不会提交真实密码或 API Key，因此首次克隆需要一次性生成本地 `.env`；完成这一步后，整个系统只用一条 Compose 命令启动，不需要分别安装 Python、Node、MySQL 或 Qdrant：

```powershell
Copy-Item .env.example .env
# 仅首次需要：编辑 .env，替换密码和 SECRET_KEY 占位值
docker compose up --build -d
docker compose ps
```

`docker compose up --build -d` 会统一构建并启动 MySQL、FastAPI 与 Nginx/React。后端容器先执行 `alembic upgrade head`，再创建管理员、演示知识库，并把三篇小米客服资料切片和向量化。默认 `mock + local_hash` 不需要模型 API Key，三个服务健康后可以直接测试问答；后续启动仍只需这一条命令。

默认提供两个独立入口：用户端 `http://localhost:8080/user`，管理端 `http://localhost:8080/admin`。两个入口使用不同的浏览器 Token key，可在同一浏览器的两个标签页同时保持普通用户和管理员登录态。若端口被占用或系统保留，请在 `.env` 中同时调整 `WEB_PORT` 与 `ALLOWED_ORIGINS`。Docker Hub 不可达时，可将 `BASE_IMAGE_REGISTRY` 切换为 `public.ecr.aws/docker/library`。

默认管理员账号来自 `.env` 的 `INITIAL_ADMIN_EMAIL` 与 `INITIAL_ADMIN_PASSWORD`。首次启动会创建“小米智能客服演示知识库”，索引设备排障、商城订单和手机售后三篇测试资料。它不是小米官方生产知识库，实际权益以官网实时规则为准。完整配置、真实 LLM 接入和故障处理见[运行指南](运行指南.md)。

## 核心能力

- 邮箱或手机号注册/登录，Argon2 密码哈希、HMAC 账号桶跨 Pod 防爆破，带 issuer/audience/过期校验的 JWT；
- 严格按用户隔离的多会话、跨 Pod turn lease、历史记录、安全删除与含文字说明的点赞/点踩反馈；
- `.txt/.md/.pdf/.png/.jpg/.webp` 入库，含扫描 PDF 视觉转写、数据库内容去重、可恢复队列、续租围栏和失败补偿；
- Qdrant 本地持久化、子块召回/父页章节扩展、向量与词法混合排序、条件式 LLM 重排、规则优先、置信门和上下文预算；
- POST-SSE 流式回答、中文阶段提示、分阶段耗时、心跳、断连处理、输出前逐段引用护栏与无证据拒答；
- 显式画像、同意后长期摘要、带乐观锁的任务记忆、幂等且有状态机/版本锁的人工工单与审计工作台；
- 请求与三类推理依赖的独立 bulkhead、单探针半开熔断与安全摘录降级、容量端点及 SSE 负载脚本；
- 每人每日额度的并发安全计数、意图分类、追问建议、多知识库路由与管理统计；
- MySQL 8.4 + Alembic、外部 Qdrant、vLLM 兼容层，以及 Kubernetes HPA/PDB/灰度参考清单。

## 文档导航

- [项目说明与技术选型](项目说明.md)
- [运行指南](运行指南.md)
- [API 文档](docs/API文档.md)
- [数据库设计](docs/数据库设计.md)
- [AI / RAG 架构与 Prompt](docs/AI架构设计.md)
- [业务流程说明](docs/业务流程说明.md)
- [AI Agent 任务拆解设计](docs/AI%20Agent任务拆解设计.md)
- [记忆、画像与人工接管](docs/记忆画像与人工接管.md)
- [多模态与工具沙盒](docs/多模态与工具沙盒.md)
- [生产架构、容量与灰度](docs/生产架构与灰度发布.md)
- [Question.md 需求追踪矩阵](docs/需求追踪矩阵.md)
- [Question.md 评审导读与设计闭环](docs/Question评审导读.md)
- [安全策略与漏洞报告](SECURITY.md)

## 本次交付验证

- 后端：99 项测试通过，整体语句覆盖率 82.23%，Ruff 检查通过；
- 前端：TypeScript 与 Vite 生产构建通过；
- 依赖：`pip check` 通过，Python 与 npm 已知漏洞审计均为 0；
- 容器：本轮最新镜像已重建，MySQL、FastAPI、Nginx 均为 healthy，运行中数据库为 Alembic `20260723_0012 (head)`；外部 Qdrant 1.18.3 + 2 个 worker 分布式 overlay 也完成全新卷验证；
- AI 质量：30 条困难黄金用例已用线上同构的 Query Planner、Qdrant dense+IDF sparse、weighted RRF、实体门、Rerank 与上下文打包链路复跑；Stable、Canary 及 Canary+Rerank 均通过质量门。该离线小样本是 CI 回归门，不冒充生产模型答案级评测。
- 公开链路：`/user`、`/admin` 双入口均返回 200，普通用户和管理员角色在线验证通过；部署后脚本通过 Nginx 实际执行中文 UTF-8 登录、建会话、RAG、POST-SSE 和清理。最新小米客服矩阵 10/10 通过，覆盖口语排障/物流、电池安全、第三方渠道、维修报价、未知费用拒答、提示词注入和多轮渠道追问；管理员冒烟同时验证人工工单幂等与 version 递增；
- 业务：真实容器链路完成 3 篇小米场景资料入库、带非空来源的 grounded SSE、画像 consent、任务记忆、人工工单和管理审计；最新 10 并发基线完成 131 次 SSE，零错误/零拒绝，来源就绪 p95 717.53ms、首字 p95 845.40ms、完整响应 p95 1570.25ms，测试会话与合成账号清理为 0 残留；Alembic 当前代码 head 为 `20260723_0012`，画像事实具备来源、置信度、有效期和软撤销语义。

验证命令与测试范围详见[运行指南](运行指南.md)。离线 `local_hash + mock` 仅用于零密钥复现和 CI，不代表真实 LLM/embedding 的生产语义质量。自动验收使用的临时栈会在测试后清理；需要人工查看效果时，可用被忽略的本地 `.env` 保留演示栈运行。

## 提交前检查

1. 确认 `.env`、API Key、数据库数据和上传文件未纳入版本控制；
2. 使用目标模型配置重新运行测试和容器验收；
3. 推送后从干净目录重新克隆并执行一次一键启动；
4. 按题目指定渠道发送仓库链接，并在邮件中填写姓名与院校。

一键启动的首次配置、自动初始化、健康检查、双入口和真实模型切换细节见[运行指南第 1 节](运行指南.md#1-一次配置后续一键启动docker-compose)。

本项目使用 [MIT License](LICENSE)。
