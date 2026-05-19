#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import secrets
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from hashlib import pbkdf2_hmac
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiofiles
import httpx
import jwt
import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator


CONFIG_PATH = Path(__file__).with_name("config.yaml")
STATIC_DIR = Path(__file__).with_name("static")
DB_PATH = Path(__file__).with_name("prompts.db")
SEARCH_SUFFIX = " prompt github"
REQUEST_TIMEOUT = 30.0
TOKEN_EXPIRE_MINUTES = 30
JWT_ISSUER = "prompt-miner"
JWT_AUDIENCE = "prompt-miner-ui"
AUTH_COOKIE_NAME = "prompt_miner_session"
LOGIN_WINDOW_MINUTES = 15
LOGIN_LOCK_MINUTES = 15
LOGIN_MAX_ATTEMPTS = 5
SEARCH_QUERY_MAX_LENGTH = 120
PROMPT_LIST_MAX_LIMIT = 100
PBKDF2_ITERATIONS = 600000

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
    cookie_secure: bool | None = None


class LLMConfig(BaseModel):
    api_key: str = ""
    base_url: str
    model_name: str


class CrawlerConfig(BaseModel):
    jina_base_url: str
    jina_api_key: str = ""


class StorageConfig(BaseModel):
    vault_path: str
    allowed_export_paths: list[str] = Field(default_factory=list)


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
    access_token: str | None = None
    token_type: str = "bearer"


class SessionResponse(BaseModel):
    authenticated: bool
    username: str | None = None


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


class OptimizePromptRequest(BaseModel):
    content: str = Field(..., min_length=1)

    @field_validator("content")
    @classmethod
    def strip_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("content 不能为空")
        return normalized


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
    total: int


class PromptDetail(BaseModel):
    id: int
    uid: str
    title: str
    content: str
    tags: list[str]
    category: str
    raw_markdown: str
    updated_at: str
    version: int = 1
    variables: list[str] = Field(default_factory=list)


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
auth_scheme = HTTPBearer(auto_error=False)
LOGIN_ATTEMPTS: dict[str, dict[str, Any]] = {}


def env_override(name: str, fallback: str) -> str:
    value = os.getenv(name)
    if value is None:
        return fallback
    return value.strip()


def password_is_hashed(value: str) -> bool:
    return value.startswith("pbkdf2_sha256$")


def hash_password(password: str, salt: str | None = None) -> str:
    actual_salt = salt or secrets.token_hex(16)
    derived = pbkdf2_hmac("sha256", password.encode("utf-8"), actual_salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${actual_salt}${derived.hex()}"


def verify_password(plain_password: str, configured_password: str) -> bool:
    if password_is_hashed(configured_password):
        try:
            _, iterations, salt, expected_hash = configured_password.split("$", 3)
            actual_hash = pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                salt.encode("utf-8"),
                int(iterations),
            ).hex()
            return compare_digest(actual_hash, expected_hash)
        except ValueError:
            LOGGER.error("password 哈希格式非法")
            return False
    return compare_digest(plain_password, configured_password)


def apply_env_overrides(config: AppConfig) -> AppConfig:
    config.security.username = env_override("PROMPT_MINER_USERNAME", config.security.username)
    config.security.password = env_override("PROMPT_MINER_PASSWORD", config.security.password)
    config.security.jwt_secret_key = env_override("PROMPT_MINER_JWT_SECRET_KEY", config.security.jwt_secret_key)
    cookie_secure_override = os.getenv("PROMPT_MINER_COOKIE_SECURE")
    if cookie_secure_override is not None:
        config.security.cookie_secure = cookie_secure_override.strip().lower() in {"1", "true", "yes", "on"}
    config.llm.api_key = env_override("PROMPT_MINER_LLM_API_KEY", config.llm.api_key)
    config.llm.base_url = env_override("PROMPT_MINER_LLM_BASE_URL", config.llm.base_url)
    config.llm.model_name = env_override("PROMPT_MINER_LLM_MODEL_NAME", config.llm.model_name)
    config.crawler.jina_api_key = env_override("PROMPT_MINER_JINA_API_KEY", config.crawler.jina_api_key)
    allowed_paths_override = os.getenv("PROMPT_MINER_ALLOWED_EXPORT_PATHS")
    if allowed_paths_override is not None:
        config.storage.allowed_export_paths = [
            item.strip() for item in allowed_paths_override.split(",") if item.strip()
        ]
    return config


def validate_security_config(config: AppConfig) -> None:
    if not config.security.username:
        raise RuntimeError("缺少安全配置: username")
    if password_is_hashed(config.security.password):
        parts = config.security.password.split("$")
        if len(parts) != 4:
            raise RuntimeError("安全配置不合格: password 哈希格式错误")
    elif len(config.security.password) < 12:
        raise RuntimeError("安全配置不合格: password 长度必须至少为 12")
    if len(config.security.jwt_secret_key) < 32:
        raise RuntimeError("安全配置不合格: jwt_secret_key 长度必须至少为 32")
    if not config.llm.api_key:
        LOGGER.warning("LLM API Key 为空，提示词挖掘接口将无法正常调用上游模型")
    if not password_is_hashed(config.security.password):
        LOGGER.warning("当前仍在使用明文 password，建议改为 pbkdf2_sha256 哈希")


CONFIG = apply_env_overrides(CONFIG)
validate_security_config(CONFIG)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def vault_dir() -> Path:
    path = Path(CONFIG.storage.vault_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def allowed_storage_roots() -> list[Path]:
    configured_paths = CONFIG.storage.allowed_export_paths or [CONFIG.storage.vault_path]
    roots: list[Path] = []
    for item in configured_paths:
        path = Path(item)
        path.mkdir(parents=True, exist_ok=True)
        roots.append(path.resolve())
    return roots


def path_is_within_allowed_roots(resolved: Path) -> bool:
    for root in allowed_storage_roots():
        if resolved.parent == root:
            return True
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^\w\-.]+", "-", value.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "prompt"


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def markdown_file_path(uid: str, title: str) -> Path:
    filename = f"{sanitize_slug(uid or title)}.md"
    return vault_dir() / filename


def ensure_vault_path(path: Path) -> Path:
    resolved = path.resolve()
    if not path_is_within_allowed_roots(resolved):
        raise RuntimeError("检测到非法文件路径")
    return resolved


def excerpt_text(text: str, limit: int = 160) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


JINJA_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def extract_jinja_variables(content: str) -> list[str]:
    return sorted(set(JINJA_VAR_PATTERN.findall(content)))


def tags_to_json(tags: list[str]) -> str:
    return json.dumps(sorted(set(tags)), ensure_ascii=False)


def variables_to_json(vars: list[str]) -> str:
    return json.dumps(vars, ensure_ascii=False)


def variables_from_json(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        LOGGER.warning("variables JSON 解析失败，已回退为空列表")
    return []


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
                updated_at TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                variables TEXT
            )
            """
        )

    existing_cols = set(r[1] for r in conn.execute("PRAGMA table_info(prompts)").fetchall())
    if "version" not in existing_cols:
        try:
            conn.execute("ALTER TABLE prompts ADD COLUMN version INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
    if "variables" not in existing_cols:
        try:
            conn.execute("ALTER TABLE prompts ADD COLUMN variables TEXT")
        except sqlite3.OperationalError:
            pass

    fts_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='prompts_fts'"
    ).fetchone()
    if not fts_exists:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE prompts_fts USING fts5(
                    title, content, content='prompts', content_rowid='id'
                )
                """
            )
        except sqlite3.OperationalError:
            pass

    conn.close()


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
        version = metadata.get("version", 1)
        variables = metadata.get("variables", [])
        if isinstance(variables, str):
            try:
                variables = json.loads(variables)
            except json.JSONDecodeError:
                variables = []
        return {
            "uid": path.stem,
            "title": title,
            "tags": tags,
            "category": category,
            "content": content,
            "raw_markdown": raw,
            "version": version,
            "variables": variables,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }

    legacy = parse_legacy_markdown(raw)
    legacy["uid"] = path.stem
    legacy["mtime"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return legacy


def render_markdown_document(title: str, content: str, tags: list[str], category: str, updated_at: str, version: int = 1, variables: list[str] | None = None) -> str:
    front_matter = {
        "title": title,
        "category": category,
        "tags": tags,
        "updated_at": updated_at,
        "version": version,
    }
    if variables:
        front_matter["variables"] = variables
    return (
        "---\n"
        f"{yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False)}"
        "---\n\n"
        f"{content.strip()}\n"
    )


def create_access_token(username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "exp": int((now + timedelta(minutes=TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(
        payload,
        CONFIG.security.jwt_secret_key,
        algorithm=CONFIG.security.jwt_algorithm,
    )


def client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_tracker_key(username: str, ip: str) -> str:
    return f"{username.lower()}@{ip}"


def login_state_snapshot(key: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    entry = LOGIN_ATTEMPTS.get(key)
    if entry is None:
        entry = {"count": 0, "window_started_at": now, "locked_until": None}
        LOGIN_ATTEMPTS[key] = entry
        return entry
    locked_until = entry.get("locked_until")
    window_started_at = entry.get("window_started_at", now)
    if locked_until and locked_until <= now:
        entry["count"] = 0
        entry["locked_until"] = None
        entry["window_started_at"] = now
        return entry
    if now - window_started_at > timedelta(minutes=LOGIN_WINDOW_MINUTES):
        entry["count"] = 0
        entry["locked_until"] = None
        entry["window_started_at"] = now
    return entry


def ensure_login_allowed(username: str, ip: str) -> None:
    entry = login_state_snapshot(login_tracker_key(username, ip))
    locked_until = entry.get("locked_until")
    now = datetime.now(timezone.utc)
    if locked_until and locked_until > now:
        seconds = int((locked_until - now).total_seconds())
        LOGGER.warning("登录暂时锁定: username=%s ip=%s remaining=%ss", username, ip, seconds)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录尝试过多，请稍后再试")


def register_login_failure(username: str, ip: str) -> None:
    key = login_tracker_key(username, ip)
    entry = login_state_snapshot(key)
    entry["count"] += 1
    if entry["count"] >= LOGIN_MAX_ATTEMPTS:
        entry["locked_until"] = datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCK_MINUTES)
        LOGGER.warning("触发登录锁定: username=%s ip=%s", username, ip)
    else:
        LOGGER.warning("登录失败: username=%s ip=%s count=%s", username, ip, entry["count"])


def clear_login_failures(username: str, ip: str) -> None:
    LOGIN_ATTEMPTS.pop(login_tracker_key(username, ip), None)


def request_scheme(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded_proto in {"http", "https"}:
        return forwarded_proto
    return request.url.scheme.lower()


def cookie_secure_flag(request: Request) -> bool:
    if CONFIG.security.cookie_secure is not None:
        return CONFIG.security.cookie_secure
    return request_scheme(request) == "https"


def set_auth_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=cookie_secure_flag(request),
        samesite="strict",
        max_age=TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def clear_auth_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        secure=cookie_secure_flag(request),
        samesite="strict",
        path="/",
    )


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
) -> dict:
    token = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    if token is None:
        token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未授权")
    try:
        return jwt.decode(
            token,
            CONFIG.security.jwt_secret_key,
            algorithms=[CONFIG.security.jwt_algorithm],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
    except jwt.PyJWTError as exc:
        LOGGER.warning("JWT 校验失败: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效") from exc


def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def build_search_url(keyword: str, config: AppConfig) -> str:
    encoded_query = quote(f"{keyword}{SEARCH_SUFFIX}")
    base = ensure_trailing_slash(config.crawler.jina_base_url)
    return f"{base}{encoded_query}"


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
    fenced_match = re.search(r'```json\s*(\{.*?\})\s*```', text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        return fenced_match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("模型返回中未找到 JSON 对象。")
    return text[start : end + 1]


FEWSHOT_SYSTEM = (
    "你是 Prompt-Factory-Agent，一名顶级提示词工程与数据清洗专家。"
    "你必须只返回一个合法 JSON 对象。"
    'JSON 只允许包含字段: "title", "content", "tags"。'
    '其中 "content" 必须严格包含【角色设定】、【上下文】、【核心任务】、【输出格式】四个部分。'
    '其中 "tags" 必须是数组，且至少包含一个 "#分类/xxx" 标签。'
)

FEWSHOT_EXAMPLE_INPUT = (
    "帮我写一个代码审查的prompt。用户想要一个能自动审查代码、指出潜在bug和改进建议的助手。\n"
    "希望它能用中文输出，给出具体建议，不要太啰嗦。"
)

FEWSHOT_EXAMPLE_OUTPUT = (
    '{"title":"高级代码审查助手",'
    '"content":"【角色设定】\\n你是一名资深软件工程师兼代码审查专家，拥有十年以上的编程经验，'
    '熟悉多种编程语言和最佳实践。\\n\\n【上下文】\\n- 用户输入为一段源代码，可能包含一个或多个文件。'
    '\\n- 需要在不修改原始逻辑的前提下，发现潜在问题、安全隐患和可优化之处。\\n'
    '- 对信息不足之处保持保守，不随意推测未给出的业务场景。\\n\\n【核心任务】\\n'
    '1. 逐行分析代码结构，识别逻辑错误和边界条件遗漏。\\n'
    '2. 检查安全漏洞（如SQL注入、缓冲区溢出敏感操作）。\\n'
    '3. 评估代码可读性和可维护性。\\n'
    '4. 提出具体、可执行的改进建议，并标注优先级。\\n\\n【输出格式】\\n'
    '输出一个结构化报告，包含：问题列表（位置+描述+建议）、总体评价、改进优先级（高/中/低）。',
    '"tags":["#分类/编程","#主题/CodeReview","#模型/GPT"]}'
)


async def rewrite_with_llm(raw_text: str, keyword: str, config: AppConfig) -> PromptLLMOutput:
    denoised = strip_noise(raw_text)
    excerpt = denoised[:12000]
    fallback_category = infer_category(keyword, excerpt)
    fallback_tags = infer_tags(keyword, excerpt, fallback_category)
    fallback_title = derive_title(keyword, excerpt)
    fallback_payload = PromptLLMOutput(
        title=fallback_title,
        content=(
            "【角色设定】\n"
            f'你是一名围绕"{keyword}"主题工作的高级提示词执行助手。\n\n'
            "【上下文】\n"
            "- 输入文本是由 Jina Search 聚合的多个相关网页的全文内容。\n"
            "- 请综合对比这些来源，提取其中质量最高、结构最完整的核心思想。\n"
            "- 过滤掉过时的或质量低下的变体，只保留最有价值的指令和约束。\n"
            "- 需要在不歪曲原始意图的前提下，将信息重构为可直接执行的高级提示词。\n\n"
            "【核心任务】\n"
            "1. 理解用户目标与场景。\n"
            "2. 从聚合网页文本中抽取高价值指令、约束、角色和输出要求。\n"
            "3. 过滤掉导航、广告、重复描述、低质量变体与无关噪声。\n"
            "4. 将提炼后的内容重构为稳定、清晰、可复用的终极高级 Prompt。\n\n"
            "【输出格式】\n"
            "请输出一个结构化结果，必要时使用标题、列表、步骤编号与清晰结论。\n\n"
            "【原始文本摘要】\n"
            f"{excerpt}"
        ),
        tags=fallback_tags,
    )

    client = AsyncOpenAI(api_key=config.llm.api_key, base_url=config.llm.base_url)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": FEWSHOT_SYSTEM},
        {"role": "user", "content": f"示例输入：\n{FEWSHOT_EXAMPLE_INPUT}\n\n示例输出：\n{FEWSHOT_EXAMPLE_OUTPUT}"},
        {"role": "assistant", "content": FEWSHOT_EXAMPLE_OUTPUT},
        {"role": "user", "content": (
            f"关键词: {keyword}\n"
            "输入文本是由 Jina Search 聚合的多个相关网页的全文内容。"
            "请综合对比这些来源，提取其中质量最高、结构最完整的核心思想，"
            "并重构为一个终极的高级提示词。过滤掉过时的或质量低下的变体。\n\n"
            f"网页文本:\n{excerpt}"
        )},
    ]

    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model=config.llm.model_name,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            message = response.choices[0].message.content or ""
            payload = json.loads(extract_json_object(message))
            return PromptLLMOutput.model_validate(payload)
        except Exception as exc:
            LOGGER.warning("LLM 调用失败（第 %s 次尝试）: %s", attempt + 1, exc)
            if attempt < 2:
                messages.append({"role": "user", "content": f"JSON 解析失败，请修复格式后重新输出一个合法的 JSON 对象。错误信息：{exc}"})
                continue
            LOGGER.exception("LLM 重构失败，回退到本地占位重构: %s", exc)
            return fallback_payload
    return fallback_payload


async def fetch_search_text(keyword: str, config: AppConfig) -> str:
    url = build_search_url(keyword, config)
    headers = {
        "User-Agent": "prompt-mining-engine/3.0",
        "X-Return-Format": "markdown",
    }
    if config.crawler.jina_api_key:
        headers["Authorization"] = f"Bearer {config.crawler.jina_api_key}"
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
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
    try:
        ver = int(row["version"]) if row["version"] is not None else 1
    except (KeyError, IndexError, TypeError, ValueError):
        ver = 1
    try:
        vars_val = variables_from_json(row["variables"]) if row["variables"] is not None else []
    except (KeyError, IndexError, TypeError):
        vars_val = []
    return PromptDetail(
        id=row["id"],
        uid=row["uid"],
        title=row["title"],
        content=row["content"],
        tags=tags_from_json(row["tags"]),
        category=row["category"],
        raw_markdown=row["raw_markdown"],
        updated_at=row["updated_at"],
        version=ver,
        variables=vars_val,
    )


def all_available_tags(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT tags FROM prompts").fetchall()
    tag_set: set[str] = set()
    for row in rows:
        tag_set.update(tags_from_json(row["tags"]))
    return sorted(tag_set)


async def export_prompt_to_markdown(uid: str, title: str, content: str, tags: list[str], category: str, updated_at: str, version: int = 1, variables: list[str] | None = None) -> None:
    file_path = ensure_vault_path(markdown_file_path(uid, title))
    raw_markdown = render_markdown_document(title, content, tags, category, updated_at, version, variables)
    async with aiofiles.open(file_path, "w", encoding="utf-8") as handle:
        await handle.write(raw_markdown)


def upsert_prompt_record(payload: PromptEditorPayload, uid: str | None = None, prompt_id: int | None = None) -> PromptDetail:
    prompt_uid = uid or sanitize_slug(payload.title)
    tags = payload.tags or []
    category = (payload.category or infer_category_from_tags(tags)).strip() or "通用"
    if not any(tag.startswith("#分类/") for tag in tags):
        tags = sorted(set(tags + [f"#分类/{category}"]))
    updated_at = current_timestamp()
    variables = extract_jinja_variables(payload.content)
    existing_version = 1
    if prompt_id is not None:
        conn_check = get_conn()
        existing_row = conn_check.execute("SELECT version, variables FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        conn_check.close()
        if existing_row:
            existing_version = existing_row["version"] or 1
    raw_markdown = payload.raw_markdown or render_markdown_document(
        payload.title,
        payload.content,
        tags,
        category,
        updated_at,
        existing_version,
        variables if variables else None,
    )

    conn = get_conn()
    try:
        with conn:
            if prompt_id is None:
                conn.execute(
                    """
                    INSERT INTO prompts (uid, title, content, tags, category, raw_markdown, updated_at, version, variables)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET
                      title = excluded.title,
                      content = excluded.content,
                      tags = excluded.tags,
                      category = excluded.category,
                      raw_markdown = excluded.raw_markdown,
                      updated_at = excluded.updated_at,
                      version = excluded.version,
                      variables = excluded.variables
                    """,
                    (
                        prompt_uid,
                        payload.title,
                        payload.content,
                        tags_to_json(tags),
                        category,
                        raw_markdown,
                        updated_at,
                        existing_version,
                        variables_to_json(variables) if variables else None,
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
                    SET uid = ?, title = ?, content = ?, tags = ?, category = ?, raw_markdown = ?, updated_at = ?, version = ?, variables = ?
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
                        existing_version,
                        variables_to_json(variables) if variables else None,
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
    version = getattr(detail, 'version', 1)
    variables = getattr(detail, 'variables', None)
    await export_prompt_to_markdown(
        uid=detail.uid,
        title=detail.title,
        content=detail.content,
        tags=detail.tags,
        category=detail.category,
        updated_at=detail.updated_at,
        version=version,
        variables=variables,
    )
    if previous_uid and previous_uid != detail.uid:
        old_path = ensure_vault_path(markdown_file_path(previous_uid, previous_uid))
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
    file_path = ensure_vault_path(markdown_file_path(detail.uid, detail.title))
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
            version = parsed.get("version", 1)
            variables = parsed.get("variables", [])
            with conn:
                conn.execute(
                    """
                    INSERT INTO prompts (uid, title, content, tags, category, raw_markdown, updated_at, version, variables)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET
                      title = excluded.title,
                      content = excluded.content,
                      tags = excluded.tags,
                      category = excluded.category,
                      raw_markdown = excluded.raw_markdown,
                      updated_at = excluded.updated_at,
                      version = excluded.version,
                      variables = excluded.variables
                    """,
                    (
                        parsed["uid"],
                        parsed["title"],
                        content,
                        tags_to_json(tags),
                        category,
                        raw_markdown,
                        file_mtime,
                        version,
                        variables_to_json(variables) if variables else None,
                    ),
                )
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    vault_dir()
    import_vault_to_db()
    yield


app = FastAPI(title="Prompt Mining Engine", version="3.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    ip = client_ip(request)
    ensure_login_allowed(payload.username, ip)
    if not compare_digest(payload.username, CONFIG.security.username) or not verify_password(
        payload.password, CONFIG.security.password
    ):
        register_login_failure(payload.username, ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    clear_login_failures(payload.username, ip)
    token = create_access_token(payload.username)
    set_auth_cookie(response, token, request)
    LOGGER.info("登录成功: username=%s ip=%s", payload.username, ip)
    return LoginResponse(access_token=token)


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    clear_auth_cookie(response, request)
    return response


@app.get("/api/session", response_model=SessionResponse)
async def session(claims: dict = Depends(verify_token)) -> SessionResponse:
    return SessionResponse(authenticated=True, username=str(claims.get("sub") or ""))


@app.get("/api/prompts", response_model=PromptListResponse, dependencies=[Depends(verify_token)])
async def list_prompts(
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=24, ge=1, le=PROMPT_LIST_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> PromptListResponse:
    conn = get_conn()
    try:
        conditions: list[str] = []
        values: list[Any] = []
        if q:
            query_text = q.strip()
            if len(query_text) > SEARCH_QUERY_MAX_LENGTH:
                raise HTTPException(status_code=422, detail="搜索关键词过长")
            fts_query = query_text.replace('"', '""')
            conditions.append("id IN (SELECT rowid FROM prompts_fts WHERE prompts_fts MATCH ?)")
            values.append(f'"{fts_query}"')
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
            values.append(tag)

        sql = "SELECT id, uid, title, tags, category, updated_at, content FROM prompts"
        count_sql = "SELECT COUNT(*) FROM prompts"
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)
            sql += where_clause
            count_sql += where_clause
        sql += " ORDER BY datetime(updated_at) DESC, id DESC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, [*values, limit, offset]).fetchall()
        total = int(conn.execute(count_sql, values).fetchone()[0])
        return PromptListResponse(
            items=[row_to_summary(row) for row in rows],
            available_tags=all_available_tags(conn),
            total=total,
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


@app.post("/api/optimize-prompt", response_model=PromptLLMOutput, dependencies=[Depends(verify_token)])
async def optimize_prompt(payload: OptimizePromptRequest) -> PromptLLMOutput:
    client = AsyncOpenAI(api_key=CONFIG.llm.api_key, base_url=CONFIG.llm.base_url)
    system_msg = (
        "你是 Prompt-Factory-Agent，一名顶级提示词工程专家。"
        '你必须只返回一个合法 JSON 对象，包含 "title"、"content"、"tags" 三个字段。'
        '"content" 必须严格包含【角色设定】、【上下文】、【核心任务】、【输出格式】四个部分。'
        '"tags" 必须是数组，且至少包含一个 "#分类/xxx" 标签。'
        "请将用户输入的口语化描述扩写为结构完整、可直接使用的高级 Prompt。"
    )
    user_msg = f"口语化需求：{payload.content}"

    messages: list[dict[str, str]] = [
        {"role": "system", "content": FEWSHOT_SYSTEM},
        {"role": "user", "content": f"示例输入：\n{FEWSHOT_EXAMPLE_INPUT}\n\n示例输出：\n{FEWSHOT_EXAMPLE_OUTPUT}"},
        {"role": "assistant", "content": FEWSHOT_EXAMPLE_OUTPUT},
        {"role": "user", "content": user_msg},
    ]

    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model=CONFIG.llm.model_name,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            message = response.choices[0].message.content or ""
            result = json.loads(extract_json_object(message))
            return PromptLLMOutput.model_validate(result)
        except Exception as exc:
            LOGGER.warning("optimize-prompt 调用失败（第 %s 次尝试）: %s", attempt + 1, exc)
            if attempt < 2:
                messages.append({"role": "user", "content": f"JSON 解析失败，请修复格式后重新输出一个合法的 JSON 对象。错误信息：{exc}"})
                continue
            raise HTTPException(status_code=500, detail=f"Prompt 优化失败: {exc}") from exc


def main() -> None:
    uvicorn.run(app, host=CONFIG.server.host, port=CONFIG.server.port)


if __name__ == "__main__":
    main()
