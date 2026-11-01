# paperdrift 项目方案与进度（给开发者 / 其他 agent）

> 最后更新：v0.2。改了功能或做完一项，请同步更新第 6 节（进度）和第 8 节（待办）。
> 读完本文你应该能：① 说清项目在做什么；② 知道做到哪了、哪些没验证；③ 直接接着开发。

## 1. 项目是什么

**用户的需求原话（概括）**：定期去搜静态图、动态图、视频（含 Steam 里 Wallpaper Engine 的创意工坊内容）当壁纸，用在电脑桌面、手机壁纸、某些软件的背景上。用户给一段描述（有时附参考图），系统找图；用户逐张筛选，不满意的不要；**下次找图要根据筛选结果变得更准**。

**核心闭环**：

```
需求档案 → 定时抓取(多来源) → 去重/过滤 → 打分排序 → 待审队列
                                                      ↓
        分发到各终端 ← 已喜欢库 ←──────────────── 用户筛选（喜欢/不要）
                                                      ↓
                                      反馈更新标签权重与口味向量 ──┘
```

**使用者**：单用户（项目主人自己用）。不是多用户产品，不要引入账号体系。

## 2. 用户环境与已定的决策

| 项 | 情况 |
|---|---|
| 服务器 | Ubuntu，**无 GPU**，可访问全球网站，**公网 IP 固定**，无域名，80/443 可对外开放，已装 Tailscale，Python 3.12.3 |
| 电脑 | Windows 笔记本（带 NVIDIA GPU，装有 Steam / Wallpaper Engine），另有 Mac |
| 手机 | **安卓** |
| 仓库 | 公开仓库 `luckyrichor/paperdrift`（**绝不能提交密码、令牌、服务器 IP**，都放 `.env`） |
| 内容范围 | 默认只要 SFW；需求里可勾选允许 NSFW |
| 语言 | 界面、文档、注释用中文 |

**已确定的设计决策**（改动前请先理解原因）：

1. **形态 = 服务器常驻服务 + 网页 + 以后的安卓 apk**。服务器 24 小时在线，抓取/学习都在服务器；网页和 apk 只是客户端，共用同一套 HTTP API。用户希望暴露公网。
2. **不用域名，用 Let's Encrypt 的 IP 地址证书**（6 天有效期，自动续期，certbot ≥ 5.3）。用户电脑/手机**无需安装任何证书**。（`deploy/cert.sh` 只适用于独占 80/443 的机器；实际生产机不是这种情况，见 2.1。）
3. **v1 不做 apk**，先做网页（手机浏览器可用）。apk 真正的价值只有两点：从别的 App「分享」图片当参考图、后台自动换壁纸。
4. **标签优先，CLIP 辅助**：动漫角色靠站点标签保证"是这个角色"，CLIP 只负责学"感觉"（画风/色调/构图）。CLIP 是**可选依赖**，没装就退回纯标签打分。
5. **服务器不开机依赖 Windows**：Windows 端（待做）应**主动去服务器拉任务**，不要让服务器 SSH 进 Windows（电脑会休眠/关机）。
6. **CLIP 放服务器 CPU 跑**（量小够用），不依赖 Windows GPU。
7. **Wallpaper Engine 内容分流**：视频类创意工坊作品本质是 mp4，可当普通视频入库、脱离 WE；场景类（.pkg）只有 WE 能播，需 Windows 端放进 WE 目录。下载创意工坊内容需要拥有 WE 的 Steam 账号（steamcmd）。
8. **审核时只传预览图**，用户点"喜欢"才下载原图/视频到服务器（有大小上限）。
9. 单用户**共享一个密码**；不引入多用户。

### 2.1 实际生产部署（2026-10-31，已现场验证）

> 本节用 `<服务器公网IP>` 占位：仓库是公开的，真实 IP、主机名、密码、令牌一律不入库。

**拓扑**：同一台服务器上三个项目共用 nginx 的 HTTPS 443 入口，按子路径分流：

| 路径 | 项目 | 后端（仅本机回环） |
|---|---|---|
| `https://<服务器公网IP>/paperdrift/` | 本项目 | `127.0.0.1:18080` |
| `/getaudio/` | GetAudio | `127.0.0.1:8800` |
| `/getvideo/` | GetVideo | `127.0.0.1:8900` |

GetVideo 原来的公网 8443 已停止监听；GetAudio/GetVideo 原先不带项目名前缀的入口已停用（旧路径返回 404）。80 端口由 nginx 提供证书验证。

**本项目**：
- 专用用户 `paperdrift` 运行 systemd 服务 `paperdrift.service`（active、enabled）；代码 `/opt/paperdrift`；数据 `/opt/paperdrift/data`（`paperdrift.db`、`previews`、`library`，v0.2 起还有 `refs`、`models`）。
- `.env`（root:paperdrift 640）里：`PAPERDRIFT_HOST=127.0.0.1`、`PAPERDRIFT_PORT=18080`、`PAPERDRIFT_COOKIE_SECURE=1`，密码/密钥为随机生成。
- TLS 在 nginx 终止，复用服务器上已有的正式 Let's Encrypt IP 证书，由原有的 `getaudio-cert-renew.timer` 续期并 reload nginx（定时器 active、最近一次成功；**还没等到下一次真实续期**）。
- nginx 的共用 443 server 里按项目 `include` 各自的 snippet（本项目是 `/etc/nginx/snippets/paperdrift.conf`）。

**⚠️ 前端与 nginx 的耦合**：前端写死了绝对路径 `/api/...`，所以 nginx 的 snippet **把返回 HTML 里的 `/api` 替换成 `/paperdrift/api`，并把 Cookie 的 Path 限定为 `/paperdrift/`**。因此：前端里所有接口 URL 必须保持以 `/api` 开头的普通字符串；**不要在没有同步改 nginx 的情况下改前端的路径处理**（改成相对路径或 base path 会被重写成双重前缀而失效），想根治见 8.7。前端有任何改动后，都要在正式入口重新验证。

**禁止事项（在这台机器上）**：
- 不要运行 `deploy/cert.sh`，也不要让 paperdrift 自己监听 80/443 或重启旧的 8443 站点——这些端口已被 nginx 和别的项目占用。
- 不要重新启用 `sites-available/paperdrift`（早期的 9443 试验站点，已禁用）。
- 不要用 `*.before-paperdrift-*` 这类 nginx 备份整文件回退：它早于 GetAudio/GetVideo 的路径迁移，会把现在的入口覆盖掉。
- 慎用 `deploy/setup.sh`：它会重装 systemd unit 并动 apt，只有确认 unit 没被改过才用。
- 不要修改全局网络配置、GetAudio/GetVideo 的业务配置或凭据；不要把 `data/`、`.env`、证书当作回退手段删除。

**请求体大小**：nginx 默认只放行 1MB 的请求体，应用自己的上限是 10MB。网页端上传参考图前会先在浏览器里压缩到 ≤900KB 来绕开这个限制（CLIP 只需要 224px）。以后的 apk 也要照做，或者在该 location 里加 `client_max_body_size 12m;`。

**升级（最小步骤）**：
```bash
sudo git -C /opt/paperdrift pull --ff-only
sudo /opt/paperdrift/.venv/bin/pip install -r /opt/paperdrift/requirements.txt -r /opt/paperdrift/requirements-clip.txt
sudo systemctl restart paperdrift      # 数据库在启动时自动升级（补 embedding 列、建 refs 表）
```
工作区里如有未跟踪的同名文件会让 `git pull` 失败，先处理掉再拉。

**运维与停用**：
```bash
sudo systemctl status paperdrift --no-pager
sudo journalctl -u paperdrift -n 80 --no-pager
sudo systemctl restart paperdrift
sudo nginx -t
```
停用且保留数据：`sudo systemctl disable --now paperdrift`，**只**删除共用 443 server 里的 `include /etc/nginx/snippets/paperdrift.conf;` 一行，再 `sudo nginx -t && sudo systemctl reload nginx`；GetAudio/GetVideo 的路径 snippet、证书和其他入口必须保留。

## 3. 架构

```
Ubuntu 服务器
 ├─ uvicorn（可直接 TLS，监听 443）
 │    ├─ FastAPI：/api/*（认证）、/（网页）
 │    └─ 后台协程 scheduler：每 60 秒检查到期的需求，线程池里跑抓取
 ├─ SQLite（WAL）  data/paperdrift.db
 ├─ data/previews/   预览图（审核用，服务器缓存，避免防盗链）
 ├─ data/library/    已喜欢条目的原图/视频
 ├─ data/refs/       用户上传的参考图
 └─ data/models/     CLIP 模型缓存
客户端：浏览器（电脑/手机）→ 已完成；安卓 apk → 待做；Windows 小程序 → 待做
```

### 目录结构

```
app/
  main.py          FastAPI 应用工厂 create_app(settings, embedder)；全部路由、认证依赖、原图下载
  config.py        Settings、.env 解析；密码/密钥不合格直接拒绝启动
  db.py            SQLite 封装 DB（每次操作新开连接）、建表与升级
  auth.py          HMAC 签名 token、LoginLimiter（按 IP：连续失败 5 次锁 15 分钟）
  pipeline.py      run_profile()：一次抓取的完整流程（见第 4 节）
  scoring.py       标签分
  taste.py         CLIP 口味分（z-score）
  embed.py         Embedder：fastembed 的 ONNX CLIP；失败后 10 分钟冷却
  phash.py         dHash 感知哈希与去重
  scheduler.py     is_due()、scheduler_loop()
  serve.py         启动入口 python -m app.serve（读 .env 决定 host/port/TLS）
  fetchers/        base.py(Candidate/Fetcher) wallhaven.py danbooru.py __init__.py(make_fetchers)
  static/index.html  整个前端（原生 JS，单文件，手机优先）
deploy/            setup.sh（安装/更新）、cert.sh（IP 证书+自动续期）、paperdrift.service
tests/             conftest.py（假抓取器/假向量器）、test_core.py
docs/PROJECT.md   本文
```

## 4. 核心逻辑（改动前必读）

### 一次抓取 `pipeline.run_profile`

1. **搜索**：`parse_terms(query)` 按逗号/换行切关键词 → 各 fetcher.search（单个来源异常只记录，不中断）。
2. **过滤**：丢掉 `(source, source_id)` 已在 items 里的（**无论喜欢/不要，推过的永不再推**）；按 `target`（横/竖屏）过滤。
3. **截断**：按来源热度先验 `prior` 取前 `batch_size*3` 张；缺标签的调 `fetcher.enrich()`（Wallhaven 搜索结果不带标签，需逐张查详情，限速 45 次/分钟）。
4. **预取**（6 线程）：下载预览图 → **规整成整张不裁剪的预览**（`normalize_preview`：站点缩略图的宽高比与原图相差 >6% 说明被裁过，改取原图；统一缩到最长边 1280 的 JPEG）→ dHash →（有 CLIP）算向量。Wallhaven 的 `thumbs.large` 就是被裁成 300x200 的，所以优先用 `thumbs.original`，仍被裁则回退原图。
5. **打分**：`score = Σ权重(tag)/sqrt(标签数+1) + 0.5*prior`，再加 `0.7 * 口味z分`（需有 喜欢/不要/参考图 之一，且所有候选都有向量）。
6. **选择**：80% 取最高分；20% 从剩余里随机"探索"（**没有任何反馈信号时 explore=0**）。
7. **去重入库**：dHash 汉明距离 ≤ 5 视为重复（对该需求已有的全部条目）；通过的写 `items`（status=new）。

### 学习

- 点喜欢：该条目每个标签权重 **+1**；点不要：**−1**。`undo`/改主意会先撤销旧决定的影响（`main.decide`）。
- 口味分：候选向量与 喜欢项+参考图 的 top-5 平均余弦相似度，减去与 不要项 的；在候选池内做 z-score（避免 CLIP 相似度整体偏高）。
- 不要的"原因"（style/character/composition/resolution/other）**目前只存储，不参与学习**（见待办）。
- 老条目没有向量时（CLIP 后装的），下次抓取会用预览文件**懒补算**并回写。

### 数据模型（SQLite）

- `profiles`：id, name, **query（必填，逗号分隔关键词）**, target(any/landscape/portrait), interval_hours(默认 24), batch_size(默认 30), nsfw, enabled, last_run, last_status
- `items`：profile_id, source, source_id（三者唯一）, kind(image/gif/video), page_url, preview_url, full_url, preview_path, local_path, width, height, orientation, tags(JSON), phash(hex), embedding(BLOB float32), score, status(new/liked/rejected), reason, created_at, decided_at
- `refs`：profile_id, path, embedding
- `tag_weights`：(profile_id, tag) → weight

### HTTP API（`Authorization: Bearer <token>` 或 cookie `pd_token`）

登录 `POST /api/login {"password"}` → `{"token"}`（30 天有效）。其余见 README 的接口表；要点：
`/api/profiles`（增删改查、`/run` 立即抓取）、`/api/items`（按 status/kind/orientation 过滤）、`/api/items/{id}/preview`、`/api/items/{id}/decision`、`/api/items/{id}/file`（仅已喜欢；原图未下载完返回 409）、`/api/profiles/{id}/refs`（请求体=图片原始字节）、`/api/me`（返回 `clip` 是否可用）。

### 安全约定

所有 `/api/*` 必须认证；密码只在 `.env`；`.env`、`data/` 在 `.gitignore`；上传限制类型与大小；`proxy_headers=False`（不信任 X-Forwarded-For）。新增接口必须加 `Depends(require_auth)`，并在 `tests/test_core.py` 补未登录 401 的断言。

## 5. 开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt          # 想本地试 CLIP 再装 requirements-clip.txt
cp .env.example .env                         # 改密码；SECRET 用 secrets.token_hex(32) 生成
python -m app.serve                          # http://127.0.0.1:8000
pytest                                       # 全程假数据，不联网
```

- 测试里用 `FakeFetcher` / `FakeEmbedder`（`tests/conftest.py`）替换真实网络和模型；`clip_client` 夹具演示"标签相同、只有颜色不同"来验证口味分。**新逻辑请沿用这种可注入、可离线的写法**。
- **开发沙盒通常连不上 Wallhaven / Danbooru / Hugging Face / Steam**，所以真实站点和真实模型只能由用户在服务器上验证。写了无法实测的代码，请在 README 的"已知限制"里如实标注。
- 提交规范：小步提交，信息说明"做了什么、为什么"。
- 前端是单个 `index.html`、无构建步骤；改完用 Playwright 在 390px 宽度下点一遍（登录→建需求→审图→参考图）。**输入框必须用 `keyboard.type()` 真实键入，不要只用 `fill()`**：`fill()` 绕过键盘事件，曾因此漏掉「onkeydown 返回 false 吞掉所有按键」导致无法输入密码的 bug（`tests/test_ui_login.py` 是回归测试）。

## 6. 进度（当前 v0.2）

图例：✅ 已完成且有测试　🟡 已写但**未在真实环境验证**　⬜ 未开始

| 模块 | 状态 | 说明 |
|---|---|---|
| 需求档案（描述必填，终端/频率可选） | ✅ | |
| 数据库、调度（每天一次、重启补跑） | ✅ | 进程内调度；同一需求不会并发运行 |
| 登录认证、登录限速、Bearer token | ✅ | |
| 网页审核界面（滑动/快捷键、原因、撤销、已喜欢/不要列表、计数） | ✅ | 手机尺寸已用无头浏览器验证 |
| 标签加权学习 + 20% 探索 | ✅ | |
| 去重（来源 ID + dHash） | ✅ | |
| 参考图上传 + CLIP 口味分（逻辑） | ✅ | 用假向量测试 |
| CLIP 真实模型加载（fastembed，`Qdrant/clip-ViT-B-32-vision`） | ✅（模型层）/ 🟡（整条流程） | 生产机上模型已下载，`Embedder.embed()` 对测试图输出 512 维向量。第一次预下载曾因 `~/.cache` 不可写失败，已修（`HF_HOME` 默认指到 `data/hf`）。**还没在网页里端到端验证**：上传参考图后抓取，状态栏应显示 `CLIP ✓` |
| Wallhaven 抓取器 | ✅ | 生产机实测：关键词 `raiden shogun, genshin impact` 抓到 211 个候选，batch_size=5 时约 46 秒入队 5 张 |
| Danbooru 抓取器 | ✅（抓取层）/ 🟡（入队） | 单独实测一页返回 81 个候选，预览图可下载解码；**但第一批入队的全来自 Wallhaven**，Danbooru 条目走完整审核流程还没有验证 |
| 调度到期自动运行 | ✅ | 把档案 last_run 改成 25 小时前，真实调度器自动跑了；**没有**等满 24 小时、**没有**测过机器重启 |
| 喜欢后下载原图 | ✅ | 生产机实测下载 4.7MB 原图成功；视频/动图没测 |
| 公网 IP 的 HTTPS（nginx 子路径 + 复用现有证书） | ✅ | 外部请求 200 且证书校验通过；未登录 401、Bearer、安全 Cookie 都测过；**还没等到一次真实续期** |
| `deploy/cert.sh`（独占 443 的部署方式） | 🟡 | **从未在真机运行**（生产机端口被占用，不适用）。同一台机器上用 certbot 5.8 的 staging+webroot 成功签发过测试证书，只说明 IP 证书申请本身可行，不等于脚本跑通 |
| systemd 部署（`deploy/setup.sh`） | ✅（v0.1，生产机两步都跑通） | v0.2 新增的 CLIP 依赖安装步骤未验证；**生产机目前还在 v0.1（a29e77b）**，需按 2.1 升级才有 CLIP/参考图 |
| 中文描述 → 站点标签（LLM） | ⬜ | 见 8.1 |
| 抓取翻页（避免越抓越没新图） | ⬜ | 见 8.2，**重要** |
| Steam 创意工坊 | ⬜ | 见 8.3 |
| Windows 小程序（拉取已喜欢、换桌面壁纸、放进 WE 目录） | ⬜ | 见 8.4 |
| 安卓 apk | ⬜ | 见 8.5 |
| Pixiv 等更多来源、软件背景分发 | ⬜ | 见 8.6 |

**生产机现状（2026-10-31）**：运行的是 **v0.1（提交 `a29e77b`）**，v0.2（CLIP、参考图）尚未部署。库里有一个测试档案「验收：雷电将军」（SFW、batch_size=5、每 24 小时、已启用），共 15 张待审（来自两次手动抓取和一次伪造到期触发的自动抓取，其间的测试反馈都已撤销）。

**仍未验证**：整天连续运行、机器重启后的恢复、下一次真实证书续期、手机触摸操作、动图/视频条目、Steam 创意工坊、用户对结果的审美验收、Danbooru 来源条目的完整审核流程、v0.2 的 CLIP 真实模型。

## 7. 已知问题与限制

1. **只抓每个来源的前 2 页**（`search(..., pages=2)`）。推过的不再推，所以同一需求跑几轮后候选会被"已推过"耗尽，待审队列越来越空。**这是当前最大的功能缺口（8.2）。**
2. 关键词必须是英文或站点标签；中文描述搜不到东西（8.1）。
3. Danbooru 免费账号一次最多 2 个标签（前两个词发给服务器，其余词在本地按标签过滤）；有账号则 6 个。SFW 只要 `rating=general`。ugoira（zip 动图）不支持。
3b. **v0.1/v0.2 早期入库的 Wallhaven 条目，预览图是被裁过的**（300x200）。新抓的不受影响；想修复就删除该需求再重建（测试档案可以这么做）。
3c. Wallhaven `thumbs.original` 是否确为未裁剪版本**没有实测**（沙盒连不上）；若不是，`normalize_preview` 会回退去下原图，只是更慢、更占流量。
4. Wallhaven 逐张查标签，一次抓取要几分钟。
5. 视频类条目审核时只能看一张预览静图，没有播放；`kind` 目前只有 image/gif/video。
6. token 无法单独吊销，只能换 `PAPERDRIFT_SECRET`（会让所有登录失效）。登录限速在内存里，重启清零。
7. 不要的"原因"未参与学习；角色类标签没有区分（Danbooru 有 character 分类，目前和普通标签一视同仁）。
8. 删除条目/清理旧预览图的机制没有，`data/previews` 会一直增长。
9. **登录限速在 nginx 后面是全局共用的**：应用 `proxy_headers=False`，看到的客户端 IP 永远是 `127.0.0.1`，所以任何人连错 5 次会把**包括主人在内的所有人**锁 15 分钟（单用户下是被他人拒绝服务的风险，而不是被暴力破解）。修法见 8.6 末条。
10. 一次抓取较慢（Wallhaven 逐张查标签等），实测 batch_size=5 约 46 秒；batch_size=30 会更久。

## 8. 待办（按建议顺序，每项含可直接开工的设计）

### 8.1 中文描述 → 站点标签（LLM）　优先级：高，工作量小

- 目标：用户写"原神 雷电将军 夜景 紫色"，系统自动得到 `{"wallhaven": ["raiden shogun","night"], "danbooru": ["raiden_shogun_(genshin_impact)"]}`。
- 做法：`profiles` 加列 `search_terms`（JSON，按来源）；创建/修改需求时若 `query` 含非 ASCII，调用 LLM（Anthropic API，密钥放 `.env` 的 `ANTHROPIC_API_KEY`，模型名可配置）生成；结果缓存，网页上展示并**允许用户修改确认**；`pipeline` 优先用 `search_terms[source]`，没有则回退原 `query` 的切分。
- 注意：LLM 可能编造不存在的标签 → 对 Danbooru 可先调 `/tags.json?search[name_matches]=`（或 autocomplete）校验；调用失败不能阻塞创建需求。测试里注入假 LLM。

### 8.2 抓取翻页与"新鲜度"　优先级：高

- 目标：多次运行不枯竭。
- 做法：每个 (profile, source) 记一个翻页游标（新表 `fetch_state`）；每次从游标继续往后翻若干页，翻到底后回到第 1 页并换排序（Wallhaven 的 `sorting` 可在 relevance/date_added/favorites/random 间轮换，`random` 要带 seed）；Danbooru 用 `page`/`b<id>` 方式往更旧的帖子翻。连续几轮"新增为 0"要在 `last_status` 里明确提示。
- 同时可加：清理策略（"不要"超过 N 天的预览图可删，只保留 phash/标签以便去重）。

### 8.2b Danbooru 搜索经验与「质量过滤」（来自用户提供的另一个 agent 的成功案例）

用户对这个 agent 的结果"挺满意"，其中有一张他非常喜欢（**请用户把它传为参考图**）。它的做法：

- 接口：`/posts.json`（搜图）、`/tags.json`（查标签规范写法）、`/posts/{id}.json`（单帖复核）；图片直链是返回里的 `file_url`（`cdn.donmai.us/original/...`）。
- 基础查询：`genshin_impact solo rating:general score:>N`，`limit=200`，翻 `page=1..6`。第一版 `score:>150` 只剩 76 条且横版仅 2 张，降到 `>60` 后原始约 1200 条、去重过滤后 966 条（横版 68 条）。**匿名时 `order` 排序无效（仍按 id 倒序），排序要在本地做。**
- 角色：老角色用 `raiden_shogun`；**新角色必须带作品后缀**，如 `lauma_(genshin_impact)`，裸名返回 0 条。用 `/tags.json?search[name_matches]=*genshin_impact*&search[category]=4&limit=1000` 能列出该作品的全部角色标签（category=4 是角色）。有些角色帖子本身就很少（如某角色 0/0、42 条里横版仅 1 张）。
- 复核：`/posts/{id}.json` 读 `tag_string` 发现个别帖子其实是 `1boy`，需排除。
- 本地筛选阈值：`rating == g`、角色标签数 == 1（单角色）、宽高比 ≥ 1.45、宽 ≥ 1600px，再按 score 排序取前几名；两批共下载 35 张，人工逐张看完留下 25 张。

**已吸收进代码**（`app/fetchers/danbooru.py`）：标签规范化解析（`/tags.json?search[name_matches]=词*`，只接受精确名或 `词_(…)`，取 post_count 最大者）、单页 200 条、服务器端 `rating:g`（名额不够时 422 自动退回本地过滤）、超出标签名额的词本地过滤、SFW=仅 general、热度先验取 `max(score, fav_count)`。

**还没做（建议作为需求档案的「可选高级项」，默认关闭，不要改变"只填描述就能用"的原则）**：最小宽度、最小宽高比、只要单角色（`solo`/角色标签数=1）、最小 score；把"角色标签自动补全/候选列表"做进网页（与 8.1 合并）；用 `/posts/{id}.json` 的性别标签（`1boy`/`1girl`）做复核。

### 8.3 Steam 创意工坊（Wallpaper Engine，appid 431960）

- 搜索：Steam Web API `IPublishedFileService/QueryFiles`（需要 Steam Web API Key，放 `.env`），按关键词/标签搜；`Candidate` 增加 `kind="scene"` 等类型和 `workshop_id`，预览图用作品预览。
- 类型：创意工坊作品有 Video / Scene / Web / Application 等类型（应通过标签区分；**需实测确认字段**）。视频类下载后目录里有 `project.json`（含 `file` 字段指向 mp4）和视频文件，可提取 mp4 当普通视频入库；场景类是 `.pkg`，只能给 WE 用；Application 类有风险，默认不收。
- 下载：steamcmd `workshop_download_item 431960 <id>`，需**拥有 WE 的 Steam 账号**登录（可能涉及 Steam Guard）。**待用户决定下载放哪**：服务器（需把 Steam 凭据放服务器）还是 Windows 端（凭据留在用户自己电脑）。推荐 Windows 端下载，服务器只存元数据。
- UI：审核卡片标明类型（"视频 · 可通用" / "场景 · 仅 WE"）；档案可加可选项"只要视频类"。
- 版权：作品属于原作者，仅供个人使用，不要提供转发/公开功能。

### 8.4 Windows 小程序

- 职责：**主动轮询服务器**（Bearer token）拉"已喜欢"列表 → 下载原图/视频到本地目录 → 设桌面壁纸（Win32 `SystemParametersInfo`）；场景类作品放进 WE 的 projects 目录并用 WE 命令行切换（具体参数**需查 WE 文档确认**）。
- 要求：开机自启、断网/休眠后补做、不开放任何入站端口、token 存本机。
- 服务器侧需要的支撑：`/api/items?status=liked` 加"自上次同步以来"的增量参数，或给条目加 `synced_at`。

### 8.5 安卓 apk

- 形态：薄壳——WebView 加载服务器网页（服务器地址可配置，用 Bearer token 或 cookie）+ 原生能力：
  1. **分享入口**：接收其他 App 的 `ACTION_SEND` 图片 → 选需求 → `POST /api/profiles/{id}/refs`。
  2. **保存**：点喜欢后把原图下载到 `Pictures/Wallpapers`。
  3. **自动换壁纸**：WorkManager 定时从"已喜欢"里取一张，用 `WallpaperManager` 设置。
- 服务器是 Let's Encrypt 证书，apk 无需特殊信任配置。

### 8.6 其他

- **登录限速取真实客户端 IP**：仅当直连对端是回环地址（即本机 nginx）时，才采用 nginx 设置的 `X-Forwarded-For` 最右一项作为限速键；其余情况仍用连接 IP。需要生产机 nginx 配置里加 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`（**先确认现有 snippet 是否已设**）。补一个测试。

- 更多来源：Pixiv（需登录/代理，注意反爬）、Gelbooru、Reddit；每个来源一个适配器，输出统一的 `Candidate`，并在 `make_fetchers` 注册。
- 软件背景分发：每个软件对应一个输出文件夹，系统把符合规则（如竖屏 + 某需求）的已喜欢图放进去。
- "不要"原因参与学习：`character` 原因 → 惩罚角色类标签更重；`resolution` → 记入需求的最低分辨率；`style` → 加大 CLIP 负样本权重。
- 视频预览：悬停/点击播放短预览。
- 多终端池：电脑横屏池、手机竖屏池分别维护"使用中"状态。

### 8.7 去掉对 nginx「重写 HTML」的依赖

- 现状见 2.1。目标：前端用相对于页面的 URL（或由服务端注入 base path，如 `PAPERDRIFT_BASE_PATH`），Cookie Path 由应用自己设置，从而可以删掉 nginx 里的 HTML 替换。
- **必须与 nginx 配置同一次变更上线**，否则会出现 `/paperdrift/paperdrift/api` 双前缀。上线前后各跑一遍：登录 → 建需求 → 审图 → 上传参考图 → 下载原图。

## 9. 给接手 agent 的工作约定

- 先读本文 + `README.md`，跑一遍 `pytest` 确认基线全绿再动手。
- 改行为就改测试；新增来源/模型层一律做成**可注入**，让测试不联网。
- 不确定的外部接口（Steam、WE 命令行、certbot 参数）**先查官方文档再写**，写完在 README"已知限制"和本文第 6 节标成 🟡。
- 不要把密码、令牌、真实 IP、主机名、用户数据写进仓库或测试（文档里用 `<服务器公网IP>` 这类占位）。
- 改动生产机前先读 2.1，尤其是禁止事项。
- 不要擅自改已确定的设计决策（第 2 节）；确需改动，先在本文记录理由。
- 完成一项后：更新第 6 节状态、勾掉第 8 节对应条目、提交并推送。

## 10. 近期行动清单（2026-10-31）

**用户（项目主人）**
1. 删除生产机上未跟踪的旧交接文件，升级到最新（见 2.1「升级」）。
2. 预下载 CLIP 模型并确认能出向量：
   ```bash
   cd /opt/paperdrift && sudo -u paperdrift env HF_HOME=/opt/paperdrift/data/hf .venv/bin/python -c "
   from fastembed import ImageEmbedding
   ImageEmbedding('Qdrant/clip-ViT-B-32-vision', cache_dir='/opt/paperdrift/data/models')
   print('下载完成')"
   # 必须带 HF_HOME：paperdrift 用户的家目录 /opt/paperdrift 不可写，Hugging Face 默认写 ~/.cache 会 Permission denied
   ```
3. 打开正式入口：审完那 15 张（这就是"审美验收"）；给需求上传 2–3 张参考图（顺便验证 nginx 重写对新接口有效）；点「立即抓取」，看状态栏是否 `CLIP ✓`；留意卡片上有没有 `danbooru` 来源的图。
4. 把结果反馈给开发者：升级/下载的报错输出、状态栏文字、结果质量、是否出现过 danbooru 来源。

**开发者 / agent（建议顺序）**
1. 8.2 抓取翻页——否则几轮之后待审队列会枯竭。
2. 8.1 中文描述转标签（需要用户提供 LLM 的 API Key 并确认愿意使用；见 8.1）。
3. 登录限速取真实 IP（8.6 首条；需先确认生产机 nginx snippet 是否已设置 `X-Forwarded-For`）。
4. 8.3 Steam 创意工坊（先让用户决定下载放服务器还是 Windows 端）→ 8.4 Windows 小程序 → 8.5 安卓 apk。
