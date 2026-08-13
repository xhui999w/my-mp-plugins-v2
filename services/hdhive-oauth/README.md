# HDHive OAuth Gateway

## 遨游页面

- 页面：`/explore`
- Provider 状态：`/api/explore/providers`
- 筛选元数据：`/api/explore/filters`
- 发现：`/api/explore/discover`
- 榜单：`/api/explore/ranking/tmdb/trending-week`
- 详情：`/api/explore/media/movie/{tmdb_id}`

可以在网页设置页填写 TMDB API Key，也可以通过环境变量
`TMDB_API_KEY` 配置。没有配置时服务仍会正常启动，遨游页面会明确显示
“数据源尚未配置”，不会使用模拟数据冒充真实数据。

为 `Tg115Transfer` 提供影巢OAuth授权、Token加密保存、资源查询和解锁。
App Secret与用户Token只保存在服务器，不返回MoviePilot。

## 阿里云部署

1. 在影巢OpenAPI后台把固定IP白名单改为阿里云服务器公网IP。
2. 把域名（例如 `hdhive.120345.xyz`）通过Cloudflare Tunnel映射到：

   ```text
   http://127.0.0.1:8765
   ```

3. 在服务器创建目录并下载部署文件：

   ```bash
   mkdir -p /opt/hdhive-oauth/data
   cd /opt/hdhive-oauth
   curl -O https://raw.githubusercontent.com/xhui999w/my-mp-plugins-v2/main/services/hdhive-oauth/docker-compose.yml
   curl -o .env.example https://raw.githubusercontent.com/xhui999w/my-mp-plugins-v2/main/services/hdhive-oauth/.env.example
   cp .env.example .env
   ```

4. 生成安装密钥：

   ```bash
   openssl rand -hex 32
   ```

5. 生成Token加密密钥（镜像发布后执行）：

   ```bash
   docker run --rm ghcr.io/xhui999w/hdhive-oauth:latest \
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

6. 编辑 `.env`：

   - `HDHIVE_CLIENT_ID`：影巢应用Client ID。
   - `HDHIVE_APP_SECRET`：完整App Secret。
   - `HDHIVE_REDIRECT_URI`：`https://hdhive.120345.xyz/oauth/callback`。
   - `INSTALLATION_KEY`：第4步生成的值。
   - `TOKEN_ENCRYPTION_KEY`：第5步生成的值。

7. 启动：

   ```bash
   docker compose pull
   docker compose up -d
   docker compose logs -f
   ```

8. 检查：

   ```bash
   curl http://127.0.0.1:8765/health
   curl https://hdhive.120345.xyz/health
   ```

## MoviePilot配置

- 影巢OAuth服务地址：`https://hdhive.120345.xyz`
- 安装密钥：与服务端 `INSTALLATION_KEY` 相同
- 安装标识：插件自动生成
- App Secret、Access Token和Refresh Token留空

保存后进入插件详情页，点击“前往影巢OAuth授权”。

## 安全要求

- `.env`、数据库和备份不能上传GitHub。
- 不要开放数据库文件或Docker管理端口。
- 不要更换 `TOKEN_ENCRYPTION_KEY`，否则旧Token无法解密。
- 如果更换 `INSTALLATION_KEY`，MoviePilot配置也必须同步更新。
- 建议仅通过Cloudflare Tunnel公开服务，不直接开放8765端口。
