"""
115网盘转存助手 - Telegram频道监控插件
自动监控TG公开频道中的115分享链接，转存到115网盘指定目录
"""
import re
import json
import time
import hashlib
import sqlite3
import os
from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.plugin import PluginBase
from app.core.config import settings
from app.log import logger

# MP订阅系统集成
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType


class Tg115Transfer(PluginBase):
    """
    115网盘转存助手
    自动监控TG公开频道中的115分享链接，转存到115网盘指定目录
    """
    plugin_name = "115网盘转存助手"
    plugin_desc = "自动监控TG频道中的115分享链接并转存到指定目录"
    plugin_icon = "https://raw.githubusercontent.com/mrtian2016/MoviePilot-Plugins/main/icons/default.png"
    plugin_version = "1.2.1"
    plugin_author = "xhui999w"
    author_url = "https://github.com/xhui999w"
    plugin_config_prefix = "tg115transfer_"
    plugin_order = 20
    auth_level = 1

    # ==================== 插件状态 ====================
    _enabled = False
    _onlyonce = False

    # ==================== 配置项 ====================
    # Telegram
    _channels = ""          # 公开频道列表（每行一个）
    _bot_token = ""         # Bot Token（可选，用于通知）
    _admin_user_id = ""     # 管理员TG用户ID（可选）

    # 115网盘
    _cookie_115 = ""        # 115 Cookie
    _save_dir = "0"         # 115保存目录ID（0=根目录）

    # 策略
    _check_interval = 10    # 检查间隔（分钟）
    _dedup_mode = "skip"    # 重复处理方式: skip|reprocess
    _max_items = 50         # 单次最大处理数量
    _notify_enabled = True  # 启用通知

    # 订阅集成
    _subscribe_mode = "disabled"  # disabled | only_missing

    # ==================== 运行时 ====================
    _db_path = ""
    _scheduler = None

    # 支持的域名列表
    SUPPORTED_DOMAINS = [
        "115.com", "115cdn.com", "anxia.com"
    ]
    SHARE_PATTERN = re.compile(
        r"https?://(?:115|115cdn|anxia)\.com/s/\w+"
    )

    # ============================================================
    #  插件生命周期
    # ============================================================

    def init_plugin(self, config: dict = None):
        """初始化插件，读取配置"""
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._channels = config.get("channels", "").strip()
            self._cookie_115 = config.get("cookie_115", "").strip()
            self._save_dir = str(config.get("save_dir", "0") or "0")
            interval = config.get("check_interval", 10)
            self._check_interval = int(interval) if str(interval).isdigit() else 10
            self._dedup_mode = config.get("dedup_mode", "skip")
            max_items = config.get("max_items", 50)
            self._max_items = int(max_items) if str(max_items).isdigit() else 50
            self._notify_enabled = config.get("notify_enabled", True)
            self._subscribe_mode = config.get("subscribe_mode", "disabled")
            self._bot_token = config.get("bot_token", "").strip()
            self._admin_user_id = config.get("admin_user_id", "").strip()

        # 初始化数据库
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        db_dir = os.path.join(plugin_dir, "data")
        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "tg115transfer.db")
        self._init_database()

        if self._enabled or self._onlyonce:
            if not self._cookie_115:
                logger.warning("【115转存助手】未配置115 Cookie，转存功能无法生效")

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.monitor_channels,
                    trigger="date",
                    run_date=datetime.now() + timedelta(seconds=3)
                )
                if self._scheduler.get_jobs():
                    self._scheduler.start()
                self._onlyonce = False
                self.update_config({"onlyonce": False})

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册API端点，供前端调用"""
        return [
            {
                "path": "/test_cookie",
                "endpoint": self.api_test_cookie,
                "methods": ["POST"],
                "summary": "测试115Cookie",
                "description": "检查115 Cookie是否有效并返回账号脱敏信息"
            },
            {
                "path": "/test_bot",
                "endpoint": self.api_test_bot,
                "methods": ["POST"],
                "summary": "测试TG Bot",
                "description": "向管理员发送一条测试通知消息"
            },
            {
                "path": "/scan_now",
                "endpoint": self.api_scan_now,
                "methods": ["POST"],
                "summary": "立即扫描",
                "description": "立即触发一次频道扫描和转存"
            },
            {
                "path": "/stats",
                "endpoint": self.api_stats,
                "methods": ["GET"],
                "summary": "运行统计",
                "description": "获取最近扫描状态和今日统计"
            },
            {
                "path": "/check_dir",
                "endpoint": self.api_check_dir,
                "methods": ["POST"],
                "summary": "检查目录",
                "description": "检查115保存目录ID是否有效"
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置表单：按功能分区展示（修复：cols 写在 props 层级）"""
        return [
            {
                'component': 'VForm',
                'content': [
                    # ========== 区域一：运行设置 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            # 区域标题
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【运行设置】启用插件并配置基本运行参数'
                                        }
                                    }
                                ]
                            },
                            # 开关：启用 + 立即运行（各占一半）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {
                                        'model': 'enabled', 'label': '启用插件'
                                    }}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {
                                        'model': 'onlyonce', 'label': '立即运行一次'
                                    }}
                                ]
                            },
                            # 输入：检查间隔 + 最大处理数（各占一半）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'check_interval',
                                            'label': '检查间隔（分钟）',
                                            'type': 'number',
                                            'min': 1, 'max': 1440,
                                            'hint': '建议 5~30 分钟'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_items',
                                            'label': '单次最大处理数量',
                                            'type': 'number',
                                            'min': 1, 'max': 500,
                                            'hint': '每次扫描最多处理的新链接数'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 区域二：Telegram 来源 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【Telegram 来源】配置要监控的公开频道。公开频道无需Bot进频道，插件通过 t.me/s/ 页面抓取。'
                                        }
                                    }
                                ]
                            },
                            # 频道列表（占满一行）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'channels',
                                            'label': '公开频道列表（每行一个）',
                                            'placeholder': 'oneonefivewpfx\nanother_channel',
                                            'rows': 4,
                                            'hint': '填写频道用户名，不需要 t.me/ 前缀，一行一个'
                                        }
                                    }
                                ]
                            },
                            # 提示：Bot 为可选项
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'text',
                                            'text': '以下 Bot 配置为可选项，仅用于发送转存结果通知。不配置不影响监控与转存功能。'
                                        }
                                    }
                                ]
                            },
                            # Bot Token（占比 8/12）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'bot_token',
                                            'label': 'Bot Token（可选）',
                                            'placeholder': '1234567890:ABCdefGHIjkl...',
                                            'type': 'password',
                                            'hint': '用于发送通知消息'
                                        }
                                    }
                                ]
                            },
                            # 管理员ID（占比 4/12）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'admin_user_id',
                                            'label': '管理员用户ID（可选）',
                                            'placeholder': '123456789',
                                            'hint': '接收通知的TG账号数字ID'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 区域三：115 网盘 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '【115 网盘配置】Cookie保存到插件配置中，请勿泄露。保存后可在「插件详情」页测试有效性。'
                                        }
                                    }
                                ]
                            },
                            # Cookie（占满一行）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookie_115',
                                            'label': '115 Cookie（必填）',
                                            'placeholder': '粘贴完整的 115 Cookie 字符串',
                                            'rows': 2,
                                            'hint': '敏感信息，请勿截屏分享。可通过 API 测试有效性'
                                        }
                                    }
                                ]
                            },
                            # 保存目录 + 重复处理方式（各占一半）
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_dir',
                                            'label': '保存目录ID',
                                            'placeholder': '0（根目录）',
                                            'hint': '登录115网页版后从地址栏获取 cid 参数值'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'dedup_mode',
                                            'label': '重复处理方式',
                                            'placeholder': 'skip',
                                            'hint': 'skip=跳过已处理, reprocess=重新转存'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 区域四：订阅集成 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【订阅集成】集成MoviePilot订阅系统，只转存还缺的资源'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'subscribe_mode',
                                            'label': '订阅集成模式',
                                            'items': [
                                                {'title': '不启用', 'value': 'disabled'},
                                                {'title': '仅转存订阅缺失资源', 'value': 'only_missing'},
                                            ],
                                            'hint': 'disabled=处理所有TG链接；only_missing=只转存订阅中仍缺失的资源'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'text',
                                            'text': '启用后插件将读取MP订阅列表，通过标题匹配TG资源。仅当订阅资源存在缺失集数时才会触发转存。'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 区域五：通知 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【通知设置】控制插件运行时的通知行为'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {
                                        'model': 'notify_enabled',
                                        'label': '启用转存结果通知'
                                    }}
                                ]
                            },
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "channels": "oneonefivewpfx",
            "cookie_115": "",
            "save_dir": "0",
            "check_interval": 10,
            "dedup_mode": "skip",
            "max_items": 50,
            "notify_enabled": True,
            "subscribe_mode": "disabled",
            "bot_token": "",
            "admin_user_id": "",
        }

    def get_page(self) -> List[dict]:
        """插件详情页面：展示运行状态、统计和最近记录（修复：cols 写在 props 层级）"""
        stats = self._load_stats()
        records = self._get_recent_records(10)

        last_scan = stats.get("last_scan_time", "从未运行")
        next_scan = stats.get("next_scan_time", "未设置")

        # 统计摘要
        sub_mode_label = {"disabled": "未集成", "only_missing": "仅转存缺失"}
        sub_text = sub_mode_label.get(self._subscribe_mode, self._subscribe_mode)
        status_text = (
            f"当前状态: {'已启用' if self._enabled else '已暂停'}  |  "
            f"上次扫描: {last_scan}  |  "
            f"下次扫描: {next_scan}  |  "
            f"监控频道: {len([c for c in self._channels.split(chr(10)) if c.strip()]) if self._channels else 0} 个"
            f"  |  订阅模式: {sub_text}"
        )

        return [
            # 状态条
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info' if self._enabled else 'warning',
                                    'variant': 'tonal',
                                    'text': status_text
                                }
                            }
                        ]
                    }
                ]
            },
            # 今日统计（三列）
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 4},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info', 'variant': 'outlined',
                                    'text': f"今日发现: {stats.get('today_finds', 0)}"
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 4},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'success', 'variant': 'outlined',
                                    'text': f"转存成功: {stats.get('today_ok', 0)}"
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 4},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'error', 'variant': 'outlined',
                                    'text': f"转存失败: {stats.get('today_fail', 0)}"
                                }
                            }
                        ]
                    },
                ]
            },
            # 最近记录标题
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info', 'variant': 'tonal',
                                    'text': f"最近 {len(records)} 条转存记录"
                                }
                            }
                        ]
                    }
                ]
            },
        ] + [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'success' if r['status'] == 'success' else 'error',
                                        'variant': 'text',
                                        'text': f"[{r['channel']}] {r.get('subscribe_name', '') or '无订阅'} | {r['short_url']} → {r['result']}"
                                    }
                            }
                        ]
                    }
                ]
            }
            for r in records
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "Tg115Transfer",
            "name": "115转存监控服务",
            "trigger": "interval",
            "func": self.monitor_channels,
            "kwargs": {"minutes": self._check_interval}
        }]

    def stop_service(self):
        """服务停止时保存状态"""
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        self._save_stats({
            "next_scan_time": "",
            "status": "stopped"
        })

    # ============================================================
    #  数据库操作
    # ============================================================

    def _init_database(self):
        """初始化数据库：消息去重表 + 统计表"""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT UNIQUE NOT NULL,
                channel TEXT NOT NULL,
                date TEXT,
                source_url TEXT,
                share_url TEXT NOT NULL,
                subscribe_name TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS plugin_stats (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_messages_status
                ON messages(status)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_messages_created
                ON messages(created_at)""")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"【115转存助手】数据库初始化失败: {e}")

    def _gen_msg_id(self, data_post: str, share_url: str) -> str:
        """
        基于内容哈希生成稳定的去重ID（修复1）
        Python hash() 进程重启后变化，改用 hashlib.sha256
        """
        digest = hashlib.sha256(share_url.encode()).hexdigest()[:16]
        return f"{data_post}_{digest}"

    def _is_processed(self, msg_id: str) -> bool:
        """检查消息是否已处理过"""
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute("SELECT status FROM messages WHERE msg_id = ?", (msg_id,))
            row = c.fetchone()
            conn.close()
            if not row:
                return False
            if self._dedup_mode == "reprocess" and row[0] == "failed":
                return False  # 失败的可重试
            return True
        except Exception:
            return False

    def _save_record(self, msg_id: str, channel: str, date: str,
                     source_url: str, share_url: str,
                     status: str = "pending", result: str = "",
                     subscribe_name: str = ""):
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """INSERT OR REPLACE INTO messages
                (msg_id, channel, date, source_url, share_url, subscribe_name, status, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, channel, date, source_url, share_url, subscribe_name, status, result)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"【115转存助手】保存记录失败: {e}")

    def _get_recent_records(self, limit: int = 10) -> List[Dict]:
        """获取最近转存记录"""
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                """SELECT channel, share_url, subscribe_name, status, result, created_at
                   FROM messages ORDER BY id DESC LIMIT ?""",
                (limit,)
            )
            rows = [dict(r) for r in c.fetchall()]
            conn.close()
            for r in rows:
                url = r.get("share_url", "")
                r["short_url"] = url[:50] + "..." if len(url) > 50 else url
            return rows
        except Exception:
            return []

    def _get_today_stats(self) -> Dict:
        """获取今日统计"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            c.execute("""SELECT status, COUNT(*) FROM messages
                         WHERE date(created_at) = ? GROUP BY status""",
                      (today,))
            rows = c.fetchall()
            conn.close()
            stats = {"today_finds": 0, "today_ok": 0, "today_fail": 0}
            for status, count in rows:
                if status == "success":
                    stats["today_ok"] = count
                elif status == "failed":
                    stats["today_fail"] = count
                stats["today_finds"] += count
            return stats
        except Exception:
            return {"today_finds": 0, "today_ok": 0, "today_fail": 0}

    def _load_stats(self) -> Dict:
        """从数据库加载运行时统计"""
        stats = self._get_today_stats()
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()
            for key in ["last_scan_time", "next_scan_time", "status"]:
                c.execute("SELECT value FROM plugin_stats WHERE key = ?", (key,))
                row = c.fetchone()
                if row:
                    stats[key] = row[0]
            conn.close()
        except Exception:
            pass
        # 计算下次扫描时间
        if self._enabled and stats.get("last_scan_time"):
            try:
                last = datetime.strptime(stats["last_scan_time"], "%Y-%m-%d %H:%M:%S")
                nxt = last + timedelta(minutes=self._check_interval)
                stats["next_scan_time"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                stats["next_scan_time"] = "计算失败"
        defaults = {
            "today_finds": 0, "today_ok": 0, "today_fail": 0,
            "last_scan_time": "从未运行", "next_scan_time": "未设置", "status": "unknown"
        }
        defaults.update(stats)
        return defaults

    def _save_stats(self, updates: Dict):
        """保存运行时统计到数据库"""
        try:
            conn = sqlite3.connect(self._db_path)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for key, value in updates.items():
                conn.execute(
                    """INSERT OR REPLACE INTO plugin_stats (key, value, updated_at)
                       VALUES (?, ?, ?)""",
                    (key, str(value), now)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"【115转存助手】保存统计失败: {e}")

    # ============================================================
    #  TG频道爬取与链接提取
    # ============================================================

    def _normalize_share_url(self, url: str) -> str:
        """
        规范化115分享链接（修复2）
        将 115cdn.com / anxia.com 统一转为 115.com
        """
        url = re.sub(r"https?://115cdn\.com/", "https://115.com/", url)
        url = re.sub(r"https?://anxia\.com/", "https://115.com/", url)
        return url

    def _extract_links_from_element(self, element, message_text: str = "") -> List[Dict]:
        """
        从HTML元素中提取所有115分享链接（修复3）
        覆盖: <a href>、纯文本URL、按钮、转发消息
        """
        found = []

        # 1. 提取所有 <a> 标签 href
        for a_tag in element.find_all("a"):
            href = a_tag.get("href", "").strip()
            if href and self.SHARE_PATTERN.search(href):
                href = self._normalize_share_url(href)
                found.append({"url": href, "context": message_text})

        # 2. 提取纯文本中的裸URL
        raw_text = element.get_text(separator=" ", strip=True)
        for m in re.finditer(r"https?://(?:115|115cdn|anxia)\.com/s/\w+", raw_text):
            url = self._normalize_share_url(m.group(0))
            # 避免与 <a> 标签重复
            if not any(f["url"] == url for f in found):
                found.append({"url": url, "context": message_text})

        return found

    def scrape_channel(self, channel: str) -> List[Dict]:
        """
        爬取 t.me/s/{channel} 页面，全面提取115分享链接
        返回: [{msg_id, channel, date, source_url, share_url, message_text}, ...]
        """
        url = f"https://t.me/s/{channel}"
        results = []

        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            }
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            resp.encoding = "utf-8"

            soup = BeautifulSoup(resp.text, "html.parser")
            msg_divs = soup.select("div.tgme_widget_message")

            for div in msg_divs:
                data_post = div.get("data-post", "")
                if not data_post:
                    continue

                # 时间
                time_tag = div.select_one("time.datetime")
                msg_date = time_tag.get("datetime", "") if time_tag else ""

                # 消息链接
                date_link = div.select_one("a.tgme_widget_message_date")
                msg_url = date_link.get("href", "") if date_link else ""

                # 收集本条消息的全部文本（用于提取码关联）
                full_text = div.get_text(separator="\n", strip=True)

                # --- 提取链接: 多种来源 ---
                all_links = []

                # 3a. 主消息文本中的链接
                text_div = div.select_one("div.tgme_widget_message_text")
                if text_div:
                    all_links.extend(
                        self._extract_links_from_element(text_div, full_text)
                    )

                # 3b. caption 中的链接
                caption_div = div.select_one("div.tgme_widget_message_caption")
                if caption_div:
                    all_links.extend(
                        self._extract_links_from_element(caption_div, full_text)
                    )

                # 3c. 按钮中的链接
                for btn in div.select("a.tgme_widget_message_inline_button"):
                    btn_href = btn.get("href", "").strip()
                    if btn_href and self.SHARE_PATTERN.search(btn_href):
                        btn_href = self._normalize_share_url(btn_href)
                        all_links.append({"url": btn_href, "context": full_text})

                # 3d. 转发消息来源中的链接
                fwd_div = div.select_one("div.tgme_widget_message_forwarded_from")
                if fwd_div:
                    all_links.extend(
                        self._extract_links_from_element(fwd_div, full_text)
                    )

                # --- 去重URL后生成结果 ---
                seen_urls = set()
                for link in all_links:
                    share_url = link["url"]
                    if share_url in seen_urls:
                        continue
                    seen_urls.add(share_url)

                    msg_id = self._gen_msg_id(data_post, share_url)
                    if self._is_processed(msg_id):
                        continue

                    results.append({
                        "msg_id": msg_id,
                        "channel": channel,
                        "date": msg_date,
                        "source_url": msg_url,
                        "share_url": share_url,
                        "message_text": link.get("context", full_text),
                    })

            logger.info(
                f"【115转存助手】频道 @{channel} 扫描完成，新链接: {len(results)} 条"
            )

        except requests.RequestException as e:
            logger.error(f"【115转存助手】爬取 @{channel} 网络错误: {e}")
        except Exception as e:
            logger.error(f"【115转存助手】爬取 @{channel} 解析异常: {e}")

        return results

    def _extract_receive_code(self, share_url: str, message_text: str = "") -> str:
        """
        从URL参数和消息文本中关联提取提取码（修复4）
        优先级: URL参数 > 消息正文
        """
        # 1. URL参数
        q = parse_qs(urlparse(share_url).query)
        for param in ("password", "pwd", "code", "提取码"):
            if param in q and q[param][0]:
                return q[param][0]

        # 2. 消息正文关联提取
        if message_text:
            patterns = [
                r'提取码[：:]\s*(\w{4,})',
                r'密码[：:]\s*(\w{4,})',
                r'提取码\s+(\w{4,})',
                r'密码\s+(\w{4,})',
                r'pwd[：:]\s*(\w{4,})',
                r'password[：:]\s*(\w{4,})',
                r'[Pp][Ww][Dd]\s*[：:]\s*(\w{4,})',
            ]
            for pattern in patterns:
                m = re.search(pattern, message_text)
                if m:
                    code = m.group(1)
                    logger.info(f"【115转存助手】从消息文本提取到提取码: {code}")
                    return code

        return ""

    # ============================================================
    #  115 转存操作
    # ============================================================

    def _parse_share_code(self, share_url: str) -> Optional[str]:
        """从规范化后的URL提取 share_code"""
        m = re.search(r"115\.com/s/(\w+)", share_url)
        return m.group(1) if m else None

    def transfer_115(self, share_url: str, message_text: str = "") -> Tuple[bool, str]:
        """
        转存115分享链接到用户网盘（修复4+5）
        - 支持从消息文本提取提取码（修复4）
        - 支持分页获取文件列表（修复5）
        """
        if not self._cookie_115:
            return False, "115 Cookie 未配置"

        share_code = self._parse_share_code(share_url)
        if not share_code:
            return False, f"无法解析分享链接，已规范化为: {share_url}"

        # 获取提取码（URL参数 → 消息文本）
        receive_code = self._extract_receive_code(share_url, message_text)

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
            # ---- Step 1: 获取分享信息（支持分页，修复5） ----
            all_file_ids = []
            offset = 0
            limit = 100

            while True:
                snap_data = {
                    "share_code": share_code,
                    "receive_code": receive_code,
                    "limit": limit,
                    "offset": offset
                }
                snap_resp = requests.post(
                    "https://webapi.115.com/share/snap",
                    data=snap_data, headers=headers, timeout=30
                )
                snap_json = snap_resp.json()

                if snap_json.get("state") is not True:
                    err = snap_json.get("error") or snap_json.get("message", "获取分享信息失败")
                    # "无需重复接收"也算成功
                    if "无需重复接收" in str(err):
                        return True, "文件已存在，跳过"
                    return False, f"115分享信息获取失败: {err}"

                file_list = snap_json.get("data", {}).get("list", [])
                if not file_list:
                    break

                for f in file_list:
                    fid = f.get("fid") or f.get("cid") or f.get("file_id")
                    if fid:
                        all_file_ids.append(str(fid))

                # 判断是否还有下一页
                total = snap_json.get("data", {}).get("count", 0)
                offset += limit
                if offset >= total:
                    break
                time.sleep(1)  # 翻页间隔

            if not all_file_ids:
                return False, "分享链接中未找到可获取的文件"

            # ---- Step 2: 获取用户ID ----
            user_resp = requests.get(
                "https://my.115.com/?ct=ajax&ac=get_user_aq",
                headers=headers, timeout=30
            )
            user_json = user_resp.json()
            uid = user_json.get("data", {}).get("uid", "")
            if not uid:
                return False, "获取用户身份失败，Cookie 可能已过期"

            # ---- Step 3: 执行转存 ----
            time.sleep(2)
            recv_data = {
                "user_id": uid,
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": ",".join(all_file_ids),
                "cid": self._save_dir
            }
            recv_resp = requests.post(
                "https://webapi.115.com/share/receive",
                data=recv_data, headers=headers, timeout=60
            )
            recv_json = recv_resp.json()

            if recv_json.get("state"):
                return True, f"转存成功: {len(all_file_ids)} 个文件"
            elif "无需重复接收" in recv_json.get("error", ""):
                return True, "文件已存在，跳过"
            else:
                err_msg = recv_json.get("error") or recv_json.get("message", "未知错误")
                return False, f"转存失败: {err_msg}"

        except requests.RequestException as e:
            return False, f"网络错误: {e}"
        except Exception as e:
            return False, f"转存异常: {e}"

    def _test_115_cookie(self) -> Tuple[bool, str]:
        """测试115 Cookie是否有效，返回 (有效, 脱敏信息)"""
        if not self._cookie_115:
            return False, "Cookie 未配置"
        headers = {
            "Cookie": self._cookie_115,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            ),
        }
        try:
            resp = requests.get(
                "https://my.115.com/?ct=ajax&ac=get_user_aq",
                headers=headers, timeout=15
            )
            data = resp.json().get("data", {})
            uid = data.get("uid", "")
            nick = data.get("nickname") or data.get("user_name", "")
            if uid:
                # 脱敏
                masked_nick = nick[:2] + "*" * max(len(nick) - 2, 1) if nick else "未知用户"
                return True, f"有效 | 用户: {masked_nick} | UID: {uid[-4:]:>4s}"
            return False, f"无效: {resp.json().get('error', 'Cookie 已过期')}"
        except Exception as e:
            return False, f"检测异常: {e}"

    # ============================================================
    #  订阅集成（MP Subscribe System）
    # ============================================================

    def _load_subscribes(self) -> List:
        """
        加载MP中所有正在订阅中的订阅项
        返回: List[Subscribe] (SQLAlchemy模型)
        """
        try:
            subscribes = SubscribeOper().list(state="R")
            if subscribes:
                logger.info(
                    f"【115转存助手】加载到 {len(subscribes)} 个活跃订阅"
                )
                for sub in subscribes:
                    logger.info(
                        f"  └─ [{sub.type}] {sub.name} "
                        f"(季: {sub.season or 'N/A'}, "
                        f"缺集: {sub.lack_episode or 0})"
                    )
            else:
                logger.info("【115转存助手】未找到任何活跃订阅")
            return subscribes or []
        except Exception as e:
            logger.error(f"【115转存助手】加载订阅失败: {e}")
            return []

    def _match_subscribe(self, text: str,
                         subscribes: List) -> Optional[Any]:
        """
        通过标题/名称匹配订阅
        策略: 订阅名称包含在消息文本中（不区分大小写）

        返回匹配到的第一个订阅，或 None
        """
        if not text or not subscribes:
            return None

        text_lower = text.lower()

        # 优先按名称完整匹配
        for sub in subscribes:
            sub_name = sub.name.lower().strip()
            if sub_name and sub_name in text_lower:
                logger.info(
                    f"【115转存助手】订阅匹配成功: [{sub.name}] "
                    f"← 消息包含订阅名称"
                )
                return sub

        # 按关键词分解匹配（取中文/英文主要关键词）
        for sub in subscribes:
            sub_name = sub.name.strip()
            # 提取中文部分
            cn_parts = re.findall(r'[\u4e00-\u9fff]+', sub_name)
            for part in cn_parts:
                if len(part) >= 2 and part in text_lower:
                    logger.info(
                        f"【115转存助手】订阅模糊匹配成功: [{sub.name}] "
                        f"← 关键词 [{part}]"
                    )
                    return sub

        return None

    def _filter_by_subscription(self,
                                shares: List[Dict]) -> List[Dict]:
        """
        根据MP订阅过滤需要转存的资源

        在 only_missing 模式下:
        - 匹配到订阅 → 检查是否有缺失集数 → 有则转存
        - 未匹配到订阅 → 跳过

        返回过滤后的 shares 列表（新增 subscribe_name 字段）
        """
        if self._subscribe_mode == "disabled":
            return shares  # 不过滤，原样返回

        subscribes = self._load_subscribes()
        if not subscribes:
            if self._subscribe_mode == "only_missing":
                logger.info(
                    "【115转存助手】only_missing 模式下无活跃订阅，跳过本轮"
                )
                return []
            return shares

        filtered = []
        skipped_no_match = 0
        skipped_complete = 0

        for share in shares:
            msg_text = share.get("message_text", "")
            share_url = share.get("share_url", "")

            matched = self._match_subscribe(msg_text, subscribes)

            if matched:
                # 匹配到订阅 → 检查是否需要
                lack = matched.lack_episode or 0
                if lack > 0 or matched.type == MediaType.MOVIE.value:
                    # 有缺失或电影（电影只要在订阅中就算需要）
                    share["subscribe_name"] = matched.name
                    filtered.append(share)
                    logger.info(
                        f"【115转存助手】✅ 订阅 [{matched.name}] "
                        f"缺 {lack} 集，加入转存队列"
                    )
                else:
                    skipped_complete += 1
                    logger.info(
                        f"【115转存助手】⏭️ 订阅 [{matched.name}] "
                        f"已齐全，跳过转存"
                    )
            elif self._subscribe_mode == "only_missing":
                skipped_no_match += 1
                logger.debug(
                    f"【115转存助手】⏭️ 未匹配到订阅，跳过: "
                    f"{share_url[:60]}"
                )
            else:
                # disabled模式不会走到这里
                filtered.append(share)

        if skipped_no_match or skipped_complete:
            logger.info(
                f"【115转存助手】订阅过滤结果: "
                f"待转存 {len(filtered)} 条 | "
                f"未匹配跳过 {skipped_no_match} 条 | "
                f"已齐全跳过 {skipped_complete} 条"
            )

        return filtered

    # ============================================================
    #  通知
    # ============================================================

    def send_notify(self, text: str):
        """通过TG Bot发送通知"""
        if not self._notify_enabled or not self._bot_token or not self._admin_user_id:
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

    # ============================================================
    #  主监控逻辑
    # ============================================================

    def monitor_channels(self):
        """主循环：扫描所有频道，提取链接并转存"""
        if not self._cookie_115:
            logger.warning("【115转存助手】未配置115 Cookie，跳过监控")
            return

        channels_str = self._channels.strip()
        if not channels_str:
            logger.warning("【115转存助手】未配置TG频道，跳过监控")
            return

        channels = [c.strip() for c in channels_str.split("\n") if c.strip()]
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        self._save_stats({"last_scan_time": now_str, "status": "running"})

        logger.info(
            f"【115转存助手】开始监控 {len(channels)} 个频道, "
            f"间隔 {self._check_interval} 分钟"
        )

        # ---- Step 1: 扫描所有频道，收集链接 ----
        all_msgs = []
        for ch in channels:
            new_msgs = self.scrape_channel(ch)
            all_msgs.extend(new_msgs)

        # ---- Step 2: 按订阅策略过滤（如有配置） ----
        if self._subscribe_mode != "disabled":
            logger.info(
                f"【115转存助手】订阅集成模式: {self._subscribe_mode}, "
                f"开始过滤 {len(all_msgs)} 条发现"
            )
            all_msgs = self._filter_by_subscription(all_msgs)

        # ---- Step 3: 处理过滤后的链接 ----
        total_new = 0
        total_ok = 0
        total_fail = 0
        processed = 0

        for msg in all_msgs:
            if processed >= self._max_items:
                logger.info(
                    f"【115转存助手】已达单次最大处理数 "
                    f"({self._max_items})，停止本轮扫描"
                )
                break

            processed += 1
            total_new += 1
            share_url = msg["share_url"]
            message_text = msg.get("message_text", "")
            subscribe_name = msg.get("subscribe_name", "")

            logger.info(
                f"【115转存助手】[{processed}/{self._max_items}] "
                f"发现: {share_url}"
            )

            ok, msg_text = self.transfer_115(share_url, message_text)
            status = "success" if ok else "failed"

            self._save_record(
                msg_id=msg["msg_id"],
                channel=msg["channel"],
                date=msg["date"],
                source_url=msg["source_url"],
                share_url=share_url,
                subscribe_name=subscribe_name,
                status=status,
                result=msg_text
            )

            if ok:
                total_ok += 1
            else:
                total_fail += 1

            notify_text = (
                f"{'✅' if ok else '❌'} 115转存{'成功' if ok else '失败'}\n"
                f"📎 {share_url}\n"
                f"📝 {msg_text}"
            )
            logger.info(f"【115转存助手】{notify_text}")
            self.send_notify(notify_text)

            time.sleep(3)  # 防限流

        # 保存统计
        self._save_stats({"status": "idle", "next_scan_time": now_str})

        summary = (
            f"【115转存助手】本轮监控完成 | "
            f"发现 {total_new} 条 | "
            f"成功 {total_ok} 条 | "
            f"失败 {total_fail} 条"
        )
        logger.info(summary)
        if total_new > 0:
            self.send_notify(summary)

    # ============================================================
    #  API 处理函数
    # ============================================================

    def api_test_cookie(self, **kwargs) -> Dict:
        """检测115 Cookie有效性（API）"""
        valid, info = self._test_115_cookie()
        return {"code": 0 if valid else 1, "data": {"valid": valid, "info": info}}

    def api_test_bot(self, **kwargs) -> Dict:
        """测试Bot通知（API）"""
        if not self._bot_token or not self._admin_user_id:
            return {"code": 1, "data": {"ok": False, "message": "Bot Token 或管理员ID 未配置"}}
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": self._admin_user_id,
                "text": "✅ 115转存助手 Bot 测试消息\n如果您看到这条消息，说明通知配置正常。",
                "parse_mode": "HTML"
            }, timeout=15)
            if resp.json().get("ok"):
                return {"code": 0, "data": {"ok": True, "message": "测试消息已发送"}}
            return {"code": 1, "data": {"ok": False, "message": resp.json().get("description", "发送失败")}}
        except Exception as e:
            return {"code": 1, "data": {"ok": False, "message": str(e)}}

    def api_scan_now(self, **kwargs) -> Dict:
        """立即触发扫描（API）"""
        if not self._enabled:
            return {"code": 1, "data": {"ok": False, "message": "插件未启用，请先启用插件"}}
        scheduler = BackgroundScheduler(timezone=settings.TZ)
        scheduler.add_job(
            func=self.monitor_channels,
            trigger="date",
            run_date=datetime.now() + timedelta(seconds=1)
        )
        scheduler.start()
        return {"code": 0, "data": {"ok": True, "message": "扫描任务已触发"}}

    def api_stats(self, **kwargs) -> Dict:
        """获取运行统计（API）"""
        stats = self._load_stats()
        return {"code": 0, "data": stats}

    def api_check_dir(self, **kwargs) -> Dict:
        """检查115目录ID是否有效（API）"""
        if not self._cookie_115:
            return {"code": 1, "data": {"valid": False, "message": "Cookie 未配置"}}
        # 尝试获取目录信息
        dir_id = self._save_dir
        headers = {
            "Cookie": self._cookie_115,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        try:
            resp = requests.get(
                f"https://webapi.115.com/files/getinfo?file_id={dir_id}",
                headers=headers, timeout=15
            )
            data = resp.json()
            if data.get("state"):
                name = data.get("data", {}).get("file_name", "根目录")
                return {"code": 0, "data": {"valid": True, "name": name}}
            return {"code": 1, "data": {"valid": False, "message": data.get("error", "目录不存在")}}
        except Exception as e:
            return {"code": 1, "data": {"valid": False, "message": str(e)}}
