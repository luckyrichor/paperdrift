# paperdrift

定期替你搜壁纸（静图 / 动图 / 视频），你只管筛：喜欢或不要。它会根据你的筛选结果，越来越懂你的口味。

- **需求档案**：只有「描述/关键词」必填；目标终端（横屏/竖屏）、抓取频率可选，默认每天一次
- **来源**：Wallhaven、Danbooru（后续：Pixiv、Steam 创意工坊）
- **去重**：来源 ID + 感知哈希（同图不同尺寸也能识别），推过的永不再推
- **学习口味**：标签加权（喜欢 +1，不要 −1），每批保留约 20% 名额探索新方向
- **界面**：手机电脑通用的网页（← 不要 / → 喜欢，手机左右滑），同一套 API 以后给 apk 用
- **安全**：单用户密码登录、登录失败限速、全部接口需认证、密码和密钥只放 `.env`（不进仓库）

## 本地试跑

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # 改 PAPERDRIFT_PASSWORD；PAPERDRIFT_SECRET 用 python3 -c "import secrets;print(secrets.token_hex(32))" 生成
python -m app.serve           # 打开 http://127.0.0.1:8000
pytest                        # 跑测试（用假数据，不联网）
```

## 部署到 Ubuntu 服务器

```bash
git clone https://github.com/luckyrichor/paperdrift.git && cd paperdrift
sudo bash deploy/setup.sh     # 第一次：生成 /opt/paperdrift/.env 后退出
sudo nano /opt/paperdrift/.env   # 把 PAPERDRIFT_PASSWORD 改成你的强密码
sudo bash deploy/setup.sh     # 第二次：安装并启动 systemd 服务（默认 http://服务器IP:8000）
```

### 给公网 IP 加 HTTPS（不需要域名）

Let's Encrypt 已支持 IP 地址证书（有效期约 6 天，需自动续期；certbot ≥ 5.3）。前提：80、443 端口对外开放。

```bash
# 先用测试证书验证流程（浏览器不信任，但能确认 80 端口验证、自动续期都通）
sudo bash deploy/cert.sh <服务器公网IP> --staging
# 通了以后去掉 --staging 再跑一次，换成正式证书
sudo bash deploy/cert.sh <服务器公网IP>
```

脚本会：用独立 venv 装新版 certbot → 申请 IP 证书 → 配好每 12 小时一次的自动续期 → 续期后自动重启服务 → 把 `.env` 切到 443 + HTTPS。
证书续期失败的话，几天后网页和 apk 会报证书过期，可用 `systemctl list-timers certbot-renew.timer` 和 `journalctl -u certbot-renew` 检查。

## 给 apk / 其他客户端的接口

`POST /api/login {"password": "..."}` 返回 `{"token": "..."}`，之后所有请求带 `Authorization: Bearer <token>`（有效期 30 天）。

| 接口 | 说明 |
|---|---|
| `GET/POST /api/profiles`，`PATCH/DELETE /api/profiles/{id}` | 需求档案 |
| `POST /api/profiles/{id}/run` | 立即抓取一次 |
| `GET /api/items?profile_id=&status=new\|liked\|rejected&kind=&orientation=` | 条目列表 |
| `GET /api/items/{id}/preview` | 预览图（审核时用，体积小） |
| `POST /api/items/{id}/decision {"decision":"like\|reject\|undo","reason":"style\|character\|composition\|resolution\|other"}` | 筛选 |
| `GET /api/items/{id}/file` | 已喜欢条目的原图（点喜欢后服务器才下载原图，下载完成前返回 409，稍后重试） |

## 已知限制（v0.1）

- **抓取器还没在真实站点上验证过**——按官方 API 文档写的，开发环境连不上这些站。第一次在服务器上跑，如果「立即抓取」后状态栏显示错误，把它贴出来即可修。
- **关键词目前请用英文或站点标签**（如 `raiden shogun, genshin impact`）。中文描述 → 标签的转换（LLM）在计划中。
- Wallhaven 搜索结果不带标签，需要逐张查详情（API 限速 45 次/分钟），所以一次抓取要几分钟。
- Danbooru 免费账号一次最多 2 个标签，只取前两个关键词。
- 默认只要 SFW（Danbooru 的 general / sensitive 分级）；创建需求时可勾选允许 NSFW。
- Danbooru 的 ugoira（zip 动图）暂不支持。

## 路线图

1. CLIP 图像向量打分 + 参考图上传（冷启动、学画风/色调/构图）
2. Steam 创意工坊（元数据 → 视频类提取 mp4；场景类交给 Windows 小程序放进 WE 目录）
3. 安卓 apk（套网页 + 分享入口 + 自动换壁纸）
4. Windows 端小程序（主动拉取「已喜欢」列表、换桌面壁纸）
5. LLM：把描述转成各站标签
