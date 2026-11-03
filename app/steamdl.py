"""用 SteamCMD 下载 Wallpaper Engine 创意工坊条目。

需要一个拥有 Wallpaper Engine 的 Steam 账号，且已在服务器上手动登录过一次（密码/令牌验证码只在服务器终端里输入，
SteamCMD 之后靠缓存的登录凭据工作）。本项目的配置里只有「用户名」，没有密码。
"""
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

APPID = "431960"
log = logging.getLogger("paperdrift.steamdl")
_lock = threading.Lock()              # SteamCMD 同一时间只跑一个


class SteamDownloadError(Exception):
    """消息可直接显示给用户。"""


def friendly(output: str) -> str:
    o = output.lower()
    if "steam guard" in o or "two-factor" in o or "invalid password" in o or "login failure" in o or "password:" in o:
        return "Steam 登录失效或需要验证码：请在服务器上重新手动登录一次 SteamCMD"
    if "no subscription" in o or "access denied" in o or "not owned" in o:
        return "这个 Steam 账号没有 Wallpaper Engine（或没有权限下载这个条目）"
    if "timeout" in o:
        return "下载超时"
    return "SteamCMD 下载失败（详见服务日志）"


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def download_item(settings, fid: str, run=subprocess.run) -> Path:
    """下载到 data/steam/…，成功返回条目文件夹。"""
    if not settings.steam_user:
        raise SteamDownloadError("服务器没有配置 Steam 下载账号（.env 里的 STEAM_USER）")
    if not fid.isdigit():
        raise SteamDownloadError("无效的创意工坊 ID")
    base = settings.steam_dir
    home = base / "home"                                   # SteamCMD 的登录缓存放这里（服务用户的 HOME 受限）
    home.mkdir(parents=True, exist_ok=True)
    item = base / "steamapps" / "workshop" / "content" / APPID / fid
    cmd = [settings.steamcmd_path, "+force_install_dir", str(base), "+login", settings.steam_user,
           "+workshop_download_item", APPID, fid, "+quit"]
    with _lock:
        try:
            r = run(cmd, capture_output=True, text=True, timeout=3600, env={**os.environ, "HOME": str(home)}, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise SteamDownloadError("服务器上找不到 SteamCMD（检查 STEAMCMD_PATH）") from None
        except subprocess.TimeoutExpired:
            shutil.rmtree(item, ignore_errors=True)
            raise SteamDownloadError("下载超时（1 小时）") from None
    out = (r.stdout or "") + (r.stderr or "")
    if not item.exists() or not any(item.iterdir()):
        log.warning("steamcmd failed for %s: %s", fid, out[-600:])
        raise SteamDownloadError(friendly(out))
    if dir_size(item) > settings.steam_max_mb * 1024 * 1024:
        size = dir_size(item) // 1024 // 1024
        shutil.rmtree(item, ignore_errors=True)
        raise SteamDownloadError(f"文件太大（{size} MB，上限 {settings.steam_max_mb} MB）")
    return item


def pack(item_dir: Path, dest_zip: Path) -> Path:
    """把条目文件夹打成 zip（解压到 Wallpaper Engine 的 projects/myprojects 下即可使用）。"""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    base = shutil.make_archive(str(dest_zip.with_suffix("")), "zip", root_dir=item_dir.parent, base_dir=item_dir.name)
    return Path(base)
