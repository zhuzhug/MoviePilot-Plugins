import os
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase
from app.downloader import DownloaderHelper
from app.chain.chain_base import ChainBase
from app.log import logger


class ResourceCleaner(_PluginBase):
    """
    资源清理插件 - 分析源目录、下载器种子与媒体库软链接关系，清理孤立文件和幽灵种子。
    """

    plugin_name = "资源清理"
    plugin_desc = "扫描源目录、下载器种子与媒体库软链接，分类展示资源状态并支持清理孤立文件和幽灵种子。"
    plugin_icon = "resourcecleaner.png"
    plugin_version = "1.1.0"
    plugin_label = "资源管理"
    plugin_author = "local"
    plugin_config_prefix = "resourcecleaner_"
    plugin_order = 100
    auth_level = 1

    # 配置项
    _enabled = False
    _source_dirs = []                 # 源目录列表
    _library_dirs = []                # 媒体库目录列表
    _exclude_dirs = []                # 排除目录列表
    _selected_downloaders = []        # 选中的下载器名称列表
    _allow_delete = False
    _scanning = False
    _cache = {}                       # 缓存扫描结果
    _downloader_names = []            # 所有已配置的下载器名称

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件配置"""
        self.stop_service()
        self._enabled = False
        self._source_dirs = []
        self._library_dirs = []
        self._exclude_dirs = []
        self._selected_downloaders = []
        self._allow_delete = False
        self._cache = {}
        self._downloader_names = []

        # 读取已配置的下载器名称
        try:
            downloaders = self.settings.DOWNLOADER
            if downloaders and isinstance(downloaders, list):
                self._downloader_names = [d.get("name") for d in downloaders if d.get("name")]
        except Exception as e:
            logger.warning(f"获取下载器列表失败: {e}")

        if not config:
            return

        self._enabled = config.get("enabled", False)
        self._source_dirs = self._parse_dirs(config.get("source_dirs", ""))
        self._library_dirs = self._parse_dirs(config.get("library_dirs", ""))
        self._exclude_dirs = self._parse_dirs(config.get("exclude_dirs", ""))

        # 处理选中的下载器
        selected = config.get("selected_downloaders")
        if isinstance(selected, str):
            self._selected_downloaders = [x.strip() for x in selected.split(",") if x.strip()]
        elif isinstance(selected, list):
            self._selected_downloaders = selected
        else:
            self._selected_downloaders = []

        self._allow_delete = config.get("allow_delete", False)

        # 加载上次扫描结果
        saved = self.get_data("scan_result")
        if saved:
            self._cache = saved

    def _parse_dirs(self, text: str) -> List[str]:
        """解析用户输入的目录列表（每行一个，或逗号分隔）"""
        if not text:
            return []
        parts = text.replace(",", "\n").splitlines()
        dirs = []
        for p in parts:
            p = p.strip()
            if p and p not in dirs:
                dirs.append(p)
        return dirs

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/scan",
                "method": "post",
                "summary": "触发扫描",
                "auth": "apikey",
                "endpoint": self.scan
            },
            {
                "path": "/results",
                "method": "get",
                "summary": "获取扫描结果",
                "auth": "apikey",
                "endpoint": self.get_results
            },
            {
                "path": "/delete_orphan_all",
                "method": "post",
                "summary": "删除所有孤立文件",
                "auth": "apikey",
                "endpoint": self.delete_orphan_all
            },
            {
                "path": "/delete_ghost_all",
                "method": "post",
                "summary": "清理所有幽灵种子",
                "auth": "apikey",
                "endpoint": self.delete_ghost_all
            }
        ]

    def scan(self, request_data: dict = None) -> Dict[str, Any]:
        """触发扫描"""
        if self._scanning:
            return {"success": False, "message": "正在扫描中，请稍后"}
        threading.Thread(target=self._do_scan, daemon=True).start()
        return {"success": True, "message": "扫描已开始"}

    def _do_scan(self):
        """执行扫描逻辑"""
        self._scanning = True
        try:
            # 获取指定下载器的种子
            downloader = DownloaderHelper()
            if self._selected_downloaders:
                # 逐个获取选中的下载器种子
                all_torrents = []
                for dl_name in self._selected_downloaders:
                    try:
                        torrents = downloader.get_torrents(downloader=dl_name)
                        if torrents:
                            all_torrents.extend(torrents)
                    except Exception as e:
                        logger.warning(f"获取下载器 {dl_name} 种子失败: {e}")
                torrents = all_torrents
            else:
                # 未选择时获取所有下载器
                torrents = downloader.get_torrents()

            if not torrents:
                logger.warning("未获取到下载器种子信息，请检查下载器配置或选择正确的下载器")
                self._cache = {"error": "未获取到下载器种子信息，请检查下载器配置或选择正确的下载器"}
                self._scanning = False
                return

            # 遍历源目录，收集视频文件
            video_exts = {'.mkv', '.mp4', '.avi', '.ts', '.rmvb', '.flv', '.wmv', '.m2ts', '.strm'}
            all_files = []
            for src_dir in self._source_dirs:
                if not os.path.exists(src_dir):
                    logger.warning(f"源目录不存在: {src_dir}")
                    continue
                for root, _, files in os.walk(src_dir):
                    if any(root.startswith(excl) for excl in self._exclude_dirs):
                        continue
                    for f in files:
                        ext = os.path.splitext(f)[1].lower()
                        if ext in video_exts:
                            full = os.path.join(root, f)
                            try:
                                size = os.path.getsize(full)
                            except:
                                size = 0
                            all_files.append({"path": full, "size": size, "name": f, "dir": root})

            # 扫描媒体库软链接
            library_links = {}
            for lib_dir in self._library_dirs:
                if not os.path.exists(lib_dir):
                    logger.warning(f"媒体库目录不存在: {lib_dir}")
                    continue
                for root, _, files in os.walk(lib_dir):
                    for f in files:
                        full = os.path.join(root, f)
                        if os.path.islink(full):
                            target = os.readlink(full)
                            library_links[target] = full

            # 建立TR种子路径映射
            tr_seed_paths = set()
            tr_map = {}
            for t in torrents:
                seed_path = os.path.join(t.save_path, t.name) if hasattr(t, 'save_path') and t.save_path else ""
                if os.path.exists(seed_path):
                    tr_seed_paths.add(seed_path)
                tr_map[t.hash] = {
                    "name": t.name,
                    "path": seed_path,
                    "size": t.size,
                    "downloader": getattr(t, 'downloader', '')
                }

            # 分类
            categories = {
                "A_完好": [],
                "B_未整理": [],
                "C_幽灵": [],
                "D_孤立": [],
                "E_已整理已删种": []
            }

            for sf in all_files:
                path = sf["path"]
                in_tr = path in tr_seed_paths
                has_link = path in library_links
                if in_tr and has_link:
                    categories["A_完好"].append(sf)
                elif in_tr and not has_link:
                    categories["B_未整理"].append(sf)
                elif not in_tr and has_link:
                    categories["E_已整理已删种"].append(sf)
                else:
                    categories["D_孤立"].append(sf)

            # 幽灵种子：TR做种但源文件不存在
            for t in torrents:
                seed_path = os.path.join(t.save_path, t.name) if hasattr(t, 'save_path') and t.save_path else ""
                if not os.path.exists(seed_path):
                    categories["C_幽灵"].append({
                        "hash": t.hash,
                        "name": t.name,
                        "size": t.size,
                        "path": seed_path or "源文件丢失",
                        "downloader": getattr(t, 'downloader', '')
                    })

            # 统计
            stats = {}
            total_size = 0
            for cat, items in categories.items():
                if cat == "C_幽灵":
                    count = len(items)
                    size = sum(item.get("size", 0) for item in items)
                else:
                    count = len(items)
                    size = sum(item.get("size", 0) for item in items)
                stats[cat] = {"count": count, "size": size}
                total_size += size

            stats["total_size"] = total_size

            self._cache = {
                "stats": stats,
                "categories": categories,
                "timestamp": int(time.time())
            }
            self.save_data("scan_result", self._cache)
            self._scanning = False
            logger.info("扫描完成")
        except Exception as e:
            logger.error(f"扫描出错: {str(e)}")
            self._cache = {"error": str(e)}
            self._scanning = False

    def get_results(self) -> Dict[str, Any]:
        if not self._cache:
            data = self.get_data("scan_result")
            if data:
                self._cache = data
        return {"success": True, "data": self._cache}

    def delete_orphan_all(self, request_data: dict = None) -> Dict[str, Any]:
        if not self._allow_delete:
            return {"success": False, "message": "删除功能未启用，请在配置中开启"}
        d_files = self._cache.get("categories", {}).get("D_孤立", [])
        if not d_files:
            return {"success": False, "message": "没有孤立文件可删除"}
        deleted = []
        failed = []
        for item in d_files:
            path = item.get("path")
            if not path or not os.path.exists(path):
                continue
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    deleted.append(path)
                    parent = os.path.dirname(path)
                    if os.path.exists(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                else:
                    failed.append((path, "不是文件"))
            except Exception as e:
                failed.append((path, str(e)))
        if deleted:
            new_d = [item for item in d_files if item.get("path") not in deleted]
            self._cache["categories"]["D_孤立"] = new_d
            self._cache["stats"]["D_孤立"]["count"] = len(new_d)
            self._cache["stats"]["D_孤立"]["size"] = sum(item.get("size", 0) for item in new_d)
            self.save_data("scan_result", self._cache)
        return {"success": True, "deleted": deleted, "failed": failed}

    def delete_ghost_all(self, request_data: dict = None) -> Dict[str, Any]:
        if not self._allow_delete:
            return {"success": False, "message": "删除功能未启用，请在配置中开启"}
        ghost_items = self._cache.get("categories", {}).get("C_幽灵", [])
        if not ghost_items:
            return {"success": False, "message": "没有幽灵种子可清理"}
        hashes = [item["hash"] for item in ghost_items if item.get("hash")]
        if not hashes:
            return {"success": False, "message": "没有有效的种子哈希"}
        try:
            chain = ChainBase()
            downloader_map = {}
            for item in ghost_items:
                dl = item.get("downloader", "")
                if dl not in downloader_map:
                    downloader_map[dl] = []
                downloader_map[dl].append(item["hash"])
            success_count = 0
            fail_details = []
            for dl, hlist in downloader_map.items():
                try:
                    chain.remove_torrents(hlist, delete_file=False, downloader=dl)
                    success_count += len(hlist)
                except Exception as e:
                    fail_details.append(f"{dl}: {str(e)}")
            if success_count > 0:
                self._cache["categories"]["C_幽灵"] = []
                self._cache["stats"]["C_幽灵"]["count"] = 0
                self._cache["stats"]["C_幽灵"]["size"] = 0
                self.save_data("scan_result", self._cache)
            return {"success": True, "cleaned": success_count, "failed": fail_details}
        except Exception as e:
            return {"success": False, "message": f"清理幽灵种子失败: {str(e)}"}

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置"""
        downloader_options = []
        for name in self._downloader_names:
            downloader_options.append({"text": name, "value": name})

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件"
                        }
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "selected_downloaders",
                            "label": "下载器",
                            "items": downloader_options,
                            "multiple": True,
                            "chips": True,
                            "clearable": True,
                            "hint": "选择要分析的下载器，不选则扫描所有"
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "source_dirs",
                            "label": "源目录（每行一个）",
                            "rows": 3,
                            "hint": "要扫描的下载目录，例如 /media/downloads/BT下载"
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "library_dirs",
                            "label": "媒体库目录（每行一个）",
                            "rows": 3,
                            "hint": "整理后的媒体库目录，用于检查软链接，例如 /media/tv"
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "exclude_dirs",
                            "label": "排除目录（每行一个）",
                            "rows": 2,
                            "hint": "不扫描的目录，例如保种目录"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "allow_delete",
                            "label": "启用删除功能（危险）",
                            "color": "error"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "selected_downloaders": [],
            "source_dirs": "",
            "library_dirs": "",
            "exclude_dirs": "",
            "allow_delete": False
        }

    def get_page(self) -> Optional[List[dict]]:
        if not self._enabled:
            return None

        data = self._cache or self.get_data("scan_result")
        if not data:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "text": "尚未扫描，请点击「开始扫描」按钮进行分析"
                    }
                },
                {
                    "component": "VBtn",
                    "props": {
                        "color": "primary",
                        "text": "开始扫描"
                    },
                    "events": {
                        "click": {
                            "api": f"/plugin/{self.__class__.__name__}/scan?apikey={self.settings.API_TOKEN}",
                            "method": "post"
                        }
                    }
                }
            ]
        if "error" in data:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "text": f"扫描出错: {data['error']}"
                    }
                }
            ]

        stats = data.get("stats", {})
        categories = data.get("categories", {})
        timestamp = data.get("timestamp", 0)
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp else "未知"

        stat_cards = []
        label_map = {
            "A_完好": "完好",
            "B_未整理": "未整理",
            "C_幽灵": "幽灵种子",
            "D_孤立": "孤立文件",
            "E_已整理已删种": "已整理已删种"
        }
        for cat, info in stats.items():
            if cat == "total_size":
                continue
            label = label_map.get(cat, cat)
            stat_cards.append({
                "component": "VCard",
                "props": {
                    "class": "ma-2",
                    "width": "180",
                    "elevation": "2"
                },
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "content": label
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "text-h6"},
                        "content": f"{info['count']} 项\n{self._size_fmt(info['size'])}"
                    }
                ]
            })

        panels = []
        for cat, items in categories.items():
            if not items:
                continue
            label = label_map.get(cat, cat)
            if cat == "C_幽灵":
                headers = [
                    {"text": "种子名", "value": "name"},
                    {"text": "大小", "value": "size"},
                    {"text": "路径", "value": "path"}
                ]
                table_items = []
                for item in items:
                    table_items.append({
                        "name": item.get("name", ""),
                        "size": self._size_fmt(item.get("size", 0)),
                        "path": item.get("path", ""),
                        "_raw_size": item.get("size", 0)
                    })
            else:
                headers = [
                    {"text": "文件路径", "value": "path"},
                    {"text": "大小", "value": "size"}
                ]
                table_items = []
                for item in items:
                    table_items.append({
                        "path": item.get("path", ""),
                        "size": self._size_fmt(item.get("size", 0)),
                        "_raw_size": item.get("size", 0)
                    })

            panel_content = [
                {
                    "component": "VDataTable",
                    "props": {
                        "headers": headers,
                        "items": table_items,
                        "items-per-page": 10,
                        "density": "compact",
                        "hover": True
                    }
                }
            ]

            if self._allow_delete and (cat in ["D_孤立", "C_幽灵"]):
                if cat == "D_孤立":
                    btn_text = "删除所有孤立文件"
                    api_path = "/delete_orphan_all"
                else:
                    btn_text = "清理所有幽灵种子"
                    api_path = "/delete_ghost_all"
                panel_content.append({
                    "component": "VBtn",
                    "props": {
                        "color": "error",
                        "text": btn_text,
                        "class": "mt-2"
                    },
                    "events": {
                        "click": {
                            "api": f"/plugin/{self.__class__.__name__}{api_path}?apikey={self.settings.API_TOKEN}",
                            "method": "post"
                        }
                    }
                })

            panels.append({
                "component": "VExpansionPanel",
                "props": {
                    "title": f"{label}（{len(items)} 项）",
                    "subtitle": f"大小: {self._size_fmt(sum(item.get('_raw_size', 0) for item in table_items))}"
                },
                "content": panel_content
            })

        return [
            {
                "component": "VRow",
                "props": {"class": "mb-4"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": "12", "sm": "8"},
                        "content": {
                            "component": "VRow",
                            "props": {"no-gutters": True},
                            "content": stat_cards
                        }
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": "12", "sm": "4", "class": "text-right"},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"color": "grey-lighten-2"},
                                "content": f"扫描时间: {time_str}"
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "text": "重新扫描",
                                    "class": "ml-2"
                                },
                                "events": {
                                    "click": {
                                        "api": f"/plugin/{self.__class__.__name__}/scan?apikey={self.settings.API_TOKEN}",
                                        "method": "post"
                                    }
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VExpansionPanels",
                "props": {"multiple": True, "class": "mt-4"},
                "content": panels
            }
        ]

    def _size_fmt(self, size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.2f}KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024:.2f}MB"
        else:
            return f"{size / 1024 / 1024 / 1024:.2f}GB"

    def stop_service(self) -> None:
        return None
