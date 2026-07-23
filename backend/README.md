# Aegis 后端

FastAPI 后端负责认证、会话/记忆/任务、人工工单、多模态知识入库、Qdrant 检索、提示词组装、流中引用护栏、LLM 降级与 SSE。模型密钥和向量能力只存在服务端。

完整项目推荐在仓库根目录执行一条 `docker compose up --build -d`。它会等待 MySQL、升级 Alembic、初始化并向量化演示资料，再启动前端；无需单独执行本节的开发命令。以下内容用于后端独立开发与排障。

## 本地启动

要求 Python 3.12、可访问的 MySQL 8.4。若只想调试接口，可把 `DATABASE_URL` 改成 SQLite。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DATABASE_URL = "sqlite:///./data/aegis.db"
$env:ENVIRONMENT = "development"
uvicorn app.main:app --reload --port 8000
```

生产必须先执行 `alembic upgrade head` 并设置 `AUTO_CREATE_TABLES=false`。分布式入库设置 `INGESTION_MODE=queue` 与外部 `QDRANT_URL`，然后独立运行 `python -m app.worker`。worker 在处理期间续租；同一会话的生成链路也使用 MySQL turn lease 跨 Pod 串行化，默认 TTL 由 `CHAT_TURN_LEASE_SECONDS=300` 控制。

接口文档位于 `http://localhost:8000/docs`。开发模式默认建立表、创建示例管理员与“小米智能客服演示知识库”的三篇测试文档：

- 账号：`admin@example.com`
- 密码：`Admin123!`

这个默认密码只能用于本地开发；生产模式在使用默认密码时会拒绝启动。

内嵌资料是技术演示材料，不代表小米官方实时承诺。启动器只管理 `is_seed=true` 的系统资料，升级语料时不会删除同一知识库中的用户上传文档；旧向量清理失败时会先把旧文档标记为不可检索，避免新旧政策混答。

## 模型配置

默认 `LLM_PROVIDER=mock`、`EMBEDDING_PROVIDER=local_hash`，无需密钥即可验证完整上传、切片、索引、检索、引用和流式链路。Mock 是抽取式演示器，不应被描述为生产 LLM。

接入 OpenAI：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=384
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-your-embedding-key
```

接入本地 Ollama 的 OpenAI 兼容接口时，把 `LLM_BASE_URL` 与 `EMBEDDING_BASE_URL` 设为 `http://host.docker.internal:11434/v1`，`LLM_API_KEY` 设为任意非空占位值（例如 `ollama-local`），并使用 Ollama 中已经拉取的聊天与 embedding 模型。生产可让两个 URL 指向独立推理池；`EMBEDDING_API_KEY` 留空时才回退使用 `LLM_API_KEY`。不接受 OpenAI `dimensions` 参数的 pooling 模型设置 `EMBEDDING_SEND_DIMENSIONS=false`，`EMBEDDING_DIMENSIONS` 仍需填写其原生输出维数供 Qdrant 校验。切换模型或维数前必须重建 collection 并重新索引。

双套灰度必须同时配置 `LLM_CANARY_BASE_URL/MODEL` 和 `EMBEDDING_CANARY_BASE_URL/MODEL/DIMENSIONS`，并让 `QDRANT_COLLECTION` 与 `QDRANT_CANARY_COLLECTION` 指向两个物理集合。系统在 Stable/Canary 两套索引都写成功后才发布文档；查询始终使用会话绑定的 Embedding/collection，Canary 故障只显式回退 Stable，不混用向量空间。租户、用户、Conversation 名单和 `tenant_id + conversation_id` HMAC 稳定桶由 Experiment Router 处理。

管理员页面可保存 DeepSeek/百炼等 Provider：密钥在后端加密、只回显不可逆指纹，启用前必须联网验证；百炼套件同时验证 Embedding、Rerank 和真实图片理解。生产仍应将加密主密钥托管在 KMS/Vault，而不是与数据库放在一起。

图片/扫描 PDF 理解需设置 `MULTIMODAL_PROVIDER=openai_compatible` 与 `VISION_MODEL`。vLLM 也使用同一 OpenAI-compatible 适配层；上线前必须在目标 GPU 实测模型的多模态限制和上下文长度。

全部环境变量见根目录 `.env.example`，完整运行方式见 `运行指南.md`。

## 测试

```powershell
cd backend
pip install -r requirements-dev.txt
pytest --cov=app --cov-report=term-missing --cov-fail-under=75
ruff check app tests scripts migrations
python -m scripts.evaluate_rag
alembic upgrade head
alembic check
```

测试使用临时文件 SQLite、隔离的临时 Qdrant 目录和离线生成器，不访问外部 LLM，也不读取真实密钥。真实百炼评测需由操作员显式运行，报告保存在 `evaluation/dashscope-*.json`，不得在 CI 打印密钥。

## 生产边界

- Qdrant 本地模式只适合单进程验收。横向扩容必须配置外部 Qdrant；queue 模式在缺少 `QDRANT_URL` 时会拒绝启动。
- 生产使用 Alembic 作为唯一迁移入口；开启 `AUTO_CREATE_TABLES` 会拒绝启动。
- 应用以 MySQL HMAC 账号桶提供跨 Pod 登录防爆破；入口层仍应补充 TLS、IP/设备维度限流、WAF、集中审计、密钥管理与备份。应用控制不能替代基础设施控制。
- 业务数据和上传文件含潜在个人信息，应按企业数据分级策略设置保存期限与删除作业。
