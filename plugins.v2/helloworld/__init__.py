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
                    # ========== 区域四：通知 ==========
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
            "bot_token": "",
            "admin_user_id": "",
        }
