import os
import secrets
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


def default_state_dir():
    installed = Path.home() / "Library/Application Support/BOSS-Hiring"
    if (installed / "deployment.json").is_file():
        return installed / "state"
    environment = Path(sys.prefix)
    if environment.name == ".venv" and (environment.parent / "pyproject.toml").is_file():
        return environment.parent / ".state"
    return Path(__file__).resolve().parent.parent / ".state"


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cities: list[str] = Field(default_factory=lambda: ["杭州", "上海"], min_length=1, max_length=2)
    keywords: list[str] = Field(default_factory=lambda: ["AI应用开发", "大模型应用", "Agent开发", "智能体开发"], min_length=1, max_length=20)
    exclude_companies: list[str] = Field(default_factory=list, max_length=100)
    daily_limit: int = Field(default=20, ge=1, le=100)
    interval_seconds: int = Field(default=30, ge=10, le=3600)
    collect_limit: int = Field(default=20, ge=1, le=100)

    @field_validator("cities")
    @classmethod
    def valid_cities(cls, values):
        if len(set(values)) != len(values) or any(v not in {"杭州", "上海"} for v in values):
            raise ValueError("城市仅支持杭州、上海，且不能重复")
        return values

    @field_validator("keywords", "exclude_companies")
    @classmethod
    def valid_words(cls, values):
        if any(not v.strip() or len(v) > 100 for v in values):
            raise ValueError("关键词不能为空且最多 100 个字符")
        return list(dict.fromkeys(v.strip() for v in values))


class Config:
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir or os.environ.get("BOSS_STATE_DIR", default_state_dir())).resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        token_file = self.state_dir / "api-token"
        try:
            descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
        os.chmod(token_file, 0o600)
        self.token = token_file.read_text().strip()
        if len(self.token) < 24:
            raise RuntimeError("本地访问令牌无效，请停止服务后重新生成")
