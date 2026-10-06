"""
当季新番与热门影视发现插件

合并「当季新番」与「热门TV与电影」：
- 番剧视图：TMDB / Bangumi / 蜜柑 / 番组百科 / 每日更新 多源整合，按星期分组
- 影视视图：TMDB 热门影视（排除动画），TV 按热度排序，电影按热度排序
- 统一订阅：自动识别最新季，避免重复订阅
"""

import html
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from fastapi import Request
from pydantic import BaseModel, Field
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils


class SubscribeParams(BaseModel):
    """订阅参数"""
    title: str = Field(default="", description="标题")
    year: str = Field(default="", description="年份")
    tmdb_id: Optional[Union[str, int]] = Field(default=None, description="TMDB ID")
    bangumi_id: Optional[Union[str, int]] = Field(default=None, description="Bangumi ID")
    media_type: str = Field(default="tv", description="媒体类型: tv 或 movie")
    mikan_id: Optional[str] = Field(default=None, description="蜜柑ID")


class MediaDiscovery(_PluginBase):
    """当季新番与热门影视发现插件。"""

    plugin_name = "当季新番与热门影视"
    plugin_desc = "发现当季新番和热门影视，支持多数据源，按日期分组，一键订阅追剧。"
    plugin_icon = "mdi-play-circle"
    plugin_version = "1.3.0"
    plugin_label = "订阅"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "media_discovery_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _current_view = "anime"  # 当前视图：anime 或 movies（详情页切换按钮控制）
    _anime_source = "auto"   # 番剧数据源
    _movies_source = "tmdb_hot"  # 影视数据源
    _min_rating = 0.0
    _min_year = 0
    _auto_refresh = ""
    _notify_new = False
    _search_keyword = ""
    _hide_subscribed = False
    _cache: Dict[str, Any] = {}
    _cache_time: float = 0
    _cache_ttl: int = 3600
    _scheduler: Optional[BackgroundScheduler] = None  # deprecated
    _last_notify_date: str = ""
    _last_notify_time = float = 0
    _loading = False

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._current_view = "anime"
        self._anime_source = "auto"
        self._movies_source = "tmdb_hot"
        self._min_rating = 0.0
        self._min_year = 0
        self._auto_refresh = ""
        self._notify_new = False
        self._last_notify_date = ""
        self._last_notify_time = 0
        self._loading = False
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._anime_source = str(config.get("anime_source") or "auto")
        self._movies_source = str(config.get("movies_source") or "tmdb_hot")
        self._min_rating = float(config.get("min_rating") or 0.0)
        self._min_year = int(config.get("min_year") or 0)
        self._auto_refresh = str(config.get("auto_refresh") or "")
        self._notify_new = bool(config.get("notify_new"))

        # 恢复上次视图选择
        try:
            saved_view = self.get_data("current_view")
            if saved_view:
                self._current_view = str(saved_view)
        except Exception:
            pass

        # 从持久化数据恢复上次通知日期
        try:
            saved = self.get_data("last_notify_date")
            saved_time = self.get_data("last_notify_time")
            if saved_time:
                self._last_notify_time = float(saved_time)
            if saved:
                self._last_notify_date = str(saved)
        except Exception:
            pass

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {"path": "/refresh", "endpoint": self._refresh_data, "methods": ["GET"], "summary": "刷新数据", "auth": "bear"},
            {"path": "/switch_view", "endpoint": self._switch_view, "methods": ["POST"], "summary": "切换视图", "auth": "bear"},
            {"path": "/subscribe", "endpoint": self._subscribe_anime, "methods": ["POST"], "summary": "订阅", "auth": "bear"},
            {"path": "/unsubscribe", "endpoint": self._unsubscribe_anime, "methods": ["POST"], "summary": "取消订阅", "auth": "bear"},
            {"path": "/reset_notify", "endpoint": self._reset_notify_date, "methods": ["GET"], "summary": "重置通知日期", "auth": "bear"},
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """返回定时刷新调度服务。"""
        if not self._auto_refresh:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._auto_refresh)
        except Exception as e:
            logger.error(f"自动刷新 cron 表达式无效: {self._auto_refresh}，{e}")
            return []
        return [{"id": "MediaDiscoveryRefresh", "name": "当季新番与热门影视自动刷新", "trigger": trigger, "func": self._scheduled_refresh, "kwargs": {}}]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {"component": "VForm", "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 9}, "content": [
                        {"component": "div", "props": {"class": "text-body-2 text-grey"}, "text": "详情页顶部可切换番剧/影视视图，配置页的数据源分别对应两个视图"}
                    ]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VSelect", "props": {
                            "model": "anime_source", "label": "番剧数据源",
                            "items": [
                                {"title": "自动整合（推荐）", "value": "auto"},
                                {"title": "TMDB 动画", "value": "tmdb"},
                                {"title": "Bangumi", "value": "bangumi"},
                                {"title": "蜜柑", "value": "mikan"},
                                {"title": "番组百科", "value": "anibk"},
                                {"title": "番组百科·每日更新", "value": "anibk_daily"},
                            ],
                            "hint": "番剧视图使用",
                            "persistent-hint": True,
                        }}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VSelect", "props": {
                            "model": "movies_source", "label": "影视数据源",
                            "items": [
                                {"title": "自动整合（TMDB+豆瓣）", "value": "auto"},
                                {"title": "TMDB 热门影视", "value": "tmdb_hot"},
                                {"title": "豆瓣热榜（TV+电影）", "value": "douban_hot"},
                            ],
                            "hint": "影视视图使用（剧集+电影，排除动画）",
                            "persistent-hint": True,
                        }}
                    ]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VTextField", "props": {"model": "min_rating", "label": "最低评分", "type": "number", "hint": "0=全部"}},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VTextField", "props": {"model": "min_year", "label": "最早年份", "type": "number", "hint": "0=全部，如2010"}},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VTextField", "props": {
                            "model": "auto_refresh", "label": "自动刷新 Cron 表达式",
                            "hint": "留空=关闭。示例: 0 10 * * * (每天10点), 0 */6 * * * (每6小时)",
                            "placeholder": "0 10 * * *",
                            "density": "compact", "hide-details": False,
                        }},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VSwitch", "props": {"model": "notify_new", "label": "新内容发现时通知"}},
                    ]},
                ]},
            ]}
        ], {
            "enabled": False,
            "anime_source": "auto",
            "movies_source": "auto",
            "min_rating": 0.0,
            "min_year": 0,
            "auto_refresh": "",
            "notify_new": False
        }

    # ==================== 页面渲染 ====================

    def get_page(self) -> Optional[List[dict]]:
        """渲染插件详情页面（番剧/影视共用统一布局）。"""
        if not self._enabled:
            return None
        api_token = settings.API_TOKEN
        refresh_api = f"plugin/MediaDiscovery/refresh?apikey={api_token}"
        self._loading = True
        data = self._get_anime_list()
        self._loading = False
        if not data:
            return [
                {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "text-center pa-4"}, "content": [
                            {"component": "VIcon", "props": {"size": "48", "color": "grey", "class": "mb-2"}, "text": "mdi-television"},
                            {"component": "div", "props": {"class": "text-body-1 text-grey"}, "text": "暂无数据，点击刷新"},
                        ]},
                    ]},
                ]},
                {"component": "div", "props": {"class": "text-center mt-4"}, "content": [
                    {"component": "VBtn", "props": {"color": "primary", "variant": "tonal", "prepend-icon": "mdi-refresh"}, "text": "刷新",
                     "events": {"click": {"api": refresh_api, "method": "get"}}},
                ]},
            ]

        # 客户端过滤
        sk = self._search_keyword.strip().lower()
        if sk:
            data = [a for a in data if sk in a.get("title", "").lower()]
        if self._hide_subscribed:
            data = [a for a in data if not a.get("subscribed")]

        is_anime_view = self._current_view == "anime"
        from collections import OrderedDict
        # 按热度/评分降序排列（TMDB 用 popularity，豆瓣用 rating）
        data_sorted = sorted(
            data,
            key=lambda a: a.get("popularity", 0) if a.get("popularity", 0) else a.get("rating", 0),
            reverse=True
        )

        # 按媒体类型分组
        tv_list = [a for a in data_sorted if a.get("media_type", "tv") == "tv"]
        movie_list = [a for a in data_sorted if a.get("media_type", "tv") == "movie"]
        total = len(data_sorted)
        tv_count = len(tv_list)
        movie_count = len(movie_list)
        sub_count = sum(1 for a in data_sorted if a.get("subscribed"))

        def _render_grid(items: List[Dict[str, Any]], visible_count: int, color: str) -> List[dict]:
            """渲染卡片网格：前 visible_count 个直接显示，其余折叠。"""
            cols = [self._build_anime_card(a, api_token) for a in items]
            result: List[dict] = []
            if not cols:
                return result
            # 前 visible_count 个直接显示
            for i in range(0, min(visible_count, len(cols)), 2):
                row_content = []
                row_content.append({"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i]]})
                if i + 1 < len(cols) and i + 1 < visible_count:
                    row_content.append({"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i + 1]]})
                result.append({"component": "VRow", "props": {"dense": True}, "content": row_content})
            # 折叠剩余
            if len(cols) > visible_count:
                expansion_content = []
                for i in range(visible_count, len(cols), 2):
                    row_content = []
                    row_content.append({"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i]]})
                    if i + 1 < len(cols):
                        row_content.append({"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i + 1]]})
                    expansion_content.append({"component": "VRow", "props": {"dense": True}, "content": row_content})
                result.append({"component": "VExpansionPanels", "props": {"variant": "accordion", "multiple": True}, "content": [
                    {"component": "VExpansionPanel", "props": {}, "content": [
                        {"component": "VExpansionPanelTitle", "props": {"class": "text-subtitle-2 d-flex align-center px-3 py-2"}, "content": [
                            {"component": "VChip", "props": {"color": color, "variant": "flat", "size": "small", "class": "mr-2"}, "text": f"展开更多 ({len(cols) - visible_count} 部)"},
                        ]},
                        {"component": "VExpansionPanelText", "props": {"class": "pa-3"}, "content": expansion_content},
                    ]},
                ]})
            return result

        page = [
            # 视图切换按钮组
            {"component": "VRow", "props": {"class": "mb-3"}, "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "div", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VBtn", "props": {
                            "color": "primary" if is_anime_view else "grey",
                            "variant": "tonal" if is_anime_view else "outlined",
                            "class": "mr-2",
                            "prepend-icon": "mdi-play-circle",
                        }, "text": "番剧",
                         "events": {"click": {"api": f"plugin/MediaDiscovery/switch_view?apikey={api_token}", "method": "post", "params": {"view": "anime"}}}},
                        {"component": "VBtn", "props": {
                            "color": "orange" if not is_anime_view else "grey",
                            "variant": "tonal" if not is_anime_view else "outlined",
                            "class": "mr-2",
                            "prepend-icon": "mdi-movie-open",
                        }, "text": "热门影视",
                         "events": {"click": {"api": f"plugin/MediaDiscovery/switch_view?apikey={api_token}", "method": "post", "params": {"view": "movies"}}}},
                        {"component": "VChip", "props": {"color": "primary" if is_anime_view else "orange", "variant": "flat", "size": "small"}, "text": f"当前: {'番剧' if is_anime_view else '热门影视'}"},
                        {"component": "VChip", "props": {"color": "grey", "variant": "outlined", "size": "small", "class": "ml-2"}, "text": "热度排序: 高→低"},
                    ]},
                ]},
            ]},
            # 统计
            {"component": "VRow", "props": {"class": "mb-2"}, "content": [
                {"component": "VCol", "props": {"cols": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "color": "primary"}, "content": [
                        {"component": "VCardText", "props": {"class": "text-center py-2"}, "content": [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(tv_count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": "TV动画" if is_anime_view else "TV"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "color": "orange"}, "content": [
                        {"component": "VCardText", "props": {"class": "text-center py-2"}, "content": [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(movie_count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": "电影"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "color": "success"}, "content": [
                        {"component": "VCardText", "props": {"class": "text-center py-2"}, "content": [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(sub_count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": "已订阅"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "color": "warning"}, "content": [
                        {"component": "VCardText", "props": {"class": "text-center py-2"}, "content": [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(total - sub_count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": "未订阅"},
                        ]},
                    ]},
                ]},
            ]},
            # 工具栏
            {"component": "VRow", "props": {"class": "mb-1", "align": "center"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                    {"component": "VTextField", "props": {"model": "search", "label": "搜索标题", "density": "compact", "clearable": True, "hide-details": True, "prepend-inner-icon": "mdi-magnify"}},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VSwitch", "props": {"model": "hide_subscribed", "label": "仅看未订阅", "density": "compact", "hide-details": True, "color": "primary"}},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 4, "class": "text-right"}, "content": [
                    {"component": "VBtn", "props": {"size": "small", "variant": "text", "prepend-icon": "mdi-refresh"}, "text": "刷新",
                     "events": {"click": {"api": refresh_api, "method": "get"}}},
                ]},
            ]},
        ]

        # TV 分组（番剧/影视共用统一布局）
        if tv_list:
            page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                {"component": "VChip", "props": {"color": "primary", "variant": "flat", "size": "small", "class": "mr-2"}, "text": "TV动画" if is_anime_view else "TV剧集"},
                {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{tv_count} 部（按热度排序）"},
            ]})
            page.extend(_render_grid(tv_list, 4, "primary"))

        # 电影分组（番剧/影视共用统一布局）
        if movie_list:
            page.append({"component": "VDivider", "props": {"class": "my-3"}})
            page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                {"component": "VChip", "props": {"color": "orange", "variant": "flat", "size": "small", "class": "mr-2"}, "text": "电影" if is_anime_view else "电影/热映"},
                {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{movie_count} 部（按热度排序）"},
            ]})
            page.extend(_render_grid(movie_list, 4, "orange"))

        return page

    def _build_anime_card(self, anime: Dict[str, Any], api_token: str) -> dict:
        """构建单个番剧/影视卡片。"""
        title = anime.get("title", "未知")
        rating = anime.get("rating", 0)
        poster = anime.get("poster", "")
        overview = anime.get("overview", "")[:80]
        air_date = anime.get("air_date", "")
        tmdb_id = anime.get("tmdb_id", "")
        bangumi_id = anime.get("bangumi_id", "")
        subscribed = anime.get("subscribed", False)
        rating_color = "success" if rating >= 7.0 else ("warning" if rating >= 5.0 else "grey")

        subscribe_btn = {"component": "VBtn", "props": {
            "size": "x-small", "variant": "tonal",
            "prepend-icon": "mdi-check" if subscribed else "mdi-plus",
            "color": "success" if subscribed else "primary",
        }, "text": "已订阅" if subscribed else "订阅"}
        params = {"title": title, "year": anime.get("year", ""), "media_type": anime.get("media_type", "tv")}
        if tmdb_id:
            params["tmdb_id"] = tmdb_id
        if bangumi_id:
            params["bangumi_id"] = bangumi_id
        if anime.get("mikan_id"):
            params["mikan_id"] = anime["mikan_id"]

        if subscribed:
            subscribe_btn["events"] = {"click": {
                "api": f"plugin/MediaDiscovery/unsubscribe?apikey={api_token}",
                "method": "post", "params": params,
            }}
        else:
            subscribe_btn["events"] = {"click": {
                "api": f"plugin/MediaDiscovery/subscribe?apikey={api_token}",
                "method": "post", "params": params,
            }}

        return {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
            {"component": "VRow", "props": {"no-gutters": True, "class": "fill-height"}, "content": [
                {"component": "VCol", "props": {"cols": 4, "md": 3}, "content": [
                    {"component": "VImg", "props": {"src": poster, "height": "150", "cover": True, "class": "rounded-l"}} if poster else
                    {"component": "div", "props": {"style": "height:150px;background:grey-lighten-3", "class": "rounded-l"}},
                ]},
                {"component": "VCol", "props": {"cols": 8, "md": 9, "class": "d-flex flex-column"}, "content": [
                    {"component": "VCardText", "props": {"class": "flex-grow-1 py-2"}, "content": [
                        {"component": "div", "props": {"class": "d-flex align-center mb-1"}, "content": [
                            {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold flex-grow-1 text-truncate"}, "text": title},
                            subscribe_btn,
                        ]},
                        {"component": "div", "props": {"class": "d-flex align-center mb-1 flex-wrap", "style": "gap:4px"}, "content": [
                            {"component": "VChip", "props": {"size": "x-small", "color": rating_color, "variant": "tonal"},
                             "text": f"★ {rating}" if rating else "暂无"},
                            {"component": "VChip", "props": {"size": "x-small", "color": "grey", "variant": "outlined"},
                             "text": air_date[:10] if air_date else "未知日期"},
                            {"component": "VBtn", "props": {
                                "size": "x-small", "variant": "text", "color": "orange",
                                "prepend-icon": "mdi-database-search", "target": "_blank",
                                "href": anime.get("mikan_link") or (
                                    f"https://www.themoviedb.org/{'tv' if anime.get('media_type') == 'tv' else 'movie'}/{tmdb_id}"
                                    if tmdb_id else f"https://bgm.tv/subject/{bangumi_id}" if bangumi_id else ""
                                ),
                                "disabled": not (anime.get("mikan_link") or tmdb_id or bangumi_id),
                            }, "text": "TMDB" if tmdb_id else ("蜜柑" if anime.get("mikan_link") else ("Bangumi" if bangumi_id else "详情"))},
                        ]},
                        {"component": "div", "props": {"class": "text-caption text-grey mt-1", "style": "line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden"},
                         "text": overview + "..." if len(overview) >= 80 else overview},
                    ]},
                ]},
            ]},
        ]}

    # ==================== 数据获取 ====================

    def _get_anime_list(self) -> List[Dict[str, Any]]:
        """获取当前视图的数据列表。"""
        now = time.time()
        # 缓存键按视图模式和对应数据源区分
        cache_key = f"{self._current_view}:{self._anime_source if self._current_view == 'anime' else self._movies_source}"
        if self._cache.get(cache_key) and (now - self._cache_time) < self._cache_ttl:
            cached_list = self._cache[cache_key]
            self._check_subscriptions(cached_list)
            return cached_list

        # 根据当前视图选择数据源
        if self._current_view == "movies":
            # 影视视图
            if self._movies_source == "auto":
                anime_list = self._fetch_movies_auto()
            elif self._movies_source == "douban_hot":
                anime_list = self._fetch_douban()
            else:
                anime_list = self._fetch_hot_movies()
        else:
            # 番剧视图
            if self._anime_source == "auto":
                anime_list = self._fetch_auto()
            elif self._anime_source == "mikan":
                anime_list = self._fetch_mikan()
            elif self._anime_source == "bangumi":
                anime_list = self._fetch_bangumi()
            elif self._anime_source == "anibk":
                anime_list = self._fetch_anibk()
            elif self._anime_source == "anibk_daily":
                anime_list = self._fetch_anibk_daily()
            else:
                anime_list = self._fetch_tmdb()

        if anime_list:
            self._check_subscriptions(anime_list)
            if self._min_rating > 0:
                anime_list = [a for a in anime_list if a.get("rating", 0) >= self._min_rating]
            if self._min_year > 0:
                anime_list = [a for a in anime_list if int(a.get("year", "0") or "0") >= self._min_year]

            # 每日推送通知
            if self._notify_new:
                today_weekday = datetime.now().isoweekday()
                today_items = []
                for a in anime_list:
                    ad = a.get("air_date", "")
                    if ad:
                        try:
                            ad_date = datetime.strptime(ad[:10], "%Y-%m-%d").date()
                            if ad_date.isoweekday() == today_weekday:
                                today_items.append(a)
                        except Exception:
                            pass

                if today_items:
                    today_items.sort(key=lambda a: a.get("rating", 0), reverse=True)
                    titles = "\n".join([f"· {a.get('title')} ★{a.get('rating', 0)}" for a in today_items[:20]])
                    label = "热门影视" if self._current_view == "movies" else "新番"
                    title_text = f"[{label}] 今日更新 ({len(today_items)}部)"
                else:
                    titles = "今日暂无更新"
                    title_text = "[当季新番与热门影视] 今日更新 (0部)"

                self.post_message(mtype=NotificationType.Manual, title=title_text, text=titles)
                logger.info(f"已推送通知，{len(today_items)}部，视图: {self._current_view}，时间: {datetime.now()}")

        self._cache[cache_key] = anime_list
        self._cache_time = now
        return anime_list

    def _get_season_range(self) -> Tuple[str, str]:
        """获取当前季度的日期范围。"""
        now = datetime.now()
        m, y = now.month, now.year
        if m <= 3: return f"{y}-01-01", f"{y}-03-31"
        elif m <= 6: return f"{y}-04-01", f"{y}-06-30"
        elif m <= 9: return f"{y}-07-01", f"{y}-09-30"
        else: return f"{y}-10-01", f"{y}-12-31"

    def _get_season_label(self) -> str:
        """获取当前季节的标签（如 2026年冬）。"""
        now = datetime.now()
        names = {1: "冬", 4: "春", 7: "夏", 10: "秋"}
        return f"{now.year}年{names.get(((now.month-1)//3)*3+1, '')}季"

    # ==================== 番剧数据源 ====================

    def _fetch_auto(self) -> List[Dict[str, Any]]:
        """自动整合多个番剧数据源。"""
        tmdb_list = self._fetch_tmdb()
        bangumi_list = self._fetch_bangumi()
        mikan_list = self._fetch_mikan()
        anibk_list = self._fetch_anibk()
        merged: Dict[str, Dict[str, Any]] = {}
        for a in tmdb_list:
            k = a.get("title", "").lower().strip()
            if k: merged[k] = a
        for a in bangumi_list:
            k = a.get("title", "").lower().strip()
            if k and k not in merged:
                merged[k] = a
            elif k and k in merged and not merged[k].get("mikan_link") and a.get("mikan_link"):
                merged[k]["mikan_link"] = a["mikan_link"]
        for a in mikan_list:
            k = a.get("title", "").lower().strip()
            if not k: continue
            if k in merged:
                if not merged[k].get("mikan_link"):
                    merged[k]["mikan_link"] = a.get("mikan_link", "")
            else:
                merged[k] = a
        for a in anibk_list:
            k = a.get("title", "").lower().strip()
            if not k: continue
            if k in merged:
                if not merged[k].get("anibk_link"):
                    merged[k]["anibk_link"] = a.get("anibk_link", "")
            else:
                merged[k] = a
        logger.info(f"自动整合: TMDB={len(tmdb_list)}, Bangumi={len(bangumi_list)}, 蜜柑={len(mikan_list)}, 番组百科={len(anibk_list)} → {len(merged)}")
        return list(merged.values())

    def _fetch_tmdb(self) -> List[Dict[str, Any]]:
        """从 TMDB 获取当季动画（TV动画和电影）。"""
        anime_list = []
        try:
            now = datetime.now()
            # TV动画：当季范围
            tv_gte, tv_lte = self._get_season_range()
            ru = RequestUtils(proxies=settings.PROXY)

            # 查询当季TV动画（只含动画类型，genre 16）
            tv_params = {"api_key": settings.TMDB_API_KEY, "language": "zh-CN", "with_genres": "16",
                         "first_air_date.gte": tv_gte, "first_air_date.lte": tv_lte,
                         "sort_by": "first_air_date.desc", "page": 1}
            tv_resp = ru.get("https://api.themoviedb.org/3/discover/tv", params=tv_params, timeout=30)
            if tv_resp:
                tv_data = json.loads(tv_resp)
                sl = self._get_season_label()
                for item in tv_data.get("results", [])[:50]:
                    anime_list.append({"title": item.get("name", ""), "year": str(item.get("first_air_date", "")[:4]) if item.get("first_air_date") else "", "air_date": item.get("first_air_date", ""), "season": sl, "rating": round(item.get("vote_average", 0), 1), "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "", "overview": item.get("overview", ""), "tmdb_id": item.get("id", ""), "popularity": round(item.get("popularity", 0) or 0, 2), "media_type": "tv", "subscribed": False})

            # 查询当季动画电影（genre 16 且是 movie）
            mov_gte, mov_lte = self._get_season_range()
            movie_params = {"api_key": settings.TMDB_API_KEY, "language": "zh-CN", "with_genres": "16",
                            "release_date.gte": mov_gte, "release_date.lte": mov_lte,
                            "sort_by": "release_date.desc", "page": 1}
            movie_resp = ru.get("https://api.themoviedb.org/3/discover/movie", params=movie_params, timeout=30)
            if movie_resp:
                movie_data = json.loads(movie_resp)
                for item in movie_data.get("results", [])[:50]:
                    anime_list.append({"title": item.get("title", ""), "year": str(item.get("release_date", "")[:4]) if item.get("release_date") else "", "air_date": item.get("release_date", ""), "season": "", "rating": round(item.get("vote_average", 0), 1), "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "", "overview": item.get("overview", ""), "tmdb_id": item.get("id", ""), "popularity": round(item.get("popularity", 0) or 0, 2), "media_type": "movie", "subscribed": False})
        except Exception as e:
            logger.error(f"TMDB 请求失败: {e}")
        return anime_list

    def _fetch_bangumi(self) -> List[Dict[str, Any]]:
        """从 Bangumi 获取本周放送的番剧。"""
        anime_list = []
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            resp = ru.get("https://api.bgm.tv/calendar", timeout=30)
            if resp:
                data = json.loads(resp)
                year = str(datetime.now().year)
                sl = self._get_season_label()
                for day_group in data:
                    weekday_info = day_group.get("weekday", {})
                    weekday_id = weekday_info.get("id", 0)
                    for item in day_group.get("items", []):
                        ad = item.get("air_date", "")
                        if ad and year in ad:
                            anime_list.append({
                                "title": item.get("name", ""),
                                "year": year,
                                "air_date": ad,
                                "air_weekday": weekday_id,
                                "season": sl,
                                "rating": round(item.get("rating", {}).get("score", 0), 1),
                                "poster": item.get("images", {}).get("large", ""),
                                "overview": item.get("summary", "")[:120],
                                "tmdb_id": "",
                                "bangumi_id": str(item.get("id", "")),
                                "subscribed": False,
                            })
        except Exception as e:
            logger.error(f"Bangumi 请求失败: {e}")
        return anime_list

    def _fetch_mikan(self) -> List[Dict[str, Any]]:
        """从蜜柑（mikanime.tv）获取当季番剧。"""
        anime_list = []
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            resp = ru.get("https://mikanime.tv/", timeout=30)
            if not resp: return []
            pattern = r'<a[^>]*href="(/Home/Bangumi/\d+)"[^>]*class="an-text"[^>]*title="([^"]*)"'
            matches = re.findall(pattern, resp)
            year = str(datetime.now().year)
            sl = self._get_season_label()
            seen = set()
            for link_path, raw_title in matches:
                title = html.unescape(raw_title).strip()
                if not title or title in seen: continue
                seen.add(title)
                mikan_id = link_path.split('/')[-1] if link_path else ""
                anime_list.append({"title": title, "year": year, "air_date": "", "season": sl, "rating": 0, "poster": "", "overview": f"蜜柑资源 · {title}", "tmdb_id": "", "bangumi_id": "", "mikan_link": f"https://mikanime.tv{link_path}", "mikan_id": mikan_id, "subscribed": False})
        except Exception as e:
            logger.error(f"蜜柑请求失败: {e}")
        return anime_list

    def _fetch_anibk(self) -> List[Dict[str, Any]]:
        """从番组百科（anibk.com）抓取当季新番列表。"""
        anime_list = []
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            now = datetime.now()
            year_code = now.year - 1968
            month = now.month
            if month <= 3:
                season_code = 1
            elif month <= 6:
                season_code = 2
            elif month <= 9:
                season_code = 3
            else:
                season_code = 4
            year_str = f"{year_code:02d}"
            url = f"https://www.anibk.com/bk/bk{year_str}{season_code}.html"
            resp = ru.get(url, timeout=30)
            if not resp:
                return []
            # 解析 HTML 提取番剧信息
            pattern = r'<div class="char-bk-sub">\s*<a[^>]*href="/bk/(\d+)"[^>]*title="([^"]*)"[^>]*>.*?</a>'
            matches = re.findall(pattern, resp, re.DOTALL)
            for anime_id, raw_title in matches:
                title = html.unescape(raw_title).strip()
                if not title:
                    continue
                anime_list.append({
                    "title": title,
                    "year": str(now.year),
                    "air_date": "",
                    "season": self._get_season_label(),
                    "rating": 0,
                    "poster": "",
                    "overview": f"番组百科 · {title}",
                    "tmdb_id": "",
                    "bangumi_id": "",
                    "anibk_link": f"https://www.anibk.com/bk/{anime_id}",
                    "anibk_id": anime_id,
                    "subscribed": False,
                })
        except Exception as e:
            logger.error(f"番组百科请求失败: {e}")
        return anime_list

    def _fetch_anibk_daily(self) -> List[Dict[str, Any]]:
        """从番组百科抓取「今日更新」列表。"""
        anime_list: List[Dict[str, Any]] = []
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            resp = ru.get("https://www.anibk.com/", timeout=30)
            if not resp:
                return []
            today_weekday = datetime.now().isoweekday()
            # 解析每周放送表
            pattern = r'<div class="char-bk-sub">\s*<a[^>]*href="/bk/(\d+)"[^>]*title="([^"]*)"[^>]*>.*?</a>\s*<span class="k">(.*?)</span>\s*<span class="v">(.*?)</span>'
            matches = re.findall(pattern, resp, re.DOTALL)
            for anime_id, raw_title, episode, air_time in matches:
                title = html.unescape(raw_title).strip()
                if not title:
                    continue
                episode = html.unescape(episode).strip()
                air_time = html.unescape(air_time).strip()
                anime_list.append({
                    "title": f"{title} {episode}" if episode else title,
                    "year": str(datetime.now().year),
                    "air_date": datetime.now().strftime("%Y-%m-%d"),
                    "season": self._get_season_label(),
                    "rating": 0,
                    "poster": "",
                    "overview": f"今日更新 · {title} {episode} · {air_time}",
                    "tmdb_id": "",
                    "bangumi_id": "",
                    "anibk_link": f"https://www.anibk.com/bk/{anime_id}",
                    "anibk_id": anime_id,
                    "air_weekday": today_weekday,
                    "subscribed": False,
                })
        except Exception as e:
            logger.error(f"番组百科每日更新请求失败: {e}")
        return anime_list

    # ==================== 影视数据源 ====================

    def _fetch_movies_auto(self) -> List[Dict[str, Any]]:
        """自动整合多个影视数据源：TMDB + 豆瓣 + TVDB。"""
        tmdb_list = self._fetch_hot_movies()
        douban_list = self._fetch_douban()
        tvdb_list = self._fetch_tvdb()

        merged: Dict[str, Dict[str, Any]] = {}

        # TMDB 优先（数据最全）
        for a in tmdb_list:
            k = a.get("title", "").lower().strip()
            if k:
                merged[k] = a

        # 豆瓣补充（有 tmdb_id 的优先，否则标题匹配）
        for a in douban_list:
            k = a.get("title", "").lower().strip()
            if not k:
                continue
            if k in merged:
                # 已有条目，补充豆瓣评分（如果 TMDB 评分缺失）
                if not merged[k].get("rating") and a.get("rating"):
                    merged[k]["rating"] = a["rating"]
            else:
                # 新条目
                if a.get("tmdb_id"):
                    merged[k] = a
                else:
                    # 尝试用标题匹配 TMDB（已在上面处理）
                    merged[k] = a

        # TVDB 补充（同理）
        for a in tvdb_list:
            k = a.get("title", "").lower().strip()
            if not k:
                continue
            if k in merged:
                if not merged[k].get("rating") and a.get("rating"):
                    merged[k]["rating"] = a["rating"]
            else:
                merged[k] = a

        logger.info(f"影视自动整合: TMDB={len(tmdb_list)}, 豆瓣={len(douban_list)}, TVDB={len(tvdb_list)} → {len(merged)}")
        return list(merged.values())

    def _fetch_douban(self) -> List[Dict[str, Any]]:
        """从豆瓣获取热门影视（解析公开页面）。"""
        anime_list = []
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            # 豆瓣电影热榜
            movie_url = "https://movie.douban.com/j/search_subjects?type=movie&tag=热门&page_limit=30&page_start=0"
            movie_resp = ru.get(movie_url, timeout=30)
            if movie_resp:
                try:
                    movie_data = json.loads(movie_resp)
                    for item in movie_data.get("subjects", [])[:30]:
                        title = item.get("title", "")
                        rate = item.get("rate", "")
                        rating = float(rate) if rate and rate != "" else 0
                        anime_list.append({
                            "title": title,
                            "year": "",
                            "air_date": "",
                            "season": "",
                            "rating": rating,
                            "poster": item.get("cover", ""),
                            "overview": "",
                            "tmdb_id": "",
                            "bangumi_id": "",
                            "douban_id": str(item.get("id", "")),
                            "media_type": "movie",
                            "subscribed": False,
                        })
                except Exception as e:
                    logger.warning(f"解析豆瓣电影失败: {e}")

            # 豆瓣剧集热榜
            tv_url = "https://movie.douban.com/j/search_subjects?type=tv&tag=热门&page_limit=30&page_start=0"
            tv_resp = ru.get(tv_url, timeout=30)
            if tv_resp:
                try:
                    tv_data = json.loads(tv_resp)
                    for item in tv_data.get("subjects", [])[:30]:
                        title = item.get("title", "")
                        rate = item.get("rate", "")
                        rating = float(rate) if rate and rate != "" else 0
                        anime_list.append({
                            "title": title,
                            "year": "",
                            "air_date": "",
                            "season": self._get_season_label(),
                            "rating": rating,
                            "poster": item.get("cover", ""),
                            "overview": "",
                            "tmdb_id": "",
                            "bangumi_id": "",
                            "douban_id": str(item.get("id", "")),
                            "media_type": "tv",
                            "subscribed": False,
                        })
                except Exception as e:
                    logger.warning(f"解析豆瓣剧集失败: {e}")
        except Exception as e:
            logger.error(f"豆瓣请求失败: {e}")
        return anime_list

    def _fetch_tvdb(self) -> List[Dict[str, Any]]:
        """从 TVDB 获取流行影视（需配置 API Key）。"""
        anime_list = []
        try:
            tvdb_key = getattr(settings, 'TVDB_API_KEY', '')
            if not tvdb_key:
                logger.warning("TVDB API Key 未配置，跳过 TVDB 数据源")
                return []

            ru = RequestUtils(proxies=settings.PROXY)
            # TVDB v4 API（需 Bearer token）
            # 1. 获取 token
            auth_url = "https://api4.thetvdb.com/v4/login"
            auth_payload = {"apikey": tvdb_key}
            auth_resp = ru.post(auth_url, json=auth_payload, timeout=30)
            if not auth_resp:
                logger.warning("TVDB 登录失败")
                return []

            try:
                auth_data = json.loads(auth_resp)
                token = auth_data.get("data", {}).get("token", "")
                if not token:
                    logger.warning("TVDB token 获取失败")
                    return []
            except Exception as e:
                logger.warning(f"解析 TVDB 登录响应失败: {e}")
                return []

            # 2. 获取流行剧集
            headers = {"Authorization": f"Bearer {token}"}
            tv_url = "https://api4.thetvdb.com/v4/lists/310/popular?page=0"
            tv_resp = ru.get(tv_url, headers=headers, timeout=30)
            if tv_resp:
                try:
                    tv_data = json.loads(tv_resp)
                    for item in tv_data.get("data", {}).get("objects", [])[:30]:
                        tvdb_id = item.get("id", "")
                        name = item.get("name", "")
                        year = item.get("year", "")
                        image = item.get("image_url", "")
                        anime_list.append({
                            "title": name,
                            "year": str(year) if year else "",
                            "air_date": "",
                            "season": self._get_season_label(),
                            "rating": 0,
                            "poster": image,
                            "overview": "",
                            "tmdb_id": "",
                            "tvdb_id": str(tvdb_id),
                            "media_type": "tv",
                            "subscribed": False,
                        })
                except Exception as e:
                    logger.warning(f"解析 TVDB 剧集失败: {e}")

            # 3. 获取流行电影
            movie_url = "https://api4.thetvdb.com/v4/lists/334/popular?page=0"
            movie_resp = ru.get(movie_url, headers=headers, timeout=30)
            if movie_resp:
                try:
                    movie_data = json.loads(movie_resp)
                    for item in movie_data.get("data", {}).get("objects", [])[:30]:
                        tvdb_id = item.get("id", "")
                        name = item.get("name", "")
                        year = item.get("year", "")
                        image = item.get("image_url", "")
                        anime_list.append({
                            "title": name,
                            "year": str(year) if year else "",
                            "air_date": "",
                            "season": "",
                            "rating": 0,
                            "poster": image,
                            "overview": "",
                            "tmdb_id": "",
                            "tvdb_id": str(tvdb_id),
                            "media_type": "movie",
                            "subscribed": False,
                        })
                except Exception as e:
                    logger.warning(f"解析 TVDB 电影失败: {e}")
        except Exception as e:
            logger.error(f"TVDB 请求失败: {e}")
        return anime_list

    def _fetch_movies_auto(self) -> List[Dict[str, Any]]:
        """影视自动整合：TMDB + 豆瓣热榜，按标题去重合并。"""
        tmdb_list = self._fetch_hot_movies()
        douban_list = self._fetch_douban()
        merged: Dict[str, Dict[str, Any]] = {}
        # TMDB 优先
        for a in tmdb_list:
            k = a.get("title", "").lower().strip()
            if k:
                merged[k] = a
        # 豆瓣补充
        for a in douban_list:
            k = a.get("title", "").lower().strip()
            if not k:
                continue
            if k not in merged:
                merged[k] = a
        logger.info(f"影视自动整合: TMDB={len(tmdb_list)}, 豆瓣={len(douban_list)} → {len(merged)}")
        return list(merged.values())

    def _fetch_douban(self) -> List[Dict[str, Any]]:
        """从豆瓣获取热榜（TV热榜 + 电影热榜 + 正在热映）。"""
        anime_list: List[Dict[str, Any]] = []
        try:
            from app.modules.douban import DoubanModule
            douban = DoubanModule()
            douban.init_module()

            def _parse_douban_item(item, media_type, source_label):
                """解析豆瓣 MediaInfo 为插件统一格式。"""
                douban_info = getattr(item, 'douban_info', {}) or {}
                rating = 0
                poster = ""
                overview = ""
                try:
                    rating_data = douban_info.get('rating') or {}
                    rating = round(float(rating_data.get('value', 0) or 0), 1)
                except Exception:
                    pass
                try:
                    pic = douban_info.get('pic') or {}
                    poster = pic.get('large') or pic.get('normal') or getattr(item, 'poster_path', '') or ""
                except Exception:
                    poster = getattr(item, 'poster_path', '') or ""
                try:
                    overview = douban_info.get('comment', '')[:100] or getattr(item, 'overview', '')[:100]
                except Exception:
                    overview = ""
                return {
                    "title": getattr(item, 'title', '') or "",
                    "year": str(getattr(item, 'year', '') or ""),
                    "air_date": "",
                    "season": "",
                    "rating": rating,
                    "poster": poster,
                    "overview": overview or f"{source_label} · {getattr(item, 'title', '')}",
                    "tmdb_id": getattr(item, 'tmdb_id', '') or "",
                    "popularity": rating,
                    "douban_id": getattr(item, 'douban_id', '') or "",
                    "media_type": media_type,
                    "subscribed": False,
                }

            # 豆瓣TV热榜
            try:
                tv_items = douban.tv_hot(page=1, count=30)
                for item in (tv_items or []):
                    anime_list.append(_parse_douban_item(item, "tv", "豆瓣TV热榜"))
            except Exception as e:
                logger.warning(f"豆瓣TV热榜获取失败: {e}")

            # 豆瓣电影热榜
            try:
                movie_items = douban.movie_hot(page=1, count=30)
                for item in (movie_items or []):
                    anime_list.append(_parse_douban_item(item, "movie", "豆瓣电影热榜"))
            except Exception as e:
                logger.warning(f"豆瓣电影热榜获取失败: {e}")

            # 正在热映
            try:
                showing_items = douban.movie_showing(page=1, count=20)
                for item in (showing_items or []):
                    anime_list.append(_parse_douban_item(item, "movie", "正在热映"))
            except Exception as e:
                logger.warning(f"正在热映获取失败: {e}")

            logger.info(f"豆瓣热榜: TV+电影+热映 共 {len(anime_list)} 条")
        except Exception as e:
            logger.error(f"豆瓣模块初始化失败: {e}")
        return anime_list

    def _fetch_hot_movies(self) -> List[Dict[str, Any]]:
        """从 TMDB 获取热门影视（TV + 电影，排除动画）。"""
        anime_list = []
        try:
            now = datetime.now()
            ru = RequestUtils(proxies=settings.PROXY)

            # TV剧集：过去1年内首播，排除动画（genre 16），按热度排序
            tv_gte = f"{now.year - 1}-01-01"
            tv_lte = now.strftime("%Y-%m-%d")
            tv_params = {"api_key": settings.TMDB_API_KEY, "language": "zh-CN", "without_genres": "16",
                         "first_air_date.gte": tv_gte, "first_air_date.lte": tv_lte,
                         "sort_by": "popularity.desc", "page": 1}
            tv_resp = ru.get("https://api.themoviedb.org/3/discover/tv", params=tv_params, timeout=30)
            if tv_resp:
                tv_data = json.loads(tv_resp)
                sl = self._get_season_label()
                for item in tv_data.get("results", [])[:50]:
                    anime_list.append({"title": item.get("name", ""), "year": str(item.get("first_air_date", "")[:4]) if item.get("first_air_date") else "", "air_date": item.get("first_air_date", ""), "season": sl, "rating": round(item.get("vote_average", 0), 1), "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "", "overview": item.get("overview", ""), "tmdb_id": item.get("id", ""), "popularity": round(item.get("popularity", 0) or 0, 2), "media_type": "tv", "subscribed": False})

            # 电影：当季上映，按上映日期排序
            mov_gte, mov_lte = self._get_season_range()
            movie_params = {"api_key": settings.TMDB_API_KEY, "language": "zh-CN",
                            "release_date.gte": mov_gte, "release_date.lte": mov_lte,
                            "sort_by": "release_date.desc", "page": 1}
            movie_resp = ru.get("https://api.themoviedb.org/3/discover/movie", params=movie_params, timeout=30)
            if movie_resp:
                movie_data = json.loads(movie_resp)
                for item in movie_data.get("results", [])[:50]:
                    anime_list.append({"title": item.get("title", ""), "year": str(item.get("release_date", "")[:4]) if item.get("release_date") else "", "air_date": item.get("release_date", ""), "season": "", "rating": round(item.get("vote_average", 0), 1), "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "", "overview": item.get("overview", ""), "tmdb_id": item.get("id", ""), "popularity": round(item.get("popularity", 0) or 0, 2), "media_type": "movie", "subscribed": False})
        except Exception as e:
            logger.error(f"TMDB 热门影视请求失败: {e}")
        return anime_list

    # ==================== AI 增强（可选） ====================

    def _enhance_with_llm(self, anime_list: List[Dict[str, Any]]) -> None:
        """使用大模型增强简介（通过 OpenAI 兼容 API）。"""
        if not settings.LLM_API_KEY:
            logger.debug("LLM API Key 未配置，跳过 AI 增强")
            return

        base_url = settings.LLM_BASE_URL or "https://api.deepseek.com"
        model = settings.LLM_MODEL or "deepseek-chat"

        batch_size = 5
        for i in range(0, len(anime_list), batch_size):
            batch = anime_list[i:i + batch_size]
            titles_batch = []
            for anime in batch:
                title = anime.get("title", "")
                overview = anime.get("overview", "")
                if title and not anime.get("llm_enhanced"):
                    titles_batch.append(f"{title}|||{overview[:150]}")

            if not titles_batch:
                continue

            prompt = f"""你是一个影视推荐助手。请为以下每部影视作品写一段30字以内的中文推荐语。
每行格式：标题|||推荐语
直接输出结果，不要加标题。

{chr(10).join(titles_batch)}"""

            try:
                url = f"{base_url.rstrip('/')}/chat/completions"
                headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}", "Content-Type": "application/json"}
                payload = {"model": model, "messages": [{"role": "system", "content": "你是影视推荐助手，回复简洁。"}, {"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 1024}

                ru = RequestUtils(proxies=settings.PROXY if settings.LLM_USE_PROXY else None)
                resp = ru.post(url, json=payload, headers=headers, timeout=30)
                if resp and hasattr(resp, "json"):
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    for line in content.strip().split("\n"):
                        if "|||" in line:
                            parts = line.split("|||", 1)
                            if len(parts) == 2:
                                r_title = parts[0].strip().lower()
                                r_overview = parts[1].strip()
                                for anime in batch:
                                    if anime.get("title", "").lower() == r_title and r_overview:
                                        anime["overview"] = r_overview[:100]
                                        anime["llm_enhanced"] = True
            except Exception as e:
                logger.debug(f"LLM 请求失败: {e}")
                break

    # ==================== 订阅检查 ====================

    def _check_subscriptions(self, anime_list: List[Dict[str, Any]]) -> None:
        """检查订阅状态，标记已订阅的番剧/影视。"""
        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe
            from app.db.models.subscribehistory import SubscribeHistory
            db = ScopedSession()
            try:
                subs = db.query(Subscribe).filter(Subscribe.state.in_(["R", "N"]), Subscribe.type.in_(["电视剧", "电影"])).all()
                sub_ids = {s.tmdbid for s in subs if s.tmdbid}
                sub_names = {s.name for s in subs if s.name}
                history_subs = db.query(SubscribeHistory).all()
                history_ids = {h.tmdbid for h in history_subs if h.tmdbid}
                for a in anime_list:
                    tmdb_id = a.get("tmdb_id")
                    if tmdb_id:
                        try:
                            tid = int(tmdb_id)
                            if tid in sub_ids or tid in history_ids:
                                a["subscribed"] = True
                                continue
                        except (ValueError, TypeError):
                            pass
                    mikan_id = a.get("mikan_id", "")
                    matched = False

                    if mikan_id:
                        try:
                            mikan_map = self.get_data("mikan_subscription_map") or {}
                            if mikan_id in mikan_map:
                                a["subscribed"] = True
                                matched = True
                        except Exception as e:
                            logger.warning(f"查询蜜柑映射失败: {e}")

                    if not matched:
                        def normalize_title(t):
                            return re.sub(r'[^\w\s]', '', t).lower().strip()

                        raw_title = a.get("title", "")
                        title = normalize_title(raw_title)
                        sub_names_normalized = {normalize_title(name): name for name in sub_names}
                        if title and title in sub_names_normalized:
                            a["subscribed"] = True
                            matched = True
                        else:
                            for sub_name in sub_names:
                                sub_normalized = normalize_title(sub_name)
                                if sub_normalized and (sub_normalized in title or title in sub_normalized):
                                    a["subscribed"] = True
                                    matched = True
                                    break
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"检查订阅失败: {e}")

    # ==================== API ====================

    def _refresh_data(self) -> dict:
        """清除缓存并重新获取数据。"""
        self._cache = {}
        self._cache_time = 0
        self._loading = False
        try:
            self.save_data("last_anime_titles", [])
        except Exception as e:
            logger.warning(f"清除持久化数据失败: {e}")

        try:
            data = self._get_anime_list()
            logger.info(f"数据刷新完成，视图: {self._current_view}，共 {len(data or [])} 条记录")
            return {"success": True, "count": len(data or [])}
        except Exception as e:
            logger.error(f"数据刷新失败: {e}")
            return {"success": False, "message": str(e)}

    def _switch_view(self, params: dict = None) -> dict:
        """切换番剧/影视视图（由详情页切换按钮调用）。"""
        try:
            new_view = params.get("view") if params else None
            if new_view not in ("anime", "movies"):
                return {"success": False, "message": "无效的视图模式"}

            old_view = self._current_view
            self._current_view = new_view

            # 持久化视图选择
            self.save_data("current_view", new_view)

            # 清除当前视图的缓存（切换后立即显示对应视图数据）
            cache_key = f"{new_view}:{self._anime_source if new_view == 'anime' else self._movies_source}"
            if cache_key in self._cache:
                del self._cache[cache_key]

            logger.info(f"视图切换: {old_view} -> {new_view}，已清除对应缓存")
            return {"success": True, "message": f"已切换到{'番剧' if new_view == 'anime' else '热门影视'}视图", "view": new_view}
        except Exception as e:
            logger.error(f"切换视图失败: {e}")
            return {"success": False, "message": str(e)}

    def _scheduled_refresh(self):
        """定时刷新（由调度器调用）。"""
        self._cache = {}; self._cache_time = 0
        self._get_anime_list()

    def _subscribe_anime(self, params: SubscribeParams) -> dict:
        """订阅番剧/影视。"""
        title = params.title
        year = params.year
        tmdb_id = params.tmdb_id
        bangumi_id = params.bangumi_id
        mikan_id = params.mikan_id
        if not title:
            return {"success": False, "message": "缺少标题"}
        logger.info(f"收到订阅请求: title={title}, year={year}, tmdb_id={tmdb_id}, bangumi_id={bangumi_id}, mikan_id={mikan_id}")

        mtype = MediaType.MOVIE if params.media_type == "movie" else MediaType.TV

        # 对电视剧，从 TMDB 获取最新季号
        season = None
        if mtype == MediaType.TV and tmdb_id:
            try:
                season = self._get_latest_season(int(tmdb_id))
            except Exception as e:
                logger.warning(f"获取季数异常，回退默认: {e}")

        # 检查该季是否已订阅或已完成
        if self._is_season_subscribed(tmdb_id, season):
            season_desc = f"第{season}季" if season else "已有"
            return {"success": False, "message": f"{title} {season_desc}已订阅或已完成，无需重复订阅"}

        try:
            sid, msg = SubscribeChain().add(
                title=title,
                year=year,
                mtype=mtype,
                tmdbid=int(tmdb_id) if tmdb_id else None,
                bangumiid=int(bangumi_id) if bangumi_id else None,
                season=season,
                message=True,
            )
            logger.info(f"订阅结果: sid={sid}, msg={msg}")
            if sid:
                self._cache = {}; self._cache_time = 0
                self._save_mikan_map(mikan_id, sid)
                season_info = f" 第{season}季" if season else ""
                return {"success": True, "message": f"已订阅 {title}{season_info}，{msg}"}
            else:
                logger.info(f"订阅失败，尝试TMDB搜索: {title}")
                try:
                    search_url = "https://api.themoviedb.org/3/search/multi"
                    api_params = {
                        "api_key": settings.TMDB_API_KEY,
                        "language": "zh-CN",
                        "query": title,
                        "page": 1
                    }
                    ru = RequestUtils(proxies=settings.PROXY)
                    resp = ru.get(search_url, params=api_params, timeout=10)
                    if resp:
                        data = json.loads(resp)
                        results = data.get("results", [])
                        logger.info(f"TMDB搜索结果: 标题='{title}', 找到{len(results)}个结果")
                        for result in results[:5]:
                            result_title = result.get("name") or result.get("title", "")
                            result_type = result.get("media_type", "")
                            result_id = result.get("id")
                            if title.lower() in result_title.lower() or result_title.lower() in title.lower():
                                logger.info(f"TMDB搜索到匹配: {result_title} (ID: {result_id}, 类型: {result_type})")
                                mtype2 = MediaType.MOVIE if result_type == "movie" else MediaType.TV
                                fallback_season = None
                                if mtype2 == MediaType.TV:
                                    try:
                                        fallback_season = self._get_latest_season(result_id)
                                    except Exception as e:
                                        logger.warning(f"回退搜索获取季数异常: {e}")
                                if self._is_season_subscribed(result_id, fallback_season):
                                    continue
                                sid2, msg2 = SubscribeChain().add(
                                    title=title,
                                    year=year,
                                    mtype=mtype2,
                                    tmdbid=result_id,
                                    season=fallback_season,
                                    message=True,
                                )
                                if sid2:
                                    self._cache = {}; self._cache_time = 0
                                    self._save_mikan_map(mikan_id, sid2)
                                    season_info = f" 第{fallback_season}季" if fallback_season else ""
                                    return {"success": True, "message": f"已订阅 {title}{season_info}（通过TMDB搜索匹配），{msg2}"}
                except Exception as e:
                    logger.warning(f"TMDB搜索异常: {e}")
                return {"success": False, "message": msg or "订阅失败"}
        except Exception as e:
            logger.warning(f"订阅异常: {e}")
            return {"success": False, "message": str(e)}

    def _get_latest_season(self, tmdb_id: int) -> Optional[int]:
        """从 TMDB 获取最新季号（排除 season 0 特别篇）。"""
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            resp = ru.get(url, params={"api_key": settings.TMDB_API_KEY, "language": "zh-CN"}, timeout=10)
            if resp:
                data = json.loads(resp)
                seasons = data.get("seasons", [])
                normal_seasons = [s for s in seasons if s.get("season_number", 0) > 0]
                if normal_seasons:
                    latest = max(s.get("season_number", 1) for s in normal_seasons)
                    logger.info(f"TMDB 最新季号: tmdbid={tmdb_id} season={latest}")
                    return latest
                return 1
        except Exception as e:
            logger.warning(f"获取季数失败: {e}")
        return None

    def _is_season_subscribed(self, tmdb_id: Optional[Union[str, int]], season: Optional[int]) -> bool:
        """检查指定季是否已有活跃订阅或已完成订阅。"""
        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe
            from app.db.models.subscribehistory import SubscribeHistory
            db = ScopedSession()
            try:
                if tmdb_id:
                    tid = int(tmdb_id)
                    active = db.query(Subscribe).filter(
                        Subscribe.tmdbid == tid,
                        Subscribe.state.in_(["R", "N"]),
                    ).all()
                    if season is not None:
                        if any(s.season == season for s in active):
                            logger.info(f"第{season}季已有活跃订阅: tmdbid={tid}")
                            return True
                    elif active:
                        logger.info(f"已存在活跃订阅: tmdbid={tid}")
                        return True
                    history = db.query(SubscribeHistory).filter(
                        SubscribeHistory.tmdbid == tid,
                    ).all()
                    if season is not None:
                        if any(h.season == season for h in history):
                            logger.info(f"第{season}季已完成订阅: tmdbid={tid}")
                            return True
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"检查订阅状态失败: {e}")
        return False

    def _save_mikan_map(self, mikan_id, sid):
        """保存蜜柑ID到订阅ID的映射。"""
        if not mikan_id:
            return
        try:
            mikan_map = self.get_data("mikan_subscription_map") or {}
            mikan_map[str(mikan_id)] = sid
            self.save_data("mikan_subscription_map", mikan_map)
        except Exception as e:
            logger.warning(f"保存蜜柑映射失败: {e}")

    def _unsubscribe_anime(self, params: SubscribeParams) -> dict:
        """取消订阅番剧/影视。"""
        title = params.title
        tmdb_id = params.tmdb_id
        if not title and not tmdb_id:
            return {"success": False, "message": "缺少标题或TMDB ID"}
        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe
            db = ScopedSession()
            try:
                if tmdb_id:
                    deleted = db.query(Subscribe).filter(Subscribe.tmdbid == int(tmdb_id)).delete()
                else:
                    deleted = db.query(Subscribe).filter(Subscribe.name == title).delete()
                db.commit()
                if deleted:
                    self._cache = {}; self._cache_time = 0
                    if params.mikan_id:
                        try:
                            mikan_map = self.get_data("mikan_subscription_map") or {}
                            if params.mikan_id in mikan_map:
                                del mikan_map[params.mikan_id]
                                self.save_data("mikan_subscription_map", mikan_map)
                        except Exception as e:
                            logger.warning(f"删除蜜柑映射失败: {e}")
                    return {"success": True, "message": f"已取消订阅: {title or tmdb_id}"}
                else:
                    return {"success": False, "message": "未找到订阅记录"}
            finally:
                db.close()
        except Exception as e:
            logger.error(f"订阅异常: {e}")
            return {"success": False, "message": str(e)}

    def _reset_notify_date(self) -> dict:
        """重置通知日期，允许今天再次推送。"""
        try:
            self._last_notify_date = ""
            self.save_data("last_notify_date", "")
            logger.info("已重置通知日期")
            return {"success": True, "message": "已重置通知日期，今天可以再次推送"}
        except Exception as e:
            logger.error(f"重置通知日期失败: {e}")
            return {"success": False, "message": str(e)}

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running: self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败: {e}")
