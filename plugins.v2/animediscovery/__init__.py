"""
当季新番发现插件

从 TMDB / Bangumi / 蜜柑 获取当季新番列表，
按日期分组排列，一键订阅追番。
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


class AnimeDiscovery(_PluginBase):
    """当季新番发现插件。"""

    plugin_name = "当季新番"
    plugin_desc = "发现当季新番，按日期分组，一键订阅追番。"
    plugin_icon = "mdi-play-circle"
    plugin_version = "2.9.0"
    plugin_label = "订阅"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "anime_discovery_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _data_source = "auto"
    _min_rating = 0.0
    _auto_refresh = ""
    _notify_new = False
    _search_keyword = ""
    _hide_subscribed = False
    _cache: Dict[str, Any] = {}
    _cache_time: float = 0
    _cache_ttl: int = 3600
    _scheduler: Optional[BackgroundScheduler] = None  # deprecated, kept for stop_service compat
    _last_notify_date: str = ""  # 上次通知日期，防止重复推送（持久化）

    def init_plugin(self, config: dict = None) -> None:
        self.stop_service()
        self._enabled = False
        self._data_source = "auto"
        self._min_rating = 0.0
        self._min_year = 0
        self._auto_refresh = ""
        self._notify_new = False
        self._last_notify_date = ""
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._data_source = str(config.get("data_source") or "auto")
        self._min_rating = float(config.get("min_rating") or 0.0)
        self._min_year = int(config.get("min_year") or 0)
        self._auto_refresh = str(config.get("auto_refresh") or "")
        self._notify_new = bool(config.get("notify_new"))

        # 从持久化数据恢复上次通知日期，防止重启后重复推送
        try:
            saved = self.get_data("last_notify_date")
            if saved:
                self._last_notify_date = str(saved)
        except Exception:
            pass

        # 调度由 get_service 统一注册到 MP 系统调度器，不再自建 BackgroundScheduler

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/refresh", "endpoint": self._refresh_data, "methods": ["GET"], "summary": "刷新数据", "auth": "bear"},
            {"path": "/subscribe", "endpoint": self._subscribe_anime, "methods": ["POST"], "summary": "订阅番剧", "auth": "bear"},
            {"path": "/unsubscribe", "endpoint": self._unsubscribe_anime, "methods": ["POST"], "summary": "取消订阅", "auth": "bear"},
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """返回定时刷新调度服务，cron 表达式由用户配置。"""
        if not self._auto_refresh:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._auto_refresh)
        except Exception as e:
            logger.error(f"自动刷新 cron 表达式无效: {self._auto_refresh}，{e}")
            return []
        return [{"id": "AnimeDiscoveryRefresh", "name": "当季新番自动刷新", "trigger": trigger, "func": self._scheduled_refresh, "kwargs": {}}]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return [
            {"component": "VForm", "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VSelect", "props": {
                            "model": "data_source", "label": "数据源",
                            "items": [
                                {"title": "自动整合（推荐）", "value": "auto"},
                                {"title": "TMDB", "value": "tmdb"},
                                {"title": "Bangumi", "value": "bangumi"},
                                {"title": "蜜柑", "value": "mikan"},
                            ],
                        }}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VTextField", "props": {"model": "min_rating", "label": "最低评分", "type": "number", "hint": "0=全部"}},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VTextField", "props": {"model": "min_year", "label": "最早年份", "type": "number", "hint": "0=全部，如2010"}},
                    ]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VTextField", "props": {
                            "model": "auto_refresh", "label": "自动刷新 Cron 表达式",
                            "hint": "留空=关闭。示例: 0 10 * * * (每天10点), 0 */6 * * * (每6小时), 0 8,20 * * * (每天8点和20点)",
                            "placeholder": "0 10 * * *",
                            "density": "compact", "hide-details": False,
                        }},
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VSwitch", "props": {"model": "notify_new", "label": "新番发现时通知"}},
                    ]},
                ]},
            ]}
        ], {"enabled": False, "data_source": "auto", "min_rating": 0.0, "min_year": 0, "auto_refresh": "", "notify_new": False}

    # ==================== 页面渲染 ====================

    def get_page(self) -> Optional[List[dict]]:
        if not self._enabled:
            return None
        api_token = settings.API_TOKEN
        refresh_api = f"plugin/AnimeDiscovery/refresh?apikey={api_token}"
        data = self._get_anime_list()
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

        # 按媒体类型分组
        from collections import OrderedDict
        tv_list = [a for a in data if a.get("media_type", "tv") == "tv"]
        movie_list = [a for a in data if a.get("media_type", "tv") == "movie"]
        
        # TV动画按星期分组
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        tv_dated: OrderedDict = OrderedDict()
        tv_undated = []
        for anime in tv_list:
            ad = anime.get("air_date", "")
            try:
                dt = datetime.strptime(ad, "%Y-%m-%d")
                dk = weekdays[dt.weekday()]
                tv_dated.setdefault(dk, []).append(anime)
            except Exception:
                tv_undated.append(anime)
        
        # 按星期顺序排序
        sorted_tv_dated: OrderedDict = OrderedDict()
        for dk in weekdays:
            if dk in tv_dated:
                sorted_tv_dated[dk] = tv_dated[dk]
        tv_dated = sorted_tv_dated
        
        # 电影/OVA/剧场版按日期分组（或简单列表）
        movie_dated: OrderedDict = OrderedDict()
        movie_undated = []
        for anime in movie_list:
            ad = anime.get("air_date", "")
            try:
                dt = datetime.strptime(ad, "%Y-%m-%d")
                dk = dt.strftime("%Y年%m月")
                movie_dated.setdefault(dk, []).append(anime)
            except Exception:
                movie_undated.append(anime)
        
        # 按月份从新到旧排序
        sorted_movie_dated: OrderedDict = OrderedDict()
        for dk in sorted(movie_dated.keys(), reverse=True):
            sorted_movie_dated[dk] = movie_dated[dk]
        movie_dated = sorted_movie_dated
        
        total = len(data)
        tv_count = len(tv_list)
        movie_count = len(movie_list)
        sub_count = sum(1 for a in data if a.get("subscribed"))

        page = [
            # 统计
            {"component": "VRow", "props": {"class": "mb-2"}, "content": [
                {"component": "VCol", "props": {"cols": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "color": "primary"}, "content": [
                        {"component": "VCardText", "props": {"class": "text-center py-2"}, "content": [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(tv_count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": "TV动画"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "color": "orange"}, "content": [
                        {"component": "VCardText", "props": {"class": "text-center py-2"}, "content": [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(movie_count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": "电影/OVA"},
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
                    {"component": "VTextField", "props": {"model": "search", "label": "搜索番名", "density": "compact", "clearable": True, "hide-details": True, "prepend-inner-icon": "mdi-magnify"}},
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

        # 渲染TV动画分组
        if tv_dated or tv_undated:
            page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                {"component": "VChip", "props": {"color": "primary", "variant": "flat", "size": "small", "class": "mr-2"}, "text": "TV动画"},
                {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{tv_count} 部"},
            ]})
            
            # 按星期分组
            for dk, animes in tv_dated.items():
                page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                    {"component": "VChip", "props": {"color": "blue", "variant": "outlined", "size": "x-small", "class": "mr-2"}, "text": dk},
                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{len(animes)} 部"},
                ]})
                cols = [self._build_anime_card(a, api_token) for a in animes]
                if len(cols) > 1:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i]]} for i in range(0, len(cols), 2)
                    ]})
                elif cols:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [cols[0]]},
                    ]})
            
            # 无日期的TV动画
            if tv_undated:
                page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                    {"component": "VChip", "props": {"color": "grey", "variant": "outlined", "size": "x-small", "class": "mr-2"}, "text": "其他"},
                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{len(tv_undated)} 部"},
                ]})
                cols = [self._build_anime_card(a, api_token) for a in tv_undated]
                if len(cols) > 1:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i]]} for i in range(0, len(cols), 2)
                    ]})
                elif cols:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [cols[0]]},
                    ]})
        
        # 渲染电影/OVA/剧场版分组
        if movie_dated or movie_undated:
            page.append({"component": "VDivider", "props": {"class": "my-3"}})
            page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                {"component": "VChip", "props": {"color": "orange", "variant": "flat", "size": "small", "class": "mr-2"}, "text": "电影/OVA/剧场版"},
                {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{movie_count} 部"},
            ]})
            
            # 按月份分组
            for dk, animes in movie_dated.items():
                page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                    {"component": "VChip", "props": {"color": "orange", "variant": "outlined", "size": "x-small", "class": "mr-2"}, "text": dk},
                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{len(animes)} 部"},
                ]})
                cols = [self._build_anime_card(a, api_token) for a in animes]
                if len(cols) > 1:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i]]} for i in range(0, len(cols), 2)
                    ]})
                elif cols:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [cols[0]]},
                    ]})
            
            # 无日期的电影/OVA
            if movie_undated:
                page.append({"component": "div", "props": {"class": "d-flex align-center mb-1 mt-2"}, "content": [
                    {"component": "VChip", "props": {"color": "grey", "variant": "outlined", "size": "x-small", "class": "mr-2"}, "text": "其他"},
                    {"component": "div", "props": {"class": "text-caption text-grey"}, "text": f"{len(movie_undated)} 部"},
                ]})
                cols = [self._build_anime_card(a, api_token) for a in movie_undated]
                if len(cols) > 1:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [cols[i]]} for i in range(0, len(cols), 2)
                    ]})
                elif cols:
                    page.append({"component": "VRow", "props": {"dense": True}, "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [cols[0]]},
                    ]})
        
        return page

    def _build_anime_card(self, anime: Dict[str, Any], api_token: str) -> dict:
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
        if tmdb_id: params["tmdb_id"] = tmdb_id
        if bangumi_id: params["bangumi_id"] = bangumi_id
        if anime.get("mikan_id"): params["mikan_id"] = anime["mikan_id"]
        
        if subscribed:
            # 已订阅状态，点击取消订阅
            subscribe_btn["events"] = {"click": {
                "api": f"plugin/AnimeDiscovery/unsubscribe?apikey={api_token}",
                "method": "post", "params": params,
            }}
        else:
            # 未订阅状态，点击订阅
            subscribe_btn["events"] = {"click": {
                "api": f"plugin/AnimeDiscovery/subscribe?apikey={api_token}",
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
                                "href": anime.get("mikan_link") or f"https://mikanime.tv/Home/Search?searchstr={quote(title)}",
                            }, "text": "蜜柑"},
                        ]},
                        {"component": "div", "props": {"class": "text-caption text-grey mt-1", "style": "line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden"},
                         "text": overview + "..." if len(overview) >= 80 else overview},
                    ]},
                ]},
            ]},
        ]}

    # ==================== 数据获取 ====================

    def _get_anime_list(self) -> List[Dict[str, Any]]:
        now = time.time()
        if self._cache.get("anime_list") and (now - self._cache_time) < self._cache_ttl:
            return self._cache["anime_list"]

        if self._data_source == "auto":
            anime_list = self._fetch_auto()
        elif self._data_source == "mikan":
            anime_list = self._fetch_mikan()
        elif self._data_source == "bangumi":
            anime_list = self._fetch_bangumi()
        else:
            anime_list = self._fetch_tmdb()

        if anime_list:
            self._check_subscriptions(anime_list)
            if self._min_rating > 0:
                anime_list = [a for a in anime_list if a.get("rating", 0) >= self._min_rating]
            if self._min_year > 0:
                anime_list = [a for a in anime_list if int(a.get("year", "0") or "0") >= self._min_year]

            # 新番通知（每天最多推送一次）
            if self._notify_new:
                today = datetime.now().strftime("%Y-%m-%d")
                if self._last_notify_date == today:
                    logger.info("今日已推送过新番通知，跳过")
                else:
                    # 用持久化的上次列表做比对，避免缓存过期导致全量推送
                    saved_list = self.get_data("last_anime_titles") or []
                    saved_titles = set(saved_list)
                    new_anime = [a for a in anime_list if a.get("title") and a["title"] not in saved_titles]
                    if new_anime:
                        def get_air_desc(ad):
                            if not ad:
                                return "未知"
                            try:
                                ad_date = datetime.strptime(ad[:10], "%Y-%m-%d").date()
                                today_date = datetime.now().date()
                                diff = (ad_date - today_date).days
                                if diff == 0:
                                    return "今天播出"
                                elif diff == 1:
                                    return "明天"
                                elif 2 <= diff <= 6:
                                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                                    return weekdays[ad_date.weekday()]
                                else:
                                    return ad_date.strftime("%m-%d")
                            except:
                                return "未知"
                        
                        titles = "\n".join([f"· {a.get('title')} ★{a.get('rating', 0)} | {get_air_desc(a.get('air_date', ''))}" for a in new_anime[:5]])
                        title_text = f"{today} 新番更新 (+{len(new_anime)})"
                        self.post_message(mtype=NotificationType.Manual, title=title_text, text=titles)
                        self._last_notify_date = today
                        try:
                            self.save_data("last_notify_date", today)
                        except Exception:
                            pass
                        logger.info(f"已推送新番通知，今日日期: {today}")
                    # 无论是否推送，都更新持久化列表
                    try:
                        self.save_data("last_anime_titles", [a.get("title", "") for a in anime_list if a.get("title")])
                    except Exception:
                        pass

        self._cache["anime_list"] = anime_list
        self._cache_time = now
        return anime_list

    def _get_season_range(self) -> Tuple[str, str]:
        now = datetime.now()
        m, y = now.month, now.year
        if m <= 3: return f"{y}-01-01", f"{y}-03-31"
        elif m <= 6: return f"{y}-04-01", f"{y}-06-30"
        elif m <= 9: return f"{y}-07-01", f"{y}-09-30"
        else: return f"{y}-10-01", f"{y}-12-31"

    def _get_season_label(self) -> str:
        now = datetime.now()
        names = {1: "冬", 4: "春", 7: "夏", 10: "秋"}
        return f"{now.year}年{names.get(((now.month-1)//3)*3+1, '')}季"

    # ==================== 数据源 ====================

    def _fetch_auto(self) -> List[Dict[str, Any]]:
        tmdb_list = self._fetch_tmdb()
        bangumi_list = self._fetch_bangumi()
        mikan_list = self._fetch_mikan()
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
        logger.info(f"自动整合: TMDB={len(tmdb_list)}, Bangumi={len(bangumi_list)}, 蜜柑={len(mikan_list)} → {len(merged)}")
        return list(merged.values())

    def _fetch_tmdb(self) -> List[Dict[str, Any]]:
        anime_list = []
        try:
            gte, lte = self._get_season_range()
            ru = RequestUtils(proxies=settings.PROXY)
            
            # 查询TV动画
            tv_params = {"api_key": settings.TMDB_API_KEY, "language": "zh-CN", "with_genres": "16", "with_original_language": "ja", "first_air_date.gte": gte, "first_air_date.lte": lte, "sort_by": "popularity.desc", "page": 1}
            tv_resp = ru.get("https://api.themoviedb.org/3/discover/tv", params=tv_params, timeout=30)
            if tv_resp:
                tv_data = json.loads(tv_resp)
                sl = self._get_season_label()
                for item in tv_data.get("results", [])[:30]:
                    anime_list.append({"title": item.get("name", ""), "year": str(item.get("first_air_date", "")[:4]) if item.get("first_air_date") else "", "air_date": item.get("first_air_date", ""), "season": sl, "rating": round(item.get("vote_average", 0), 1), "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "", "overview": item.get("overview", ""), "tmdb_id": item.get("id", ""), "media_type": "tv", "subscribed": False})
            
            # 查询动画电影
            movie_params = {"api_key": settings.TMDB_API_KEY, "language": "zh-CN", "with_genres": "16", "with_original_language": "ja", "release_date.gte": gte, "release_date.lte": lte, "sort_by": "popularity.desc", "page": 1}
            movie_resp = ru.get("https://api.themoviedb.org/3/discover/movie", params=movie_params, timeout=30)
            if movie_resp:
                movie_data = json.loads(movie_resp)
                for item in movie_data.get("results", [])[:20]:
                    anime_list.append({"title": item.get("title", ""), "year": str(item.get("release_date", "")[:4]) if item.get("release_date") else "", "air_date": item.get("release_date", ""), "season": "", "rating": round(item.get("vote_average", 0), 1), "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "", "overview": item.get("overview", ""), "tmdb_id": item.get("id", ""), "media_type": "movie", "subscribed": False})
        except Exception as e:
            logger.error(f"TMDB 请求失败: {e}")
        return anime_list

    def _fetch_bangumi(self) -> List[Dict[str, Any]]:
        anime_list = []
        try:
            ru = RequestUtils(proxies=settings.PROXY)
            resp = ru.get("https://api.bgm.tv/calendar", timeout=30)
            if resp:
                data = json.loads(resp)
                year = str(datetime.now().year)
                sl = self._get_season_label()
                for item in data:
                    ad = item.get("date", "")
                    if ad and year in ad:
                        anime_list.append({"title": item.get("name", ""), "year": year, "air_date": ad, "season": sl, "rating": round(item.get("rating", {}).get("score", 0), 1), "poster": item.get("images", {}).get("large", ""), "overview": item.get("summary", "")[:120], "tmdb_id": "", "bangumi_id": str(item.get("id", "")), "subscribed": False})
        except Exception as e:
            logger.error(f"Bangumi 请求失败: {e}")
        return anime_list

    def _fetch_mikan(self) -> List[Dict[str, Any]]:
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
                # 从link_path提取蜜柑ID
                mikan_id = link_path.split('/')[-1] if link_path else ""
                anime_list.append({"title": title, "year": year, "air_date": "", "season": sl, "rating": 0, "poster": "", "overview": f"蜜柑资源 · {title}", "tmdb_id": "", "bangumi_id": "", "mikan_link": f"https://mikanime.tv{link_path}", "mikan_id": mikan_id, "subscribed": False})
        except Exception as e:
            logger.error(f"蜜柑请求失败: {e}")
        return anime_list

    # ==================== AI 增强 ====================

    def _enhance_with_llm(self, anime_list: List[Dict[str, Any]]) -> None:
        """使用大模型增强番剧简介（通过 OpenAI 兼容 API）。"""
        if not settings.LLM_API_KEY:
            logger.debug("LLM API Key 未配置，跳过 AI 增强")
            return

        base_url = settings.LLM_BASE_URL or "https://api.deepseek.com"
        model = settings.LLM_MODEL or "deepseek-chat"

        # 批量处理（每批5个，减少请求次数）
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

            prompt = f"""你是一个动漫推荐助手。请为以下每部番剧写一段30字以内的中文推荐语。
每行格式：番名|||推荐语
直接输出结果，不要加标题。

{chr(10).join(titles_batch)}"""

            try:
                url = f"{base_url.rstrip('/')}/chat/completions"
                headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}", "Content-Type": "application/json"}
                payload = {"model": model, "messages": [{"role": "system", "content": "你是动漫推荐助手，回复简洁。"}, {"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 1024}

                ru = RequestUtils(proxies=settings.PROXY if settings.LLM_USE_PROXY else None)
                resp = ru.post(url, json=payload, headers=headers, timeout=30)
                if resp and hasattr(resp, "json"):
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    # 解析结果
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
                break  # 失败时停止后续批次

    # ==================== 订阅检查 ====================

    def _check_subscriptions(self, anime_list: List[Dict[str, Any]]) -> None:
        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe
            db = ScopedSession()
            try:
                # R=运行中, N=新建待调度，都算已订阅
                subs = db.query(Subscribe).filter(Subscribe.state.in_(["R", "N"]), Subscribe.type.in_(["电视剧", "电影"])).all()
                sub_ids = {s.tmdbid for s in subs if s.tmdbid}
                sub_names = {s.name for s in subs if s.name}
                for a in anime_list:
                    # 按 tmdb_id 匹配
                    tmdb_id = a.get("tmdb_id")
                    if tmdb_id:
                        try:
                            if int(tmdb_id) in sub_ids:
                                a["subscribed"] = True
                                continue
                        except (ValueError, TypeError):
                            pass
                    # 混合匹配策略：TMDB ID > 蜜柑ID > 智能标题匹配
                    mikan_id = a.get("mikan_id", "")
                    matched = False
                    
                    # 1. 蜜柑ID匹配（通过本地映射表）
                    if mikan_id:
                        try:
                            mikan_map = self.get_data("mikan_subscription_map") or {}
                            if mikan_id in mikan_map:
                                a["subscribed"] = True
                                matched = True
                                logger.info(f"蜜柑ID匹配成功: mikan_id={mikan_id} -> subscribe_id={mikan_map[mikan_id]}")
                        except Exception as e:
                            logger.warning(f"查询蜜柑映射失败: {e}")
                    
                    # 2. 智能标题匹配（如果蜜柑ID未匹配）
                    if not matched:
                        def normalize_title(t):
                            # 去除标点符号和特殊字符，只保留字母数字和中文
                            return re.sub(r'[^\w\s]', '', t).lower().strip()
                        
                        raw_title = a.get("title", "")
                        title = normalize_title(raw_title)
                        sub_names_normalized = {normalize_title(name): name for name in sub_names}
                        if title and title in sub_names_normalized:
                            a["subscribed"] = True
                            matched = True
                            logger.info(f"标题匹配成功: 原始='{raw_title}', 标准化='{title}' -> 订阅='{sub_names_normalized[title]}'")
                        else:
                            logger.info(f"标题匹配失败: 原始='{raw_title}', 标准化='{title}', 可用订阅标题: {list(sub_names_normalized.keys())[:5]}")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"检查订阅失败: {e}")

    # ==================== API ====================

    def _refresh_data(self) -> dict:
        self._cache = {}; self._cache_time = 0
        data = self._get_anime_list()
        return {"success": True, "count": len(data or [])}

    def _scheduled_refresh(self):
        self._cache = {}; self._cache_time = 0
        self._get_anime_list()

    def _subscribe_anime(self, params: SubscribeParams) -> dict:
        title = params.title
        year = params.year
        tmdb_id = params.tmdb_id
        bangumi_id = params.bangumi_id
        if not title: return {"success": False, "message": "缺少标题"}
        logger.info(f"收到订阅请求: title={title}, year={year}, tmdb_id={tmdb_id}, bangumi_id={bangumi_id}")
        try:
            # 使用 MP 订阅系统，支持自动搜刮下载
            # 根据媒体类型选择订阅类型
            if params.media_type == "movie":
                mtype = MediaType.MOVIE
                season = None
            else:
                mtype = MediaType.TV
                season = 1
            
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
                # 保存蜜柑ID到本地映射表
                if params.mikan_id:
                    try:
                        mikan_map = self.get_data("mikan_subscription_map") or {}
                        mikan_map[params.mikan_id] = sid
                        self.save_data("mikan_subscription_map", mikan_map)
                    except Exception as e:
                        logger.warning(f"保存蜜柑映射失败: {e}")
                return {"success": True, "message": f"已订阅 {title}，{msg}"}
            else:
                return {"success": False, "message": msg or "订阅失败"}
        except Exception as e:
            logger.warning(f"订阅异常: {e}")
            return {"success": False, "message": str(e)}

    def _unsubscribe_anime(self, params: SubscribeParams) -> dict:
        title = params.title
        tmdb_id = params.tmdb_id
        if not title and not tmdb_id:
            return {"success": False, "message": "缺少标题或TMDB ID"}
        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe
            db = ScopedSession()
            try:
                # 根据tmdb_id或标题删除订阅
                if tmdb_id:
                    deleted = db.query(Subscribe).filter(Subscribe.tmdbid == int(tmdb_id)).delete()
                else:
                    deleted = db.query(Subscribe).filter(Subscribe.name == title).delete()
                db.commit()
                if deleted:
                    self._cache = {}; self._cache_time = 0
                    # 删除蜜柑ID映射
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
            return {"success": False, "message": str(e)}

    def stop_service(self) -> None:
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running: self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败: {e}")
