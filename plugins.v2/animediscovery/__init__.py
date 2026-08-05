"""
当季新番发现插件

从 TMDB/Bangumi 获取当季新番列表，展示评分、海报、简介，
并从已配置站点（如 MiKan）查询资源可用性，支持一键订阅。
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils


class AnimeDiscovery(_PluginBase):
    """当季新番发现插件。"""

    plugin_name = "当季新番"
    plugin_desc = "发现当季新番，查看评分与站点资源，一键订阅追番。"
    plugin_icon = "mdi-play-circle"
    plugin_version = "1.0.0"
    plugin_label = "订阅"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "anime_discovery_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _site_id: int = 0  # 用于查询资源的站点 ID（0=自动选择）
    _min_rating: float = 0.0  # 最低评分过滤
    _cache: Dict[str, Any] = {}
    _cache_time: float = 0
    _cache_ttl: int = 3600  # 缓存 1 小时

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._site_id = 0
        self._min_rating = 0.0
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._site_id = int(config.get("site_id") or 0)
        self._min_rating = float(config.get("min_rating") or 0.0)

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
            {
                "path": "/refresh",
                "endpoint": self._refresh_data,
                "methods": ["GET"],
                "summary": "刷新当季新番数据",
                "auth": "bear",
            },
            {
                "path": "/subscribe",
                "endpoint": self._subscribe_anime,
                "methods": ["POST"],
                "summary": "订阅一部番剧",
                "auth": "bear",
            },
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_id",
                                            "label": "资源查询站点 ID",
                                            "hint": "填 0 自动选择，或填具体站点 ID（如 MiKan 的 ID）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_rating",
                                            "label": "最低评分",
                                            "hint": "低于此评分的番剧不显示，0=显示全部",
                                            "persistent-hint": True,
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {"enabled": False, "site_id": 0, "min_rating": 0.0}

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None

        api_token = settings.API_TOKEN
        refresh_api = f"plugin/AnimeDiscovery/refresh?token={api_token}"

        # 获取数据
        data = self._get_anime_list()
        if not data:
            return [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-center pa-4"},
                                    "content": [
                                        {
                                            "component": "VIcon",
                                            "props": {"size": "48", "color": "grey", "class": "mb-2"},
                                            "text": "mdi-loading",
                                        },
                                        {
                                            "component": "div",
                                            "props": {"class": "text-body-1 text-grey"},
                                            "text": "暂无数据，点击下方按钮刷新",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "text-center mt-4"},
                    "content": [
                        {
                            "component": "VBtn",
                            "props": {
                                "color": "primary",
                                "variant": "tonal",
                                "prepend-icon": "mdi-refresh",
                            },
                            "text": "刷新当季新番",
                            "events": {
                                "click": {
                                    "api": refresh_api,
                                    "method": "get",
                                }
                            },
                        }
                    ],
                },
            ]

        # 构建番剧卡片列表
        cards = []
        for anime in data:
            title = anime.get("title", "未知")
            rating = anime.get("rating", 0)
            poster = anime.get("poster", "")
            overview = anime.get("overview", "")[:120]
            year = anime.get("year", "")
            season = anime.get("season", "")
            tmdb_id = anime.get("tmdb_id", "")
            bangumi_id = anime.get("bangumi_id", "")
            subscribed = anime.get("subscribed", False)
            mikan_available = anime.get("mikan_available", False)
            mikan_link = anime.get("mikan_link", "")

            # 评分颜色
            if rating >= 7.0:
                rating_color = "success"
            elif rating >= 5.0:
                rating_color = "warning"
            else:
                rating_color = "grey"

            # 订阅按钮
            subscribe_btn = {
                "component": "VBtn",
                "props": {
                    "size": "small",
                    "variant": "tonal",
                    "prepend-icon": "mdi-plus" if not subscribed else "mdi-check",
                    "color": "success" if subscribed else "primary",
                    "disabled": subscribed,
                },
                "text": "已订阅" if subscribed else "订阅",
            }
            if not subscribed and tmdb_id:
                subscribe_btn["events"] = {
                    "click": {
                        "api": f"plugin/AnimeDiscovery/subscribe?token={api_token}",
                        "method": "post",
                        "params": {
                            "tmdb_id": tmdb_id,
                            "title": title,
                            "year": year,
                        },
                    }
                }

            # MiKan 状态
            mikan_chip = {
                "component": "VChip",
                "props": {
                    "size": "x-small",
                    "color": "success" if mikan_available else "grey",
                    "variant": "tonal",
                },
                "text": "MiKan ✓" if mikan_available else "MiKan ✗",
            }

            # 海报 + 信息卡片
            card_content = [
                {
                    "component": "VRow",
                    "props": {"no-gutters": True},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 3, "md": 2},
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": poster,
                                        "height": "120",
                                        "cover": True,
                                        "rounded": "lg",
                                    } if poster else {
                                        "height": "120",
                                        "rounded": "lg",
                                        "color": "grey-lighten-3",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 9, "md": 10, "class": "pl-3"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "d-flex align-center mb-1"},
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {"class": "text-subtitle-1 font-weight-bold flex-grow-1"},
                                            "text": title,
                                        },
                                        subscribe_btn,
                                    ],
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-grey mb-1"},
                                    "text": f"{year} · 季度 {season}" if season else str(year),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "d-flex align-center mb-1"},
                                    "content": [
                                        {
                                            "component": "VChip",
                                            "props": {
                                                "size": "x-small",
                                                "color": rating_color,
                                                "class": "mr-2",
                                            },
                                            "text": f"★ {rating}" if rating else "暂无评分",
                                        },
                                        mikan_chip,
                                    ],
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-grey", "style": "line-height: 1.4;"},
                                    "text": overview + "..." if len(overview) >= 120 else overview,
                                },
                            ],
                        },
                    ],
                },
            ]

            cards.append(
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-2"},
                    "content": [
                        {"component": "VCardText", "content": card_content}
                    ],
                }
            )

        # 统计信息
        total = len(data)
        sub_count = sum(1 for a in data if a.get("subscribed"))
        mikan_count = sum(1 for a in data if a.get("mikan_available"))

        return [
            # 顶部统计
            {
                "component": "VRow",
                "props": {"class": "mb-3"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 4},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "primary"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(total)},
                                            {"component": "div", "props": {"class": "text-caption"}, "text": "当季新番"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 4},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "success"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(sub_count)},
                                            {"component": "div", "props": {"class": "text-caption"}, "text": "已订阅"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 4},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "warning"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(mikan_count)},
                                            {"component": "div", "props": {"class": "text-caption"}, "text": "MiKan 有资源"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
            # 刷新按钮
            {
                "component": "div",
                "props": {"class": "text-right mb-2"},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "size": "small",
                            "variant": "text",
                            "prepend-icon": "mdi-refresh",
                        },
                        "text": "刷新",
                        "events": {
                            "click": {
                                "api": refresh_api,
                                "method": "get",
                            }
                        },
                    }
                ],
            },
            # 番剧列表
            *cards,
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        pass

    # ==================== 内部方法 ====================

    def _get_anime_list(self) -> List[Dict[str, Any]]:
        """获取当季新番列表（带缓存）。"""
        now = time.time()
        if self._cache.get("anime_list") and (now - self._cache_time) < self._cache_ttl:
            return self._cache["anime_list"]

        anime_list = self._fetch_season_anime()
        if anime_list:
            # 检查订阅状态
            self._check_subscriptions(anime_list)
            # 检查 MiKan 资源
            self._check_mikan_resources(anime_list)
            # 评分过滤
            if self._min_rating > 0:
                anime_list = [a for a in anime_list if a.get("rating", 0) >= self._min_rating]
            # 按评分排序
            anime_list.sort(key=lambda x: x.get("rating", 0), reverse=True)

        self._cache["anime_list"] = anime_list
        self._cache_time = now
        return anime_list

    def _fetch_season_anime(self) -> List[Dict[str, Any]]:
        """从 TMDB/Bangumi 获取当季新番。"""
        anime_list = []
        try:
            # 获取当前季度
            now = datetime.now()
            month = now.month
            if month <= 3:
                season = "winter"
                year = now.year
            elif month <= 6:
                season = "spring"
                year = now.year
            elif month <= 9:
                season = "summer"
                year = now.year
            else:
                season = "fall"
                year = now.year

            # TMDB 获取当季动画
            url = f"https://api.themoviedb.org/3/discover/tv"
            params = {
                "api_key": settings.TMDB_API_KEY,
                "with_genres": "16",  # Animation
                "with_original_language": "ja",
                "first_air_date.gte": f"{year}-{'01' if season == 'winter' else '04' if season == 'spring' else '07' if season == 'summer' else '10'}-01",
                "first_air_date.lte": f"{year}-{'03' if season == 'winter' else '06' if season == 'spring' else '09' if season == 'summer' else '12'}-31",
                "sort_by": "popularity.desc",
                "page": 1,
            }

            request_utils = RequestUtils(proxies=settings.PROXY)
            response = request_utils.get(url, params=params)
            if response:
                data = response.json()
                for item in data.get("results", [])[:30]:
                    anime_list.append({
                        "title": item.get("name", ""),
                        "year": str(item.get("first_air_date", "")[:4]) if item.get("first_air_date") else "",
                        "season": f"{year}年{['冬', '春', '夏', '秋'][['winter', 'spring', 'summer', 'fall'].index(season)]}季",
                        "rating": round(item.get("vote_average", 0), 1),
                        "poster": f"https://image.tmdb.org/t/p/w300{item.get('poster_path', '')}" if item.get("poster_path") else "",
                        "overview": item.get("overview", ""),
                        "tmdb_id": item.get("id", ""),
                        "bangumi_id": "",
                        "subscribed": False,
                        "mikan_available": False,
                        "mikan_link": "",
                    })
                response.close()
        except Exception as e:
            logger.error(f"获取当季新番失败: {e}")

        return anime_list

    def _check_subscriptions(self, anime_list: List[Dict[str, Any]]) -> None:
        """检查哪些番剧已订阅。"""
        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe

            db = ScopedSession()
            try:
                subscribes = db.query(Subscribe).filter(
                    Subscribe.state == "R",
                    Subscribe.type == "电视剧",
                ).all()
                sub_titles = {s.name for s in subscribes}
                sub_tmdb_ids = {s.tmdb_id for s in subscribes if s.tmdb_id}

                for anime in anime_list:
                    if anime.get("tmdb_id") in sub_tmdb_ids:
                        anime["subscribed"] = True
                    elif anime.get("title") in sub_titles:
                        anime["subscribed"] = True
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"检查订阅状态失败: {e}")

    def _check_mikan_resources(self, anime_list: List[Dict[str, Any]]) -> None:
        """从站点查询 MiKan 资源可用性。"""
        try:
            from app.helper.indexer import IndexerHelper

            indexer = IndexerHelper()
            for anime in anime_list:
                title = anime.get("title", "")
                if not title:
                    continue
                try:
                    # 搜索站点资源
                    results = indexer.search_by_title(title, sites=[self._site_id] if self._site_id else None)
                    if results:
                        anime["mikan_available"] = True
                        # 取第一个结果的链接
                        if hasattr(results[0], "enclosure"):
                            anime["mikan_link"] = results[0].enclosure or ""
                except Exception:
                    pass
                # 避免请求过快
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"查询 MiKan 资源失败: {e}")

    def _refresh_data(self) -> dict:
        """手动刷新数据（API 端点）。"""
        self._cache = {}
        self._cache_time = 0
        data = self._fetch_season_anime()
        if data:
            self._check_subscriptions(data)
            self._check_mikan_resources(data)
            if self._min_rating > 0:
                data = [a for a in data if a.get("rating", 0) >= self._min_rating]
            data.sort(key=lambda x: x.get("rating", 0), reverse=True)
        self._cache["anime_list"] = data
        self._cache_time = time.time()
        return {"success": True, "count": len(data or [])}

    def _subscribe_anime(self, tmdb_id: str = "", title: str = "", year: str = "") -> dict:
        """订阅一部番剧（API 端点）。"""
        if not tmdb_id or not title:
            return {"success": False, "message": "缺少参数"}

        try:
            from app.db import ScopedSession
            from app.db.models.subscribe import Subscribe

            db = ScopedSession()
            try:
                # 检查是否已存在
                existing = db.query(Subscribe).filter(
                    Subscribe.tmdb_id == str(tmdb_id),
                ).first()
                if existing:
                    return {"success": False, "message": "已订阅"}

                # 创建订阅
                sub = Subscribe(
                    name=title,
                    year=year,
                    type="电视剧",
                    tmdb_id=str(tmdb_id),
                    season=1,
                    state="R",
                )
                db.add(sub)
                db.commit()

                # 清除缓存
                self._cache = {}
                self._cache_time = 0

                # 发送通知
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="新番订阅成功",
                    text=f"已订阅：{title} ({year})",
                )

                return {"success": True, "message": f"已订阅 {title}"}
            finally:
                db.close()
        except Exception as e:
            logger.error(f"订阅失败: {e}")
            return {"success": False, "message": str(e)}
