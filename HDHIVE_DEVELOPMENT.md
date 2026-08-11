# 影巢 OpenAPI 集成开发说明

## 完成内容

版本 `1.5.0` 已实现两条入口：

```text
Telegram公开频道
→ 识别 hdhive.com/resource/<slug>
→ HDHiveClient 调用官方OpenAPI
→ 获取详情并解锁115分享
→ 复用 Tg115Transfer.transfer_115()
→ 保存到影巢目标115目录
→ 保存去重记录和处理结果
```

```text
MoviePilot活跃订阅
→ 读取媒体类型、TMDB ID、季和缺集状态
→ 阿里云OAuth服务查询影巢OpenAPI
→ 筛选115资源、季和积分预算
→ 解锁并复用现有115转存
```

普通115分享、磁力/ED2K和MoviePilot订阅过滤逻辑继续保留。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `plugins.v2/tg115transfer/__init__.py` | MoviePilot入口、配置、TG解析、定时任务、数据库和转存分流 |
| `plugins.v2/tg115transfer/hdhive.py` | 独立的影巢链接校验和OpenAPI客户端 |
| `plugins.v2/tg115transfer/hdhive_gateway.py` | MoviePilot访问自建OAuth服务的客户端 |
| `services/hdhive-oauth/` | OAuth回调、Token加密、资源查询/解锁和Docker部署 |
| `.github/workflows/hdhive-oauth-image.yml` | 自动构建amd64/arm64 GHCR镜像 |
| `tests/test_hdhive.py` | 链接白名单、资源解析、Token刷新和错误处理测试 |
| `package.v2.json`、`package.json` | 插件市场版本和说明 |
| `README.md`、`README_EN.md` | 中英文用户说明 |

## OpenAPI实现

严格使用官方双层认证：

- `X-API-Key: <App Secret>`
- `Authorization: Bearer <Access Token>`

使用接口：

- `GET /api/open/ping`
- `GET /api/open/me`
- `GET /api/open/shares/:slug`
- `POST /api/open/resources/unlock`
- `POST /api/public/openapi/oauth/token`
- `POST /api/public/openapi/oauth/refresh`

授权范围需包含 `meta query unlock`。遇到
`OPENAPI_REFRESH_REQUIRED` 时，客户端会使用Refresh Token刷新一次，
并把新Token保存回MoviePilot配置。单条资源异常会写日志和失败记录，
不会终止整个TG监听。

## 数据库和去重

继续使用 `tg115transfer.db`，新增 `hdhive_records` 表，保存：

- Telegram消息ID
- 影巢链接
- 资源名称
- 115链接
- 状态（等待、成功、失败）
- 创建和更新时间

`telegram_message_id + hdhive_url` 是唯一键。处理前通过SQLite事务原子
占用任务，避免普通频道任务与影巢独立任务同时转存同一资源。

## 新增配置

- 启用影巢自动转存
- 影巢频道地址（默认 `https://t.me/oneonefivewpfx`）
- 影巢目标115目录ID
- 影巢检测间隔
- 影巢OpenAPI地址
- App Secret
- Access Token
- Refresh Token

密钥和Token不得写进源码、GitHub、Issue或日志截图。

## 部署步骤

OAuth服务完整操作见 `services/hdhive-oauth/README.md`。推荐部署在具有
固定公网IP的阿里云服务器，并将该IP填入影巢OpenAPI白名单。

1. 备份MoviePilot插件配置和 `tg115transfer.db`。
2. 在MoviePilot刷新插件市场。
3. 确认市场显示 `1.5.0`，再更新插件。
4. 打开配置，确认原有115 Cookie和频道仍在。
5. 填入影巢App Secret、Access Token和Refresh Token。
6. 确认OAuth授权范围包含 `meta query unlock`。
7. 影巢目标目录先使用专门的测试目录。
8. 开启影巢自动转存和“立即运行一次”，保存。
9. 在日志中搜索 `影巢` 或 `115转存助手`。

浏览器已登录影巢不等于OpenAPI已授权；插件仍需要当前应用对应的
App Secret和OAuth Token。

## 测试

```text
python -m unittest discover -s tests -v
python -m py_compile plugins.v2/tg115transfer/hdhive.py plugins.v2/tg115transfer/__init__.py
```

自动测试不使用真实Token或消耗积分。实际转存测试需要自己的OpenAPI凭据、
一条有效影巢资源和测试用115目录。

## 升级注意事项

- 不要删除数据库，否则可能重复转存旧资源。
- `skip` 模式会跳过失败记录；需要重试时临时选择 `reprocess`。
- `OPENAPI_REAUTH_REQUIRED` 表示Refresh Token失效，需要重新OAuth授权。
- `SCOPE_NOT_ALLOWED` 或 `USER_SCOPE_NOT_ALLOWED` 表示授权范围不足。
- `INSUFFICIENT_POINTS` 表示影巢积分不足，不是115转存错误。
