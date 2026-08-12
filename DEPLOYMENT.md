# 部署指南

本文档介绍如何部署 **Save Restricted Content Bot v3 · 中文优化版**。推荐使用 Docker Compose，开箱即用且包含 MongoDB。

---

## 1. 前置准备

部署前请准备好以下凭证：

### 1.1 Telegram API 凭证

1. 浏览器打开 [my.telegram.org](https://my.telegram.org)，用手机号登录。
2. 进入 **API development tools** → [my.telegram.org/apps](https://my.telegram.org/apps)。
3. 创建应用，记录页面上显示的：
   - `api_id`（数字）
   - `api_hash`（字母数字字符串）

官方说明：<https://core.telegram.org/api/obtaining_api_id>

### 1.2 机器人 Token

1. 在 Telegram 打开有蓝色认证标记的 [@BotFather](https://t.me/botfather)。
2. 发送 `/newbot`，按提示填写名称和 username（须以 `bot` 结尾）。
3. BotFather 返回一串 Token，格式如 `1234567890:AAxxxx...`。

> Token 等同密码，切勿泄露或提交到 Git。若泄露立即在 BotFather 用 `/revoke` 重置。

### 1.3 你的用户 ID（OWNER_ID）

向 [@userinfobot](https://t.me/userinfobot) 发送任意消息，它会返回你的数字用户 ID。多个管理员用空格分隔。

### 1.4 日志群组与强制订阅频道（可选）

- `LOG_GROUP`：默认投递频道的 Chat ID（通常是 `-100...` 负数）。提取的文件会发到该频道；须把 `/setbot` 的自定义机器人加入该频道并授予发帖权限。
- `FORCE_SUB`：强制订阅频道的 Chat ID；不需要则填 `0`。

获取 Chat ID：把目标群组/频道转发一条消息给 @userinfobot 即可。

---

## 2. 方式一：Docker Compose 部署（推荐）

此方式自带 MongoDB，一条命令启动全部服务。

### 2.1 克隆与配置

```bash
git clone https://github.com/paceyw/Save-Restricted-Bot-Chinese.git
cd Save-Restricted-Bot-Chinese
cp .env.example .env
```

编辑 `.env`，至少填写以下项：

```dotenv
# 必填
API_ID=你的api_id
API_HASH=你的api_hash
BOT_TOKEN=你的BotFather_token
OWNER_ID=你的数字用户ID

# MongoDB（示例：使用本仓库自带的 compose 中的 mongo 服务）
MONGO_DB=mongodb://savebot_app:你的应用密码@mongo:27017/telegram_downloader?authSource=admin
DB_NAME=telegram_downloader

# 加密密钥——务必改成随机值，勿用源码默认值！
MASTER_KEY=用openssl生成的32字节十六进制
IV_KEY=用openssl生成的16字节十六进制

# 可选
LOG_GROUP=-100你的投递频道ID
FORCE_SUB=0
```

生成加密密钥：

```bash
openssl rand -hex 32   # → 填入 MASTER_KEY
openssl rand -hex 16   # → 填入 IV_KEY
```

### 2.2 启动

```bash
docker compose up -d --build
```

### 2.3 查看状态与日志

```bash
docker compose ps
docker compose logs -f --tail=200 bot
```

启动成功后，日志中应出现 `Pyro App Started...` 和 `SpyLib started (API-only, no update loop)...`。在 Telegram 私聊你的机器人发送 `/start`、`/status`、`/myplan` 验证响应。

### 2.4 停止与更新

```bash
# 停止（保留数据）
docker compose stop

# 安全停止并删除容器（保留数据库卷）
docker compose down

# 更新到最新版本
git pull
docker compose up -d --build
```

> ⚠️ **切勿** 使用 `docker compose down -v`，`-v` 会删除 MongoDB 数据卷，清空全部用户数据。

---

## 3. 方式二：VPS 直接部署

适合不用 Docker 的环境。

### 3.1 安装依赖

```bash
sudo apt update
sudo apt install -y ffmpeg git python3-pip python3-venv
```

### 3.2 克隆并安装

```bash
git clone https://github.com/paceyw/Save-Restricted-Bot-Chinese.git
cd Save-Restricted-Bot-Chinese
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3.3 配置环境变量

创建 `.env` 文件（参考 `.env.example`），或直接导出：

```bash
export API_ID=你的api_id
export API_HASH=你的api_hash
export BOT_TOKEN=你的BotFather_token
export OWNER_ID=你的用户ID
export MONGO_DB=mongodb://用户:密码@localhost:27017/telegram_downloader
export MASTER_KEY=$(openssl rand -hex 32)
export IV_KEY=$(openssl rand -hex 16)
```

> VPS 直接部署需要**自行准备 MongoDB**，可安装本地 MongoDB 或使用云数据库（如 MongoDB Atlas）。

### 3.4 后台运行（screen）

```bash
screen -S bot
python3 main.py
# 按 Ctrl+A 然后按 D 脱离
# 重新进入：screen -r bot
# 停止：screen -S bot -X quit
```

---

## 4. 环境变量完整参考

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `API_ID` | ✅ | — | Telegram API ID |
| `API_HASH` | ✅ | — | Telegram API Hash |
| `BOT_TOKEN` | ✅ | — | 机器人 Token |
| `OWNER_ID` | ✅ | — | 管理员 ID，多个用空格分隔 |
| `MONGO_DB` | ✅ | — | MongoDB 连接 URI |
| `DB_NAME` | — | `telegram_downloader` | 数据库名 |
| `MASTER_KEY` | ✅* | 演示值 | 会话加密密钥，**必须覆盖** |
| `IV_KEY` | ✅* | 演示值 | 解密密钥，**必须覆盖** |
| `STRING` | — | 空 | 高级账号会话字符串，启用 4GB 上传 |
| `LOG_GROUP` | — | `-1001234456` | 默认投递频道 ID（文件发到该频道，须将 `/setbot` 机器人加入并授发帖权限） |
| `FORCE_SUB` | — | `-10012345567` | 强制订阅频道 ID，`0` 不启用 |
| `FREEMIUM_LIMIT` | — | `0` | 免费用户提取上限 |
| `PREMIUM_LIMIT` | — | `500` | 高级用户批量上限 |
| `YT_COOKIES` | — | 空 | YouTube 下载 cookie（Netscape 格式） |
| `INSTA_COOKIES` | — | 空 | Instagram 下载 cookie |
| `JOIN_LINK` | — | `t.me/team_spy_pro` | 加入链接 |
| `ADMIN_CONTACT` | — | — | 管理员联系方式（`/terms` 按钮指向） |
| `PAY_NOTICE` | — | 见 config | 付费提示文案（`/start` `/pay` `/plan` 等）；不填用默认值 |
| `PLAN_D_*` / `PLAN_W_*` / `PLAN_M_*` | — | 见 config | 日/周/月方案配置 |

\* `MASTER_KEY`/`IV_KEY` 虽有默认值，但**生产环境必须覆盖为随机值**，否则会话加密形同虚设。

### Cookie 获取方法

下载 YouTube/Instagram cookie 需用浏览器扩展（如 Chrome 的 "Get cookies.txt LOCALLY"）导出 Netscape 格式 cookie 文件，将内容粘贴到对应环境变量。

---

## 5. 常见问题

### Q: 机器人启动了但命令无反应？

确认日志中 `Pyro App Started...` 已出现。本 Fork 已修复原版命令失效问题；若仍无反应，检查是否误装了原版代码。

### Q: `/status` 显示"未激活"？

`/status` 的"登录状态"显示用户会话是否已配置。使用 `/login` 登录后会变为活跃。

### Q: 如何修改付费提示文案？

编辑 `config.py` 中的 `PAY_NOTICE` 常量，或通过环境变量无法覆盖（它是硬编码常量）。如需改成环境变量读取，可自行修改 `config.py`。

### Q: MongoDB 连接失败？

- Docker Compose 部署：确认 `MONGO_DB` 中的用户名/密码与 `mongo-init` 创建的一致。
- VPS 部署：确认 MongoDB 服务已启动、认证配置正确、`MONGO_DB` URI 格式无误。

### Q: 如何备份数据？

```bash
# Docker 部署备份
docker compose exec -T mongo mongodump --archive --gzip \
  --username <root用户> --password <root密码> --authenticationDatabase admin \
  > backup_$(date +%Y%m%d).archive.gz
```

---

## 6. 部署后验证清单

启动后依次验证：

- [ ] 日志出现 `Pyro App Started...`
- [ ] 私聊机器人 `/start` 有响应（显示付费提示）
- [ ] `/status` 返回状态信息
- [ ] `/myplan` 返回套餐或提示
- [ ] `/help` 显示分页帮助
- [ ] `/settings` 弹出设置按钮面板
- [ ] （管理员）`/add <ID> <时长> <单位>` 可添加会员
