import base64
import hashlib
import hmac
import time

TOKEN_TTL = 30 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60


def make_token(secret: str, ttl: int = TOKEN_TTL) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(secret.encode(), f"v1:{exp}".encode(), hashlib.sha256).digest()
    return f"{exp}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def check_token(secret: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    want = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"v1:{exp}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return hmac.compare_digest(sig, want)


class LoginLimiter:
    """按来源 IP 限制登录失败次数：连续失败 5 次，锁 15 分钟。"""

    def __init__(self):
        self.state: dict[str, tuple[int, float]] = {}

    def locked(self, ip: str) -> bool:
        fails, until = self.state.get(ip, (0, 0.0))
        return fails >= MAX_FAILS and time.time() < until

    def fail(self, ip: str) -> None:
        fails, until = self.state.get(ip, (0, 0.0))
        if fails >= MAX_FAILS and time.time() >= until:
            fails = 0
        self.state[ip] = (fails + 1, time.time() + LOCK_SECONDS)

    def ok(self, ip: str) -> None:
        self.state.pop(ip, None)
