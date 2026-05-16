---
name: execute_prompt_mining
description: 接收外部传入的关键词变量，调度 Prompt-Factory-Agent 与 Python 异步爬虫抓取全网提示词相关网页源，完成去噪、抽取、结构化重构，并将标准 Markdown 结果追加写入本地 ./vault/ 文件夹。用于提示词挖掘、网页文本清洗、批量沉淀高质量 Prompt 资产。
---

# execute_prompt_mining

## 适用场景
当任务满足以下任一条件时，启用本技能：

- 用户提供一个或多个关键词，要求全网挖掘提示词相关内容
- 需要把网页抓取结果清洗为高质量结构化 Prompt
- 需要联动 `Prompt-Factory-Agent` 进行提示词重构
- 需要把结果以标准 Markdown 形式持续沉淀到本地 `./vault/`

## 输入
外部至少传入以下变量：

- `keyword`

可选变量：

- `language`: 期望输出语言，默认 `zh-CN`
- `max_sources`: 最大抓取源数量，默认 `20`
- `topic_hint`: 主题提示，用于缩小抓取范围
- `output_slug`: 指定输出文件名；未提供时根据 `keyword` 生成

## 依赖约定
- 当前目录下必须存在 [AGENTS.md](/opt/agents/AGENTS.md)，其中已注册 `Prompt-Factory-Agent`
- `Prompt-Factory-Agent` 负责将混乱网页文本统一重构为结构化高级 Prompt，并输出带 `#分类/xxx` 标签的 JSON 元数据
- Python 爬虫必须采用异步方式执行，优先使用 `asyncio` 架构

## 执行流程
### 1. 参数标准化
- 校验 `keyword` 非空
- 去除首尾空白，生成安全文件名片段
- 若未提供 `output_slug`，则基于 `keyword` 生成 slug

### 2. 全网抓取
- 围绕 `keyword`、`topic_hint` 组合搜索意图
- 抓取与提示词、Prompt Engineering、工作流模板、应用案例、角色卡、指令设计相关的网页文本
- 优先保留正文信息密度高、重复转载少、与关键词语义强相关的来源
- 对抓取结果进行去重，避免同站镜像、分页残片和聚合页污染

### 3. 原文清洗
- 提取正文主内容
- 删除导航、页脚、广告、推荐阅读、按钮文案、脚本残片、版权噪声、重复段落
- 将每个来源整理为可供大模型进一步处理的纯文本块

### 4. 调度 Prompt-Factory-Agent
- 将每个来源或归并后的主题文本交由 `Prompt-Factory-Agent`
- 明确要求输出两部分：
  - 四段式结构化高级 Prompt
  - 合法 JSON 元数据，且 `category_tags` 至少包含一个 `#分类/xxx` 标签

### 5. 结果归并
- 合并相似来源的共性 Prompt 模式
- 删除低质量、过短、明显营销化或语义重复的结果
- 为最终沉淀内容补充来源摘要、关键词和生成时间

### 6. 本地落盘
- 确保本地存在 `./vault/` 文件夹；若不存在则创建
- 采用“追加写入”策略，将新结果写入 `./vault/<slug>.md`
- 若文件不存在则新建；若存在则在文末追加新的条目块
- 追加内容时不得覆盖已有人工内容

## 输出文件规范
输出文件必须为 Markdown，并建议按以下结构追加：

````markdown
# Prompt Mining Vault

## Keyword
<keyword>

## Timestamp
<ISO8601 time>

## Source Summary
- 来源数量：<n>
- 主题概述：<summary>

## Structured Prompt
【角色设定】
...

【上下文/边界约束】
...

【核心任务】
...

【输出格式】
...

## Metadata
```json
{
  "agent": "Prompt-Factory-Agent",
  "language": "zh-CN",
  "source_type": "web_scraped_text",
  "category_tags": ["#分类/xxx"],
  "content_topic": "",
  "task_intent": "",
  "noise_removed": [],
  "assumptions": [],
  "output_version": "1.0"
}
```

## Sources
- <url 1>
- <url 2>
````

## 执行要求
- 默认先抓取，再清洗，再交给 `Prompt-Factory-Agent` 重构
- 默认输出中文，除非调用方明确指定其他语言
- 默认保留来源 URL 列表，便于后续回溯
- 结果必须可直接用于构建提示词资产库，不得写成分析草稿

## 质量门槛
- 不收录明显无关页面
- 不收录纯导航页、聚合页、采集镜像页的噪声文本
- 不输出缺少四段结构的 Prompt
- 不输出缺少 `#分类/xxx` 标签的 JSON
- 不覆盖 `./vault/` 中已有内容，只能追加

## 失败处理
- 若抓取结果为空，输出原因并停止写入
- 若网页内容噪声过高，先降噪后再进入重构阶段
- 若结构化结果不完整，必须重试清洗或重新调度 `Prompt-Factory-Agent`
- 若 `./vault/` 不存在且无法创建，明确报错并停止

## 最终产物
成功执行后，至少产出以下结果：

1. 一份或多份来自网页源的清洗文本
2. 一份由 `Prompt-Factory-Agent` 生成的结构化高级 Prompt
3. 一份带 `#分类/xxx` 标签的 JSON 元数据
4. 一份追加写入 `./vault/` 的 Markdown 记录文件
