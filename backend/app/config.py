from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("MYCLAUDE_DATA_DIR", str(ROOT / ".local")))


class ProviderRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai", "anthropic"]
    base_url: str
    api_key_env: str
    model: str | None = None

    @field_validator("base_url")
    @classmethod
    def route_url(cls, value):
        return Settings.clean_url(value)

    @field_validator("api_key_env")
    @classmethod
    def env_name(cls, value):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("凭据引用必须是环境变量名")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai", "anthropic"] = "openai"
    model: str = "qwen-flash"
    subagent_model: str | None = None
    vision_model: str | None = None
    speech_provider: Literal["auto", "nls", "dashscope"] = "auto"
    asr_model: str = "qwen3-asr-flash"
    tts_model: str = "qwen3-tts-flash"
    tts_voice: str = "Cherry"
    privacy_store_history: bool = True
    privacy_store_traces: bool = True
    privacy_allow_cloud: bool = True
    rate_limit_rpm: int = Field(default=120, ge=1, le=6000)
    rate_limit_concurrency: int = Field(default=4, ge=1, le=32)
    upstream_preferences: dict = Field(default_factory=dict)
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    auth_mode: Literal["api-key", "oauth"] = "api-key"
    allowed_tools: list[str] = Field(default_factory=list)
    permission_mode: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    plugin_external_directories: list[str] = Field(default_factory=list)
    plugin_bundled_root: str | None = None
    plugin_install_root: str | None = None
    plugin_registry_path: str | None = None
    plugin_max_output_tokens: int | None = Field(default=None, ge=1, le=1000000)
    plugin_enabled: dict[str, bool] = Field(default_factory=dict)
    runtime_environment: dict[str, str] = Field(default_factory=dict)
    upstream_mcp_servers: dict[str, dict] = Field(default_factory=dict)
    oauth_config: dict = Field(default_factory=dict)
    denied_tools: list[str] = Field(default_factory=list)
    lsp_servers: dict[str, list[str]] = Field(default_factory=dict)
    provider_retries: int = Field(default=2, ge=0, le=20)
    telemetry_enabled: bool = False
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    hooks: dict[str, list] = Field(default_factory=dict)
    trusted_roots: list[str] = Field(default_factory=list)
    trust_denied_roots: list[str] = Field(default_factory=list)
    trust_allowlist: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("trust_allowlist")
    @classmethod
    def trust_patterns(cls, value):
        if any(
            not item.get("pattern", "").strip() or set(item) - {"pattern", "worktree_pattern"}
            for item in value
        ):
            raise ValueError("trust_allowlist 需要 pattern，可选 worktree_pattern")
        return value

    model_aliases: dict[str, str] = Field(default_factory=dict)
    fallback_primary: str | None = None
    fallback_models: list[str] = Field(default_factory=list, max_length=5)
    provider_routes: dict[str, ProviderRoute] = Field(default_factory=dict)
    rules_import: Literal["auto", "none"] | list[str] = "auto"
    permission_rules: dict[str, list[str]] = Field(default_factory=dict)
    sandbox_config: dict = Field(default_factory=dict)

    @field_validator("sandbox_config")
    @classmethod
    def sandbox_options(cls, value):
        allowed = {
            "enabled",
            "namespaceRestrictions",
            "networkIsolation",
            "filesystemMode",
            "allowedMounts",
        }
        if set(value) - allowed:
            raise ValueError("未知 sandbox 配置字段")
        for key in ("enabled", "namespaceRestrictions", "networkIsolation"):
            if key in value and not isinstance(value[key], bool):
                raise ValueError("sandbox 布尔配置不合法")
        if "filesystemMode" in value and value["filesystemMode"] not in {
            "off",
            "read-only",
            "workspace-write",
        }:
            raise ValueError("未知 filesystemMode")
        if "allowedMounts" in value and (
            not isinstance(value["allowedMounts"], list)
            or any(not isinstance(p, str) for p in value["allowedMounts"])
        ):
            raise ValueError("allowedMounts 必须为路径数组")
        return value

    @field_validator("permission_rules")
    @classmethod
    def permission_rules_valid(cls, value):
        if any(key not in {"allow", "deny", "ask"} for key in value):
            raise ValueError("permission_rules 只接受 allow/deny/ask")
        return value

    @field_validator("hooks")
    @classmethod
    def hooks_valid(cls, value):
        from .hooks import validate_hooks

        return validate_hooks(value)

    max_tokens: int = Field(default=4096, ge=64, le=32768)
    temperature: float = Field(default=0.4, ge=0, le=2)
    max_turns: int = Field(default=8, ge=1, le=30)
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    connect_timeout_seconds: int = Field(default=12, ge=1, le=300)
    context_chars: int = Field(default=60000, ge=5000, le=200000)
    auto_compact_tokens: int = Field(default=45000, ge=0, le=1000000)
    workspace: str = str(ROOT / "workspace")
    system_prompt: str = Field(
        default="你是一个严谨的中文编程助手。先理解问题，需要时使用工具。依据真实工具结果回答，并说明无法完成的部分。",
        max_length=12000,
    )
    embedding_model: str = "text-embedding-v4"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    rag_enabled: bool = True
    qdrant_url: str = "http://127.0.0.1:17333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "myclaude_learning_v1"
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_remote_host: str = "127.0.0.1"
    ssh_remote_port: int = Field(default=6333, ge=1, le=65535)
    ssh_local_port: int = Field(default=17333, ge=1024, le=65535)

    @field_validator("base_url", "embedding_base_url", "qdrant_url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        for suffix in ("/chat/completions", "/embeddings", "/messages"):
            if value.endswith(suffix):
                value = value.removesuffix(suffix)
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("请输入不含密钥、查询参数的 HTTP(S) 基础地址")
        return value

    @field_validator("qdrant_collection")
    @classmethod
    def collection_name(cls, value: str) -> str:
        if not re.fullmatch(r"myclaude_[a-zA-Z0-9_-]{1,90}", value):
            raise ValueError("集合名必须以 myclaude_ 开头，只能包含字母、数字、下划线和连字符")
        return value

    @field_validator("ssh_host", "ssh_remote_host")
    @classmethod
    def ssh_name(cls, value: str) -> str:
        if value and (value.startswith("-") or not re.fullmatch(r"[a-zA-Z0-9_.@:-]+", value)):
            raise ValueError("SSH 地址格式不合法")
        return value

    @field_validator("model", "embedding_model")
    @classmethod
    def model_name(cls, value: str) -> str:
        if not value.strip() or len(value) > 180:
            raise ValueError("模型 ID 不能为空或过长")
        return value.strip()

    @field_validator("subagent_model")
    @classmethod
    def subagent_model_name(cls, value: str | None) -> str | None:
        return cls.model_name(value) if value is not None else None

    @field_validator("workspace")
    @classmethod
    def workspace_path(cls, value: str) -> str:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("工作目录必须是已存在的文件夹")
        return str(path)


SECRET_FIELDS = {"api_key", "embedding_api_key", "qdrant_api_key"}


class ConfigStore:
    def __init__(self, data: Path = DATA, root: Path = ROOT):
        self.data, self.root = data, root
        self.extra_secrets = set()
        self.path = data / "settings.json"
        data.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.current = Settings.model_validate_json(self.path.read_text(encoding="utf-8"))
        else:
            env = dotenv_values(root / ".env")
            values = {"workspace": str(root / "workspace")}
            for source, target in [
                ("LLM_URL", "base_url"),
                ("LLM_MODEL", "model"),
                ("LLM_API_KEY", "api_key"),
            ]:
                if env.get(source):
                    values[target] = env[source]
            self.current = Settings(**values)
            self._save(self.current)
        self.remember_runtime_secrets()

    def remember_runtime_secrets(self):
        self.runtime_environment = dict(self.current.runtime_environment)
        self.extra_secrets.update(v for v in self.runtime_environment.values() if len(v) >= 8)
        for server in self.current.upstream_mcp_servers.values():
            for section in ("env", "headers"):
                self.extra_secrets.update(
                    str(v) for v in server.get(section, {}).values() if len(str(v)) >= 8
                )

    def _save(self, settings: Settings):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def public(self) -> dict:
        from .config_layers import mask

        values = self.current.model_dump()
        for name in SECRET_FIELDS:
            values[f"{name}_set"] = bool(values.pop(name))
        # Raw imported MCP credentials and environment must never become form values.
        values.pop("runtime_environment", None)
        values.pop("upstream_mcp_servers", None)
        values["imported_mcp"] = mask(self.current.upstream_mcp_servers)
        values["runtime_environment_names"] = sorted(self.runtime_environment)
        return values

    def update(self, changes: dict) -> Settings:
        # Empty password input means keep; explicit clear list means remove.
        values = self.current.model_dump()
        clear = changes.pop("clear_secrets", [])
        if not isinstance(clear, list) or any(x not in SECRET_FIELDS for x in clear):
            raise ValueError("无效的密钥清除字段")
        for name, value in changes.items():
            if name in SECRET_FIELDS and not value:
                continue
            values[name] = value
        for name in clear:
            values[name] = ""
        updated = Settings(**values)
        self._save(updated)
        self.current = updated
        self.remember_runtime_secrets()
        return updated

    def redact(self, value: str) -> str:
        for secret in sorted(self.extra_secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        for name in SECRET_FIELDS:
            secret = getattr(self.current, name)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
