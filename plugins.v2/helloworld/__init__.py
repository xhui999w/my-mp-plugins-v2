import re
import json
import time
import sqlite3
import os
from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from app.plugins import _PluginBase
from app.log import logger


class HelloWorld(_PluginBase):
    """
    115订阅转存助手
    自动监控TG频道中的115分享链接，转存到115网盘指定目录
    """
    plugin_name = "115订阅转存助手"
    plugin_desc = "自动监控TG频道115分享链接并转存到115网盘"
    plugin_icon = "https://raw.githubusercontent.com/mrtian2016/MoviePilot-Plugins/main/icons/default.png"
    plugin_version = "1.1.0"
    plugin_author = "xhui999w"
    author_url = "https://github.com/xhui999w"
    plugin_config_prefix = "helloworld_"
    plugin_order = 20
    auth_level = 1

    # 插件状态
    _enabled = False
    _onlyonce = False

    # 配置项
    _channels = ""         # TG频道列表（每行一个）
    _cookie_115 = ""       # 115 Cookie
    _save_dir = "0"        # 115保存目录ID（0=根目录）
    _check_interval = 10   # 检查间隔（分钟）
    _bot_token = ""        # TG Bot Token（可选）
    _admin_user_id = ""    # TG管理员ID（可选）

    # 内部
    _db_path = ""

    def init_plugin(self, config: dict = None):
        """插件初始化"""
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._channels = config.get("channels", "").strip()
            self._cookie_115 = config.get("cookie_115", "").strip()
            self._save_dir = str(config.get("save_dir", "0") or "0")
            interval = config.get("check_interval", 10)
            self._check_interval = int(interval) if str(interval).isdigit() else 10
            self._bot_token = config.get("bot_token", "").strip()
            self._admin_user_id = config.get("admin_user_id", "").strip()

        # 初始化数据库目录
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        db_dir = os.path.join(plugin_dir, "data")
        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "monitor.db")
        self._init_database()

        if self._enabled or self._onlyonce:
            logger.info("【115转存助手】服务已开启！")
            if not self._cookie_115:
                logger.warning("【115转存助手】未配置115 Cookie，转存功能无法生效！")

            if self._onlyonce:
                self.async_task(self.monitor_channels, delay=3)
                self._onlyonce = False
                self.update_config({"onlyonce": False})

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置表单"""
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'cols': 12,
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}},
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}},
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 12,
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'channels',
                                            'label': 'TG频道列表（每行一个频道名）',
                                            'placeholder': 'oneonefivewpfx\nanother_channel',
                                            'rows': 4,
                                            'hint': '填写频道用户名，不需要 t.me/ 前缀。一行一个'
                                        }
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 12,
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookie_115',
                                            'label': '115 Cookie（必填）',
                                            'placeholder': '粘贴完整的115 Cookie字符串',
                                            'rows': 3
                                        }
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 6,
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_dir',
                                            'label': '115保存目录ID',
                                            'placeholder': '0（根目录）',
                                            'hint': '登录115后从浏览器地址栏获取 cid 值'
                                        }
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 6,
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'check_interval',
                                            'label': '检查间隔（分钟）',
                                            'placeholder': '10',
                                            'type': 'number',
                                            'hint': '建议5-30分钟'
                                        }
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 12,
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'TG Bot配置是可选的，不配置不影响监控转存功能'
                                        }
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 6,
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'bot_token',
                                            'label': 'TG Bot Token（可选）',
                                            'placeholder': '123456789:ABCdefGHIjkl'
                                        }
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'cols': 6,
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'admin_user_id',
                                            'label': 'TG管理员用户ID（可选）',
                                            'placeholder': '123456789'
                                        }
                                    },
                                ]
                            },
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "channels": "oneonefivewpfx",
            "cookie_115": "",
            "save_dir": "0",
            "check_interval": 10,
            "bot_token": "",
            "admin_user_id": ""
        }

    def get_page(self) -> List[dict]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'cols': 12,
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '115订阅转存助手 v1.1.0 - 自动监控TG频道中的115分享链接并转存到指定目录。'
                                        }
                                    },
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "HelloWorld",
            "name": "115转存服务",
            "trigger": "interval",
            "func": self.monitor_channels,
            "kwargs": {"minutes": self._check_interval}
        }]

    def stop_service(self):
        pass

    # ===================== 数据库操作 =====================

    def _init_database(self):
        """初始化SQLite数据库，记录已处理的消息"""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT UNIQUE NOT NULL,
                channel TEXT,
                date TEXT,
                source_url TEXT,
                share_url TEXT,
                status TEXT DEFAULT 'pending',
                result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.commit()
            conn.close()
            logger.debug(f"【115转存助手】数据库已初始化: {self._db_path}")
        except Exception as e:
            logger.error(f"【115转存助手】数据库初始化失败: {e}")

    def _is_processed(self, msg_id: str) -> bool:
        """检查消息是否已处理过"""
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute("SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,))
            row = c.fetchone()
            conn.close()
            return row is not None
        except:
            return False

    def _save_record(self, msg_id: str, channel: str, date: str,
                     source_url: str, share_url: str,
                     status: str = "pending", result: str = ""):
        """保存消息处理记录"""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """INSERT OR REPLACE INTO messages
                (msg_id, channel, date, source_url, share_url, status, result)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, channel, date, source_url, share_url, status, result)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"【115转存助手】保存记录失败: {e}")

    # ===================== TG频道爬取 =====================

    def scrape_channel(self, channel: str) -> List[Dict]:
        """爬取 t.me/s/{channel} 页面，提取115分享链接"""
        url = f"https://t.me/s/{channel}"
        results = []

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/125.0.0.0 Safari/537.36"
            }
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            resp.encoding = "utf-8"

            soup = BeautifulSoup(resp.text, "html.parser")
            msg_divs = soup.select("div.tgme_widget_message")

            for div in msg_divs:
                # 消息唯一ID: 频道名_消息data-post
                data_post = div.get("data-post", "")
                if not data_post:
                    continue

                # 时间
                time_tag = div.select_one("time.datetime")
                msg_date = time_tag.get("datetime", "") if time_tag else ""

                # 消息链接
                date_link = div.select_one("a.tgme_widget_message_date")
                msg_url = date_link.get("href", "") if date_link else ""

                # 提取消息中的所有链接
                text_div = div.select_one("div.tgme_widget_message_text")
                if not text_div:
                    continue

                # 提取所有 <a> 标签的 href
                for a_tag in text_div.find_all("a"):
                    href = a_tag.get("href", "").strip()
                    if not href:
                        continue

                    # 匹配115分享链接格式
                    if re.search(
                        r"https?://(?:115|115cdn|anxia)\.com/s/\w+",
                        href
                    ):
                        msg_id = f"{channel}_{data_post}_{hash(href)}"
                        if not self._is_processed(msg_id):
                            results.append({
                                "msg_id": msg_id,
                                "channel": channel,
                                "date": msg_date,
                                "source_url": msg_url,
                                "share_url": href
                            })

            logger.info(
                f"【115转存助手】频道 @{channel} 扫描完成，新消息: {len(results)} 条"
            )

        except requests.RequestException as e:
            logger.error(f"【115转存助手】爬取 @{channel} 网络错误: {e}")
        except Exception as e:
            logger.error(f"【115转存助手】爬取 @{channel} 解析异常: {e}")

        return results

    # ===================== 115转存 =====================

    def _parse_share_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """解析115分享链接，返回 (share_code, receive_code)"""
        m = re.search(r"115\.com/s/(\w+)", url)
        if not m:
            return None, None
        share_code = m.group(1)

        # 提取 password 参数
        receive_code = ""
        q = parse_qs(urlparse(url).query)
        if "password" in q:
            receive_code = q["password"][0]
        elif "pwd" in q:
            receive_code = q["pwd"][0]

        return share_code, receive_code

    def transfer_115(self, share_url: str) -> Tuple[bool, str]:
        """转存115分享链接到用户网盘"""
        if not self._cookie_115:
            return False, "115 Cookie 未配置"

        share_code, receive_code = self._parse_share_url(share_url)
        if not share_code:
            return False, f"无法解析分享链接: {share_url}"

        headers = {
            "Cookie": self._cookie_115,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/plain, */*"
        }

        try:
            # Step 1: 获取分享信息
            snap_data = {
                "share_code": share_code,
                "receive_code": receive_code,
                "limit": 100,
                "offset": 0
            }
            snap_resp = requests.post(
                "https://webapi.115.com/share/snap",
                data=snap_data, headers=headers, timeout=30
            )
            snap_json = snap_resp.json()

            if not snap_json.get("state"):
                err = snap_json.get("error", "获取分享信息失败")
                return False, f"115分享无效或已过期: {err}"

            file_list = snap_json.get("data", {}).get("list", [])
            if not file_list:
                return False, "分享链接中没有文件"

            # Step 2: 获取用户ID
            user_resp = requests.get(
                "https://my.115.com/?ct=ajax&ac=get_user_aq",
                headers=headers, timeout=30
            )
            user_json = user_resp.json()
            uid = user_json.get("data", {}).get("uid", "")
            if not uid:
                return False, "获取用户ID失败，Cookie可能已过期"

            # Step 3: 执行转存
            file_ids = []
            for f in file_list:
                fid = f.get("fid") or f.get("cid")
                if fid:
                    file_ids.append(str(fid))

            if not file_ids:
                return False, "无法获取文件ID"

            time.sleep(2)  # 防限流
            recv_data = {
                "user_id": uid,
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": ",".join(file_ids),
                "cid": self._save_dir
            }
            recv_resp = requests.post(
                "https://webapi.115.com/share/receive",
                data=recv_data, headers=headers, timeout=30
            )
            recv_json = recv_resp.json()

            if recv_json.get("state"):
                return True, f"转存成功: {len(file_ids)} 个文件"
            elif "无需重复接收" in recv_json.get("error", ""):
                return True, "文件已存在，跳过"
            else:
                return False, f"转存失败: {recv_json.get('error', '未知错误')}"

        except requests.RequestException as e:
            return False, f"网络错误: {e}"
        except Exception as e:
            return False, f"转存异常: {e}"

    # ===================== 通知 =====================

    def send_notify(self, text: str):
        """通过TG Bot发送通知"""
        if not self._bot_token or not self._admin_user_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id": self._admin_user_id,
                "text": text,
                "parse_mode": "HTML"
            }, timeout=15)
        except Exception as e:
            logger.error(f"【115转存助手】通知发送失败: {e}")

    # ===================== 主逻辑 =====================

    def monitor_channels(self):
        """主监控函数：扫描所有频道，提取链接并转存"""
        if not self._cookie_115:
            logger.warning("【115转存助手】未配置115 Cookie，跳过监控")
            return

        channels_str = self._channels.strip()
        if not channels_str:
            logger.warning("【115转存助手】未配置TG频道，跳过监控")
            return

        channels = [c.strip() for c in channels_str.split("\n") if c.strip()]
        logger.info(f"【115转存助手】开始监控 {len(channels)} 个频道，间隔 {self._check_interval} 分钟")

        total_new = 0
        total_ok = 0

        for ch in channels:
            new_msgs = self.scrape_channel(ch)
            for msg in new_msgs:
                total_new += 1
                share_url = msg["share_url"]
                logger.info(f"【115转存助手】发现链接: {share_url}")

                ok, msg_text = self.transfer_115(share_url)
                status = "success" if ok else "failed"
                self._save_record(
                    msg_id=msg["msg_id"],
                    channel=msg["channel"],
                    date=msg["date"],
                    source_url=msg["source_url"],
                    share_url=share_url,
                    status=status,
                    result=msg_text
                )

                if ok:
                    total_ok += 1

                notify = f"{'✅' if ok else '❌'} 115转存{'成功' if ok else '失败'}\n🔗 {share_url}\n📝 {msg_text}"
                logger.info(f"【115转存助手】{notify}")
                self.send_notify(notify)

                time.sleep(3)  # 每条消息间隔，避免触发限流

        summary = f"【115转存助手】监控完成: 发现 {total_new} 条，成功转存 {total_ok} 条"
        logger.info(summary)
        if total_new > 0:
            self.send_notify(summary)
