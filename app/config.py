import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析（systemd 部署时由 EnvironmentFile 提供，这里仅方便本地开发）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    password: str
    secret: str
    data_dir: Path
    cookie_secure: bool
    max_download_mb: int
    wallhaven_key: str
    danbooru_login: str
    danbooru_key: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "paperdrift.db"

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"


def load_settings() -> Settings:
    _load_dotenv(Path(".env"))
    password = os.environ.get("PAPERDRIFT_PASSWORD", "")
    secret = os.environ.get("PAPERDRIFT_SECRET", "")
    if len(password) < 8 or password.startswith("change-me"):
        raise RuntimeError("请在 .env 中设置至少 8 位、且不是示例值的 PAPERDRIFT_PASSWORD")
    if len(secret) < 32:
        raise RuntimeError("请在 .env 中设置 PAPERDRIFT_SECRET（至少 32 字符，setup.sh 会自动生成）")
    data_dir = Path(os.environ.get("PAPERDRIFT_DATA_DIR", "./data")).resolve()
    s = Settings(
        password=password,
        secret=secret,
        data_dir=data_dir,
        cookie_secure=os.environ.get("PAPERDRIFT_COOKIE_SECURE", "0") == "1",
        max_download_mb=int(os.environ.get("PAPERDRIFT_MAX_DOWNLOAD_MB", "300")),
        wallhaven_key=os.environ.get("WALLHAVEN_API_KEY", ""),
        danbooru_login=os.environ.get("DANBOORU_LOGIN", ""),
        danbooru_key=os.environ.get("DANBOORU_API_KEY", ""),
    )
    for d in (s.data_dir, s.previews_dir, s.library_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
