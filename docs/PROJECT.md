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

### 2.1 实际生产部署（2026-10-31，已验证）

用户的服务器上还跑着别的服务（GetAudio 占 80/443，GetVideo 占 8443），所以 paperdrift **不独占端口**，而是：

- 应用只监听 `127.0.0.1:18080`（`.env` 里设 `PAPERDRIFT_HOST`/`PAPERDRIFT_PORT`），由专用用户 `paperdrift` 的 systemd 服务运行；代码在 `/opt/paperdrift`。
- **nginx 终止 TLS，用子路径 `/paperdrift/` 反向代理**；证书复用服务器上已有的 Let's Encrypt IP 证书（由原有的续期定时器续期并 reload nginx）。`.env` 里 `PAPERDRIFT_COOKIE_SECURE=1`。
- 因为前端写死了绝对路径 `/api/...`，nginx **替换了返回 HTML 里的 `/api` 为 `/paperdrift/api`，并把 Cookie 的 Path 限定为 `/paperdrift/`**。⚠️ 所以：前端里所有接口 URL 必须保持以 `/api` 开头的普通字符串；**不要在没有同步改 nginx 配置的情况下改前端的路径处理**（比如改成相对路径/加 base path，会被重写成双重前缀而失效）。想根治见 8.7。
- 在这台机器上**不要运行 `deploy/cert.sh`**（会抢 80/443）；**慎用 `deploy/setup.sh`**（会重装 systemd unit 并动 apt，只有确认 unit 没被改过才用）。
- 生产机升级的最小步骤：
  ```bash
  sudo git -C /opt/paperdrift pull --ff-only
  sudo /opt/paperdrift/.venv/bin/pip install -r /opt/paperdrift/requirements.txt -r /opt/paperdrift/requirements-clip.txt
  sudo systemctl restart paperdrift        # 数据库会在启动时自动升级（补 embedding 列、建 refs 表）
  ```
- 具体主机名、nginx 文件路径、IP、凭据**不写进公开仓库**；它们在服务器本机的 `HANDOFF.local.md`（已在 `.gitignore`，`*.local.md`）。

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
4. **预取**（6 线程）：下载预览图 → dHash →（有 CLIP）算向量。
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
- 前端是单个 `index.html`、无构建步骤；改完用 Playwright 在 390px 宽度下点一遍（登录→建需求→审图→参考图）。

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
| CLIP 真实模型加载（fastembed，`Qdrant/clip-ViT-B-32-vision`） | 🟡 | 首次使用从 Hugging Face 下载约 350MB；生产机还没升级到 v0.2，尚未实测 |
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

## 7. 已知问题与限制

1. **只抓每个来源的前 2 页**（`search(..., pages=2)`）。推过的不再推，所以同一需求跑几轮后候选会被"已推过"耗尽，待审队列越来越空。**这是当前最大的功能缺口（8.2）。**
2. 关键词必须是英文或站点标签；中文描述搜不到东西（8.1）。
3. Danbooru 免费账号一次最多 2 个标签（只取前两个关键词）；有账号则 6 个。ugoira（zip 动图）不支持。
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
- 不要把密码、令牌、IP、主机名、nginx 路径、用户数据写进仓库或测试；服务器专属的交接信息放 `*.local.md`（已被 .gitignore 忽略）。
- 不要在生产机上直接运行 `deploy/cert.sh`；改动生产机前先读 2.1。
- 不要擅自改已确定的设计决策（第 2 节）；确需改动，先在本文记录理由。
- 完成一项后：更新第 6 节状态、勾掉第 8 节对应条目、提交并推送。
