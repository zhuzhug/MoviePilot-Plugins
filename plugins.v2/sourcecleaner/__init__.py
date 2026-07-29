"""媒体库清理插件：扫描下载目录，检测悬空软链、孤儿元数据、空目录、重复资源等残留文件，提供安全的清理方案。"""

import os
import re
import shutil
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import Body
import pytz
from pydantic import BaseModel, Field

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class DeleteItemParams(BaseModel):
    """删除单条清理项参数。"""
    path: str = Field(..., description="文件或目录路径")
    category: str = Field(..., description="分类 ID")


class DeleteBatchParams(BaseModel):
    """批量删除清理项参数。"""
    category: str = Field(..., description="分类 ID")
    paths: List[str] = Field(default_factory=list, description="要删除的路径列表")


# 分类元数据：(id, 中文标题, 图标)
_CATEGORY_META: List[Tuple[str, str, str]] = [
    ("dangling", "悬空软链", "mdi-link-variant-off"),
    ("orphan_meta", "孤儿元数据", "mdi-file-document-outline"),
    ("empty_dir", "空目录", "mdi-folder-open-outline"),
    ("dup_resource", "重复资源", "mdi-content-duplicate"),
]

_SOURCE_CATEGORY_META: List[Tuple[str, str, str]] = [
    ("source_transferred", "已入库源文件", "mdi-file-check"),
    ("source_orphan", "孤立源文件", "mdi-file-question"),
    ("source_torrent", "无效种子文件", "mdi-file-remove-outline"),
    ("source_empty_dir", "源目录空目录", "mdi-folder-open-outline"),
    ("source_dup", "源文件重复", "mdi-file-multiple"),
]

_ALL_CATEGORY_META = _CATEGORY_META + _SOURCE_CATEGORY_META
_SOURCE_CATEGORY_IDS: Set[str] = {c[0] for c in _SOURCE_CATEGORY_META}

# 安全等级映射
_SAFETY_MAP = {
    "dangling": ("safe", "安全可删", "软链目标已不存在，删除链接无任何影响"),
    "orphan_meta": ("safe", "安全可删", "无对应视频的 .nfo/.jpg/.srt，不影响播放"),
    "empty_dir": ("safe", "安全可删", "空目录可直接删除"),
    "dup_resource": ("warn", "需确认", "同片不同版本，删除前确认保留哪个"),
    "source_transferred": ("danger", "谨慎", "下载目录中已整理到媒体库的文件"),
    "source_orphan": ("danger", "谨慎", "无下载任务跟踪，可能被其他工具使用"),
    "source_torrent": ("warn", "需确认", ".torrent 文件残留"),
    "source_empty_dir": ("safe", "安全可删", "源数据空目录"),
    "source_dup": ("warn", "需确认", "下载目录中的重复文件"),
}

# 元数据文件扩展名
_METADATA_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".tbn", ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi", ".txt"}


class SourceCleaner(_PluginBase):
    """源文件清理插件：扫描下载目录，检测残留文件并提供安全的清理方案。"""

    plugin_name = "源文件清理"
    plugin_desc = "扫描下载目录残留：悬空软链、孤儿元数据、空目录、重复资源，支持单条/批量删除并级联清理。"
    plugin_icon = "media-cleanup.png"
    plugin_version = "2.0.0"
    plugin_label = "下载器"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "sourcecleaner_"
    plugin_order = 90
    auth_level = 1

    # 运行态
    _enabled: bool = False
    _scan_dirs: str = ""
    _scan_scope: str = "all"
    _enable_dangling: bool = True
    _enable_orphan_meta: bool = True
    _enable_empty_dir: bool = True
    _enable_dup_resource: bool = True
    _enable_source_transferred: bool = True
    _enable_source_orphan: bool = True
    _enable_source_torrent: bool = True
    _enable_source_empty_dir: bool = True
    _enable_source_dup: bool = True
    _protected_dirs: str = ""
    _cron: str = "0 5 * * *"
    _notify: bool = False
    _include_regex: str = ""
    _exclude_regex: str = ""
    _max_display_per_type: int = 200
    _allow_delete: bool = False

    _scan_lock: Optional[threading.Lock] = None
    _scanning: bool = False
    _cancel_event: Optional[threading.Event] = None
    _scan_result: Optional[Dict[str, Any]] = None

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._cancel_event = threading.Event()
        self._scan_result = self._empty_result()

        if not config:
            self._enabled = False
            return

        self._enabled = bool(config.get("enabled", False))
        self._scan_dirs = str(config.get("scan_dirs", "") or "")
        self._scan_scope = str(config.get("scan_scope", "all") or "all")
        self._enable_dangling = bool(config.get("enable_dangling", True))
        self._enable_orphan_meta = bool(config.get("enable_orphan_meta", True))
        self._enable_empty_dir = bool(config.get("enable_empty_dir", True))
        self._enable_dup_resource = bool(config.get("enable_dup_resource", True))
        self._enable_source_transferred = bool(config.get("enable_source_transferred", True))
        self._enable_source_orphan = bool(config.get("enable_source_orphan", True))
        self._enable_source_torrent = bool(config.get("enable_source_torrent", True))
        self._enable_source_empty_dir = bool(config.get("enable_source_empty_dir", True))
        self._enable_source_dup = bool(config.get("enable_source_dup", True))
        self._protected_dirs = str(config.get("protected_dirs", "") or "")
        self._cron = str(config.get("cron", "") or "0 5 * * *")
        self._notify = bool(config.get("notify", False))
        self._include_regex = str(config.get("include_regex", "") or "")
        self._exclude_regex = str(config.get("exclude_regex", "") or "")
        self._max_display_per_type = int(config.get("max_display_per_type", 200) or 200)
        self._allow_delete = bool(config.get("allow_delete", False))

        # 加载持久化缓存
        try:
            saved = self.get_data("cache")
            if isinstance(saved, dict) and saved.get("items") is not None:
                self._scan_result = saved
        except Exception as err:
            logger.debug(f"[SourceCleaner] 加载缓存失败：{err}")

        if self._enabled:
            logger.info(f"[SourceCleaner] 初始化完成，扫描范围={self._scan_scope}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/scan", "endpoint": self.scan_api, "methods": ["GET"], "auth": "bear", "summary": "执行扫描"},
            {"path": "/cancel", "endpoint": self.cancel_scan_api, "methods": ["GET"], "auth": "bear", "summary": "取消扫描"},
            {"path": "/status", "endpoint": self.status_api, "methods": ["GET"], "auth": "bear", "summary": "扫描状态"},
            {"path": "/result", "endpoint": self.result_api, "methods": ["GET"], "auth": "bear", "summary": "扫描结果"},
            {"path": "/config", "endpoint": self.save_config_api, "methods": ["POST"], "auth": "bear", "summary": "保存配置"},
            {"path": "/delete_item", "endpoint": self.delete_item_api, "methods": ["POST"], "auth": "bear", "summary": "删除单条"},
            {"path": "/delete_batch", "endpoint": self.delete_batch_api, "methods": ["POST"], "auth": "bear", "summary": "批量删除"},
        ]

    def save_config_api(self, config: Dict[str, Any] = Body(default=None)) -> Dict[str, Any]:
        """保存插件配置。"""
        try:
            if config:
                self.update_config(config)
                self.init_plugin(config)
            return {"code": 0, "message": "配置已保存"}
        except Exception as err:
            return {"code": 1, "message": f"保存失败：{err}"}

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        return [{"nav_key": "main", "title": "源文件清理", "icon": "mdi-broom", "section": "organize", "permission": "manage", "order": 49}]

    def get_render_mode(self) -> Tuple[str, str]:
        return "vue", "dist/assets"

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [], self._current_config()

    def get_page(self) -> List[dict]:
        return []

    def get_dashboard_meta(self) -> Optional[List[Dict[str, Any]]]:
        if not self.get_state():
            return None
        return [{"key": "summary", "name": "源文件清理"}]

    def get_dashboard(self, key: str, **kwargs):
        if not self.get_state():
            return None
        result = self._scan_result or self._empty_result()
        summary = self._summary_of(result)
        total = summary.get("total", 0)
        total_size = self._fmt_size(summary.get("total_size", 0))
        subtitle = f"共 {total} 项 · {total_size}"
        return ({"cols": 12, "sm": 6, "md": 4}, {"title": "源文件清理", "subtitle": subtitle, "refresh": 60, "border": True}, None)

    def stop_service(self):
        self._scanning = False
        if self._cancel_event:
            self._cancel_event.set()

    # ---- API 方法 ----

    def scan_api(self) -> Dict[str, Any]:
        if not self._enabled:
            return {"code": 1, "message": "插件未启用"}
        if self._scanning:
            return {"code": 0, "message": "已有扫描任务在执行"}
        summary = self._run_scan()
        return {"code": 0, "message": "扫描完成", "data": summary}

    def cancel_scan_api(self) -> Dict[str, Any]:
        if self._scanning:
            self._cancel_event.set()
            return {"code": 0, "message": "扫描取消请求已发送"}
        return {"code": 0, "message": "当前无扫描任务"}

    def status_api(self) -> Dict[str, Any]:
        return {"code": 0, "data": {"scanning": self._scanning}}

    def result_api(self) -> Dict[str, Any]:
        return {"code": 0, "data": self._summary_of(self._scan_result)}

    def delete_item_api(self, params: DeleteItemParams) -> Dict[str, Any]:
        try:
            if not self._allow_delete:
                return {"code": 1, "message": "删除功能未启用"}
            if not os.path.exists(params.path):
                return {"code": 1, "message": "文件不存在"}
            if os.path.isdir(params.path):
                shutil.rmtree(params.path)
            else:
                os.remove(params.path)
            return {"code": 0, "message": "已删除"}
        except Exception as err:
            return {"code": 1, "message": f"删除失败：{err}"}

    def delete_batch_api(self, params: DeleteBatchParams) -> Dict[str, Any]:
        if not self._allow_delete:
            return {"code": 1, "message": "删除功能未启用"}
        deleted = 0
        errors = []
        for path in params.paths:
            try:
                if not os.path.exists(path):
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                deleted += 1
            except Exception as err:
                errors.append(f"{path}: {err}")
        return {"code": 0, "message": f"已删除 {deleted} 项", "errors": errors}

    # ---- 扫描核心 ----

    def _run_scan(self) -> Dict[str, Any]:
        with self._scan_lock:
            if self._scanning:
                return {"message": "已有扫描任务在执行"}
            self._scanning = True
        self._cancel_event.clear()

        try:
            started_at = time.time()
            include_pat = self._compile_regex(self._include_regex)
            exclude_pat = self._compile_regex(self._exclude_regex)
            result = self._empty_result()
            result["started_at"] = started_at
            scope = self._scan_scope

            all_roots: List[str] = []
            media_roots = self._resolve_scan_roots() if scope in ("media_only", "all") else []
            source_roots = self._resolve_source_roots() if scope in ("source_only", "all") else []
            all_roots = media_roots + source_roots

            if scope in ("media_only", "all"):
                result["scan_dirs"] = media_roots
            if scope in ("source_only", "all"):
                result["source_dirs"] = source_roots

            if not all_roots:
                result["finished_at"] = time.time()
                result["errors"].append("未配置任何扫描目录")
                self._scan_result = result
                self.save_data("cache", result)
                self._scanning = False
                return self._summary_of(result)

            dup_groups: Dict[str, List[str]] = defaultdict(list)

            if media_roots:
                for root in media_roots:
                    if self._cancel_event.is_set():
                        break
                    try:
                        self._walk_media(root, result, include_pat, exclude_pat, dup_groups)
                    except Exception as err:
                        result["errors"].append(f"媒体库扫描 {root} 失败：{err}")

                if self._enable_dup_resource:
                    for key, files in dup_groups.items():
                        if len(files) <= 1:
                            continue
                        files.sort()
                        keep = files[0]
                        for fp in files[1:]:
                            sz = 0
                            try:
                                sz = os.path.getsize(fp)
                            except OSError:
                                pass
                            self._append_item(result, "dup_resource", {"path": fp, "target": keep, "group_key": key, "size": sz})

            if source_roots:
                downloader_torrents = self._collect_downloader_torrents() if self._enable_source_orphan else {}
                for root in source_roots:
                    if self._cancel_event.is_set():
                        break
                    try:
                        self._walk_source(root, result, include_pat, exclude_pat, downloader_torrents)
                    except Exception as err:
                        result["errors"].append(f"源数据扫描 {root} 失败：{err}")

            result["finished_at"] = time.time()
            self._scan_result = result
            try:
                self.save_data("cache", result)
            except Exception:
                pass
            return self._summary_of(result)
        finally:
            self._scanning = False

    def _walk_media(self, root, result, include_pat, exclude_pat, dup_groups):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, topdown=True):
            if self._cancel_event.is_set():
                break
            if not self._path_allowed(dirpath, include_pat, exclude_pat):
                dirnames[:] = []
                continue

            if self._enable_dangling:
                for name in filenames:
                    fp = os.path.join(dirpath, name)
                    try:
                        if os.path.islink(fp) and not os.path.exists(fp):
                            target = self._readlink_safe(fp)
                            sz = 0
                            try:
                                sz = os.stat(fp).st_size
                            except OSError:
                                pass
                            self._append_item(result, "dangling", {"path": fp, "target": target, "size": sz})
                    except OSError:
                        continue

            if self._enable_orphan_meta:
                has_video = False
                meta_files = []
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in settings.RMT_MEDIAEXT:
                        has_video = True
                    elif ext in _METADATA_EXTS:
                        meta_files.append(os.path.join(dirpath, name))
                if not has_video and meta_files:
                    for fp in meta_files:
                        sz = 0
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            pass
                        self._append_item(result, "orphan_meta", {"path": fp, "size": sz})

            if self._enable_empty_dir:
                if not filenames and not dirnames:
                    self._append_item(result, "empty_dir", {"path": dirpath, "size": 0})

            if self._enable_dup_resource:
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in settings.RMT_MEDIAEXT:
                        continue
                    fp = os.path.join(dirpath, name)
                    try:
                        if os.path.islink(fp):
                            continue
                        stem = self._normalize_stem(name)
                        if stem:
                            dup_groups[stem].append(fp)
                    except OSError:
                        continue

    def _walk_source(self, root, result, include_pat, exclude_pat, downloader_torrents):
        protected_dirs = self._get_protected_dirs()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, topdown=True):
            if self._cancel_event.is_set():
                break

            if self._enable_source_transferred:
                transferred_paths = None
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in settings.RMT_MEDIAEXT:
                        continue
                    fp = os.path.join(dirpath, name)
                    try:
                        if os.path.islink(fp):
                            continue
                        if transferred_paths is None:
                            transferred_paths = self._collect_transferred_paths()
                        if os.path.normpath(fp) in transferred_paths:
                            sz = 0
                            try:
                                sz = os.path.getsize(fp)
                            except OSError:
                                pass
                            self._append_item(result, "source_transferred", {"path": fp, "target": "已入库", "group_key": "已入库源文件", "size": sz})
                    except OSError:
                        continue

            if self._enable_source_orphan:
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in settings.RMT_MEDIAEXT:
                        continue
                    fp = os.path.join(dirpath, name)
                    try:
                        if os.path.islink(fp):
                            continue
                        is_tracked = any(fp.startswith(sp + "/") or fp == sp for sp in downloader_torrents)
                        if not is_tracked:
                            sz = 0
                            try:
                                sz = os.path.getsize(fp)
                            except OSError:
                                pass
                            self._append_item(result, "source_orphan", {"path": fp, "target": "无下载器跟踪", "group_key": "孤立源文件", "size": sz})
                    except OSError:
                        continue

            if self._enable_source_empty_dir and not dirnames and not filenames:
                if os.path.abspath(dirpath) != os.path.abspath(root):
                    self._append_item(result, "source_empty_dir", {"path": dirpath, "target": "源目录空目录", "group_key": "源目录空目录", "size": 0})

    # ---- 辅助方法 ----

    def _empty_result(self):
        return {"scan_dirs": [], "source_dirs": [], "started_at": 0.0, "finished_at": 0.0, "errors": [], "items": {cat[0]: [] for cat in _ALL_CATEGORY_META}, "truncated": {cat[0]: False for cat in _ALL_CATEGORY_META}}

    def _summary_of(self, result):
        if not result:
            result = self._empty_result()
        counts = {cat[0]: len(result["items"].get(cat[0], [])) for cat in _ALL_CATEGORY_META}
        total_size = sum(int(it.get("size", 0)) for cat_id in counts for it in result["items"].get(cat_id, []))
        elapsed = max(0.0, result.get("finished_at", 0.0) - result.get("started_at", 0.0))
        return {"counts": counts, "total": sum(counts.values()), "total_size": total_size, "scan_dirs": result.get("scan_dirs", []), "source_dirs": result.get("source_dirs", []), "errors": result.get("errors", []), "started_at": result.get("started_at", 0.0), "finished_at": result.get("finished_at", 0.0), "elapsed_seconds": round(elapsed, 2), "truncated": result.get("truncated", {})}

    def _append_item(self, result, category, item):
        bucket = result["items"].setdefault(category, [])
        if len(bucket) >= self._max_display_per_type:
            result["truncated"][category] = True
            return
        bucket.append(item)

    def _resolve_scan_roots(self):
        roots = []
        if self._scan_dirs:
            for line in self._scan_dirs.splitlines():
                p = line.strip()
                if p and os.path.isdir(p):
                    roots.append(p)
        else:
            try:
                dir_confs = self.systemconfig.get("Directories") or []
                for d in dir_confs:
                    lib_path = d.get("library_path") if isinstance(d, dict) else None
                    if lib_path and os.path.isdir(lib_path) and lib_path not in roots:
                        roots.append(lib_path)
            except Exception:
                pass
        return roots

    def _resolve_source_roots(self):
        roots = []
        try:
            dir_confs = self.systemconfig.get("Directories") or []
            for d in dir_confs:
                dl_path = d.get("download_path") if isinstance(d, dict) else None
                if dl_path and os.path.isdir(dl_path) and dl_path not in roots:
                    roots.append(dl_path)
        except Exception:
            pass
        return roots

    def _collect_transferred_paths(self):
        paths = set()
        try:
            from app.db.transferhistory_oper import TransferHistoryOper
            histories = TransferHistoryOper().list_by_title(title="", count=-1)
            for h in (histories or []):
                src = getattr(h, "src", None)
                if src:
                    paths.add(os.path.normpath(src))
        except Exception:
            pass
        return paths

    def _collect_downloader_torrents(self):
        torrents = {}
        try:
            from app.helper.downloader import DownloaderHelper
            for instance in DownloaderHelper().iterate_module_instances():
                try:
                    for t in instance.torrents_info():
                        sp = getattr(t, "save_path", "") or getattr(t, "download_dir", "") or ""
                        if sp:
                            torrents[sp] = True
                except Exception:
                    continue
        except Exception:
            pass
        return torrents

    def _get_protected_dirs(self):
        dirs = []
        if self._protected_dirs:
            for line in self._protected_dirs.splitlines():
                p = line.strip()
                if p and os.path.isdir(p):
                    dirs.append(os.path.abspath(p))
        return dirs

    def _path_allowed(self, path, include_pat, exclude_pat):
        if exclude_pat and exclude_pat.search(path):
            return False
        if include_pat and not include_pat.search(path):
            return False
        return True

    @staticmethod
    def _compile_regex(pattern):
        if not pattern:
            return None
        try:
            return re.compile(pattern)
        except re.error:
            return None

    @staticmethod
    def _readlink_safe(path):
        try:
            return os.readlink(path)
        except OSError:
            return ""

    @staticmethod
    def _fmt_size(n):
        for u in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024:
                return f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} PB"

    @staticmethod
    def _normalize_stem(name):
        stem = os.path.splitext(name)[0]
        stem = re.sub(r"\[.*?\]", " ", stem)
        stem = re.sub(r"\(.*?\)", " ", stem)
        for tag in ["1080p", "2160p", "720p", "4K", "BluRay", "WEB-DL", "REMUX", "x264", "x265", "HEVC", "HDR", "DV", "DTS-HD.MA.7.1.Atmos", "AC3", "DTS", "AAC", "10bit", "HEVC", "FLAC"]:
            stem = re.sub(rf"[-._\s]*\b{re.escape(tag)}\b", "", stem, flags=re.IGNORECASE)
        stem = re.sub(r"[-._\s]+", " ", stem).strip().lower()
        return stem if len(stem) > 3 else ""

    def _current_config(self):
        return {
            "enabled": self._enabled, "scan_dirs": self._scan_dirs, "scan_scope": self._scan_scope,
            "enable_dangling": self._enable_dangling, "enable_orphan_meta": self._enable_orphan_meta,
            "enable_empty_dir": self._enable_empty_dir, "enable_dup_resource": self._enable_dup_resource,
            "enable_source_transferred": self._enable_source_transferred, "enable_source_orphan": self._enable_source_orphan,
            "enable_source_torrent": self._enable_source_torrent, "enable_source_empty_dir": self._enable_source_empty_dir,
            "enable_source_dup": self._enable_source_dup, "protected_dirs": self._protected_dirs,
            "cron": self._cron, "notify": self._notify, "include_regex": self._include_regex,
            "exclude_regex": self._exclude_regex, "max_display_per_type": self._max_display_per_type,
            "allow_delete": self._allow_delete,
        }
