"""启动入口：python -m app.serve"""
import logging
import os

import uvicorn

from .config import load_settings
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()                     # 密码/密钥没设好会直接拒绝启动
    cert = os.environ.get("PAPERDRIFT_SSL_CERT") or None
    key = os.environ.get("PAPERDRIFT_SSL_KEY") or None
    if cert and not settings.cookie_secure:
        logging.warning("已启用 HTTPS，建议同时设置 PAPERDRIFT_COOKIE_SECURE=1")
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("PAPERDRIFT_HOST", "0.0.0.0"),
        port=int(os.environ.get("PAPERDRIFT_PORT", "8000")),
        ssl_certfile=cert, ssl_keyfile=key,
        proxy_headers=False,                      # 不信任 X-Forwarded-For，登录限速按真实连接 IP 算
        server_header=False,
    )


if __name__ == "__main__":
    main()
