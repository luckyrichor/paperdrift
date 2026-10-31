#!/usr/bin/env bash
# 为服务器公网 IP 申请 Let's Encrypt 证书（6 天有效期，自动续期），并让 paperdrift 用上它。
# 用法：sudo bash deploy/cert.sh <公网IP> [--staging]
# 前提：80 端口对外开放且没被占用（验证和续期时临时用）；paperdrift 监听 443。
set -euo pipefail
IP="${1:?用法: cert.sh <公网IP> [--staging]}"; shift || true
STAGING="${1:-}"
APP_DIR=/opt/paperdrift
CB=/opt/certbot/bin/certbot

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }

# IP 证书需要 certbot >= 5.3（用 pip 装在独立 venv，避开 apt 里的旧版本）
if [ ! -x "$CB" ]; then
  apt-get install -y -qq python3-venv
  python3 -m venv /opt/certbot
  /opt/certbot/bin/pip install -q --upgrade pip certbot
fi
"$CB" --version

HOOK=/etc/letsencrypt/renewal-hooks/deploy/paperdrift.sh
mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<HOOKEOF
#!/usr/bin/env bash
# certbot 签发/续期成功后：复制证书给 paperdrift 用户并重启服务
set -e
install -o paperdrift -g paperdrift -m 640 /etc/letsencrypt/live/$IP/fullchain.pem $APP_DIR/certs/fullchain.pem
install -o paperdrift -g paperdrift -m 640 /etc/letsencrypt/live/$IP/privkey.pem   $APP_DIR/certs/privkey.pem
systemctl restart paperdrift
HOOKEOF
chmod 755 "$HOOK"

"$CB" certonly $STAGING --non-interactive --agree-tos --register-unsafely-without-email \
  --preferred-profile shortlived --standalone --ip-address "$IP" \
  --deploy-hook "$HOOK"

# 自动续期：每 12 小时检查一次（证书只有 6 天）
cat > /etc/systemd/system/certbot-renew.service <<UNIT
[Unit]
Description=Renew Let's Encrypt certificates (paperdrift)
[Service]
Type=oneshot
ExecStart=$CB renew --quiet
UNIT
cat > /etc/systemd/system/certbot-renew.timer <<'UNIT'
[Unit]
Description=Renew Let's Encrypt certificates twice a day
[Timer]
OnCalendar=*-*-* 00,12:17:00
RandomizedDelaySec=10min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now certbot-renew.timer

# 写入 .env（仅当尚未配置）
ENVF="$APP_DIR/.env"
sed -i "s|^PAPERDRIFT_PORT=.*|PAPERDRIFT_PORT=443|; \
        s|^PAPERDRIFT_SSL_CERT=.*|PAPERDRIFT_SSL_CERT=$APP_DIR/certs/fullchain.pem|; \
        s|^PAPERDRIFT_SSL_KEY=.*|PAPERDRIFT_SSL_KEY=$APP_DIR/certs/privkey.pem|; \
        s|^PAPERDRIFT_COOKIE_SECURE=.*|PAPERDRIFT_COOKIE_SECURE=1|" "$ENVF"
systemctl restart paperdrift
echo ">>> 完成。访问 https://$IP/"
[ -n "$STAGING" ] && echo ">>> 注意：这是测试证书（--staging），浏览器不信任；确认流程通畅后去掉 --staging 再跑一次。"
echo ">>> 检查续期定时器：systemctl list-timers certbot-renew.timer"
