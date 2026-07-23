# Aegis 前端

React 19 + TypeScript 7 + Vite 8 单页应用。聊天接口使用 `fetch` 读取 POST SSE，因此支持携带 JSON 请求体和 Bearer Token；解析器会处理网络分片、一个事件跨多个 TCP chunk，以及一个 chunk 内包含多个事件的情况。`done/error` 是协议终止帧，无终止帧 EOF 会被标成异常断流，并保留已接收文本和“不完整”提示。

完整项目无需单独启动前端：在仓库根目录执行 `docker compose up --build -d` 即可同时启动 MySQL、后端和本前端。以下命令只用于前端独立开发。

## 启动

```powershell
cd frontend
npm ci
npm run dev
```

开发服务器监听 `http://localhost:5173`，用户端为 `/user`，管理端为 `/admin`；默认把 `/api` 代理到 `http://localhost:8000`。两个入口分别使用独立 Token key，便于同一浏览器同时验收双角色。如前后端域名不同，可设置：

```dotenv
VITE_API_BASE=https://api.example.com/api/v1
```

## 质量检查

```powershell
npm run typecheck
npm run build
```

页面不使用 `dangerouslySetInnerHTML`，模型输出按纯文本展示，避免把知识库或模型生成内容直接作为 HTML 执行。生产 Nginx 同时配置 CSP、禁止 iframe、安全响应头和 SSE 反向代理禁用缓冲。

管理员界面包含中文“模型与密钥”和“灰度发布”：前者只显示密钥存在性/不可逆指纹并执行后端能力探针；后者独立控制 LLM、Embedding、Prompt、RAG、vLLM service 与 Shadow，使用版本锁防止多管理员覆盖，并提供一键回滚 Stable。浏览器从不直连模型 API。

入口本身不代替授权：`/admin` 只接受管理员角色，`/user` 只接受普通用户角色；后端每个管理接口还会重新查询数据库角色，不信任路径、菜单隐藏或 JWT 内的角色声明作为最终权限依据。

访问令牌当前存于 `localStorage`，适合前后端分离的演示系统。更高安全等级部署建议改为同站 `HttpOnly + Secure + SameSite` Cookie，并配套 CSRF Token、短时访问令牌与可撤销刷新令牌。
