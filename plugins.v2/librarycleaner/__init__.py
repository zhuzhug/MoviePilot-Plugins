"""媒体库清理（LibraryCleaner）插件。

首版 v0.1.0 目标：
- 扫描本地媒体库路径，识别 3 类默认清理项：
  1) 悬空软链（symlink 目标已不存在）
  2) 孤儿元数据（.nfo/.jpg/.png/.srt/.ass 等同目录无媒体视频）
  3) 空目录
- VTabs 分类展示扫描结果，支持手动"立即扫描"
- 默认试运行（Dry-run），只报不删
- 高级项（重复软/硬链、失联视频）与删除动作在 v0.2.0 补齐
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.plugins import _PluginBase
from app.schemas import NotificationType

logger = logging.getLogger(__name__)


# 元数据后缀（同目录下若只有这些文件而无视频，判定为孤儿元数据）
_METADATA_EXTS = {
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn",
    ".srt", ".ass", ".ssa", ".sup", ".vtt", ".idx", ".sub",
}

# 检测项元数据：id, 中文标题, 图标
_CATEGORY_META: List[Tuple[str, str, str]] = [
    ("dangling", "悬空软链", "mdi-link-variant-off"),
    ("orphan_meta", "孤儿元数据", "mdi-file-document-outline"),
    ("empty_dir", "空目录", "mdi-folder-open-outline"),
    ("dup_softlink", "重复软链接", "mdi-link-variant"),
    ("dup_hardlink", "重复硬链接", "mdi-file-multiple"),
    ("missing_video", "失联视频", "mdi-file-question-outline"),
]


class LibraryCleaner(_PluginBase):
    """媒体库清理插件。"""

    plugin_name = "媒体库清理"
    plugin_desc = "扫描媒体库残留：悬空软链、孤儿元数据、空目录（后续版本支持重复链接、失联视频与批量删除）。"
    plugin_icon = "clean.png"
    plugin_version = "0.1.0"
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
    _enable_dup_softlink: bool = False
    _enable_dup_hardlink: bool = False
    _enable_missing_video: bool = False
    _dry_run: bool = True
    _cron: str = "0 5 * * *"
    _notify: bool = False
    _include_regex: str = ""
    _exclude_regex: str = ""
    _max_display_per_type: int = 200

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
        self._enable_dup_softlink = bool(config.get("enable_dup_softlink", False))
        self._enable_dup_hardlink = bool(config.get("enable_dup_hardlink", False))
        self._enable_missing_video = bool(config.get("enable_missing_video", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._cron = str(config.get("cron", "") or "0 5 * * *")
        self._notify = bool(config.get("notify", False))
        self._include_regex = str(config.get("include_regex", "") or "")
        self._exclude_regex = str(config.get("exclude_regex", "") or "")
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
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """节点 A 暂不注册定时任务，v0.2.0 再启用 CRON。"""
        return []

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
            result["dry_run"] = self._dry_run
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
            "dry_run": True,
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
            "dry_run": result.get("dry_run", True),
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
            if summary.get("dry_run"):
                title += " [试运行]"
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
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "dry_run", "label": "试运行（只扫描不删除）"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_dup_softlink",
                                        "label": "检测重复软链接（v0.2.0）",
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
                                        "label": "检测重复硬链接（v0.2.0）",
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
                                        "label": "检测失联视频（v0.2.0）",
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
                                        "label": "定时扫描 CRON（v0.2.0 生效）",
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
                            "type": "info",
                            "variant": "tonal",
                            "class": "mt-2",
                        },
                        "text": "v0.1.0：默认试运行只扫描不删除；后三类检测与批量删除将在 v0.2.0 提供。手动扫描请打开插件详情页点击\"立即扫描\"。",
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
            "enable_dup_softlink": False,
            "enable_dup_hardlink": False,
            "enable_missing_video": False,
            "dry_run": True,
            "cron": "0 5 * * *",
            "notify": False,
            "include_regex": "",
            "exclude_regex": "",
            "max_display_per_type": 200,
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
                "color": "warning" if self._dry_run else "success",
                "variant": "tonal",
                "class": "mr-2",
                "size": "small",
            },
            "text": "试运行模式" if self._dry_run else "实际执行模式",
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
                    "text": f"「{cat_title}」将在 v0.2.0 版本提供。",
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
                        row_content = [{
                            "component": "VListItemTitle",
                            "props": {"style": "word-break: break-all; font-family: monospace;"},
                            "text": path,
                        }]
                        if target:
                            row_content.append({
                                "component": "VListItemSubtitle",
                                "props": {"class": "text-caption", "style": "word-break: break-all;"},
                                "text": f"→ {target}",
                            })
                        rows.append({
                            "component": "VListItem",
                            "props": {"density": "compact"},
                            "content": row_content,
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
