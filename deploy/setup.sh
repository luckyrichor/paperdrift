#!/usr/bin/env bash
# 在 Ubuntu 服务器上以 root 运行：安装/更新 paperdrift 并注册为开机自启的 systemd 服务。
# 用法：sudo bash deploy/setup.sh
set -euo pipefail

APP_DIR=/opt/paperdrift
REPO_URL="${REPO_URL:-https://github.com/luckyrichor/paperdrift.git}"

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }

apt-get update -qq
apt-get install -y -qq python3 python3-venv git

id paperdrift &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin paperdrift

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
elif [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
  echo "$APP_DIR 已存在且不是 git 仓库，请先处理"; exit 1
else
  git clone "$REPO_URL" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
# CLIP（可选，让系统能学画风/色调、支持参考图）。装失败不影响基本功能。
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements-clip.txt" \
  || echo ">>> 警告：CLIP 依赖安装失败，将只使用标签打分（可稍后重跑本脚本）"

mkdir -p "$APP_DIR/data" "$APP_DIR/certs"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  SECRET=$("$APP_DIR/.venv/bin/python" -c "import secrets;print(secrets.token_hex(32))")
  sed -i "s|^PAPERDRIFT_SECRET=.*|PAPERDRIFT_SECRET=$SECRET|" "$APP_DIR/.env"
  echo ">>> 已生成 $APP_DIR/.env。请编辑它，至少把 PAPERDRIFT_PASSWORD 改成你自己的强密码，然后再运行本脚本一次。"
  chmod 600 "$APP_DIR/.env"
  chown -R paperdrift:paperdrift "$APP_DIR/data" "$APP_DIR/certs"
  chown root:paperdrift "$APP_DIR/.env"; chmod 640 "$APP_DIR/.env"
  exit 0
fi

chown -R paperdrift:paperdrift "$APP_DIR/data" "$APP_DIR/certs"
chown root:paperdrift "$APP_DIR/.env"; chmod 640 "$APP_DIR/.env"

install -m 644 "$APP_DIR/deploy/paperdrift.service" /etc/systemd/system/paperdrift.service
systemctl daemon-reload
systemctl enable paperdrift
systemctl restart paperdrift
sleep 2
systemctl --no-pager --full status paperdrift | head -15
echo ">>> 完成。查看日志：journalctl -u paperdrift -f"
