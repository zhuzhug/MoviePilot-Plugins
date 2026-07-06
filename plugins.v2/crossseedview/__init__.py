from collections import defaultdict
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app.chain import ChainBase
from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Response


class SaveFiltersParams(BaseModel):
    """详情页筛选条件保存参数。"""

    min_count: Optional[int] = Field(default=None, description="最小辅种数")
    max_count: Optional[int] = Field(default=None, description="最大辅种数(0=不限)")
    downloader_filter: Optional[str] = Field(default=None, description="仅显示下载器名称")
    name_keyword: Optional[str] = Field(default=None, description="名称关键词(|分隔)")
    path_keyword: Optional[str] = Field(default=None, description="保存路径关键词")
    size_min_gb: Optional[float] = Field(default=None, description="大小下限(GB)")
    size_max_gb: Optional[float] = Field(default=None, description="大小上限(GB,0=不限)")


class DeleteTorrentParams(BaseModel):
    """删除单个种子参数。"""

    hash: str = Field(..., description="种子 hash")
    downloader: str = Field(..., description="下载器名称")
    delete_files: bool = Field(default=False, description="是否同时删除文件")


class CrossSeedView(_PluginBase):
    """辅种查看插件：扫描下载器中的种子，按 name+size 分组识别辅种，用于清理孤种。"""

    # region 插件元数据
    plugin_name = "辅种查看"
    plugin_desc = "扫描所有下载器种子，按“种子名+大小”识别辅种关系，用可折叠卡片展示辅种数量、保存路径与明细，支持交互筛选与可选删除。"
    plugin_icon = "seed.png"
    plugin_version = "0.4.1"
    plugin_label = "下载器"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "crossseedview_"
    plugin_order = 90
    auth_level = 1
    # endregion

    # region 运行态
    _enabled: bool = False
    _cron: str = "0 4 * * *"
    _min_count: int = 2
    _max_count: int = 0  # 0 = 不限
    _include_all_tags: bool = True
    _refresh_on_init: bool = True
    _downloader_filter: str = ""
    _name_keyword: str = ""
    _path_keyword: str = ""
    _size_min_gb: float = 0.0
    _size_max_gb: float = 0.0  # 0 = 不限
    _allow_delete: bool = False  # 安全开关：详情页是否显示删除按钮

    _cache_lock: Lock = Lock()
    _cache: Dict[str, Any] = {
        "groups": [],
        "downloaders": [],
        "total_torrents": 0,
        "total_groups": 0,
        "cross_groups": 0,
        "updated_at": "",
        "error": "",
    }
    # endregion

    def init_plugin(self, config: dict = None) -> None:
        """初始化：读取配置，可选立即扫描一次。周期性扫描由 get_service() 交给 MP 主调度器。"""
        if config:
            self._enabled = bool(config.get("enabled"))
            self._cron = str(config.get("cron") or self._cron).strip() or "0 4 * * *"
            try:
                self._min_count = max(1, int(config.get("min_count") or 2))
            except (TypeError, ValueError):
                self._min_count = 2
            try:
                self._max_count = max(0, int(config.get("max_count") or 0))
            except (TypeError, ValueError):
                self._max_count = 0
            self._include_all_tags = bool(config.get("include_all_tags", True))
            self._refresh_on_init = bool(config.get("refresh_on_init", True))
            self._downloader_filter = str(config.get("downloader_filter") or "").strip()
            self._name_keyword = str(config.get("name_keyword") or "").strip()
            self._path_keyword = str(config.get("path_keyword") or "").strip()
            try:
                self._size_min_gb = max(0.0, float(config.get("size_min_gb") or 0))
            except (TypeError, ValueError):
                self._size_min_gb = 0.0
            try:
                self._size_max_gb = max(0.0, float(config.get("size_max_gb") or 0))
            except (TypeError, ValueError):
                self._size_max_gb = 0.0
            self._allow_delete = bool(config.get("allow_delete", False))

        if not self._enabled:
            logger.info("[CrossSeedView] 插件未启用。")
            return

        # 尝试加载持久化缓存
        try:
            saved = self.get_data("cache")
            if isinstance(saved, dict) and saved.get("groups") is not None:
                with self._cache_lock:
                    self._cache = saved
                logger.info(
                    f"[CrossSeedView] 加载持久化缓存：更新于 {saved.get('updated_at')}，"
                    f"分组 {saved.get('total_groups')}"
                )
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 加载持久化缓存失败（忽略）：{err}")

        if self._refresh_on_init:
            try:
                self._refresh_cache(source="init")
            except Exception as err:  # noqa: BLE001
                logger.error(f"[CrossSeedView] 启动首次扫描失败：{err}")

        logger.info(
            f"[CrossSeedView] 初始化完成，CRON={self._cron}，"
            f"过滤下载器={self._downloader_filter or '全部'}"
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：手动重新扫描 + 保存详情页筛选。"""
        return [
            {
                "path": "/refresh",
                "endpoint": self.manual_refresh,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即重新扫描所有下载器",
            },
            {
                "path": "/save_filters",
                "endpoint": self.save_filters,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "保存详情页筛选条件",
            },
            {
                "path": "/delete_torrent",
                "endpoint": self.delete_torrent,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除指定下载器中的单个种子",
            },
            {
                "path": "/clear_filters",
                "endpoint": self.clear_filters,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "重置详情页筛选条件为默认值",
            },
        ]

    def manual_refresh(self) -> dict:
        """手动触发一次扫描，返回扫描结果概要。前端按钮直接调用。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        try:
            self._refresh_cache(source="manual")
        except Exception as err:  # noqa: BLE001
            logger.error(f"[CrossSeedView] 手动扫描失败：{err}")
            return {"success": False, "message": f"扫描失败：{err}"}
        with self._cache_lock:
            snap = dict(self._cache)
        return {
            "success": True,
            "message": "扫描完成",
            "data": {
                "updated_at": snap.get("updated_at", ""),
                "downloaders": snap.get("downloaders", []),
                "total_torrents": snap.get("total_torrents", 0),
                "total_groups": snap.get("total_groups", 0),
                "cross_groups": snap.get("cross_groups", 0),
                "error": snap.get("error", ""),
            },
        }

    def save_filters(self, params: SaveFiltersParams) -> Response:
        """保存详情页筛选条件到插件配置。前端提交按钮直接调用。

        仅合并前端上送的字段，忽略 None，避免清空未展示的配置。
        """
        updates: Dict[str, Any] = {}
        for key, value in params.dict(exclude_none=True).items():
            updates[key] = value
        if not updates:
            return Response(success=True, message="无变更")
        try:
            current = self.get_config() or {}
            current.update(updates)
            self.update_config(current)
            # 同步回内存实例变量，避免下次 render 前需要重载
            for key, value in updates.items():
                attr = f"_{key}"
                if hasattr(self, attr):
                    setattr(self, attr, value)
        except Exception as err:  # noqa: BLE001
            logger.error(f"[CrossSeedView] 保存筛选条件失败：{err}")
            return Response(success=False, message=f"保存失败：{err}")
        return Response(success=True, message="已保存")

    def clear_filters(self) -> Response:
        """重置详情页筛选条件为默认值。"""
        defaults = {
            "min_count": 2,
            "max_count": 0,
            "downloader_filter": "",
            "name_keyword": "",
            "path_keyword": "",
            "size_min_gb": 0,
            "size_max_gb": 0,
        }
        try:
            current = self.get_config() or {}
            current.update(defaults)
            self.update_config(current)
            for key, value in defaults.items():
                attr = f"_{key}"
                if hasattr(self, attr):
                    setattr(self, attr, value)
        except Exception as err:  # noqa: BLE001
            logger.error(f"[CrossSeedView] 重置筛选条件失败：{err}")
            return Response(success=False, message=f"重置失败：{err}")
        return Response(success=True, message="已重置")

    def delete_torrent(self, params: DeleteTorrentParams) -> Response:
        """删除指定下载器中的单个种子。

        由安全开关 ``_allow_delete`` 控制。删除后异步刷新缓存。
        """
        if not self._enabled:
            return Response(success=False, message="插件未启用")
        if not self._allow_delete:
            return Response(success=False, message="删除功能未启用，请在插件设置中开启")
        if not params.hash or not params.downloader:
            return Response(success=False, message="参数不完整")
        try:
            ok = ChainBase().remove_torrents(
                hashs=params.hash,
                delete_file=bool(params.delete_files),
                downloader=params.downloader,
            )
        except Exception as err:  # noqa: BLE001
            logger.error(f"[CrossSeedView] 删除种子失败 hash={params.hash} downloader={params.downloader}: {err}")
            return Response(success=False, message=f"删除失败：{err}")
        if not ok:
            logger.warning(f"[CrossSeedView] 删除种子返回失败 hash={params.hash} downloader={params.downloader}")
            return Response(success=False, message="下载器返回删除失败")
        logger.info(
            f"[CrossSeedView] 已删除种子 hash={params.hash} downloader={params.downloader} "
            f"delete_files={params.delete_files}"
        )
        # 后台重新扫描，刷新分组
        try:
            self._refresh_cache(source="delete")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 删除后刷新缓存失败（忽略）：{err}")
        return Response(success=True, message="已删除")


    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [
            {
                "id": "CrossSeedViewRefresh",
                "name": "辅种查看-立即扫描",
                "trigger": CronTrigger.from_crontab(self._cron, timezone=pytz.timezone(settings.TZ)),
                "func": self._scheduled_refresh,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认值。"""
        # 下载器候选：来自 DownloaderHelper
        try:
            downloader_options = [{"title": "全部下载器", "value": ""}] + [
                {"title": cfg.name, "value": cfg.name}
                for cfg in DownloaderHelper().get_configs().values()
            ]
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 读取下载器列表失败：{err}")
            downloader_options = [{"title": "全部下载器", "value": ""}]

        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "refresh_on_init",
                                            "label": "MP 启动时立即扫描",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "include_all_tags",
                                            "label": "包含所有种子（不限 MP 标签）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_delete",
                                            "label": "允许在详情页删除种子（危险）",
                                            "color": "error",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时刷新 CRON",
                                            "placeholder": "0 4 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_count",
                                            "label": "最小辅种数",
                                            "type": "number",
                                            "placeholder": "2",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_count",
                                            "label": "最大辅种数(0=不限)",
                                            "type": "number",
                                            "placeholder": "0",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader_filter",
                                            "label": "仅显示下载器",
                                            "items": downloader_options,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "name_keyword",
                                            "label": "名称关键词(不区分大小写,支持多个用|分隔)",
                                            "placeholder": "如: 1080p|BDRip",
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "path_keyword",
                                            "label": "保存路径关键词(不区分大小写)",
                                            "placeholder": "如: /downloads/movie",
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "size_min_gb",
                                            "label": "大小≥(GB)",
                                            "type": "number",
                                            "placeholder": "0",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "size_max_gb",
                                            "label": "大小≤(GB,0=不限)",
                                            "type": "number",
                                            "placeholder": "0",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "扫描规则：按“种子名 + 文件大小”跨下载器分组，同组视为辅种。"
                                "筛选项支持组合：辅种数上下限 / 下载器 / 名称关键词(|分隔多个) / "
                                "保存路径关键词 / 大小区间。修改后保存并重载即可生效，不需重新扫描。"
                            ),
                        },
                    },
                ],
            }
        ]
        defaults: Dict[str, Any] = {
            "enabled": False,
            "refresh_on_init": True,
            "include_all_tags": True,
            "allow_delete": False,
            "cron": "0 4 * * *",
            "min_count": 2,
            "max_count": 0,
            "downloader_filter": "",
            "name_keyword": "",
            "path_keyword": "",
            "size_min_gb": 0,
            "size_max_gb": 0,
        }
        return form, defaults

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页：展示扫描概要与辅种分组表格。"""
        with self._cache_lock:
            snapshot = dict(self._cache)
            groups: List[dict] = list(snapshot.get("groups") or [])

        # 1) 辅种数下限
        filtered = [g for g in groups if int(g.get("count", 0)) >= self._min_count]

        # 2) 辅种数上限
        if self._max_count > 0:
            filtered = [g for g in filtered if int(g.get("count", 0)) <= self._max_count]

        # 3) 下载器过滤
        selected_dl = (self._downloader_filter or "").strip()
        if selected_dl:
            filtered = [g for g in filtered if selected_dl in (g.get("downloaders") or [])]

        # 4) 名称关键词（|分隔多个，任一命中即保留）
        name_kw = (self._name_keyword or "").strip()
        if name_kw:
            kws = [k.strip().lower() for k in name_kw.split("|") if k.strip()]
            if kws:
                filtered = [
                    g for g in filtered
                    if any(k in str(g.get("name") or "").lower() for k in kws)
                ]

        # 5) 保存路径关键词
        path_kw = (self._path_keyword or "").strip().lower()
        if path_kw:
            filtered = [
                g for g in filtered
                if any(path_kw in str(p or "").lower() for p in (g.get("save_paths") or []))
            ]

        # 6) 大小区间（GB）
        gb = 1024.0 ** 3
        if self._size_min_gb > 0:
            min_bytes = self._size_min_gb * gb
            filtered = [g for g in filtered if float(g.get("size") or 0) >= min_bytes]
        if self._size_max_gb > 0:
            max_bytes = self._size_max_gb * gb
            filtered = [g for g in filtered if float(g.get("size") or 0) <= max_bytes]

        filtered.sort(key=lambda g: (-int(g.get("count", 0)), str(g.get("name") or "")))

        items = [
            {
                "name": g.get("name") or "-",
                "count": g.get("count", 0),
                "size_text": self._fmt_size(g.get("size") or 0),
                "save_paths_text": "\n".join(g.get("save_paths") or []) or "-",
                "downloaders_text": "、".join(g.get("downloaders") or []) or "-",
                "torrents": list(g.get("torrents") or []),
            }
            for g in filtered
        ]

        logger.info(
            f"[CrossSeedView] get_page: 总分组={len(groups)}，"
            f"筛选后={len(filtered)}，下载器={selected_dl or '全部'}，"
            f"名称={name_kw or '-'}，路径={path_kw or '-'}，"
            f"大小={self._size_min_gb}-{self._size_max_gb}GB"
        )

        summary_row = {
            "component": "VRow",
            "props": {"class": "mb-2"},
            "content": [
                self._stat_card("下载器数", len(snapshot.get("downloaders") or []), "primary"),
                self._stat_card("种子总数", snapshot.get("total_torrents", 0), "info"),
                self._stat_card("分组数", snapshot.get("total_groups", 0), "info"),
                self._stat_card(
                    f"≥{self._min_count} 份辅种组数",
                    snapshot.get("cross_groups", 0),
                    "success",
                ),
            ],
        }

        info_text = (
            f"最近扫描：{snapshot.get('updated_at') or '尚未扫描'}｜"
            f"扫描下载器：{'、'.join(snapshot.get('downloaders') or []) or '-'}"
        )
        filter_bits = []
        if self._max_count > 0:
            filter_bits.append(f"辅种数 {self._min_count}-{self._max_count}")
        else:
            filter_bits.append(f"辅种数 ≥{self._min_count}")
        if selected_dl:
            filter_bits.append(f"下载器={selected_dl}")
        if name_kw:
            filter_bits.append(f"名称含「{name_kw}」")
        if path_kw:
            filter_bits.append(f"路径含「{path_kw}」")
        if self._size_min_gb > 0 or self._size_max_gb > 0:
            hi = f"{self._size_max_gb}GB" if self._size_max_gb > 0 else "∞"
            filter_bits.append(f"大小 {self._size_min_gb}GB - {hi}")
        if filter_bits:
            info_text += "｜筛选：" + "，".join(filter_bits) + f"（命中 {len(items)} 组）"
        if snapshot.get("error"):
            info_text += f"｜错误：{snapshot['error']}"

        table_title = f"辅种分组（命中 {len(items)} 组）"

        # 预设筛选按钮
        save_api = f"plugin/CrossSeedView/save_filters?apikey={settings.API_TOKEN}"
        clear_api = f"plugin/CrossSeedView/clear_filters?apikey={settings.API_TOKEN}"

        def _preset_btn(label: str, color: str, data: Optional[dict], is_clear: bool = False) -> dict:
            btn = {
                "component": "VBtn",
                "props": {
                    "color": color,
                    "variant": "tonal",
                    "size": "small",
                    "class": "mr-2 mb-2",
                },
                "text": label,
            }
            if is_clear:
                btn["events"] = {"click": {"api": clear_api, "method": "get"}}
            else:
                btn["events"] = {"click": {"api": save_api, "method": "post", "params": data}}
            return btn

        preset_row = {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-2"},
            "content": [
                {
                    "component": "VCardText",
                    "props": {"class": "py-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-wrap align-center"},
                            "content": [
                                {
                                    "component": "span",
                                    "props": {"class": "text-caption mr-3"},
                                    "text": "快捷筛选：",
                                },
                                _preset_btn("全部", "primary", {"min_count": 2, "max_count": 0}),
                                _preset_btn("仅孤种", "warning", {"min_count": 1, "max_count": 1}),
                                _preset_btn("多辅种", "success", {"min_count": 3, "max_count": 0}),
                                _preset_btn("大文件", "info", {"size_min_gb": 10}),
                                _preset_btn("重置", "secondary", None, is_clear=True),
                            ],
                        }
                    ],
                }
            ],
        }

        # 构建卡片列表
        delete_api = f"plugin/CrossSeedView/delete_torrent?apikey={settings.API_TOKEN}"
        MAX_DELETE_CARDS = 50

        def _torrent_row(t: dict, show_delete: bool) -> dict:
            thash = str(t.get("hash") or "")
            dl_name = str(t.get("downloader") or "")
            save_path = str(t.get("save_path") or "-")
            row_content = [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 3},
                    "content": [
                        {
                            "component": "VChip",
                            "props": {"size": "x-small", "color": "primary", "variant": "tonal"},
                            "text": dl_name or "-",
                        }
                    ],
                },
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "text-caption text-medium-emphasis text-truncate"},
                            "text": save_path,
                        },
                        {
                            "component": "div",
                            "props": {"class": "text-caption text-disabled"},
                            "text": f"hash: {thash[:16]}..." if thash else "hash: -",
                        },
                    ],
                },
            ]
            if show_delete and thash and dl_name:
                row_content.append(
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3, "class": "text-right"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "warning",
                                    "variant": "outlined",
                                    "size": "x-small",
                                    "class": "mr-1",
                                },
                                "text": "仅删种",
                                "events": {
                                    "click": {
                                        "api": delete_api,
                                        "method": "post",
                                        "params": {
                                            "hash": thash,
                                            "downloader": dl_name,
                                            "delete_files": False,
                                        },
                                    }
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "error",
                                    "variant": "flat",
                                    "size": "x-small",
                                },
                                "text": "删种+文件",
                                "events": {
                                    "click": {
                                        "api": delete_api,
                                        "method": "post",
                                        "params": {
                                            "hash": thash,
                                            "downloader": dl_name,
                                            "delete_files": True,
                                        },
                                    }
                                },
                            },
                        ],
                    }
                )
            else:
                row_content.append(
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": []}
                )
            return {
                "component": "VRow",
                "props": {"dense": True, "class": "align-center py-1"},
                "content": row_content,
            }

        card_list_content: List[dict] = []
        if not items:
            card_list_content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "没有满足条件的辅种分组，请检查筛选条件或点击「立即重新扫描」。",
                    },
                }
            )
        else:
            expansion_items = []
            for idx, it in enumerate(items):
                show_delete = self._allow_delete and idx < MAX_DELETE_CARDS
                torrents = it.get("torrents") or []
                # 折叠面板标题：名称 + 概要 chips
                panel_title_content = [
                    {
                        "component": "div",
                        "props": {"class": "d-flex flex-wrap align-center w-100"},
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "class": "text-subtitle-2 text-truncate mr-3",
                                    "style": "flex: 1 1 auto; min-width: 0;",
                                },
                                "text": it["name"],
                            },
                            {
                                "component": "VChip",
                                "props": {
                                    "size": "x-small",
                                    "color": "success",
                                    "variant": "tonal",
                                    "class": "mr-2",
                                },
                                "text": f"辅种 {it['count']}",
                            },
                            {
                                "component": "VChip",
                                "props": {
                                    "size": "x-small",
                                    "color": "info",
                                    "variant": "tonal",
                                    "class": "mr-2",
                                },
                                "text": it["size_text"],
                            },
                            {
                                "component": "VChip",
                                "props": {
                                    "size": "x-small",
                                    "color": "primary",
                                    "variant": "tonal",
                                },
                                "text": it["downloaders_text"],
                            },
                        ],
                    }
                ]
                torrent_rows = [_torrent_row(t, show_delete) for t in torrents]
                expansion_items.append(
                    {
                        "component": "VExpansionPanel",
                        "content": [
                            {
                                "component": "VExpansionPanelTitle",
                                "props": {"class": "py-2"},
                                "content": panel_title_content,
                            },
                            {
                                "component": "VExpansionPanelText",
                                "content": torrent_rows
                                or [
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption text-disabled"},
                                        "text": "无种子明细",
                                    }
                                ],
                            },
                        ],
                    }
                )
            card_list_content.append(
                {
                    "component": "VExpansionPanels",
                    "props": {"variant": "accordion", "multiple": True},
                    "content": expansion_items,
                }
            )
            if self._allow_delete and len(items) > MAX_DELETE_CARDS:
                card_list_content.append(
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "text": f"仅前 {MAX_DELETE_CARDS} 组显示删除按钮，如需删除其他分组请先筛选缩小范围。",
                        },
                    }
                )
            elif not self._allow_delete:
                card_list_content.append(
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "density": "compact",
                            "text": "删除功能未启用。如需删除种子，请在插件设置中开启「允许在详情页删除种子（危险）」。",
                        },
                    }
                )

        # 使用说明（折叠面板，默认收起）
        help_panel = {
            "component": "VExpansionPanels",
            "props": {"class": "mb-2", "variant": "accordion"},
            "content": [
                {
                    "component": "VExpansionPanel",
                    "content": [
                        {
                            "component": "VExpansionPanelTitle",
                            "props": {"class": "text-subtitle-2"},
                            "content": [
                                {
                                    "component": "VIcon",
                                    "props": {"icon": "mdi-help-circle-outline", "class": "mr-2", "size": "small"},
                                },
                                {"component": "span", "text": "使用说明 / 什么是「分组」和「种子」？"},
                            ],
                        },
                        {
                            "component": "VExpansionPanelText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-body-2"},
                                    "content": [
                                        {
                                            "component": "p",
                                            "props": {"class": "mb-2"},
                                            "content": [
                                                {"component": "strong", "text": "种子（Torrent）"},
                                                {"component": "span", "text": "：下载器里的一条下载任务。同一部资源可以来自不同站点，每个站点各自是一个独立种子。"},
                                            ],
                                        },
                                        {
                                            "component": "p",
                                            "props": {"class": "mb-2"},
                                            "content": [
                                                {"component": "strong", "text": "分组（Group）"},
                                                {"component": "span", "text": "：本插件按「种子名 + 文件大小」把所有下载器里的种子聚在一起。同一组里的多个种子，通常就是同一部资源在不同站点的辅种。"},
                                            ],
                                        },
                                        {
                                            "component": "p",
                                            "props": {"class": "mb-2"},
                                            "content": [
                                                {"component": "strong", "text": "辅种数"},
                                                {"component": "span", "text": "：该分组里包含的种子数量。"},
                                            ],
                                        },
                                        {
                                            "component": "ul",
                                            "props": {"class": "mb-2 ml-4"},
                                            "content": [
                                                {"component": "li", "text": "辅种数 = 1：孤种，只在一个站点做种，风险高。"},
                                                {"component": "li", "text": "辅种数 ≥ 2：跨站点/跨下载器做种，同一份文件被多个种子引用。"},
                                                {"component": "li", "text": "辅种数越多，做种量越充足，删除单个种子影响越小。"},
                                            ],
                                        },
                                        {
                                            "component": "p",
                                            "props": {"class": "mb-2"},
                                            "content": [
                                                {"component": "strong", "text": "典型用法"},
                                                {"component": "span", "text": "："},
                                            ],
                                        },
                                        {
                                            "component": "ul",
                                            "props": {"class": "mb-2 ml-4"},
                                            "content": [
                                                {"component": "li", "text": "找孤种：预设按钮「仅孤种」→ 看哪些资源只有一个来源。"},
                                                {"component": "li", "text": "清冗余：预设按钮「多辅种」→ 找出辅种过多的分组，删掉重复的种子（保留文件）。"},
                                                {"component": "li", "text": "排大文件：预设按钮「大文件」→ 优先审查体积大、辅种少的资源。"},
                                            ],
                                        },
                                        {
                                            "component": "p",
                                            "props": {"class": "mb-0"},
                                            "content": [
                                                {"component": "strong", "text": "删除按钮"},
                                                {"component": "span", "text": "：默认关闭。到插件设置里开启「允许在详情页删除种子（危险）」后，前 50 组会出现「仅删种」（保留文件）和「删种+文件」（连同文件一起删）两个按钮。"},
                                            ],
                                        },
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }

        return [
            help_panel,
            {
                "component": "VAlert",
                "props": {
                    "type": "warning" if snapshot.get("error") else "info",
                    "variant": "tonal",
                    "text": info_text,
                },
            },
            {
                "component": "VRow",
                "props": {"class": "mb-2"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "block": True,
                                    "variant": "tonal",
                                    "prepend-icon": "mdi-refresh",
                                },
                                "text": "立即重新扫描",
                                "events": {
                                    "click": {
                                        "api": f"plugin/CrossSeedView/refresh?apikey={settings.API_TOKEN}",
                                        "method": "get",
                                    }
                                },
                            }
                        ],
                    }
                ],
            },
            summary_row,
            preset_row,
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": table_title},
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-2"},
                        "content": card_list_content,
                    },
                ],
            },
        ]

    # ---------------- 扫描逻辑 ----------------

    def _scheduled_refresh(self) -> None:
        """定时任务入口。"""
        try:
            self._refresh_cache(source="cron")
        except Exception as err:
            logger.error(f"[CrossSeedView] 定时扫描失败：{err}")

    def _refresh_cache(self, source: str = "manual") -> None:
        """扫描所有下载器，重建缓存。"""
        logger.info(f"[CrossSeedView] 开始扫描（来源={source}）...")

        # 通过 ChainBase.chain 拿到所有下载器插件
        try:
            downloader_names = list(DownloaderHelper().get_configs().keys())
        except Exception as err:
            logger.error(f"[CrossSeedView] 获取下载器配置失败：{err}")
            downloader_names = []

        chain = ChainBase()
        torrents_by_dl: Dict[str, List[Any]] = {}
        errors: List[str] = []

        for dl_name in downloader_names:
            try:
                # list_torrents 返回该下载器所有种子
                tors = chain.list_torrents(
                    downloader=dl_name,
                    include_all_tags=self._include_all_tags,
                )
                if tors is None:
                    tors = []
                torrents_by_dl[dl_name] = tors
                logger.info(f"[CrossSeedView]   下载器 {dl_name}: {len(tors)} 个种子")
            except Exception as err:
                logger.warning(f"[CrossSeedView] 下载器 {dl_name} 读取失败：{err}")
                errors.append(f"{dl_name}: {err}")
                torrents_by_dl[dl_name] = []

        # 按 (name, size) 聚合
        groups_map: Dict[Tuple[str, int], Dict[str, Any]] = defaultdict(
            lambda: {
                "name": "",
                "size": 0,
                "count": 0,
                "downloaders": set(),
                "save_paths": set(),
                "torrents": [],
            }
        )
        total_torrents = 0
        for dl_name, tors in torrents_by_dl.items():
            for t in tors:
                try:
                    name, size, save_path, thash = self._extract_torrent_meta(t)
                except Exception as err:
                    logger.debug(f"[CrossSeedView] 解析种子失败：{err}")
                    continue
                if not name or size <= 0:
                    continue
                total_torrents += 1
                key = (name, size)
                entry = groups_map[key]
                entry["name"] = name
                entry["size"] = size
                entry["count"] += 1
                entry["downloaders"].add(dl_name)
                if save_path:
                    entry["save_paths"].add(save_path)
                entry["torrents"].append({
                    "hash": thash,
                    "downloader": dl_name,
                    "save_path": save_path,
                })

        groups: List[Dict[str, Any]] = []
        cross_groups = 0
        for entry in groups_map.values():
            g = {
                "name": entry["name"],
                "size": entry["size"],
                "count": entry["count"],
                "downloaders": sorted(entry["downloaders"]),
                "save_paths": sorted(entry["save_paths"]),
                "torrents": entry["torrents"],
            }
            groups.append(g)
            if g["count"] >= 2:
                cross_groups += 1

        snapshot = {
            "groups": groups,
            "downloaders": downloader_names,
            "total_torrents": total_torrents,
            "total_groups": len(groups),
            "cross_groups": cross_groups,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": "；".join(errors),
        }
        with self._cache_lock:
            self._cache = snapshot
        try:
            self.save_data("cache", snapshot)
        except Exception as err:
            logger.debug(f"[CrossSeedView] 持久化缓存失败（忽略）：{err}")

        logger.info(
            f"[CrossSeedView] 扫描完成：下载器 {len(downloader_names)}，"
            f"种子 {total_torrents}，分组 {len(groups)}，辅种组 {cross_groups}"
        )

    @staticmethod
    def _extract_torrent_meta(t: Any) -> Tuple[str, int, str, str]:
        """从下载器返回的种子对象中提取 (name, size, save_path, hash)。兼容 qb/tr 两种对象。"""
        # qBittorrent: 属性 name、size、save_path/content_path
        name = ""
        size = 0
        save_path = ""
        thash = ""

        # 常见字段
        for attr in ("name",):
            v = getattr(t, attr, None) or (t.get(attr) if isinstance(t, dict) else None)
            if v:
                name = str(v)
                break

        for attr in ("size", "total_size", "totalSize"):
            v = getattr(t, attr, None) if not isinstance(t, dict) else t.get(attr)
            if v is not None:
                try:
                    size = int(v)
                    break
                except (TypeError, ValueError):
                    continue

        for attr in ("save_path", "download_dir", "downloadDir", "content_path"):
            v = getattr(t, attr, None) if not isinstance(t, dict) else t.get(attr)
            if v:
                save_path = str(v)
                break

        for attr in ("hash", "hashString", "hash_string"):
            v = getattr(t, attr, None) if not isinstance(t, dict) else t.get(attr)
            if v:
                thash = str(v)
                break

        return name, size, save_path, thash

    @staticmethod
    def _fmt_size(num: int) -> str:
        try:
            n = float(num)
        except (TypeError, ValueError):
            return "-"
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if abs(n) < 1024.0:
                return f"{n:.2f} {unit}"
            n /= 1024.0
        return f"{n:.2f} EB"

    @staticmethod
    def _stat_card(title: str, value: Any, color: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": color},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "text-center py-3"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-h5 font-weight-bold"},
                                    "text": str(value),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-medium-emphasis"},
                                    "text": title,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    def stop_service(self) -> None:
        """插件停止：释放缓存引用。定时任务由 MP 主调度器托管，无需在此关闭。"""
        try:
            with self._cache_lock:
                # 保留持久化数据，仅重置内存缓存标记；下次启用时会从 get_data 恢复
                self._cache = {
                    "groups": [],
                    "downloaders": [],
                    "total_torrents": 0,
                    "total_groups": 0,
                    "cross_groups": 0,
                    "updated_at": "",
                    "error": "",
                }
            logger.info("[CrossSeedView] 已停止服务。")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 停止服务时出现异常（忽略）：{err}")
