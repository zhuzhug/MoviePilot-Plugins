"""媒体库清理（LibraryCleaner）插件。

v0.2.0 目标：
- 扫描本地媒体库路径，识别 4 类默认清理项：
  1) 悬空软链（symlink 目标已不存在）
  2) 孤儿元数据（.nfo/.jpg/.png/.srt/.ass 等同目录无媒体视频）
  3) 空目录
  4) 重复资源（同目录同源不同版本：去除发行标签后 stem 相同的多个视频）
- VTabs 分类展示扫描结果，支持手动"立即扫描"与 CRON 定时扫描
- 提供单条 / 批量删除 API：删除时级联清理同 inode 硬链接与指向源的软链，空目录可选级联清理
- 高级项（重复软/硬链跨目录检测、失联视频）保留占位，将在 v0.3.0 补齐
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

from app.core.config import settings
from app.plugins import _PluginBase
from app.schemas import NotificationType

logger = logging.getLogger(__name__)


# 元数据后缀（同目录下若只有这些文件而无视频,判定为孤儿元数据）
_METADATA_EXTS = {
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn",
    ".srt", ".ass", ".ssa", ".sup", ".vtt", ".idx", ".sub",
}

# 检测项元数据：id, 中文标题, 图标
_CATEGORY_META: List[Tuple[str, str, str]] = [
    ("dangling", "悬空软链", "mdi-link-variant-off"),
    ("orphan_meta", "孤儿元数据", "mdi-file-document-outline"),
    ("empty_dir", "空目录", "mdi-folder-open-outline"),
    ("dup_resource", "重复资源", "mdi-content-duplicate"),
    ("dup_softlink", "重复软链接", "mdi-link-variant"),
    ("dup_hardlink", "重复硬链接", "mdi-file-multiple"),
    ("missing_video", "失联视频", "mdi-file-question-outline"),
]

# 需要占位（v0.3.0 才实现）的分类
_ADVANCED_CATEGORIES: Set[str] = {"dup_softlink", "dup_hardlink", "missing_video"}

# 去除发行标签：分辨率 / 编码 / 来源 / 音轨 / 语言 / HDR / 分组 等常见碎片
# 用于把同目录下"同一部作品的不同版本"归一化为相同 stem
_STEM_TRAIL_TOKENS = re.compile(
    r"[\s._\-\[\]()]*(?:"
    r"2160p|1080p|720p|480p|4k|uhd|hdr(?:10\+?|dv)?|dovi|dv|sdr|hdr10|"
    r"web[\s._-]?dl|webdl|webrip|bluray|bdrip|dvdrip|hdrip|remux|"
    r"h\.?264|h\.?265|x264|x265|hevc|avc|vp9|av1|"
    r"aac|ac3|eac3|dts(?:[\s._-]?hd|[\s._-]?ma|[\s._-]?x)?|truehd|atmos|flac|opus|mp3|"
    r"5\.1|7\.1|2\.0|"
    r"repack|proper|internal|extended|uncut|remastered|imax|criterion|"
    r"chs|cht|chi|eng|jpn|kor|multi|cn|"
    r"10bit|8bit"
    r")",
    flags=re.IGNORECASE,
)


class DeleteItemParams(BaseModel):
    """删除单条清理项参数。"""

    category: str
    path: str


class DeleteBatchParams(BaseModel):
    """批量删除清理项参数。"""

    category: str
    paths: List[str]


class LibraryCleaner(_PluginBase):
    """媒体库清理插件。"""

    plugin_name = "媒体库清理"
    plugin_desc = "扫描媒体库残留：悬空软链、孤儿元数据、空目录、重复资源；支持单条/批量删除并级联清理同 inode 硬链与指向源的软链。"
    plugin_icon = "clean.png"
    plugin_version = "0.2.0"
    plugin_label = "媒体库"
    plugin_author = "zhuzhug"
    author_url = "https://github.com/zhuzhug"
    plugin_config_prefix = "librarycleaner_"
    plugin_order = 30
    auth_level = 1

    # 运行时配置
    _enabled: bool = False
    _scan_dirs: str = ""
    _enable_dangling: bool = True
    _enable_orphan_meta: bool = True
    _enable_empty_dir: bool = True
    _enable_dup_resource: bool = True
    _enable_dup_softlink: bool = False
    _enable_dup_hardlink: bool = False
    _enable_missing_video: bool = False
    _cron: str = "0 5 * * *"
    _notify: bool = False
    _include_regex: str = ""
    _exclude_regex: str = ""
    _max_display_per_type: int = 200
    _empty_cascade: bool = True
    _allow_delete: bool = False

    _scan_lock: Optional[threading.Lock] = None
    _scanning: bool = False
    _scan_result: Optional[Dict[str, Any]] = None

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._scan_result = self._empty_result()

        if not config:
            self._enabled = False
            return

        self._enabled = bool(config.get("enabled", False))
        self._scan_dirs = str(config.get("scan_dirs", "") or "")
        self._enable_dangling = bool(config.get("enable_dangling", True))
        self._enable_orphan_meta = bool(config.get("enable_orphan_meta", True))
        self._enable_empty_dir = bool(config.get("enable_empty_dir", True))
        self._enable_dup_resource = bool(config.get("enable_dup_resource", True))
        self._enable_dup_softlink = bool(config.get("enable_dup_softlink", False))
        self._enable_dup_hardlink = bool(config.get("enable_dup_hardlink", False))
        self._enable_missing_video = bool(config.get("enable_missing_video", False))
        self._cron = str(config.get("cron", "") or "0 5 * * *")
        self._notify = bool(config.get("notify", False))
        self._include_regex = str(config.get("include_regex", "") or "")
        self._exclude_regex = str(config.get("exclude_regex", "") or "")
        self._empty_cascade = bool(config.get("empty_cascade", True))
        self._allow_delete = bool(config.get("allow_delete", False))
        try:
            self._max_display_per_type = int(config.get("max_display_per_type", 200))
        except Exception:
            self._max_display_per_type = 200

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件对外提供的 API 列表。"""
        return [
            {
                "path": "/refresh",
                "endpoint": self.refresh_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即扫描媒体库",
            },
            {
                "path": "/result",
                "endpoint": self.result_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取最近扫描结果",
            },
            {
                "path": "/delete_item",
                "endpoint": self.delete_item_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除单条清理项",
            },
            {
                "path": "/delete_batch",
                "endpoint": self.delete_batch_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量删除清理项",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """返回 CRON 定时扫描服务；未启用或 cron 为空则不注册。"""
        if not self._enabled or not (self._cron or "").strip():
            return []
        return [
            {
                "id": "LibraryCleanerScan",
                "name": "媒体库清理定时扫描",
                "trigger": "cron",
                "func": self._run_scan,
                "kwargs": self._parse_cron(self._cron.strip()),
            }
        ]

    @staticmethod
    def _parse_cron(cron_expr: str) -> Dict[str, str]:
        """解析 5 段 CRON 表达式为 APScheduler kwargs。"""
        parts = (cron_expr or "").split()
        if len(parts) != 5:
            return {"minute": "0", "hour": "5"}
        keys = ["minute", "hour", "day", "month", "day_of_week"]
        return dict(zip(keys, parts))

    def stop_service(self) -> None:
        """停止插件服务并释放资源。"""
        return None

    # ---------------------------------------------------------------- API 端点

    def refresh_api(self) -> Dict[str, Any]:
        """立即扫描一次媒体库，同步返回摘要。"""
        if not self._enabled:
            return {"code": 1, "message": "插件未启用"}
        summary = self._run_scan()
        return {"code": 0, "message": "扫描完成", "data": summary}

    def result_api(self) -> Dict[str, Any]:
        """返回最近一次扫描结果的摘要。"""
        return {"code": 0, "data": self._summary_of(self._scan_result)}

    # -------------------------------------------------------------- 扫描主流程

    def _run_scan(self) -> Dict[str, Any]:
        """执行一次扫描，返回摘要字典。"""
        if self._scan_lock is None:
            self._scan_lock = threading.Lock()
        with self._scan_lock:
            if self._scanning:
                return {"message": "已有扫描任务在执行"}
            self._scanning = True

        try:
            started_at = time.time()
            roots = self._resolve_scan_roots()
            include_pat = self._compile_regex(self._include_regex)
            exclude_pat = self._compile_regex(self._exclude_regex)

            result = self._empty_result()
            result["scan_dirs"] = roots
            result["started_at"] = started_at

            if not roots:
                result["finished_at"] = time.time()
                result["errors"].append(
                    "未配置任何扫描目录（请在插件设置里填写扫描目录，或先在 MP 目录设置中启用媒体库路径）"
                )
                self._scan_result = result
                return self._summary_of(result)

            for root in roots:
                try:
                    self._walk_and_collect(root, result, include_pat, exclude_pat)
                except Exception as err:
                    result["errors"].append(f"扫描 {root} 失败：{err}")
                    logger.error(f"[LibraryCleaner] 扫描 {root} 失败：{err}")

            result["finished_at"] = time.time()
            self._scan_result = result

            summary = self._summary_of(result)
            self._maybe_notify(summary)
            return summary
        finally:
            self._scanning = False

    def _walk_and_collect(
        self,
        root: str,
        result: Dict[str, Any],
        include_pat: Optional[re.Pattern],
        exclude_pat: Optional[re.Pattern],
    ) -> None:
        """遍历一个根目录，按启用的检测项收集条目。"""
        empty_candidates: List[str] = []

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, topdown=True):
            if not self._path_allowed(dirpath, include_pat, exclude_pat):
                dirnames[:] = []
                continue

            # 1) 悬空软链
            if self._enable_dangling:
                for name in filenames:
                    fp = os.path.join(dirpath, name)
                    try:
                        if os.path.islink(fp) and not os.path.exists(fp):
                            target = self._readlink_safe(fp)
                            self._append_item(result, "dangling", {"path": fp, "target": target})
                    except OSError:
                        continue

            # 2) 孤儿元数据
            if self._enable_orphan_meta:
                has_video = False
                meta_files: List[str] = []
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in settings.RMT_MEDIAEXT:
                        has_video = True
                    elif ext in _METADATA_EXTS:
                        meta_files.append(os.path.join(dirpath, name))
                if not has_video and meta_files:
                    for fp in meta_files:
                        self._append_item(result, "orphan_meta", {"path": fp})

            # 3) 空目录候选
            if self._enable_empty_dir and not dirnames and not filenames:
                if os.path.abspath(dirpath) != os.path.abspath(root):
                    empty_candidates.append(dirpath)

            # 4) 重复资源：同目录、去掉发行标签后 stem 相同的多个视频
            if self._enable_dup_resource:
                groups: Dict[str, List[str]] = defaultdict(list)
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in settings.RMT_MEDIAEXT:
                        continue
                    fp = os.path.join(dirpath, name)
                    try:
                        # 只统计真实文件（软链视频不算重复资源，交给悬空软链/重复软链处理）
                        if os.path.islink(fp):
                            continue
                    except OSError:
                        continue
                    key = self._normalize_stem(name)
                    if not key:
                        continue
                    groups[key].append(fp)
                for key, files in groups.items():
                    if len(files) <= 1:
                        continue
                    files.sort()
                    keep = files[0]
                    for fp in files[1:]:
                        self._append_item(
                            result,
                            "dup_resource",
                            {"path": fp, "target": keep, "group_key": key},
                        )

        # 二次核验空目录
        if self._enable_empty_dir:
            for d in empty_candidates:
                try:
                    if os.path.isdir(d) and not os.listdir(d):
                        self._append_item(result, "empty_dir", {"path": d})
                except OSError:
                    continue

    # ------------------------------------------------------------ 通用辅助函数

    @staticmethod
    def _empty_result() -> Dict[str, Any]:
        """构造空的扫描结果结构。"""
        return {
            "scan_dirs": [],
            "started_at": 0.0,
            "finished_at": 0.0,
            "errors": [],
            "items": {cat[0]: [] for cat in _CATEGORY_META},
            "truncated": {cat[0]: False for cat in _CATEGORY_META},
        }

    def _summary_of(self, result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """生成摘要（含各分类计数与耗时）。"""
        if not result:            result = self._empty_result()
        counts = {cat[0]: len(result["items"].get(cat[0], [])) for cat in _CATEGORY_META}
        elapsed = max(0.0, result.get("finished_at", 0.0) - result.get("started_at", 0.0))
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "scan_dirs": result.get("scan_dirs", []),
            "errors": result.get("errors", []),
            "started_at": result.get("started_at", 0.0),
            "finished_at": result.get("finished_at", 0.0),
            "elapsed_seconds": round(elapsed, 2),
            "truncated": result.get("truncated", {}),
        }

    def _append_item(self, result: Dict[str, Any], category: str, item: Dict[str, Any]) -> None:
        """追加一条条目，超过展示上限则标记 truncated。"""
        bucket = result["items"].setdefault(category, [])
        if len(bucket) >= self._max_display_per_type:
            result["truncated"][category] = True
            return
        bucket.append(item)

    def _resolve_scan_roots(self) -> List[str]:
        """解析扫描根目录：优先用户填写，其次 MP 媒体库目录。"""
        roots: List[str] = []
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
            except Exception as err:
                logger.debug(f"[LibraryCleaner] 读取媒体库目录失败：{err}")
        return roots

    @staticmethod
    def _compile_regex(pattern: str) -> Optional[re.Pattern]:
        """安全编译正则，失败返回 None。"""
        if not pattern:
            return None
        try:
            return re.compile(pattern)
        except re.error as err:
            logger.warning(f"[LibraryCleaner] 正则表达式无效：{pattern} - {err}")
            return None

    @staticmethod
    def _path_allowed(
        path: str,
        include_pat: Optional[re.Pattern],
        exclude_pat: Optional[re.Pattern],
    ) -> bool:
        """按 include/exclude 正则过滤路径。"""
        if include_pat and not include_pat.search(path):
            return False
        if exclude_pat and exclude_pat.search(path):
            return False
        return True

    @staticmethod
    def _readlink_safe(path: str) -> str:
        """安全读取软链接目标。"""
        try:
            return os.readlink(path)
        except OSError:
            return ""

    @staticmethod
    def _normalize_stem(filename: str) -> str:
        """把文件名归一化为"版本无关"的 stem，用于重复资源分组。

        步骤：去扩展 → 反复剥离尾部的发行标签（分辨率/编码/来源/音轨/HDR/语言等） →
        统一空白 → 转小写。若剥完为空则返回空串。
        """
        stem = os.path.splitext(filename)[0]
        prev = None
        while prev != stem:
            prev = stem
            stem = _STEM_TRAIL_TOKENS.sub("", stem)
        stem = re.sub(r"[\s._\-\[\]()]+$", "", stem)
        stem = re.sub(r"[\s._\-]+", " ", stem).strip().lower()
        return stem

    # ---------------------------------------------------------------- 删除 API

    def delete_item_api(self, params: DeleteItemParams) -> Dict[str, Any]:
        """删除单条清理项。"""
        if not self._enabled:
            return {"code": 1, "message": "插件未启用"}
        if not self._allow_delete:
            return {"code": 1, "message": "未开启删除权限（请在设置页启用）"}
        ok, msg, extra = self._delete_one(params.category, params.path)
        # 同步移除缓存里的这条记录
        self._prune_scan_result(params.category, [params.path])
        return {"code": 0 if ok else 1, "message": msg, "data": extra}

    def delete_batch_api(self, params: DeleteBatchParams) -> Dict[str, Any]:
        """批量删除清理项。"""
        if not self._enabled:
            return {"code": 1, "message": "插件未启用"}
        if not self._allow_delete:
            return {"code": 1, "message": "未开启删除权限（请在设置页启用）"}
        success: List[str] = []
        failed: List[Dict[str, str]] = []
        cascade_total = 0
        for p in params.paths or []:
            ok, msg, extra = self._delete_one(params.category, p)
            if ok:
                success.append(p)
                cascade_total += int((extra or {}).get("cascade_count", 0))
            else:
                failed.append({"path": p, "message": msg})
        self._prune_scan_result(params.category, success)
        return {
            "code": 0 if not failed else (0 if success else 1),
            "message": f"成功 {len(success)} / 失败 {len(failed)}",
            "data": {
                "success": success,
                "failed": failed,
                "cascade_count": cascade_total,
            },
        }

    def _delete_one(self, category: str, path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """执行单条删除，返回 (成功?, 提示语, 附加信息)。"""
        try:
            if not path or ".." in Path(path).parts:
                return False, "路径非法", {}
            if not self._path_under_scan_roots(path):
                return False, "路径不在扫描目录范围内", {}
            if category == "empty_dir":
                return self._delete_empty_dir(path)
            if category == "dup_resource":
                return self._delete_file_with_links(path)
            if category == "dangling":
                return self._delete_symlink(path)
            if category == "orphan_meta":
                return self._delete_metadata_file(path)
            if category in _ADVANCED_CATEGORIES:
                return False, "该分类将在 v0.3.0 支持删除", {}
            return False, f"未知分类：{category}", {}
        except Exception as err:
            logger.exception(f"[LibraryCleaner] 删除失败 {category} {path}: {err}")
            return False, f"删除异常：{err}", {}

    def _path_under_scan_roots(self, path: str) -> bool:
        """检查目标路径是否位于当前扫描根目录之下（防越权删除）。"""
        try:
            target = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)
        except OSError:
            target = os.path.abspath(path)
        for root in self._resolve_scan_roots():
            root_abs = os.path.abspath(root)
            try:
                # 允许 target 本身就是 root（例如整块 empty 目录也可能给出根）
                if target == root_abs or target.startswith(root_abs + os.sep):
                    return True
                # 兼容 realpath：把 root 也 realpath 一下
                root_real = os.path.realpath(root_abs)
                if target == root_real or target.startswith(root_real + os.sep):
                    return True
            except OSError:
                continue
        return False

    def _delete_symlink(self, path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """删除单个软链接（悬空软链场景）。"""
        if not os.path.islink(path):
            return False, "非软链接，拒绝删除", {}
        os.unlink(path)
        self._maybe_cleanup_parent(path)
        return True, "已删除悬空软链", {"cascade_count": 0}

    def _delete_metadata_file(self, path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """删除单个元数据文件（孤儿元数据场景）。"""
        ext = os.path.splitext(path)[1].lower()
        if ext not in _METADATA_EXTS:
            return False, "非元数据文件后缀", {}
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isfile(path):
            os.remove(path)
        else:
            return False, "文件不存在", {}
        self._maybe_cleanup_parent(path)
        return True, "已删除孤儿元数据", {"cascade_count": 0}

    def _delete_empty_dir(self, path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """删除空目录。"""
        if not os.path.isdir(path):
            return False, "目录不存在", {}
        try:
            if os.listdir(path):
                return False, "目录非空，已跳过", {}
        except OSError as err:
            return False, f"读取目录失败：{err}", {}
        os.rmdir(path)
        self._maybe_cleanup_parent(path)
        return True, "已删除空目录", {"cascade_count": 0}

    def _delete_file_with_links(self, path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """删除一个真实视频文件，并级联清理：
        - 同 inode 的所有硬链接（跨扫描目录）
        - 指向该 inode/该路径的软链接
        """
        if os.path.islink(path):
            return False, "该路径为软链接，请使用悬空软链分类清理", {}
        if not os.path.isfile(path):
            return False, "文件不存在", {}

        try:
            st = os.stat(path, follow_symlinks=False)
            target_key = (st.st_dev, st.st_ino)
        except OSError as err:
            return False, f"stat 失败：{err}", {}

        # 收集扫描根下同 inode 硬链 + 指向源的软链
        hardlinks: List[str] = []
        soft_pointers: List[str] = []
        real_source = os.path.realpath(path)
        for root in self._resolve_scan_roots():
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False, topdown=True):
                for name in filenames:
                    fp = os.path.join(dirpath, name)
                    if fp == path:
                        continue
                    try:
                        if os.path.islink(fp):
                            # 只把"指向同一真实文件"的软链算进来
                            try:
                                if os.path.realpath(fp) == real_source:
                                    soft_pointers.append(fp)
                            except OSError:
                                continue
                            continue
                        s = os.stat(fp, follow_symlinks=False)
                        if (s.st_dev, s.st_ino) == target_key:
                            hardlinks.append(fp)
                    except OSError:
                        continue

        # 先删源文件
        os.remove(path)
        cascade = 0
        for fp in hardlinks:
            try:
                os.remove(fp)
                cascade += 1
            except OSError as err:
                logger.warning(f"[LibraryCleaner] 删除硬链 {fp} 失败：{err}")
        for fp in soft_pointers:
            try:
                os.unlink(fp)
                cascade += 1
            except OSError as err:
                logger.warning(f"[LibraryCleaner] 删除软链 {fp} 失败：{err}")

        # 一并把常见同名元数据带走（不跨目录）
        stem = os.path.splitext(path)[0]
        for ext in _METADATA_EXTS:
            for candidate in (stem + ext,):
                try:
                    if os.path.islink(candidate):
                        os.unlink(candidate)
                        cascade += 1
                    elif os.path.isfile(candidate):
                        os.remove(candidate)
                        cascade += 1
                except OSError:
                    continue

        self._maybe_cleanup_parent(path)
        for fp in hardlinks + soft_pointers:
            self._maybe_cleanup_parent(fp)

        return True, f"已删除文件并级联清理 {cascade} 项", {
            "cascade_count": cascade,
            "hardlinks": hardlinks,
            "soft_pointers": soft_pointers,
        }

    def _maybe_cleanup_parent(self, path: str) -> None:
        """若开启级联清理空目录，则向上尝试删空父目录（最多 3 层，扫描根内）。"""
        if not self._empty_cascade:
            return
        try:
            parent = os.path.dirname(path)
            for _ in range(3):
                if not parent or not os.path.isdir(parent):
                    return
                if not self._path_under_scan_roots(parent):
                    return
                # 不能删掉扫描根本身
                if any(os.path.abspath(parent) == os.path.abspath(r) for r in self._resolve_scan_roots()):
                    return
                try:
                    if os.listdir(parent):
                        return
                    os.rmdir(parent)
                except OSError:
                    return
                parent = os.path.dirname(parent)
        except Exception as err:
            logger.debug(f"[LibraryCleaner] 级联清理父目录失败：{err}")

    def _prune_scan_result(self, category: str, paths: List[str]) -> None:
        """从最近一次扫描结果里剔除已删除的条目，避免前端显示脏数据。"""
        if not self._scan_result or not paths:
            return
        try:
            bucket = self._scan_result.get("items", {}).get(category)
            if not bucket:
                return
            removed = set(paths)
            self._scan_result["items"][category] = [
                it for it in bucket if it.get("path") not in removed
            ]
        except Exception as err:
            logger.debug(f"[LibraryCleaner] 清理缓存条目失败：{err}")

    # 引用未使用的模块以避免 linter 抱怨（保留 shutil 未来级联删除大目录用）
    _shutil = shutil

    def _maybe_notify(self, summary: Dict[str, Any]) -> None:
        """扫描完成后按需发送通知。"""
        if not self._notify:
            return
        try:
            counts = summary.get("counts", {})
            lines = []
            for cat_id, cat_title, _ in _CATEGORY_META:
                c = counts.get(cat_id, 0)
                if c:
                    lines.append(f"· {cat_title}: {c}")
            title = f"媒体库清理扫描完成（共 {summary.get('total', 0)} 项）"
            body_lines = lines or ["未发现残留"]
            body_lines.append(f"耗时 {summary.get('elapsed_seconds', 0)} 秒")
            if summary.get("errors"):
                body_lines.append(f"错误 {len(summary['errors'])} 条，详见日志")
            self.post_message(
                mtype=NotificationType.Manual,
                title=title,
                text="\n".join(body_lines),
            )
        except Exception as err:
            logger.debug(f"[LibraryCleaner] 通知发送失败：{err}")

    # ------------------------------------------------------------ 设置页表单

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件设置表单及默认值。"""
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "扫描完成后发送通知"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "scan_dirs",
                                    "label": "扫描目录（每行一个绝对路径，留空则使用 MP 媒体库目录）",
                                    "rows": 3,
                                    "placeholder": "/media/电影\n/media/剧集",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enable_dangling", "label": "检测悬空软链"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enable_orphan_meta", "label": "检测孤儿元数据"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enable_empty_dir", "label": "检测空目录"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_dup_resource",
                                        "label": "检测同片重复资源（同标题不同分辨率/编码/来源）",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "empty_cascade",
                                        "label": "删除文件后清理产生的空父目录",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_dup_softlink",
                                        "label": "检测重复软链接（v0.3.0）",
                                        "disabled": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_dup_hardlink",
                                        "label": "检测重复硬链接（v0.3.0）",
                                        "disabled": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_missing_video",
                                        "label": "检测失联视频（v0.3.0）",
                                        "disabled": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "include_regex",
                                        "label": "路径包含正则（可选）",
                                        "placeholder": "例如 /media/(电影|剧集)",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "exclude_regex",
                                        "label": "路径排除正则（可选）",
                                        "placeholder": "例如 /(#recycle|@eaDir)",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "定时扫描 CRON（留空则不定时）",
                                        "placeholder": "0 5 * * *",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_display_per_type",
                                        "label": "每类最大展示条数",
                                        "type": "number",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "class": "mt-2",
                        },
                        "text": "v0.2.0：直接删除，无试运行。删除视频文件时会自动清理同 inode 的硬链接以及指向该文件的软链接。手动扫描请打开插件详情页点击\"立即扫描\"。",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "allow_delete",
                                        "label": "启用删除按钮（详情页每条清理项将出现红色删除按钮，谨慎开启）",
                                        "color": "error",
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ]
        defaults = {
            "enabled": False,
            "scan_dirs": "",
            "enable_dangling": True,
            "enable_orphan_meta": True,
            "enable_empty_dir": True,
            "enable_dup_resource": True,
            "empty_cascade": True,
            "enable_dup_softlink": False,
            "enable_dup_hardlink": False,
            "enable_missing_video": False,
            "cron": "0 5 * * *",
            "notify": False,
            "include_regex": "",
            "exclude_regex": "",
            "max_display_per_type": 200,
            "allow_delete": False,
        }
        return form, defaults

    # ---------------------------------------------------------------- 详情页

    def get_page(self) -> List[dict]:
        """返回插件详情页 VueRender JSON。"""
        result = self._scan_result or self._empty_result()
        summary = self._summary_of(result)
        counts = summary.get("counts", {})

        api_token = settings.API_TOKEN
        refresh_url = f"/api/v1/plugin/LibraryCleaner/refresh?apikey={api_token}"
        delete_item_url = f"/api/v1/plugin/LibraryCleaner/delete_item?apikey={api_token}"
        delete_batch_url = f"/api/v1/plugin/LibraryCleaner/delete_batch?apikey={api_token}"
        delete_item_url = f"/api/v1/plugin/LibraryCleaner/delete_item?apikey={api_token}"

        # 顶部信息条
        info_chips: List[dict] = []
        info_chips.append({
            "component": "VChip",
            "props": {
                "color": "primary" if self._enabled else "grey",
                "variant": "tonal",
                "class": "mr-2",
                "size": "small",
            },
            "text": "已启用" if self._enabled else "未启用",
        })
        info_chips.append({
            "component": "VChip",
            "props": {
                "color": "info",
                "variant": "tonal",
                "class": "mr-2",
                "size": "small",
            },
            "text": f"共 {summary.get('total', 0)} 项 · 耗时 {summary.get('elapsed_seconds', 0)}s",
        })
        started = summary.get("started_at", 0.0)
        if started:
            try:
                info_chips.append({
                    "component": "VChip",
                    "props": {
                        "color": "default",
                        "variant": "text",
                        "class": "mr-2",
                        "size": "small",
                    },
                    "text": "上次扫描: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                })
            except Exception:
                pass

        header = {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [{
                "component": "VCardText",
                "content": [{
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap align-center"},
                    "content": info_chips + [{
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "flat",
                            "prependIcon": "mdi-refresh",
                            "class": "ml-auto",
                            "events": {
                                "click": {
                                    "api": refresh_url,
                                    "method": "get",
                                    "params": {},
                                }
                            },
                        },
                        "text": "立即扫描",
                    }],
                }],
            }],
        }

        # 扫描目录简报
        scan_dirs = summary.get("scan_dirs", [])
        dir_lines = [{
            "component": "div",
            "props": {"class": "text-caption text-medium-emphasis"},
            "text": f"扫描目录：{d}",
        } for d in scan_dirs] or [{
            "component": "div",
            "props": {"class": "text-caption text-warning"},
            "text": "尚未扫描或未配置扫描目录，请在设置中填写路径或先扫描一次。",
        }]

        errors = summary.get("errors", [])
        error_block = None
        if errors:
            error_block = {
                "component": "VAlert",
                "props": {"type": "error", "variant": "tonal", "class": "mb-3"},
                "content": [{
                    "component": "div",
                    "text": f"扫描过程中出现 {len(errors)} 条错误：",
                }] + [{
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": f"· {e}",
                } for e in errors[:5]],
            }

        # VTabs 分类结果
        tabs_content: List[dict] = []
        windows_content: List[dict] = []

        for cat_id, cat_title, cat_icon in _CATEGORY_META:
            enabled_map = {
                "dangling": self._enable_dangling,
                "orphan_meta": self._enable_orphan_meta,
                "empty_dir": self._enable_empty_dir,
                "dup_softlink": self._enable_dup_softlink,
                "dup_hardlink": self._enable_dup_hardlink,
                "missing_video": self._enable_missing_video,
            }
            is_advanced = cat_id in ("dup_softlink", "dup_hardlink", "missing_video")
            c = counts.get(cat_id, 0)

            tab_label = cat_title
            if c > 0:
                tab_label = f"{cat_title} ({c})"

            tabs_content.append({
                "component": "VTab",
                "props": {"value": cat_id},
                "content": [
                    {"component": "VIcon", "props": {"start": True}, "text": cat_icon},
                    {"component": "span", "text": tab_label},
                ],
            })

            # 分类内容
            if is_advanced:
                inner = [{
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal"},
                    "text": f"「{cat_title}」将在 v0.3.0 版本提供。",
                }]
            elif not enabled_map.get(cat_id, False):
                inner = [{
                    "component": "VAlert",
                    "props": {"type": "warning", "variant": "tonal"},
                    "text": f"「{cat_title}」检测项未启用，请在设置中开启后重新扫描。",
                }]
            else:
                items = result["items"].get(cat_id, [])
                if not items:
                    inner = [{
                        "component": "VAlert",
                        "props": {"type": "success", "variant": "tonal"},
                        "text": f"未发现「{cat_title}」类残留。",
                    }]
                else:
                    rows = []
                    for it in items:
                        path = it.get("path", "")
                        target = it.get("target", "")
                        group_key = it.get("group_key", "")
                        text_children = [{
                            "component": "div",
                            "props": {"style": "word-break: break-all; font-family: monospace; font-size: 0.875rem;"},
                            "text": path,
                        }]
                        subtitle_parts = []
                        if target:
                            subtitle_parts.append(f"→ {target}")
                        if group_key and cat_id == "dup_resource":
                            subtitle_parts.append(f"分组: {group_key}")
                        if subtitle_parts:
                            text_children.append({
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis", "style": "word-break: break-all;"},
                                "text": " · ".join(subtitle_parts),
                            })

                        item_children = [{
                            "component": "div",
                            "props": {"class": "flex-grow-1", "style": "min-width: 0;"},
                            "content": text_children,
                        }]
                        if self._allow_delete:
                            item_children.append({
                                "component": "VBtn",
                                "props": {
                                    "icon": "mdi-delete",
                                    "color": "error",
                                    "variant": "text",
                                    "size": "small",
                                    "class": "ml-2",
                                    "events": {
                                        "click": {
                                            "api": delete_item_url,
                                            "method": "post",
                                            "params": {
                                                "path": path,
                                                "category": cat_id,
                                            },
                                        },
                                    },
                                },
                            })

                        rows.append({
                            "component": "VListItem",
                            "props": {"density": "compact"},
                            "content": [{
                                "component": "div",
                                "props": {"class": "d-flex align-center", "style": "width: 100%;"},
                                "content": item_children,
                            }],
                        })
                    inner = [{
                        "component": "VList",
                        "props": {"density": "compact", "lines": "two", "class": "pa-0"},
                        "content": rows,
                    }]
                    if result.get("truncated", {}).get(cat_id):
                        inner.append({
                            "component": "VAlert",
                            "props": {"type": "info", "variant": "tonal", "class": "mt-2"},
                            "text": f"已达到每类展示上限（{self._max_display_per_type}），如需查看更多请调整设置中的展示上限。",
                        })

            windows_content.append({
                "component": "VWindowItem",
                "props": {"value": cat_id},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "flat"},
                    "content": [{
                        "component": "VCardText",
                        "content": inner,
                    }],
                }],
            })

        tabs_block = {
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {
                    "component": "VTabs",
                    "props": {
                        "model": "active_tab",
                        "grow": True,
                        "showArrows": True,
                    },
                    "content": tabs_content,
                },
                {"component": "VDivider"},
                {
                    "component": "VWindow",
                    "props": {"model": "active_tab"},
                    "content": windows_content,
                },
            ],
        }

        page = [header]
        page.append({
            "component": "VCard",
            "props": {"variant": "flat", "class": "mb-3"},
            "content": [{
                "component": "VCardText",
                "content": dir_lines,
            }],
        })
        if error_block:
            page.append(error_block)
        page.append(tabs_block)
        return page

    def get_dashboard_meta(self) -> Optional[List[Dict[str, Any]]]:
        """暂不注册 Dashboard 组件。"""
        return None

    def get_render_mode(self) -> Tuple[str, Optional[str]]:
        """使用默认 VueJSON 渲染。"""
        return "vue", None
