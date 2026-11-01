# paperdrift

> 开发者 / agent：先读 [`docs/PROJECT.md`](docs/PROJECT.md)（目标、架构、进度、待办）和 [`AGENTS.md`](AGENTS.md)。

定期替你搜壁纸（静图 / 动图 / 视频），你只管筛：喜欢或不要。它会根据你的筛选结果，越来越懂你的口味。

- **需求档案**：只有「描述/关键词」必填；目标终端（横屏/竖屏）、抓取频率可选，默认每天一次
- **来源**：Wallhaven、Danbooru（后续：Pixiv、Steam 创意工坊）
- **去重**：来源 ID + 感知哈希（同图不同尺寸也能识别），推过的永不再推
- **学习口味**：标签加权（喜欢 +1，不要 −1）+ CLIP 图像向量（离喜欢的/参考图近加分，离不要的近减分），每批保留约 20% 名额探索新方向
- **参考图**：给需求上传几张你喜欢的图，冷启动就能按「感觉」找
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
**如果服务器的 80/443 已被别的服务占用**（比如已有 nginx）：不要运行 `cert.sh`。改为让 paperdrift 只监听本机（`.env` 里 `PAPERDRIFT_HOST=127.0.0.1`、`PAPERDRIFT_PORT=18080`、`PAPERDRIFT_COOKIE_SECURE=1`），由 nginx 终止 TLS 并反向代理。若挂在子路径下，注意前端用的是绝对路径 `/api/...`，需要 nginx 重写（详见 `docs/PROJECT.md` 2.1）。

证书续期失败的话，几天后网页和 apk 会报证书过期，可用 `systemctl list-timers certbot-renew.timer` 和 `journalctl -u certbot-renew` 检查。

## 给 apk / 其他客户端的接口

`POST /api/login {"password": "..."}` 返回 `{"token": "..."}`，之后所有请求带 `Authorization: Bearer <token>`（有效期 30 天）。

| 接口 | 说明 |
|---|---|
| `GET/POST /api/profiles`，`PATCH/DELETE /api/profiles/{id}` | 需求档案 |
| `POST /api/profiles/{id}/run` | 立即抓取一次 |
| `GET /api/items?profile_id=&status=new\|liked\|rejected&kind=&orientation=` | 条目列表 |
| `GET /api/items/{id}/preview` | 预览图（审核时用，体积小） |
| `POST /api/profiles/{id}/refs` | 上传参考图：请求体就是图片原始字节（`Content-Type: image/*`，≤10MB）；`GET` 同路径列出，`GET /api/refs/{id}/image` 看图，`DELETE /api/refs/{id}` 删除 |
| `POST /api/items/{id}/decision {"decision":"like\|reject\|undo","reason":"style\|character\|composition\|resolution\|other"}` | 筛选 |
| `GET /api/items/{id}/file` | 已喜欢条目的原图（点喜欢后服务器才下载原图，下载完成前返回 409，稍后重试） |

## 已知限制（v0.1）

- 抓取器已在生产机上实测：Wallhaven 能抓到并入队；Danbooru 的搜索与预览下载可用，但还没有 Danbooru 来源的条目走完整审核流程的验证。开发沙盒仍连不上这些站，所以相关改动只能由用户在服务器上验证。
- **CLIP 这一层没在真实模型上测过**：打分/排序逻辑用假向量测过，但真实模型（`Qdrant/clip-ViT-B-32-vision`，约 350MB，首次抓取时从 Hugging Face 下载，缓存在 `data/models`）是在你服务器上第一次加载。状态栏里 `CLIP ✓` 表示这次用上了口味分，`CLIP —` 表示没用上（没装依赖、模型没下载成功，或还没有任何喜欢/不要/参考图）。
- **关键词目前请用英文或站点标签**（如 `raiden shogun, genshin impact`）。中文描述 → 标签的转换（LLM）在计划中。
- Wallhaven 搜索结果不带标签，需要逐张查详情（API 限速 45 次/分钟），所以一次抓取要几分钟。
- Danbooru 免费账号一次最多 2 个标签，只取前两个关键词。
- 默认只要 SFW（Danbooru 的 general / sensitive 分级）；创建需求时可勾选允许 NSFW。
- Danbooru 的 ugoira（zip 动图）暂不支持。

## 路线图

1. ~~CLIP 图像向量打分 + 参考图上传~~（v0.2 已加）
2. Steam 创意工坊（元数据 → 视频类提取 mp4；场景类交给 Windows 小程序放进 WE 目录）
3. 安卓 apk（套网页 + 分享入口 + 自动换壁纸）
4. Windows 端小程序（主动拉取「已喜欢」列表、换桌面壁纸）
5. LLM：把描述转成各站标签
