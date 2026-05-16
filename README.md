# Prompt Miner — 提示词自动化挖掘与 Web 管理仪表盘

一个结合 Jina AI 全网搜索、大模型自动结构化提取、SQLite 高性能交互与 Markdown 双向同步的个人提示词管理中枢。

## 核心特性

- **自动挖掘** — Jina AI 驱动全网提示词抓取，LLM 自动提取结构化信息
- **SaaS 级看板** — Vue3 + Tailwind，极简交互体验
- **双存储机制** — SQLite 高性能查询 + Markdown 双向同步
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
# 编辑 config.yaml，填入你的 API Key 和认证信息

# 启动服务
python3 miner.py
```

访问 `http://localhost:8110` 即可使用。

## API 接口

### 挖掘提示词

```
POST /api/mine-prompt
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "keyword": "AI 写作技巧",
  "limit": 10
}
```

返回结构化的提示词列表，可直接供 Dify 或其他 Agent 工具调用。

## 项目结构

```
├── miner.py          # 主程序 (FastAPI + SQLite)
├── config.yaml       # 本地配置 (不提交)
├── config.example.yaml # 配置模板
├── requirements.txt  # Python 依赖
├── vault/            # Markdown 存储目录
└── prompts.db        # SQLite 数据库
```

## 安全说明

`config.yaml` 包含敏感凭据，请勿提交到公共仓库。参考 `config.example.yaml` 进行配置。