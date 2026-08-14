# Moon Dream / HDHive OAuth 服务

这是一个可独立部署的个人影视资源中心。服务把影巢 OAuth、TMDB 发现、订阅、115 转存、ED2K/磁力离线下载、Telegram 通知与频道监控集中在同一个网页中；敏感凭据只保存在服务端数据库。

## 已实现功能

- 榜单：TMDB 今日/本周趋势、热门、高分、正在上映和即将上映。
- 遨游：按类型、地区、语言、年份、评分、风格和流媒体平台筛选。
- 资源详情：统一 Provider 接口、影巢资源聚合、筛选、去重和转存到 115。
- 订阅：当前/历史列表、搜索筛选、详情、手动执行、自动定时匹配及转存。
- 解锁记录：高密度表格、状态筛选、详情、重试和安全批量删除记录。
- 授权中心：影巢 OAuth、115 Cookie/扫码、Emby、TMDB。
- 业务设置：115 保存策略、订阅周期、ED2K/磁力云下载和任务轮询。
- Telegram：通知模板、事件开关、机器人命令和公开频道自动转存。
- 安全：Token/Cookie 加密保存；安装端 API 密钥鉴权；可选网页管理员登录。

## 页面

| 页面 | 地址 |
| --- | --- |
| 榜单 | `/rankings` |
| 遨游 | `/explore` |
| 资源详情 | `/resources` |
| 订阅列表 | `/subscriptions` |
| 订阅任务 | `/tasks` |
| 解锁记录 | `/unlocks` |
| 授权中心 | `/authorizations` |
| 业务设置 | `/settings` |
| Telegram | `/telegram` |

## 部署

```bash
mkdir -p /opt/hdhive-oauth/data
cd /opt/hdhive-oauth
curl -O https://raw.githubusercontent.com/xhui999w/my-mp-plugins-v2/main/services/hdhive-oauth/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/xhui999w/my-mp-plugins-v2/main/services/hdhive-oauth/.env.example
```

编辑 `.env`：

```dotenv
HDHIVE_CLIENT_ID=影巢应用Client_ID
HDHIVE_APP_SECRET=影巢应用完整Secret
HDHIVE_REDIRECT_URI=https://你的域名/oauth/callback
INSTALLATION_KEY=使用 openssl rand -hex 32 生成
TOKEN_ENCRYPTION_KEY=Fernet密钥
WEB_ADMIN_USER=网页管理员用户名
WEB_ADMIN_PASSWORD=独立的高强度网页密码
```

Fernet 密钥可这样生成：

```bash
docker run --rm ghcr.io/xhui999w/hdhive-oauth:latest \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

启动：

```bash
docker compose pull
docker compose up -d
docker compose logs -f
curl http://127.0.0.1:8765/health
```

建议仅通过带 HTTPS 的反向代理公开服务，不直接公开容器端口。

## 首次配置

1. 登录网页，打开“授权中心”。
2. 完成影巢 OAuth；填写 TMDB Token 并测试连接。
3. 使用 115 扫码或填写 Cookie，测试授权。
4. 在“业务设置”填写 115 目录 ID 与订阅/离线策略。
5. 如需通知，在 Telegram 页面配置 Bot Token、Chat ID 和事件模板。
6. 启用频道监控后，首次检查只建立消息游标，不处理历史消息，避免意外消耗积分。

## 安装端 API

MoviePilot 或其他可信客户端使用请求头：

```text
X-Installation-ID: 安装标识
X-Installation-Key: INSTALLATION_KEY
```

主要接口包括资源查询/解锁、订阅、设置、状态以及 `POST /v1/notifications/event`。通知事件支持 `transfer_success`、`transfer_failed`、`subscription`、`manual_review`。

## Telegram 机器人命令

- `/help`：帮助
- `/status`：服务状态
- `/search 影片名称`：搜索 TMDB
- `/subscribe movie|tv TMDB_ID 标题`：创建真实订阅

可配置授权用户 ID，其他用户的命令会被忽略。

## 安全与升级

- `.env`、`data/`、数据库和备份禁止提交到 GitHub。
- 不要在日志、截图或 Issue 中公开 Cookie、Token、Secret。
- 不要随意更换 `TOKEN_ENCRYPTION_KEY`，否则旧凭据无法解密。
- 更新镜像前备份 `data/`；数据库迁移在启动时自动执行。
- 删除订阅或记录默认不会删除 115 中的媒体文件。
- 网页管理员密码应与 NAS、服务器及影巢密码不同。

## 测试

```bash
python -m pytest tests -q
```

生产环境不会用 Mock 数据冒充真实结果。缺少外部 Token 时对应 Provider 会显示“未配置”，其他模块仍可正常运行。
