"""Frozen upstream model aliases and explicit vendor routes; never cross-send credentials."""

ALIASES = {
    "opus": "anthropic/claude-opus-4-7",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-haiku-4-5-20251213",
    "grok": "xai/grok-3",
    "grok-mini": "xai/grok-3-mini",
    "kimi": "dashscope/kimi-k2.5",
}
VENDORS = {
    "anthropic": ("anthropic", "ANTHROPIC", "https://api.anthropic.com/v1"),
    "claude": ("anthropic", "ANTHROPIC", "https://api.anthropic.com/v1"),
    "openai": ("openai", "OPENAI", "https://api.openai.com/v1"),
    "xai": ("openai", "XAI", "https://api.x.ai/v1"),
    "grok": ("openai", "XAI", "https://api.x.ai/v1"),
    "dashscope": ("openai", "DASHSCOPE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "qwen": ("openai", "DASHSCOPE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "kimi": ("openai", "DASHSCOPE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "ollama": ("openai", "OLLAMA", "http://127.0.0.1:11434/v1"),
    "local": ("openai", "OPENAI", "http://127.0.0.1:11434/v1"),
}


def resolve(model, aliases=None):
    aliases = {**ALIASES, **(aliases or {})}
    seen = set()
    while model in aliases:
        if model in seen:
            raise ValueError("循环模型别名：" + model)
        seen.add(model)
        model = aliases[model]
    return model


def route(cfg, env):
    from .config import Settings

    resolved = resolve(cfg.model, cfg.model_aliases)
    vendor, slash, model = resolved.partition("/")
    if not slash or vendor not in VENDORS:
        return cfg.model_copy(update={"model": resolved})
    protocol, prefix, default_url = VENDORS[vendor]
    base = env.get(prefix + "_BASE_URL")
    key = env.get(prefix + "_API_KEY")
    # Anthropic is its own protocol; reuse a deliberately configured custom endpoint.
    same = protocol == "anthropic" and cfg.provider == "anthropic"
    if same:
        base, key = base or cfg.base_url, key or cfg.api_key
    if vendor in {"ollama", "local"}:
        base = env.get("OLLAMA_HOST") or base or default_url
        if not base.rstrip("/").endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        key = key or "ollama"
    if not key and not (same and cfg.auth_mode == "oauth"):
        raise ValueError("模型路由缺少环境变量凭据：" + prefix + "_API_KEY")
    return cfg.model_copy(
        update={
            "model": model,
            "provider": protocol,
            "base_url": Settings.clean_url(base or default_url),
            "api_key": key or "",
            "auth_mode": cfg.auth_mode if same else "api-key",
        }
    )
