"""Explicit, immutable run configuration. Imports never read dotenv or create data."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..config import BASE_DIR

Mode = Literal["offline_demo", "local", "remote"]


class RunOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Mode = "offline_demo"
    model: str = Field(default="local-model", min_length=1, max_length=128)
    token_budget: int = Field(default=30_000, ge=1, le=1_000_000, strict=True)
    context_compress: bool = False
    structured_output: bool = False
    keep_recent_steps: int = Field(default=3, ge=0, le=30)
    use_plan: bool = False
    use_skills: bool = False
    use_mcp: bool = False
    use_memory: bool = False
    use_github: bool = False


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    data_dir: Path = BASE_DIR / "data"
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    api_token: SecretStr = SecretStr("")
    allow_remote: bool = False
    default_mode: Mode = "offline_demo"
    model: str = Field(default="local-model", min_length=1, max_length=128)
    token_budget: int = Field(default=30_000, ge=1, le=1_000_000)
    model_timeout: float = Field(default=120, ge=1, le=600)
    sandbox_timeout: float = Field(default=120, ge=1, le=600)
    rate_limit: int = Field(default=5, ge=1, le=1000)
    log_format: Literal["json", "text"] = "json"

    @classmethod
    def from_env(cls) -> Settings:
        values = {}
        for name in cls.model_fields:
            key = "CODECRAFT_" + ("MODE" if name == "default_mode" else name.upper())
            if key in os.environ:
                values[name] = os.environ[key]
        if values.get("default_mode") == "remote" and "api_key" not in values:
            values["api_key"] = os.environ.get("ZHIPU_API_KEY", "")
        return cls.model_validate(values)

    def options(self, **overrides) -> RunOptions:
        return RunOptions(
            **{"mode": self.default_mode, "model": self.model, "token_budget": self.token_budget, **overrides}
        )

    def validate_mode(self, options: RunOptions) -> None:
        if options.structured_output and options.mode != "local":
            raise ValueError("structured_output仅支持local模式")
        if options.mode == "offline_demo":
            return
        url = urlsplit(self.base_url)
        try:
            _ = url.port
        except ValueError:
            raise ValueError("模型端点端口必须是0至65535的整数") from None
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("模型端点必须是无凭证的 http(s) URL")
        if options.mode == "local":
            host = url.hostname.lower()
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise ValueError("local 模式只允许 loopback 模型端点")
        elif not self.allow_remote:
            raise ValueError("remote 模式默认禁用；不会自动发送远端请求")
        elif not self.api_key.get_secret_value() or url.scheme != "https":
            raise ValueError("remote 模式需要显式密钥和 HTTPS")
