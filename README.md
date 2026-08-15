<div align="center">

# Save Restricted Content Bot v3 · 中文优化版

Telegram 私域消息转发机器人 · 修复原版 v3 命令失效问题

</div>

> 本仓库 Fork 自 [devgaganin/Save-Restricted-Content-Bot-v3](https://github.com/devgaganin/Save-Restricted-Content-Bot-v3)，并在其基础上做了关键 Bug 修复与中文本地化。
> 所有 **合规使用** 仅限转发自己有权访问的内容；不得用于绕过他人设置的访问限制、抓取受版权保护内容等用途。

---

## 🔧 本 Fork 相对原版做了什么

原版 v3 存在一个**架构性缺陷**：机器人的命令处理器一部分注册在 Telethon 客户端上，但 `shared_client.py` 为了让 Pyrogram 独占接收消息，主动关闭了 Telethon 的 update loop。结果所有写在 Telethon 上的命令**永远收不到消息**，处于完全失效状态。

本 Fork 的核心修复如下：

| 修复项 | 原版问题 | 本 Fork 处理 |
|---|---|---|
| **命令失效（核心）** | `/status`、`/transfer`、`/rem`、`/add`、`/settings`（含全部设置按钮）、`/adl`、`/dl` 共 9 个处理器注册在被关闭 loop 的 Telethon 上，全部无反应 | 全部迁移到正在收消息的 Pyrogram 客户端，命令恢复正常响应 |
| **`/myplan` 幽灵命令** | help 文本宣传 `/myplan`，但代码库无任何实现 | 新增 `/myplan`，显示当前会员套餐与到期时间 |
| **`pay.py` 崩溃** | 支付成功回调引用了未导入的 `OWNER_ID`，且 `send_message` 缺 chat_id 参数，必然抛异常 | 整个支付流程重写为统一提示文案 |
| **菜单与实际不符** | `set_bot_commands` 注册了多个实际瘫痪的命令；help 列出 `/get` `/lock` `/session` 等不存在的命令 | 菜单与 help 全部对齐真实可用命令 |
| **缺少 `.gitignore`** | 原仓库无 `.gitignore`，`telethonbot.session`（含登录态）等敏感文件易误提交 | 新增 `.gitignore`，排除 session/缓存/媒体/数据库文件 |
| **`ytdl` cookie 传参错误** | 调用处传入字面量字符串 `"YT_COOKIES"`，而非已导入的 cookie 内容 | 已修正，传入实际 cookie 值 |
| **`ytdl` 大文件阈值错误** | 大文件判断写成 `2*1024*1024`（2 MiB），与宣称的 2 GB 不符 | 已修正为 2 GB |
| **`/single` 相册只下载一项** | 链接指向相册（media group）时只取链接中的单条，图片/视频/文字丢失 | 自动检测相册并拉取整组：优先服务端复制；受限内容下载后整组重传，**一比一保留分组、顺序、原缩略图与带 tag 的说明文字** |
| **`/single` 公开链接报 MEDIA_EMPTY** | 抓取来源记录（`emp`）以用户名作键、下游按数字 chat ID 查询，永远失配：相册扩展错用自定义 bot（CHANNEL_INVALID 退化为单条），再用 user 会话的 file_id 让 bot 直发（跨客户端引用无效，MEDIA_EMPTY），且失败后不回退 | `emp` 统一按数字 chat ID 记录，相册扩展正确选用抓取客户端；file_id 直发失败自动回退下载重传；下载固定使用实际抓到消息的客户端 |
| **相册大视频丢失（>2GB）** | 带说明文字的相册被强制走"下载后重传"：无 premium 会话时自定义 bot 上限 2000 MiB，4GB 视频整组与逐条发送均失败，频道里只剩图片；且自定义 bot 不在源频道时 `copy_media_group` 直接 `CHANNEL_INVALID` | 相册一律优先**服务端整组复制**（支持替换说明文字，不重新上传、不受 bot 上传上限约束）：先由自定义 bot 复制，失败自动改用实际抓取消息的登录会话重试；下载产物校验非空/大小一致（Pyrofork 超时可能落盘 0 字节文件），坏项跳过重试，不再拆散相册；上传中 mtime 心跳防误清 |
| **`/setbot` 令牌"已保存却仍提示提供"** | 保存路径与读取路径的清洗逻辑不一致，合法 token 被判空 | 统一令牌读取与校验，`/single` 正常识别已保存的自定义 bot |
| **pyrofork 相册解析崩溃（双发）** | `send_media_group` 在相册**已成功送达后**构造响应对象 `raw.types.messages.Messages(...)` 漏传本层必填的 `topics` 参数，抛 TypeError 被调用方误判为失败而重发，频道收到重复内容（pyrofork 2.3.69 全应用范围） | `shared_client` 导入期 monkeypatch 给 `topics` 兜底 `[]`，覆盖全部 `send_media_group` 调用点（含相册转发与 missav 投递） |
| **进度消息刷屏频道** | 下载/上传进度条直接发在目标频道 | 进度报告一律发到用户与主 Bot 的私聊，频道只保留最终相册/文件 |
| **`/login` 验证码被 Telegram 失效** | 直接发原始验证码会被 Telegram 立即作废，导致登录失败 | 支持混淆格式（`1 2 3 4 5`、`s12345`、`1-2-3-4-5`），自动提取数字；登录日志手机号脱敏 |
| **`/batch` 不支持跳选链接** | 只支持"起始链接 + 数量"连续范围，无法一次提交多个不连续链接 | `/batch` 第一步直接粘贴**多条链接（每行一条）**即可逐条下载，遵守套餐条数上限，支持 /stop 取消 |
| **相册健壮性** | 无音轨视频混入相册被 Telegram 整组拒绝（MEDIA_EMPTY）；限流（FLOOD_WAIT）直接跳过该链接 | 上传前自动检测视频音轨，**无音轨视频用 ffmpeg 重封装静音 AAC 轨（流拷贝不重编码）**，整组正常成相册；仍失败时逐条回退兜底（部分成功优于全灭）；捕获 FloodWait 按等待时长自动重试 |
| **`/merge` 多条合并** | 原版无此功能 | 多条链接合并为一条消息/相册发送；自定义文字替换原文；超过 10 项自动拆分时每组附带 `(1/N)` 进度标记 |
| **任务队列** | 批量/合并/单条同步阻塞，FloodWait 等待期间用户完全无法操作 bot | 每用户独立后台 worker 串行执行；任务入队立即返回；`/tasks` 查看进度；`/stop` 取消当前+排队任务；FloodWait 不再阻塞交互 |
| **速率控制** | 硬编码 `sleep(10)` 等间隔不可调 | 批量/计数任务间隔自适应（AIMD）：无 FloodWait 时从 `BATCH_MIN_INTERVAL`（默认 2s）起步，触发 FloodWait 按 3 倍退避（封顶 `BATCH_INTERVAL` 默认 10s）再逐步回落；设 `BATCH_MIN_INTERVAL=10` 可恢复旧固定间隔；合并/频道/上传间隔仍由 `MERGE_INTERVAL`、`CHANNEL_INTERVAL`、`UPLOAD_INTERVAL` 配置；`MAX_FLOOD_RETRIES` 可调 |

### 2026-08 重构（安全基线 / 磁盘自愈 / 依赖瘦身 / 内存有界化 / DB 优化 / 消息获取优化 / batch.py 拆分 / 吞吐提升）

| 维度 | 改动 |
|---|---|
| **加密加固** | 会话/token 加密改为每条记录随机 salt 的 AES-GCM（`b64(salt+nonce+tag+ct)`），旧格式自动兼容解密；用户自定义 bot token 由明文改为加密落库，读取时自动迁移；篡改/损坏的密文拒绝启动且不清库 |
| **磁盘自愈** | 临时文件生命周期：下载产物在任务结束（成功/失败/兜底）即删除，服务端复制路径全程不落盘；异常残留（进程被杀、容器崩溃）由清扫兜底——启动时清 `downloads/` 超 1 小时文件，容器内每小时清超 4 小时孤儿文件（含 `.temp` 半成品、相册缩略图），`tmp/` 超 24 小时，ffmpeg 截图超 7 天；上传中 mtime 心跳（5s 节流）防止大文件被误删；用户头像缩略图 `{uid}.jpg` 与 session 文件不参与清理 |
| **依赖瘦身** | 移除死代码 Telethon 栈与 OpenCV（视频元数据改 ffprobe 读取）；全部依赖锁定版本（Werkzeug 2.2.2→2.2.3 修复 CVE-2023-25577） |
| **内存有界化** | 后台 sweeper（60s 周期）统一治理全部进程内缓存：任务历史完成 10 分钟后清除且每用户上限 20 条；用户 bot/session client 闲置 30 分钟自动断开驱逐（再次使用时透明重建，进行中的任务不受干扰）；消息来源标记与 linked-chat 缓存改 LRU（上限 1000）；进度状态 1 小时超时清理；登录中间态/登录锁/设置对话态均带 TTL 自动过期 |
| **DB 优化** | 任务开始执行时对 `users` 集合一次 `find_one` 快照全部设置（caption/chat_id/替换词/删除词/重命名标签），随任务贯穿下载-处理-投递全链路，替代原每条消息 3-5 次查询（稳态每任务恰好 1 次查询）；快照只保留声明过的设置键（session 等敏感字段不入任务历史）；注意：任务开始执行后修改的设置对该任务不生效，排队中的任务按开始时快照生效；`users.user_id` 与 `premium_users.user_id` 唯一索引及会员过期 TTL 索引改为启动时一次性创建（存量重复数据导致唯一索引失败时仅告警不阻断启动） |
| **消息获取优化** | 私聊抓取新增 per-user peer 缓存（输入 chat key → 可访问的 chat_id 形式，TTL 24 小时、每用户上限 500 条）：缓存命中时跳过原每条消息一次的 `get_dialogs` 全量遍历直接取消息（同一私聊批量 10 条 `get_dialogs` 调用 ≤1 次）；缓存失效/过期自动降级原预热兜底链，行为与旧版完全一致；随 sweeper 在 client 驱逐时联动清理 |
| **代码结构拆分** | 原 2130 行上帝文件 `batch.py` 拆分为 `plugins/fetch.py`（client 缓存/消息获取/peer 缓存）、`plugins/tasks.py`（任务队列/后台 sweeper）、`plugins/deliver.py`（媒体下载与投递）+ 命令层 `batch.py`（297 行）；全局单字母变量改语义名（`UB→user_bots`、`UC→user_clients`、`emp→fetch_origin`、`P→progress_state`、`Z→pending_flows`、`E→parse_link` 等）；相册/合并的逐条发送降级逻辑合一、手写 FloodWait 重试统一收敛到 `with_flood_retry`、视频/音频扩展名列表统一收敛到 `utils.func`；纯移动零功能变更 |
| **吞吐提升** | 批量/计数任务改流水线执行：预取窗口=1（链接 j+1 的抓取+下载与链接 j 的重命名+上传重叠，投递顺序不变——prepare 阶段零内容发送，全部发送留在 finish 串行段）；固定 `sleep(10)` 改为 AIMD 自适应间隔（`RateLimiter`：下限 `BATCH_MIN_INTERVAL` 默认 2s，FloodWait 3 倍退避封顶 `BATCH_INTERVAL` 默认 10s，安静时逐步回落；`BATCH_MIN_INTERVAL=10` 恢复旧行为）；进度消息由百分比步进改为时间节流（≥`PROGRESS_MIN_INTERVAL` 默认 3s 编辑一次，100% 必发），减少 edit RPC；`process_msg` 拆分 prepare/finish 两阶段支撑流水线，临时文件全程 time_ns 唯一命名，取消/外部中断时预取产物（文件+进度消息）保证排空清理 |
| **稳定性修复** | 修复视频上传 width/height 实参历史互换（非方形视频曾以转置尺寸渲染）；修复非会员批量/合并条数上限检查的 `FREMIUM_LIMIT` 拼写错误（原触发 NameError 致流程中断）；premium/stats 全部会员到期日格式化路径补测试覆盖（`%Y` 曾被全局改名误伤）；批量任务结束与限流退避新增分析日志（wall/成功数/终期间隔，便于吞吐观测） |

> ⚠️ 安全提示：老版本部署过的 session 文件与 bot token 应视为已暴露，建议在 Telegram 内终止旧会话并重置 bot token；`IV_KEY` 现仅用于解密旧格式数据，仍需保留原值直至全部旧数据迁移完成。

### 支付/会员入口调整

本 Fork 将所有支付与会员开通入口（`/start`、`/pay`、`/plan`、`/myplan` 非会员分支）统一改为提示：

> 私密消息转发BOT（限私域使用），如需使用请联系管理员付费。

`/terms`（条款）页面的联系按钮和付费文案通过环境变量 `PAY_NOTICE`、`ADMIN_CONTACT` 配置（见 `config.py`），部署时在 `.env` 中填写自己的联系方式。

---

## ⚡ 可用命令

本 Fork 中**实际可用**的命令（全部已验证注册到 Pyrogram 客户端）：

### 🔑 账号与登录
| 命令 | 说明 |
|---|---|
| `/login` | 登录以访问受限内容 |
| `/logout` | 退出登录 |
| `/setbot` | 添加自定义处理机器人（用户自己的 Bot Token） |
| `/rembot` | 移除自定义机器人 |

### 📥 内容提取
| 命令 | 说明 |
|---|---|
| `/batch` | 批量提取帖子（登录后使用）：发**一个起始链接** → 按数量连续下载；或发**多条链接（每行一条）** → 逐条下载（相册同样整组转发） |
| `/single` | 单条提取（相册消息一比一转发，保留分组、原缩略图与说明文字） |
| `/merge` | 多条链接合并为一条消息/相册发送；支持自定义文字替换原文；超 10 项自动拆分并附 `(1/N)` 标记 |
| `/tasks` | 查看任务队列状态和进度 |
| `/cancel` | 取消进行中的登录/批量/设置流程 |
| `/stop` | 取消批量提取流程 |

### ⚙️ 个性化设置
| 命令 | 说明 |
|---|---|
| `/settings` | 设置重命名标签 / 标题 / 缩略图 / 会话 / 删除词语 / 替换词语等 |

### 💎 会员
| 命令 | 说明 |
|---|---|
| `/status` | 查看登录与会员状态 |
| `/myplan` | 查看您的会员套餐 |
| `/plan` | 查看会员方案（本 Fork 显示联系提示） |
| `/pay` | 开通 / 续费会员（本 Fork 显示联系提示） |
| `/transfer` | 将会员转赠他人（仅高级会员） |

### 🎬 媒体下载
| 命令 | 说明 |
|---|---|
| `/dl <链接>` | 下载视频（支持 YouTube、Instagram 等 yt-dlp 支持的站点，以及 missav.ai 视频页 —— 后者走内置 HLS 提取管线，见下） |
| `/adl <链接>` | 提取音频 |

<details>
<summary><b>missav.ai 下载说明（issue #13）</b></summary>

- 支持 `missav.ai / missav.ws / missav.live / missav123.com` 的视频页链接（含 `cn/en` 等语言前缀与 `dm\d+` 路由前缀），自动镜像轮询过 Cloudflare
- 流程：页面提取（Dean Edwards packed JS 解包）→ m3u8 → 分段并发下载（AES-128 自动解密）→ ffmpeg 封装 MP4（+faststart 流媒体优化）→ 以**一条相册消息**投递（封面 + 可直接播放的视频）
- 投递：与提取流程一致（`/settings` 投递频道 → `LOG_GROUP` → 私聊回退），频道优先用 `/setbot` 机器人发送；封面用页面 og:image；下载/上传进度只发私聊
- caption 五段式：番号 / 简介 / 演员# / 标签# / 类别#（中文字幕、无码等从 URL 徽章推导，缺失块自动省略）
- 转发自动排版：`/single` `/batch` `/merge` 的原文字若含结构化元素（番号、`演员：` `标签：` `类别：` 标签行、≥2 个 #hashtag），自动重排为同款五段式便于手动校准；纯文本与自定义说明（命令后缀 `oc`）不受影响
- >2GB 视频：ffmpeg **关键帧分段**（`-c copy` 无损 + 每段独立时间轴 + moov 前置），每段都是可直接播放、可拖进度的 Telegram 流媒体视频，整组仍在同一条相册内（1.8GB/段目标，最多 9 段）
- 内置资源防护：单任务 20k 段 / 20GB / 8 小时上限，私网与云元数据地址拒绝访问，跨用户最多同时 2 个 missav 任务
- 依赖：`curl-cffi`（Chrome TLS 指纹）、`m3u8`；`MISSAV_MIRRORS` / `MISSAV_SEGMENT_CONCURRENCY` / `MISSAV_MAX_JOBS` 可调

</details>


### ℹ️ 其他
| 命令 | 说明 |
|---|---|
| `/start` | 启动机器人（本 Fork 显示联系提示） |
| `/help` | 查看帮助（分页） |
| `/terms` | 条款和条件 |
| `/add <ID> <时长> <单位>` | 添加会员（仅管理员） |
| `/rem <ID>` | 移除会员（仅管理员） |
| `/set` | 设置机器人命令菜单（仅管理员） |

---

## 🔑 必需的环境变量

| 变量 | 说明 | 获取方式 |
|---|---|---|
| `API_ID` | Telegram API ID | [my.telegram.org](https://my.telegram.org/apps) |
| `API_HASH` | Telegram API Hash | 同上 |
| `BOT_TOKEN` | 机器人 Token | [@BotFather](https://t.me/botfather) |
| `OWNER_ID` | 管理员用户 ID（可多个，空格分隔） | [@userinfobot](https://t.me/userinfobot) |
| `MONGO_DB` | MongoDB 连接 URI | 自建 MongoDB，格式见部署指南 |
| `DB_NAME` | 数据库名，默认 `telegram_downloader` | — |
| `MASTER_KEY` | 会话加密密钥（32 字节十六进制） | 自行生成随机值，**勿用源码默认值** |
| `IV_KEY` | 解密密钥（16 字节十六进制） | 自行生成随机值，**勿用源码默认值** |

### 可选变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `STRING` | 空 | 高级账号 Pyrogram V2 会话字符串，启用后支持 4GB 上传 |
| `LOG_GROUP` | `-1001234456` | **默认投递频道**：提取的文件发到该频道，由 `/setbot` 的自定义 bot 发送（须将其加入频道并授予发帖权限）；未配置 `/setbot` 时回退私聊投递 |
| `FORCE_SUB` | `-10012345567` | 强制订阅频道 ID；填 `0` 不启用 |
| `FREEMIUM_LIMIT` | `0` | 免费用户提取上限，`0` 表示不允许 |
| `PREMIUM_LIMIT` | `500` | 高级用户批量上限 |
| `YT_COOKIES` | 空 | YouTube 下载用 Netscape cookie |
| `INSTA_COOKIES` | 空 | Instagram 下载用 cookie |
| `JOIN_LINK` | `t.me/team_spy_pro` | 加入链接 |
| `ADMIN_CONTACT` | — | 管理员联系方式 |
| `PLAN_*` | 见 `config.py` | 会员方案价格/时长配置 |
| `BATCH_MIN_INTERVAL` | `2` | 批量/计数任务自适应间隔下限/起始值（秒）；设为 `10` 恢复旧固定间隔行为 |
| `BATCH_INTERVAL` | `10` | 批量/计数任务自适应间隔上限（秒），FloodWait 退避封顶值 |
| `PROGRESS_MIN_INTERVAL` | `3` | 下载/上传进度消息编辑节流（秒），100% 时必发 |
| `MERGE_INTERVAL` | `5` | 合并提取每条链接间隔（秒） |
| `CHANNEL_INTERVAL` | `5` | 频道遍历间隔（秒） |
| `UPLOAD_INTERVAL` | `2` | 媒体上传间隔（秒） |
| `MAX_FLOOD_RETRIES` | `3` | FloodWait 最大重试次数 |
| `MISSAV_MIRRORS` | 内置列表 | missav 镜像域名，逗号分隔；留空用 `missav.ai/.ws/.live` + `missav123.com` |
| `MISSAV_SEGMENT_CONCURRENCY` | `8` | missav 分段下载并发数（1–32） |
| `MISSAV_MAX_JOBS` | `2` | 同时进行的 missav 任务数上限（跨用户） |

> ⚠️ **安全**：`config.py` 中 `MASTER_KEY`/`IV_KEY` 的默认值仅用于演示。生产部署务必通过环境变量覆盖为随机值，否则任何人都能解密你的用户会话。

---

## 🚀 快速部署

详见 [**部署指南**](DEPLOYMENT.md)。最简流程（Docker Compose）：

```bash
git clone https://github.com/paceyw/Save-Restricted-Bot-Chinese.git
cd Save-Restricted-Bot-Chinese
cp .env.example .env
# 编辑 .env 填入真实凭证
docker compose up -d --build
```

---

## 📁 项目结构

```
├── main.py              # 启动入口：加载共享客户端 + 动态加载 plugins/
├── shared_client.py     # Pyrogram（主 Bot + 可选用户账号）客户端
├── docker-compose.yml   # 一体化部署（mongo + mongo-init + bot）
├── docker/              # 容器入口与运行时清理脚本
├── Dockerfile           # 机器人镜像（python:3.10-slim + ffmpeg）
├── config.py            # 从环境变量读取配置；PAY_NOTICE 统一提示文案
├── app.py               # Flask 健康检查页（端口 5000）
├── plugins/
│   ├── start.py         # /start /help /plan /terms /set 菜单
│   ├── login.py         # 用户登录、会话保存、自定义 Bot 管理
│   ├── batch.py         # /batch /single /merge /cancel /tasks 命令层
│   ├── fetch.py         # 用户 client 缓存、消息获取、peer/linked-chat 缓存
│   ├── tasks.py         # 任务队列（TASKS/worker/dispatch）+ 后台状态 sweeper
│   ├── deliver.py       # 媒体下载、相册/合并投递、FloodWait 重试
│   ├── ytdl.py          # yt-dlp 音视频下载（Pyrogram 上传）
│   ├── settings.py      # 用户个性化设置（重命名/标题/缩略图/会话）
│   ├── premium.py       # /add 会员管理、/start 处理
│   ├── pay.py           # 付费入口（统一提示文案）
│   └── stats.py         # /status /myplan /transfer /rem
├── utils/
│   ├── func.py          # MongoDB 集合、文件处理、视频元数据
│   ├── encrypt.py       # 会话加密（AES-GCM）
│   ├── missav.py        # missav.ai HLS 下载管线（镜像轮询/packed JS/AES-128/remux，issue #13）
│   └── custom_filters.py# 登录流程过滤器
├── tests/               # pytest 回归测试（登录流程 / 设置路由 / 自定义 bot 流程 / 磁盘清理 / 加密 / 内存有界 / DB 快照与索引 / peer 缓存 / missav 下载与路由）
└── templates/welcome.html
```

### 架构说明

机器人运行时统一使用 Pyrogram：
- **Pyrogram**（`app`）：主 Bot 客户端，**唯一注册命令处理器并接收 Bot 消息**。
- **Pyrogram**（`userbot`）：配置 `STRING` 时启动的用户账号客户端，用于访问和转发受限内容。

`shared_client.py` 负责按顺序启动主 Bot 与可选用户账号；所有命令处理器都注册在 `app` 上，避免多个客户端争用主 Bot 的更新流。

---

## ⚖️ 免责声明

- 本机器人仅用于转发 **您自己有权访问** 的 Telegram 内容。
- 不对用户行为负责，不推广受版权保护的内容。
- 使用非官方客户端登录的账号可能受到 Telegram 的额外审查，请只使用合法授权的账号。
- 遵守 [Telegram API Terms of Service](https://core.telegram.org/api/terms)。

---

## 🙏 致谢

- 原作者：[devgagan / Team SPY](https://github.com/devgaganin)
- 本 Fork 仅做 Bug 修复与中文本地化，核心功能源自原项目。

<div align="center">

本 Fork 由 [paceyw](https://github.com/paceyw) 维护 · 基于 devgaganin 的原项目

</div>
