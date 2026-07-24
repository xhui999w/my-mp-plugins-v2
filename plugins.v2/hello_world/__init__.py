import datetime
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class HelloWorld(_PluginBase):
    plugin_id = "hello_world"
    plugin_name = "115订阅转存助手"
    plugin_desc = "自动解析TG/PT资源并转存至115网盘"
    plugin_icon = "https://raw.githubusercontent.com/mrtian2016/MoviePilot-Plugins/main/icons/default.png"
    plugin_version = "1.0.1"
    plugin_author = "YourName"
    plugin_priority = 20
    plugin_config_prefix = "hello_world_"
    auth_level = 1
    has_page = True

    _enabled = False
    _onlyonce = False
    _115_cookie = ""
    _save_dir = ""
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled", False)
            self._115_cookie = config.get("cookie_115", "")
            self._save_dir = config.get("save_dir", "/115/Downloads")
            self._onlyonce = config.get("onlyonce", False)
        if self._enabled or self._onlyonce:
            logger.info("【115转存助手】服务已开启！")
            if not self._115_cookie:
                logger.warning("【115转存助手】未配置 115 Cookie，转存功能无法生效！")
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.sync,
                    trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )
                if self._scheduler.get_jobs():
                    self._scheduler.start()
                self._onlyonce = False
                self.update_config({"onlyonce": False})

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookie_115',
                                            'label': '115 网盘 Cookie',
                                            'placeholder': '请在此粘贴你的 115 网盘 Cookie字符串',
                                            'rows': 3
                                        }
                                    },
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_dir',
                                            'label': '115 默认保存路径',
                                            'placeholder': '/115/Downloads'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "onlyonce": False,
            "cookie_115": "", "save_dir": "/115/Downloads"
        }

    def get_page(self) -> List[dict]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'cols': 12, 'content': [
                            {'component': 'VAlert', 'props': {'type': 'info', 'text': '115订阅转存助手已启用，配置请到设置页面。'}}
                        ]}
                    ]}
                ]
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{"id": "HelloWorld", "name": "115转存服务", "trigger": "interval", "func": self.sync, "kwargs": {"hours": 6}}]

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"【115转存助手】停止服务异常：{e}")

    def sync(self):
        logger.info("【115转存助手】开始执行同步任务...")
        if not self._115_cookie:
            logger.warning("【115转存助手】未配置 115 Cookie，跳过执行")
            return
        logger.info("【115转存助手】同步任务完成。")
