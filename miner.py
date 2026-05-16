#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiofiles
import httpx
import jwt
import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator


CONFIG_PATH = Path(__file__).with_name("config.yaml")
STATIC_DIR = Path(__file__).with_name("static")
DB_PATH = Path(__file__).with_name("prompts.db")
SEARCH_SUFFIX = " prompt github"
REQUEST_TIMEOUT = 30.0
TOKEN_EXPIRE_HOURS = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("prompt-miner")


class ServerConfig(BaseModel):
    host: str
    port: int


class SecurityConfig(BaseModel):
    username: str
    password: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"


class LLMConfig(BaseModel):
    api_key: str = ""
    base_url: str
    model_name: str


class CrawlerConfig(BaseModel):
    jina_base_url: str
    jina_api_key: str = ""


class StorageConfig(BaseModel):
    vault_path: str


class AppConfig(BaseModel):
    server: ServerConfig
    security: SecurityConfig
    llm: LLMConfig
    crawler: CrawlerConfig
    storage: StorageConfig


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)

    @field_validator("username", "password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MinePromptRequest(BaseModel):
    keyword: str = Field(..., min_length=1)

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("keyword 不能为空")
        return normalized


class PromptLLMOutput(BaseModel):
    title: str = Field(..., min_length=3)
    content: str = Field(..., min_length=30)
    tags: list[str] = Field(default_factory=list)

    @field_validator("title", "content")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        tags: list[str] = []
        for item in value:
            tag = item.strip()
            if not tag:
                continue
            tags.append(tag if tag.startswith("#") else f"#{tag}")
        return sorted(set(tags)) or ["#分类/通用"]


class PromptEditorPayload(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    raw_markdown: str | None = None

    @field_validator("title", "content")
    @classmethod
    def normalize_required(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        tags: list[str] = []
        for item in value:
            tag = item.strip()
            if not tag:
                continue
            tags.append(tag if tag.startswith("#") else f"#{tag}")
        return sorted(set(tags))


class PromptListItem(BaseModel):
    id: int
    uid: str
    title: str
    tags: list[str]
    category: str
    updated_at: str
    excerpt: str


class PromptListResponse(BaseModel):
    items: list[PromptListItem]
    available_tags: list[str]


class PromptDetail(BaseModel):
    id: int
    uid: str
    title: str
    content: str
    tags: list[str]
    category: str
    raw_markdown: str
    updated_at: str


class MinePromptResponse(BaseModel):
    keyword: str
    prompt: PromptDetail


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(raw)


CONFIG = load_config(CONFIG_PATH)
app = FastAPI(title="Prompt Mining Engine", version="3.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
auth_scheme = HTTPBearer(auto_error=False)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def vault_dir() -> Path:
    path = Path(CONFIG.storage.vault_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^\w\-.]+", "-", value.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "prompt"


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def markdown_file_path(uid: str, title: str) -> Path:
    filename = f"{sanitize_slug(uid or title)}.md"
    return vault_dir() / filename


def excerpt_text(text: str, limit: int = 160) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def tags_to_json(tags: list[str]) -> str:
    return json.dumps(sorted(set(tags)), ensure_ascii=False)


def tags_from_json(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        LOGGER.warning("标签 JSON 解析失败，已回退为空列表")
    return []


def infer_category_from_tags(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith("#分类/"):
            return tag.removeprefix("#分类/").strip() or "通用"
    return "通用"


def front_matter_from_markdown(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    front_matter = text[4:end]
    body = text[end + 5 :]
    try:
        payload = yaml.safe_load(front_matter) or {}
        if isinstance(payload, dict):
            return payload, body.lstrip()
    except yaml.YAMLError:
        LOGGER.warning("YAML Front Matter 解析失败")
    return {}, text


def parse_legacy_markdown(text: str) -> dict[str, Any]:
    title_match = re.search(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Untitled Prompt"

    tags_match = re.search(r"-\s*标签：(.+)$", text, flags=re.MULTILINE)
    tags = []
    if tags_match:
        tags = [part.strip() for part in tags_match.group(1).split(",") if part.strip()]

    content_match = re.search(r"```markdown\s*(.*?)\s*```", text, flags=re.DOTALL)
    content = content_match.group(1).strip() if content_match else text.strip()

    return {
        "title": title,
        "tags": tags,
        "category": infer_category_from_tags(tags),
        "content": content,
        "raw_markdown": text,
    }


def parse_markdown_document(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    metadata, body = front_matter_from_markdown(raw)
    if metadata:
        title = str(metadata.get("title") or path.stem).strip()
        tags = metadata.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        tags = [str(item).strip() for item in tags if str(item).strip()]
        category = str(metadata.get("category") or infer_category_from_tags(tags)).strip() or "通用"
        content = body.strip() or raw.strip()
        return {
            "uid": path.stem,
            "title": title,
            "tags": tags,
            "category": category,
            "content": content,
            "raw_markdown": raw,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }

    legacy = parse_legacy_markdown(raw)
    legacy["uid"] = path.stem
    legacy["mtime"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return legacy


def render_markdown_document(title: str, content: str, tags: list[str], category: str, updated_at: str) -> str:
    front_matter = {
        "title": title,
        "category": category,
        "tags": tags,
        "updated_at": updated_at,
    }
    return (
        "---\n"
        f"{yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False)}"
        "---\n\n"
        f"{content.strip()}\n"
    )


def init_db() -> None:
    conn = get_conn()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL,
                category TEXT NOT NULL,
                raw_markdown TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    conn.close()


def create_access_token(username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_EXPIRE_HOURS)).timestamp()),
    }
    return jwt.encode(
        payload,
        CONFIG.security.jwt_secret_key,
        algorithm=CONFIG.security.jwt_algorithm,
    )


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未授权")
    try:
        return jwt.decode(
            credentials.credentials,
            CONFIG.security.jwt_secret_key,
            algorithms=[CONFIG.security.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        LOGGER.warning("JWT 校验失败: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效") from exc


def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def build_search_url(keyword: str, config: AppConfig) -> str:
    encoded_query = quote(f"{keyword}{SEARCH_SUFFIX}")
    base = config.crawler.jina_base_url
    if "{keyword}" in base:
        return base.format(keyword=encoded_query)
    if base.endswith("?q="):
        return f"{base}{encoded_query}"
    if "duckduckgo.com" in base:
        separator = "&" if "?" in base and not base.endswith("?") else ""
        return f"{base}{separator}{encoded_query}"
    target = f"https://html.duckduckgo.com/html/?q={encoded_query}"
    return f"{ensure_trailing_slash(base)}{target}"


def clean_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_noise(text: str) -> str:
    noise_patterns = (
        "sign in",
        "skip to content",
        "cookie",
        "privacy policy",
        "terms of service",
        "advertisement",
        "recommended",
        "javascript",
        "github, inc.",
    )
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        lowered = line.lower()
        if any(pattern in lowered for pattern in noise_patterns):
            continue
        if line.startswith(("![", "<script", "<style")):
            continue
        lines.append(line)
    return clean_text("\n".join(lines))


def infer_category(keyword: str, text: str) -> str:
    combined = f"{keyword} {text}".lower()
    mapping = {
        "marketing": "营销",
        "research": "研究",
        "agent": "智能体",
        "code": "编程",
        "prompt": "提示词",
        "rag": "知识库",
        "education": "教育",
    }
    for token, category in mapping.items():
        if token in combined:
            return category
    return "通用"


def infer_tags(keyword: str, text: str, category: str) -> list[str]:
    combined = f"{keyword} {text}".lower()
    tags = [f"#分类/{category}"]
    tag_map = {
        "github": "#来源/GitHub",
        "agent": "#主题/Agent",
        "prompt engineering": "#主题/PromptEngineering",
        "system prompt": "#主题/SystemPrompt",
        "gpt": "#模型/GPT",
        "claude": "#模型/Claude",
        "qwen": "#模型/Qwen",
        "deepseek": "#模型/DeepSeek",
    }
    for token, tag in tag_map.items():
        if token in combined:
            tags.append(tag)
    return sorted(set(tags))


def derive_title(keyword: str, text: str) -> str:
    first_meaningful = next((line.strip(" #-") for line in text.splitlines() if len(line.strip()) > 8), "")
    if first_meaningful:
        return first_meaningful[:120]
    return f"{keyword} Prompt"


def extract_json_object(text: str) -> str:
    fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        return fenced_match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("模型返回中未找到 JSON 对象。")
    return text[start : end + 1]


async def rewrite_with_llm(raw_text: str, keyword: str, config: AppConfig) -> PromptLLMOutput:
    denoised = strip_noise(raw_text)
    excerpt = denoised[:3000]
    fallback_category = infer_category(keyword, excerpt)
    fallback_tags = infer_tags(keyword, excerpt, fallback_category)
    fallback_title = derive_title(keyword, excerpt)
    fallback_payload = PromptLLMOutput(
        title=fallback_title,
        content=(
            "【角色设定】\n"
            f"你是一名围绕“{keyword}”主题工作的高级提示词执行助手。\n\n"
            "【上下文】\n"
            "- 输入来源为全网检索后的网页文本，已经过基础去噪。\n"
            "- 需要在不歪曲原始意图的前提下，将信息重构为可直接执行的高级提示词。\n"
            "- 对信息不足之处保持保守，不编造具体事实。\n\n"
            "【核心任务】\n"
            "1. 理解用户目标与场景。\n"
            "2. 从原始网页文本中抽取高价值指令、约束、角色和输出要求。\n"
            "3. 清除导航、广告、重复描述与无关噪声。\n"
            "4. 将提炼后的内容重构为稳定、清晰、可复用的高级 Prompt。\n\n"
            "【输出格式】\n"
            "请输出一个结构化结果，必要时使用标题、列表、步骤编号与清晰结论。\n\n"
            "【原始文本摘要】\n"
            f"{excerpt}"
        ),
        tags=fallback_tags,
    )

    client = AsyncOpenAI(api_key=config.llm.api_key, base_url=config.llm.base_url)
    system_prompt = (
        "你是 Prompt-Factory-Agent，一名顶级提示词工程与数据清洗专家。"
        "你必须只返回一个合法 JSON 对象。"
        'JSON 只允许包含字段: "title", "content", "tags"。'
        '其中 "content" 必须严格包含【角色设定】、【上下文】、【核心任务】、【输出格式】四个部分。'
        '其中 "tags" 必须是数组，且至少包含一个 "#分类/xxx" 标签。'
    )
    user_prompt = (
        f"关键词: {keyword}\n"
        "请基于以下已去噪的网页文本，提炼并重构为高级提示词。\n\n"
        f"网页文本:\n{excerpt}"
    )

    try:
        response = await client.chat.completions.create(
            model=config.llm.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        message = response.choices[0].message.content or ""
        payload = json.loads(extract_json_object(message))
        return PromptLLMOutput.model_validate(payload)
    except Exception as exc:
        LOGGER.exception("LLM 重构失败，回退到本地占位重构: %s", exc)
        return fallback_payload


async def fetch_search_text(keyword: str, config: AppConfig) -> str:
    url = build_search_url(keyword, config)
    headers = {"User-Agent": "prompt-mining-engine/3.0"}
    if config.crawler.jina_api_key:
        headers["Authorization"] = f"Bearer {config.crawler.jina_api_key}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            LOGGER.exception("Jina 抓取失败: %s", exc)
            raise


def row_to_summary(row: sqlite3.Row) -> PromptListItem:
    return PromptListItem(
        id=row["id"],
        uid=row["uid"],
        title=row["title"],
        tags=tags_from_json(row["tags"]),
        category=row["category"],
        updated_at=row["updated_at"],
        excerpt=excerpt_text(row["content"]),
    )


def row_to_detail(row: sqlite3.Row) -> PromptDetail:
    return PromptDetail(
        id=row["id"],
        uid=row["uid"],
        title=row["title"],
        content=row["content"],
        tags=tags_from_json(row["tags"]),
        category=row["category"],
        raw_markdown=row["raw_markdown"],
        updated_at=row["updated_at"],
    )


def all_available_tags(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT tags FROM prompts").fetchall()
    tag_set: set[str] = set()
    for row in rows:
        tag_set.update(tags_from_json(row["tags"]))
    return sorted(tag_set)


async def export_prompt_to_markdown(uid: str, title: str, content: str, tags: list[str], category: str, updated_at: str) -> None:
    file_path = markdown_file_path(uid, title)
    raw_markdown = render_markdown_document(title, content, tags, category, updated_at)
    async with aiofiles.open(file_path, "w", encoding="utf-8") as handle:
        await handle.write(raw_markdown)


def upsert_prompt_record(payload: PromptEditorPayload, uid: str | None = None, prompt_id: int | None = None) -> PromptDetail:
    prompt_uid = uid or sanitize_slug(payload.title)
    tags = payload.tags or []
    category = (payload.category or infer_category_from_tags(tags)).strip() or "通用"
    if not any(tag.startswith("#分类/") for tag in tags):
        tags = sorted(set(tags + [f"#分类/{category}"]))
    updated_at = current_timestamp()
    raw_markdown = payload.raw_markdown or render_markdown_document(
        payload.title,
        payload.content,
        tags,
        category,
        updated_at,
    )

    conn = get_conn()
    try:
        with conn:
            if prompt_id is None:
                conn.execute(
                    """
                    INSERT INTO prompts (uid, title, content, tags, category, raw_markdown, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET
                      title = excluded.title,
                      content = excluded.content,
                      tags = excluded.tags,
                      category = excluded.category,
                      raw_markdown = excluded.raw_markdown,
                      updated_at = excluded.updated_at
                    """,
                    (
                        prompt_uid,
                        payload.title,
                        payload.content,
                        tags_to_json(tags),
                        category,
                        raw_markdown,
                        updated_at,
                    ),
                )
                row = conn.execute("SELECT * FROM prompts WHERE uid = ?", (prompt_uid,)).fetchone()
            else:
                existing = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
                if existing is None:
                    raise HTTPException(status_code=404, detail="提示词不存在")
                conn.execute(
                    """
                    UPDATE prompts
                    SET uid = ?, title = ?, content = ?, tags = ?, category = ?, raw_markdown = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        prompt_uid,
                        payload.title,
                        payload.content,
                        tags_to_json(tags),
                        category,
                        raw_markdown,
                        updated_at,
                        prompt_id,
                    ),
                )
                row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise HTTPException(status_code=409, detail="标题或 UID 冲突") from exc
    finally:
        if conn:
            conn.close()

    if row is None:
        raise HTTPException(status_code=500, detail="保存失败")
    return row_to_detail(row)


async def sync_record_to_file(detail: PromptDetail, previous_uid: str | None = None) -> None:
    await export_prompt_to_markdown(
        uid=detail.uid,
        title=detail.title,
        content=detail.content,
        tags=detail.tags,
        category=detail.category,
        updated_at=detail.updated_at,
    )
    if previous_uid and previous_uid != detail.uid:
        old_path = markdown_file_path(previous_uid, previous_uid)
        if old_path.exists():
            old_path.unlink()


async def delete_prompt_record(prompt_id: int) -> PromptDetail:
    conn = get_conn()
    row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="提示词不存在")
    detail = row_to_detail(row)
    with conn:
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    conn.close()
    file_path = markdown_file_path(detail.uid, detail.title)
    if file_path.exists():
        file_path.unlink()
    return detail


def import_vault_to_db() -> None:
    init_db()
    conn = get_conn()
    try:
        for path in vault_dir().glob("*.md"):
            parsed = parse_markdown_document(path)
            existing = conn.execute("SELECT * FROM prompts WHERE uid = ?", (parsed["uid"],)).fetchone()
            file_mtime = parsed["mtime"]
            if existing and existing["updated_at"] >= file_mtime:
                continue
            tags = parsed["tags"] or []
            category = parsed["category"] or infer_category_from_tags(tags)
            content = parsed["content"]
            raw_markdown = parsed["raw_markdown"]
            with conn:
                conn.execute(
                    """
                    INSERT INTO prompts (uid, title, content, tags, category, raw_markdown, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET
                      title = excluded.title,
                      content = excluded.content,
                      tags = excluded.tags,
                      category = excluded.category,
                      raw_markdown = excluded.raw_markdown,
                      updated_at = excluded.updated_at
                    """,
                    (
                        parsed["uid"],
                        parsed["title"],
                        content,
                        tags_to_json(tags),
                        category,
                        raw_markdown,
                        file_mtime,
                    ),
                )
    finally:
        conn.close()


@app.on_event("startup")
async def startup_event() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    vault_dir()
    import_vault_to_db()


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    if payload.username != CONFIG.security.username or payload.password != CONFIG.security.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    return LoginResponse(access_token=create_access_token(payload.username))


@app.get("/api/prompts", response_model=PromptListResponse, dependencies=[Depends(verify_token)])
async def list_prompts(
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
) -> PromptListResponse:
    conn = get_conn()
    try:
        conditions: list[str] = []
        values: list[Any] = []
        if q:
            conditions.append("(title LIKE ? OR content LIKE ?)")
            like = f"%{q.strip()}%"
            values.extend([like, like])
        if tag:
            conditions.append("tags LIKE ?")
            values.append(f'%"{tag}"%')

        sql = "SELECT id, uid, title, tags, category, updated_at, content FROM prompts"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY datetime(updated_at) DESC, id DESC"
        rows = conn.execute(sql, values).fetchall()
        return PromptListResponse(
            items=[row_to_summary(row) for row in rows],
            available_tags=all_available_tags(conn),
        )
    finally:
        conn.close()


@app.get("/api/prompts/{prompt_id}", response_model=PromptDetail, dependencies=[Depends(verify_token)])
async def get_prompt(prompt_id: int) -> PromptDetail:
    conn = get_conn()
    row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="提示词不存在")
    return row_to_detail(row)


@app.post("/api/prompts", response_model=PromptDetail, dependencies=[Depends(verify_token)])
async def create_prompt(payload: PromptEditorPayload) -> PromptDetail:
    detail = upsert_prompt_record(payload)
    await sync_record_to_file(detail)
    return detail


@app.put("/api/prompts/{prompt_id}", response_model=PromptDetail, dependencies=[Depends(verify_token)])
async def update_prompt(prompt_id: int, payload: PromptEditorPayload) -> PromptDetail:
    conn = get_conn()
    existing = conn.execute("SELECT uid FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    conn.close()
    if existing is None:
        raise HTTPException(status_code=404, detail="提示词不存在")
    previous_uid = existing["uid"]
    detail = upsert_prompt_record(payload, uid=sanitize_slug(payload.title), prompt_id=prompt_id)
    await sync_record_to_file(detail, previous_uid=previous_uid)
    return detail


@app.delete("/api/prompts/{prompt_id}", response_model=PromptDetail, dependencies=[Depends(verify_token)])
async def remove_prompt(prompt_id: int) -> PromptDetail:
    return await delete_prompt_record(prompt_id)


@app.post("/api/mine-prompt", response_model=MinePromptResponse, dependencies=[Depends(verify_token)])
async def mine_prompt(payload: MinePromptRequest) -> MinePromptResponse:
    try:
        raw_text = await fetch_search_text(payload.keyword, CONFIG)
        prompt = await rewrite_with_llm(raw_text, payload.keyword, CONFIG)
        category = infer_category_from_tags(prompt.tags)
        editor_payload = PromptEditorPayload(
            title=prompt.title,
            content=prompt.content,
            tags=prompt.tags,
            category=category,
        )
        detail = upsert_prompt_record(editor_payload)
        await sync_record_to_file(detail)
        return MinePromptResponse(keyword=payload.keyword, prompt=detail)
    except httpx.HTTPError as exc:
        LOGGER.exception("Jina 请求失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"Jina 请求失败: {exc}") from exc
    except Exception as exc:
        LOGGER.exception("提示词挖掘失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"提示词挖掘失败: {exc}") from exc


def main() -> None:
    uvicorn.run(app, host=CONFIG.server.host, port=CONFIG.server.port)


if __name__ == "__main__":
    main()
