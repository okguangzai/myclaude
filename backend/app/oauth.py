"""PKCE authorization, atomic credentials, expiry and refresh (runtime/oauth.rs)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import BaseModel, Field, field_validator


class OAuthConfig(BaseModel):
    authorize_url: str
    token_url: str
    client_id: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str = "http://127.0.0.1:8000/api/oauth/callback"

    @field_validator("authorize_url", "token_url", "redirect_uri")
    @classmethod
    def valid_url(cls, value):
        url = urlsplit(value)
        if not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("OAuth URL 不合法")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("OAuth 端点必须使用 HTTPS，或本机回调 HTTP")
        return value


def code_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class OAuth:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.path = store.data / "credentials.json"
        self.pending = {}
        self.lock = asyncio.Lock()
        self._remember(self.load())

    def load(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _remember(self, root):
        for key in ("accessToken", "refreshToken"):
            token = root.get("oauth", {}).get(key)
            if token:
                self.store.extra_secrets.add(token)

    def save(self, root):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(root, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        self._remember(root)

    def status(self):
        root = self.load()
        token = root.get("oauth", {})
        return {
            "authenticated": bool(token.get("accessToken")),
            "refreshable": bool(token.get("refreshToken")),
            "expires_at": token.get("expiresAt"),
            "scopes": token.get("scopes", []),
            "config": root.get("oauth_config") or self.store.current.oauth_config or None,
        }

    def begin(self, config=None):
        cfg = OAuthConfig.model_validate(config or self.status()["config"] or {})
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > time.time()}
        if len(self.pending) >= 10:
            raise ValueError("授权请求过多，请稍后重试")
        self.pending[state] = {"verifier": verifier, "config": cfg, "expires": time.time() + 600}
        params = {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": " ".join(cfg.scopes),
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        separator = "&" if "?" in cfg.authorize_url else "?"
        return {
            "authorize_url": cfg.authorize_url + separator + urlencode(params),
            "expires_in": 600,
        }

    async def _exchange(self, cfg, payload, previous=None):
        response = await self.client.post(cfg.token_url, data=payload, timeout=30)
        if response.is_error:
            raise ValueError(f"OAuth 令牌端点返回 HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise ValueError("OAuth 响应缺少 access_token")
        token = {
            "accessToken": result["access_token"],
            "refreshToken": result.get("refresh_token") or (previous or {}).get("refreshToken"),
            "expiresAt": int(time.time()) + int(result["expires_in"])
            if result.get("expires_in") is not None
            else None,
            "scopes": result.get("scope", " ".join(cfg.scopes)).split(),
        }
        root = self.load()
        root.update(oauth=token, oauth_config=cfg.model_dump())
        self.save(root)
        return token

    async def complete(self, callback):
        query = parse_qs(urlsplit(callback).query if "://" in callback else callback.lstrip("?"))
        state = query.get("state", [""])[0]
        request = self.pending.pop(state, None)
        if not request or request["expires"] <= time.time():
            raise ValueError("OAuth state 无效、已使用或已过期，请重新授权")
        if query.get("error"):
            raise ValueError("授权被拒绝或取消")
        code = query.get("code", [""])[0]
        if not code:
            raise ValueError("回调中缺少授权码")
        cfg = request["config"]
        async with self.lock:
            await self._exchange(
                cfg,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": cfg.redirect_uri,
                    "client_id": cfg.client_id,
                    "code_verifier": request["verifier"],
                    "state": state,
                },
            )
        return self.status()

    async def access_token(self, force_refresh=False):
        async with self.lock:
            root = self.load()
            token = root.get("oauth", {})
            if not token.get("accessToken"):
                raise ValueError("尚未完成 OAuth 登录")
            if force_refresh or (
                token.get("expiresAt") is not None and token["expiresAt"] <= time.time() + 30
            ):
                if not token.get("refreshToken"):
                    raise ValueError("OAuth 已过期且无刷新令牌，请重新登录")
                cfg = OAuthConfig.model_validate(root["oauth_config"])
                token = await self._exchange(
                    cfg,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": token["refreshToken"],
                        "client_id": cfg.client_id,
                        "scope": " ".join(cfg.scopes),
                    },
                    token,
                )
            return token["accessToken"]

    async def logout(self):
        async with self.lock:
            root = self.load()
            root.pop("oauth", None)
            self.pending.clear()
            self.save(root)
        return self.status()
