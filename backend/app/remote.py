"""CCR environment descriptors. Upstream has no proxy listener in this module."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
NO_PROXY = ",".join(
    (
        "localhost",
        "127.0.0.1",
        "::1",
        "169.254.0.0/16",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "anthropic.com",
        ".anthropic.com",
        "*.anthropic.com",
        "github.com",
        "api.github.com",
        "*.github.com",
        "*.githubusercontent.com",
        "registry.npmjs.org",
        "index.crates.io",
        "pypi.org",
        "files.pythonhosted.org",
        "proxy.golang.org",
    )
)


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def inherited_env(env):
    return (
        {key: env[key] for key in ENV_KEYS if key in env}
        if "HTTPS_PROXY" in env and "SSL_CERT_FILE" in env
        else {}
    )


@dataclass
class ProxyBootstrap:
    enabled: bool
    session_id: str | None
    base_url: str
    upstream_enabled: bool
    token_path: Path
    ca_bundle_path: Path
    system_ca_path: Path
    token: str | None = field(repr=False, default=None)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        token_path = Path(env.get("CCR_SESSION_TOKEN_PATH") or "/run/ccr/session_token")
        try:
            token = (
                token_path.read_text().strip() or None
                if token_path.stat().st_size < 65536
                else None
            )
        except OSError:
            token = None
        return cls(
            truthy(env.get("CLAUDE_CODE_REMOTE")),
            env.get("CLAUDE_CODE_REMOTE_SESSION_ID") or None,
            env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
            truthy(env.get("CCR_UPSTREAM_PROXY_ENABLED")),
            token_path,
            Path(
                env.get("CCR_CA_BUNDLE_PATH") or Path(env.get("HOME") or ".") / ".ccr/ca-bundle.crt"
            ),
            Path(env.get("CCR_SYSTEM_CA_BUNDLE") or "/etc/ssl/certs/ca-certificates.crt"),
            token,
        )

    @property
    def should_enable(self):
        return bool(self.enabled and self.upstream_enabled and self.session_id and self.token)

    def ws_url(self):
        base = self.base_url.rstrip("/")
        base = (
            "wss://" + base[8:]
            if base.startswith("https://")
            else "ws://" + base[7:]
            if base.startswith("http://")
            else "wss://" + base
        )
        return base + "/v1/code/upstreamproxy/ws"

    def state_for_port(self, port):
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1–65535")
        return {
            "enabled": self.should_enable,
            "proxy_url": f"http://127.0.0.1:{port}" if self.should_enable else None,
            "ca_bundle_path": str(self.ca_bundle_path) if self.should_enable else None,
            "no_proxy": NO_PROXY,
        }

    def subprocess_env(self, port):
        state = self.state_for_port(port)
        if not state["enabled"]:
            return {}
        return {
            "HTTPS_PROXY": state["proxy_url"],
            "https_proxy": state["proxy_url"],
            "NO_PROXY": NO_PROXY,
            "no_proxy": NO_PROXY,
            **{key: str(self.ca_bundle_path) for key in ENV_KEYS[4:]},
        }

    def public(self):
        return {
            "remote_enabled": self.enabled,
            "session_id": self.session_id,
            "base_url": self.base_url,
            "eligible": self.should_enable,
            "token_set": bool(self.token),
            "ws_url": self.ws_url(),
            "proxy_started": False,
        }
