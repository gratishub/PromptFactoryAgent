# Prompt Miner — 提示词自动化挖掘与 Web 管理仪表盘

一个结合 Jina AI 全网搜索、大模型自动结构化提取、SQLite 高性能交互与 Markdown 双向同步的个人提示词管理中枢。

## 核心特性

- **多知识库管理** — 支持创建、切换、删除多个独立知识库，每个知识库拥有独立的文件子目录与隔离的提示词集合
- **自动挖掘** — Jina AI 驱动全网提示词抓取，LLM 自动提取结构化信息
- **SaaS 级看板** — Vue3 + Tailwind，极简交互体验
- **双存储机制** — SQLite 高性能查询 + Markdown 双向同步（每个知识库独立子目录）
- **JWT 安全认证** — 高强度加密，API 访问无忧
- **Agent 工具链** — 无缝接入 Dify 等 Agent 平台

## 快速开始

```bash
# 克隆项目
git clone <your-repo-url>
cd prompt-miner

# 安装依赖
pip install -r requirements.txt

# 配置环境
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入基础配置
# 生产环境请优先使用环境变量注入敏感信息

export PROMPT_MINER_USERNAME="admin"
export PROMPT_MINER_PASSWORD="replace-with-strong-password"
export PROMPT_MINER_JWT_SECRET_KEY="replace-with-a-long-random-secret"
export PROMPT_MINER_LLM_API_KEY="sk-..."
export PROMPT_MINER_COOKIE_SECURE="true"

# 启动服务
python3 miner.py
```

访问 `http://localhost:8110` 即可使用。

## API 接口

### 知识库管理

```
GET /api/vaults
```
返回所有知识库列表及各自的提示词数量。

```
POST /api/vaults
Authorization: Bearer <jwt_token>
Content-Type: application/json

{ "name": "我的知识库", "slug": "my-vault" }
```
创建新知识库。知识库总数受 `storage.max_vaults` 限制（默认 5），超限返回 400。

```
DELETE /api/vaults/{vault_id}
```
删除指定知识库，同步清除数据库记录和 `./vault/{slug}/` 子目录。默认知识库 (id=1) 不可删除。

### 提示词管理

```
GET /api/prompts?vault_id=1&tag=#编程&q=搜索词
Authorization: Bearer <jwt_token>
```
列表查询。`vault_id` 为可选项，未传时默认指向默认知识库。支持 FTS5 全文搜索和标签筛选。

```
POST /api/prompts
PUT  /api/prompts/{id}
DELETE /api/prompts/{id}
```
CRUD 操作。创建和更新时可通过 `vault_id` 字段指定归属知识库，Markdown 文件自动同步到对应 `./vault/{slug}/` 子目录。

### 挖掘提示词

```
POST /api/mine-prompt
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "keyword": "AI 写作技巧"
}
```

挖掘结果自动存入当前选中的知识库。

### 优化提示词

```
POST /api/optimize-prompt
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "content": "帮我写一个..."
}
```

返回结构化的提示词内容，可直接供 Dify 或其他 Agent 工具调用。

## 项目结构

```
├── miner.py            # 主程序 (FastAPI + SQLite)
├── config.yaml         # 本地配置 (不提交)
├── config.example.yaml # 配置模板
├── requirements.txt    # Python 依赖
├── static/             # 前端静态资源 (Vue3 + Tailwind)
├── vault/              # 知识库 Markdown 存储根目录
│   ├── default/        # 默认知识库子目录
│   └── {slug}/         # 其他知识库子目录
└── prompts.db          # SQLite 数据库
```

## 安全说明

`config.yaml` 不应保存生产口令、JWT 密钥和 API Key。生产部署时请使用环境变量覆盖敏感项，并确保服务仅在 HTTPS 或可信内网后面暴露。

如果要避免保存明文密码，`PROMPT_MINER_PASSWORD` 支持使用 `pbkdf2_sha256$迭代次数$salt$hash` 格式的哈希值。

文件导出路径校验支持通过 `storage.allowed_export_paths` 配置允许目录列表。默认只允许写入 `storage.vault_path`，如果你需要扩展导出目标，必须显式把目录加入白名单。

如果服务运行在 nginx 或 Cloudflare 等反向代理后面，Cookie 的 `Secure` 属性可以通过 `security.cookie_secure` 或环境变量 `PROMPT_MINER_COOKIE_SECURE` 显式控制。未显式设置时，服务会优先参考 `X-Forwarded-Proto`。
