"""
媒体垃圾扫描插件

扫描媒体库中的垃圾文件：断链软链接、空目录、失败整理记录。
支持手动清理和定时扫描。
"""

import os
import json
import shutil
import hashlib
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 路径保留标记：任一层目录名命中即视为「勿删」，扫描阶段不出候选、删除阶段强制拒绝。
# 用于保种目录、用户上传区、用户自建区等不应参与清理的位置。
PATH_RESERVED_MARKERS = ("勿删", "保种", "契约", "勿动", "donotdelete", "no-delete", "keep")

# 删除方式："trash" 走回收站（可恢复），"delete" 直接删除
_DELETE_MODE_TRASH = "trash"
_DELETE_MODE_DELETE = "delete"



class MediaGarbageCleaner(_PluginBase):
    """资源清理插件（原名：媒体垃圾扫描）。"""

    plugin_name = "资源清理"
    plugin_desc = "扫描媒体库中的断链软链接、硬链接、重复文件、空目录、孤儿 strm、未整理资源与失败记录；支持按地址与名称保护、两级确认防误删、手动或批量清理。"
    plugin_icon = "mdi-broom"
    plugin_version = "1.9.3"
    plugin_label = "媒体整理"
    plugin_label = "媒体整理"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "mediagarbagecleaner_"
    plugin_order = 99
    auth_level = 1

    # 插件状态
    _enabled = False
    _exclude_dirs: List[str] = []
    _protect_name_keywords: List[str] = []  # 按名称保护喜欢的作品（不区分大小写，| 分隔），命中名称的不扫描/不清理
    _dup_only_video: bool = True  # 重复检测默认只针对视频类（占空间资源），跳过图片等小文件
    _video_exts: set = set()  # 视频扩展名集合（来自 settings.RMT_MEDIAEXT）
    _orphan_scan_enabled: bool = False  # 孤儿 strm 扫描
    _orphan_scan_source_dirs: List[str] = []  # 源目录白名单
    _orphan_scan_keep_disks: List[str] = []  # 保留的网盘名关键词
    _untransfer_scan_enabled: bool = False  # 下载目录未整理资源扫描
    _untransfer_exclude_dirs: List[str] = []  # 未整理资源排除目录
    _untransfer_exclude_keywords: str = ""  # 未整理资源排除关键词（文件名/父目录名包含则跳过）
    _scan_results: Dict[str, Any] = {}
    _selected: Dict[str, str] = {}  # 已选中的项目 key -> 标识符
    _delete_item_confirmed: bool = False  # 单条删除确认令牌（一次性）

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._exclude_dirs = []
        self._scan_results = {}
        self._selected = {}
        self._pending_delete = None
        self._delete_item_confirmed = False
        self._untransfer_scan_enabled = False
        self._untransfer_exclude_dirs = []
        self._untransfer_exclude_keywords = ""
        self._reserved_keywords = []
        self._delete_mode = _DELETE_MODE_DELETE
        self._orphan_scan_enabled = False
        self._orphan_scan_source_dirs = []
        self._orphan_scan_keep_disks = []
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._untransfer_scan_enabled = bool(config.get("untransfer_scan_enabled", False))
        self._untransfer_exclude_dirs = self._normalize_path_list(config.get("untransfer_exclude_dirs") or [])
        self._untransfer_exclude_keywords = str(config.get("untransfer_exclude_keywords") or "")
        # 路径保留关键词：路径任一层命中即永久豁免（扫描与删除都不再触碰）
        rk_raw = config.get("reserved_keywords") or ""
        if isinstance(rk_raw, str):
            self._reserved_keywords = [k.strip().lower() for k in rk_raw.split("|") if k.strip()]
        elif isinstance(rk_raw, list):
            self._reserved_keywords = [str(k).strip().lower() for k in rk_raw if str(k).strip()]
        else:
            self._reserved_keywords = []
        self._delete_mode = _DELETE_MODE_TRASH if config.get("delete_to_trash") else _DELETE_MODE_DELETE
        # 回收站依赖检查：缺失时删除会回退为永久删除，需在启动日志里明确告知
        if self._delete_mode == _DELETE_MODE_TRASH:
            try:
                import send2trash  # noqa: F401
            except Exception:
                self._delete_mode = _DELETE_MODE_DELETE
                logger.warning("未检测到 send2trash，回收站删除不可用，已回退为直接删除；"
                               "请执行 pip install send2trash 启用可恢复删除")
        exclude = config.get("exclude_dirs") or []
        self._exclude_dirs = self._normalize_path_list(exclude)
        # 名称保护名单：喜欢的电影/剧集按名称关键词保护，命中名称的不扫描/不展示/不删
        pk_raw = config.get("protect_name_keywords") or ""
        if isinstance(pk_raw, str):
            self._protect_name_keywords = [
                k.strip().lower() for k in pk_raw.split("|") if k.strip()
            ]
        elif isinstance(pk_raw, list):
            self._protect_name_keywords = [str(k).strip().lower() for k in pk_raw if str(k).strip()]
        else:
            self._protect_name_keywords = []
        # 重复检测范围：默认只扫视频类资源（占空间），设为 False 则覆盖所有类型
        self._dup_only_video = bool(config.get("dup_only_video", True))
        try:
            self._video_exts = {e.lower() for e in getattr(settings, "RMT_MEDIAEXT", [])}
        except Exception:
            self._video_exts = set()
        self._scan_results = self.get_data("scan_results") or {}
        # 孤儿 strm 扫描配置
        self._orphan_scan_enabled = bool(config.get("orphan_scan_enabled", False))
        self._orphan_scan_source_dirs = self._normalize_path_list(config.get("orphan_scan_source_dirs") or [])
        okd = config.get("orphan_scan_keep_disks") or []
        if isinstance(okd, str):
            okd = [p.strip() for p in okd.split("|") if p.strip()]
        self._orphan_scan_keep_disks = [p.strip() for p in okd if isinstance(p, str) and p.strip()]

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
            {"path": "/scan", "endpoint": self._scan_all, "methods": ["GET"], "summary": "执行全量扫描", "auth": "bear"},
            {"path": "/results", "endpoint": self._get_results, "methods": ["GET"], "summary": "获取扫描结果", "auth": "bear"},
            {"path": "/delete", "endpoint": self._delete_item, "methods": ["POST"], "summary": "删除单个垃圾项", "auth": "bear"},
            {"path": "/delete_all", "endpoint": self._delete_all, "methods": ["POST"], "summary": "删除所有垃圾项", "auth": "bear"},
            {"path": "/toggle_select", "endpoint": self._toggle_select, "methods": ["POST"], "summary": "切换选中状态", "auth": "bear"},
            {"path": "/select_clear", "endpoint": self._select_clear, "methods": ["GET"], "summary": "清空所有选中", "auth": "bear"},
            {"path": "/select_category", "endpoint": self._select_category, "methods": ["GET"], "summary": "按分类全选/反选可见项目", "auth": "bear"},
            {"path": "/batch_delete_selected", "endpoint": self._batch_delete_selected, "methods": ["POST"], "summary": "删除已选中的项目", "auth": "bear"},
            {"path": "/request_delete", "endpoint": self._request_delete, "methods": ["GET"], "summary": "发起删除请求（进入待确认）", "auth": "bear"},
            {"path": "/advance_delete", "endpoint": self._advance_delete, "methods": ["GET"], "summary": "进入最终确认", "auth": "bear"},
            {"path": "/confirm_delete", "endpoint": self._confirm_delete, "methods": ["GET"], "summary": "确认执行删除", "auth": "bear"},
            {"path": "/cancel_delete", "endpoint": self._cancel_delete, "methods": ["GET"], "summary": "取消待确认删除", "auth": "bear"},
            {"path": "/refresh", "endpoint": self._refresh, "methods": ["GET"], "summary": "刷新当前结果视图", "auth": "bear"},
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                    {"component": "VSwitch", "props": {"model": "dup_only_video", "label": "重复检测仅限视频类（占空间资源，跳过图片/字幕等小文件）"}},
                    {"component": "VCombobox", "props": {
                        "model": "exclude_dirs",
                        "label": "排除目录（按地址过滤，不扫描/不清理这些地址下的内容）",
                        "items": self._exclude_dir_options(),
                        "multiple": True,
                        "chips": True,
                        "clearable": True,
                        "deletableChips": True,
                        "delimiters": [",", "\n"],
                        "placeholder": "选择或输入要排除的路径，如 /media/tv/未分类/anistrm",
                    }},
                    {"component": "VTextField", "props": {
                        "model": "protect_name_keywords",
                        "label": "保护作品名称（喜欢的资源，不区分大小写，|分隔）",
                        "clearable": True,
                        "placeholder": "如: 寻梦环游记|鬼灭之刃|权力的游戏",
                    }},
                    {"component": "VSwitch", "props": {"model": "delete_to_trash", "label": "删除走回收站（可恢复；回收站不可用时自动回退直接删除）"}},
                    {"component": "VTextField", "props": {
                        "model": "reserved_keywords",
                        "label": "保留关键词（| 分隔；路径任一层目录名命中即永久豁免，不扫描不删除）",
                        "clearable": True,
                        "placeholder": "如: 保种|契约|上传",
                        "hint": "内置：勿删、保种、契约、勿动、donotdelete",
                    }},
                    {"component": "VSwitch", "props": {"model": "orphan_scan_enabled", "label": "孤儿 strm 扫描（源目录已删除但媒体库仍有残留的 strm/nfo）"}},
                    {"component": "VSwitch", "props": {"model": "untransfer_scan_enabled", "label": "下载目录未整理资源扫描（从未经整理的媒体文件，可能是堆积垃圾）"}},
                    {"component": "VCombobox", "props": {
                        "model": "untransfer_exclude_dirs",
                        "label": "未整理资源排除目录（这些目录下的文件不参与扫描）",
                        "items": self._untransfer_exclude_options(),
                        "multiple": True,
                        "chips": True,
                        "clearable": True,
                        "deletableChips": True,
                        "delimiters": [",", "\n"],
                        "placeholder": "如: /media/downloads/BT下载/保种契约勿删",
                    }},
                    {"component": "VTextField", "props": {
                        "model": "untransfer_exclude_keywords",
                        "label": "未整理资源排除关键词（| 分隔，文件名或父目录名包含则跳过）",
                        "clearable": True,
                        "placeholder": "如: 保种|种子|seed",
                        "hint": "不区分大小写，匹配文件名和父目录名",
                    }},
                    {"component": "VCombobox", "props": {
                        "model": "orphan_scan_source_dirs",
                        "label": "源目录列表（按此目录里存在的 strm 判断整理目录里哪些是孤儿）",
                        "items": self._orphan_source_options(),
                        "multiple": True,
                        "chips": True,
                        "clearable": True,
                        "deletableChips": True,
                        "delimiters": [",", "\n"],
                        "placeholder": "如: /media/downloads/网盘/115网盘, /media/downloads/网盘/夸克云盘",
                    }},
                    {"component": "VTextField", "props": {
                        "model": "orphan_scan_keep_disks",
                        "label": "保留的网盘名关键词（白名单，| 分隔；未命中的 strm 视为孤儿）",
                        "clearable": True,
                        "placeholder": "如: 115网盘|夸克云盘",
                        "hint": "留空时按源目录白名单比对；非空时优先按网盘名关键词过滤",
                    }},
                ],
            }
        ], {"enabled": False, "exclude_dirs": [], "dup_only_video": True, "protect_name_keywords": "",
            "orphan_scan_enabled": False, "orphan_scan_source_dirs": [], "orphan_scan_keep_disks": "",
            "untransfer_scan_enabled": False, "delete_to_trash": True, "reserved_keywords": ""}

    # ==================== 目录候选（下拉选项） ====================

    @staticmethod
    def _load_dir_configs() -> List[Any]:
        """读取系统配置的下载/媒体库目录。

        优先使用 DirectoryHelper（当前版本入口），旧宿主回退到 DirectoryOper。
        """
        try:
            from app.helper.directory import DirectoryHelper
            confs = DirectoryHelper().get_dirs()
            if confs:
                return list(confs)
        except Exception:
            pass
        try:
            from app.db.directory_oper import DirectoryOper  # 旧版宿主兼容
            return list(DirectoryOper().list() or [])
        except Exception:
            return []

    @staticmethod
    def _classification_paths() -> List[str]:
        """读取分类策略中启用的媒体库分类路径（用于下拉候选）。"""
        paths: List[str] = []
        try:
            from app.db.systemconfig_oper import SystemConfigOper
            raw = SystemConfigOper().get("MediaClassificationPolicy")
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                for cat in ((raw.get("active") or {}).get("categories") or []):
                    if not isinstance(cat, dict) or not cat.get("enabled", True):
                        continue
                    for p in cat.get("path") or []:
                        s = str(p).strip().rstrip("/") if p else ""
                        if s and s not in paths:
                            paths.append(s)
        except Exception:
            pass
        return paths

    @staticmethod
    def _disk_subdirs(base: str, depth: int = 1, limit: int = 200) -> List[str]:
        """列出指定目录下的子目录（限定层级与数量，用于下拉候选）。"""
        out: List[str] = []
        if not base or not os.path.isdir(base):
            return out
        base_norm = base.rstrip(os.sep)
        base_depth = base_norm.count(os.sep)
        try:
            for root, dirs, _ in os.walk(base_norm):
                cur_depth = root.rstrip(os.sep).count(os.sep) - base_depth
                if cur_depth >= depth:
                    dirs[:] = []
                    continue
                dirs[:] = sorted(
                    d for d in dirs
                    if not d.startswith(".") and d not in ("@eaDir", "#recycle")
                )
                for d in dirs:
                    out.append(os.path.join(root, d))
                    if len(out) >= limit:
                        return out
        except OSError:
            pass
        return out

    @staticmethod
    def _dedupe_paths(paths: List[str]) -> List[Dict[str, str]]:
        """去重并转换为下拉选项（保持输入顺序）。"""
        seen = set()
        opts: List[Dict[str, str]] = []
        for p in paths:
            if p is None:
                continue
            s = str(p).strip()
            if not s:
                continue
            s = s.rstrip("/") or s
            if s in seen:
                continue
            seen.add(s)
            opts.append({"title": s, "value": s})
        return opts

    def _exclude_dir_options(self) -> List[Dict[str, str]]:
        """构建排除目录下拉候选。

        来源：系统目录配置（下载/媒体库）→ 分类策略路径 → 磁盘一级子目录 →
        已扫描结果父目录 → 当前已保存值。
        """
        candidates: List[str] = []

        for d in self._load_dir_configs():
            for k in ("download_path", "library_path"):
                v = getattr(d, k, None)
                if v:
                    candidates.append(str(v))

        candidates.extend(self._classification_paths())

        for base in ("/media/tv", "/media/movie", "/media/music", "/media/downloads"):
            candidates.extend(self._disk_subdirs(base, depth=1))

        for cat in ("broken_symlinks", "hardlinks", "duplicates", "empty_dirs",
                    "orphan_streams", "untransferred"):
            for item in (self._scan_results or {}).get(cat, [])[:200]:
                p = item.get("path", "")
                if p:
                    candidates.append(os.path.dirname(p))

        candidates.extend(self._exclude_dirs or [])

        return self._dedupe_paths(candidates)

    @staticmethod
    def _detect_disk_from_strm(content: str) -> Optional[str]:
        """从 strm 内容里解析出网盘类型名。"""
        if not content:
            return None
        if ":9527" in content:
            return "115网盘"
        if "/d/" in content:
            try:
                rel = content.split("/d/", 1)[1].split("?", 1)[0]
                first_seg = urllib.parse.unquote(rel.split("/", 1)[0])
                if "115" in first_seg:
                    return "115网盘"
                if "夸克" in first_seg:
                    return "夸克云盘"
                if "迅雷" in first_seg:
                    return "迅雷云盘"
                if "百度" in first_seg or "百度云" in first_seg:
                    return "百度网盘"
                for kw in ("阿里云盘", "天翼云盘", "移动云盘", "123云盘", "城通网盘", "新浪微盘"):
                    if kw in first_seg:
                        return kw
                return first_seg
            except Exception:
                return None
        return None

    @staticmethod
    def _collect_source_dir_names(source_dirs: List[str]) -> set:
        """遍历源目录列表，收集所有 strm 父级文件夹的 basename 集合。"""
        names = set()
        for base in source_dirs:
            if not os.path.isdir(base):
                continue
            try:
                for root, dirs, files in os.walk(base):
                    for f in files:
                        if f.endswith(".strm"):
                            names.add(os.path.basename(root))
            except Exception:
                continue
        return names

    def _orphan_source_options(self) -> List[Dict[str, str]]:
        """构建源目录下拉候选：网盘源目录 + 系统下载目录 + 常见下载根 + 已保存值。"""
        candidates: List[str] = []

        cloud_root = "/media/downloads/网盘"
        if os.path.isdir(cloud_root):
            candidates.append(cloud_root)
            candidates.extend(self._disk_subdirs(cloud_root, depth=2))

        for d in self._load_dir_configs():
            dp = getattr(d, "download_path", None)
            if dp:
                root = str(dp)
                candidates.append(root)
                candidates.extend(self._disk_subdirs(root.rstrip("/") or root, depth=1))

        for extra in ("/media/downloads", "/media/downloads/BT下载", "/media/downloads/短剧",
                      "/media/downloads/Anistrm", "/media/downloads/音乐"):
            if os.path.isdir(extra):
                candidates.append(extra)

        candidates.extend(self._orphan_scan_source_dirs or [])

        return self._dedupe_paths(candidates)

    def _untransfer_exclude_options(self) -> List[Dict[str, str]]:
        """构建未整理资源排除目录下拉候选：下载目录 + 其子目录 + 扫描结果父目录 + 已保存值。"""
        candidates: List[str] = []

        dl_roots: List[str] = []
        for d in self._load_dir_configs():
            dp = getattr(d, "download_path", None)
            if dp:
                dl_roots.append(str(dp))
        for extra in ("/media/downloads", "/media/downloads/BT下载", "/media/downloads/网盘",
                      "/media/downloads/短剧", "/media/downloads/Anistrm", "/media/downloads/音乐"):
            if os.path.isdir(extra):
                dl_roots.append(extra)

        for root in dl_roots:
            root_norm = root.rstrip("/") or root
            candidates.append(root_norm)
            candidates.extend(self._disk_subdirs(root_norm, depth=1))

        for item in (self._scan_results or {}).get("untransferred", [])[:200]:
            parent = os.path.dirname(item.get("path", ""))
            if parent:
                candidates.append(parent)

        candidates.extend(self._untransfer_exclude_dirs or [])

        return self._dedupe_paths(candidates)

    def _normalize_path_list(self, raw: Any) -> List[str]:
        """把 VCombobox 提交的 {title,value} 字典列表或纯字符串列表归一化为纯路径字符串列表。

        兼容旧版换行分隔字符串（exclude_dirs 早期用 VTextarea 存储）。
        """
        if raw is None:
            return []
        if isinstance(raw, str):
            return [d.strip() for d in raw.split("\n") if d.strip()]
        result: List[str] = []
        for item in raw if isinstance(raw, (list, tuple, set)) else []:
            if isinstance(item, str):
                if item.strip():
                    result.append(item.strip())
            elif isinstance(item, dict):
                val = item.get("value")
                if val and str(val).strip():
                    result.append(str(val).strip())
        return result

    @staticmethod
    def _stat_card(title: str, value: str, icon: str, color: str, subtitle: str) -> dict:
        """对齐 MP 运维助手 的 _status_card 风格。"""
        # 有内容时加 badge 圆点提示
        icon_node: dict = {"component": "VIcon", "props": {"icon": icon, "size": "28"}}
        try:
            if int(value) > 0:
                icon_node = {
                    "component": "VBadge", "props": {"modelValue": True, "color": "error", "dot": True, "offsetX": 4, "offsetY": 4},
                    "content": [icon_node],
                }
        except (ValueError, TypeError):
            pass
        return {
            "component": "VCol", "props": {"cols": 6, "md": 3},
            "content": [{
                "component": "VCard", "props": {"variant": "tonal", "color": color, "class": "h-100 mb-4"},
                "content": [{
                    "component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "d-flex align-center justify-space-between mb-2"}, "content": [
                            {"component": "div", "props": {"class": "text-caption"}, "text": title},
                            icon_node,
                        ]},
                        {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": value},
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"}, "text": subtitle},
                    ]
                }]
            }]
        }

    @staticmethod
    def _action_button(text: str, icon: str, color: str, api: str, method: str = "post", disabled: bool = False) -> dict:
        """对齐 MP 运维助手 的 _action_button 风格：块级按钮 + tonal 配色。"""
        btn = {
            "component": "VBtn",
            "props": {
                "block": True,
                "variant": "tonal",
                "color": color,
                "prepend-icon": icon,
                "class": "text-none mb-2",
                "disabled": disabled,
            },
            "text": text,
            "events": {"click": {"api": api, "method": method}},
        }
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [btn],
        }

    @staticmethod
    def _section_header_btn(text: str, icon: str, api: str) -> dict:
        """分类卡片头部的紧凑动作按钮（全选/反选）。"""
        return {
            "component": "VBtn",
            "props": {
                "size": "x-small", "variant": "tonal", "color": "primary",
                "prepend-icon": icon, "class": "text-none ml-1",
            },
            "text": text,
            "events": {"click": {"api": api, "method": "get"}},
        }

    @staticmethod
    def _section_card(title: str, icon: str, color: str, count: int, rows: Optional[List[dict]],
                      header_actions: Optional[List[dict]] = None) -> dict:
        """带图标标题与计数 chip 的分类卡片。header_actions 为卡片标题右侧的紧凑按钮（如按分类全选/反选）。"""
        header_content = [
            {"component": "VIcon", "props": {"icon": icon, "color": color, "class": "mr-2", "size": "small"}},
            {"component": "span", "text": title},
            {"component": "VSpacer"},
        ]
        if header_actions:
            header_content.extend(header_actions)
        header_content.append(
            {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": color}, "text": f"{count} 项"}
        )
        if rows:
            body = {"component": "VList", "props": {"density": "compact", "class": "py-2"}, "content": rows}
        else:
            body = {"component": "VCardText", "props": {"class": "text-center text-caption text-medium-emphasis py-4"}, "text": "无此项"}
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-4"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-2 d-flex align-center px-4 py-2"},
                    "content": header_content,
                },
                {"component": "VDivider"},
                body,
            ],
        }

    @staticmethod
    def _item_row(key: str, title: str, subtitle: str, is_selected: bool,
                  toggle_api: str, delete_api: str, delete_params: dict,
                  image: str = "") -> dict:
        """圆角描边列表行，选中态强对比高亮（深浅主题均明显）。

        参考辅种查看 v0.5.13 的教训：Vuetify VBtn 的 ``variant="text"`` + 图标色相近时
        视觉存在感极弱，列表选择控件默认用 outlined/flat 才有区分度。这里选中态用
        绿色左边框 + 浅绿底 + 实心绿 checkbox + 绿字标题 四重标记。
        """
        if is_selected:
            row_class = "rounded border mb-1 px-2 bg-success-lighten-4"
            row_style = "border-left: 4px solid #66BB6A;"
            title_class = "text-caption text-success"
            title_style = "font-family: monospace; word-break: break-all;"
            checkbox_variant = "flat"
            checkbox_color = "success"
            checkbox_icon = "mdi-checkbox-marked"
        else:
            row_class = "rounded border mb-1 px-2"
            row_style = ""
            title_class = "text-caption"
            title_style = "font-family: monospace; word-break: break-all;"
            checkbox_variant = "outlined"
            checkbox_color = "grey-darken-1"
            checkbox_icon = "mdi-checkbox-blank-outline"
        checkbox = {
            "component": "VBtn",
            "props": {
                "icon": checkbox_icon,
                "size": "small",
                "variant": checkbox_variant,
                "color": checkbox_color,
                "class": "ma-0 pa-0 mr-2",
            },
            "events": {"click": {"api": toggle_api, "method": "post", "params": {"key": key}}},
        }
        title_block = {
            "component": "VListItemTitle",
            "props": {"class": title_class, "style": title_style},
            "text": title,
        }
        if subtitle:
            sub_block = {
                "component": "VListItemSubtitle",
                "props": {"class": "text-caption", "style": "color: #ff5252;"},
                "text": subtitle,
            }
        else:
            sub_block = {"component": "div", "props": {"class": "d-none"}}
        delete_btn = {
            "component": "VBtn",
            "props": {
                "size": "x-small",
                "variant": "flat",
                "color": "error",
                "density": "comfortable",
            },
            "text": "删除",
            "events": {"click": {"api": delete_api, "method": "post", "params": delete_params}},
        }
        list_props: Dict[str, Any] = {"density": "compact", "class": row_class}
        if row_style:
            list_props["style"] = row_style
        row_content = [checkbox]
        if image:
            row_content.append({
                "component": "VImg",
                "props": {"src": image, "width": 36, "height": 50, "cover": True, "class": "rounded mr-2", "style": "flex-shrink: 0;"},
            })
        row_content.extend([title_block, sub_block, {"component": "VSpacer"}, delete_btn])
        return {
            "component": "VListItem",
            "props": list_props,
            "content": row_content,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面：对齐「MP 运维助手」仪表盘风格的扫描结果与操作区。"""
        results = self._scan_results
        summary = results.get("summary", {})
        broken = results.get("broken_symlinks", [])
        empty = results.get("empty_dirs", [])
        failed = results.get("failed_transfers", [])

        total = summary.get("total", 0)
        has_results = total > 0

        api_token = settings.API_TOKEN
        scan_api = f"plugin/MediaGarbageCleaner/scan?token={api_token}"
        delete_all_api = f"plugin/MediaGarbageCleaner/delete_all?token={api_token}"
        delete_api = f"plugin/MediaGarbageCleaner/delete?token={api_token}"
        toggle_api = f"plugin/MediaGarbageCleaner/toggle_select?token={api_token}"
        select_clear_api = f"plugin/MediaGarbageCleaner/select_clear?token={api_token}"
        batch_delete_api = f"plugin/MediaGarbageCleaner/batch_delete_selected?token={api_token}"
        refresh_api = f"plugin/MediaGarbageCleaner/refresh?token={api_token}"
        # 两级确认删除（防误删）
        request_selected_api = f"plugin/MediaGarbageCleaner/request_delete?mode=selected&token={api_token}"
        request_all_api = f"plugin/MediaGarbageCleaner/request_delete?mode=all&token={api_token}"
        advance_delete_api = f"plugin/MediaGarbageCleaner/advance_delete?token={api_token}"
        confirm_delete_api = f"plugin/MediaGarbageCleaner/confirm_delete?token={api_token}"
        cancel_delete_api = f"plugin/MediaGarbageCleaner/cancel_delete?token={api_token}"

        # 每个分类各自的全选/反选按钮（按分组独立选择，不互相干扰）
        cat_sel = lambda prefix: [
            self._section_header_btn("全选", "mdi-select-all",
                                     f"plugin/MediaGarbageCleaner/select_category?category={prefix}&mode=all&token={api_token}"),
            self._section_header_btn("反选", "mdi-select-inverse",
                                     f"plugin/MediaGarbageCleaner/select_category?category={prefix}&mode=invert&token={api_token}"),
        ]

        selected = self._selected or {}
        selected_count = len(selected)

        # 顶部提醒区：待确认删除（两级） > 选中摘要 > 说明
        head_alerts: List[dict] = []
        pending = self._pending_delete
        if pending:
            stage = int(pending.get("stage") or 1)
            p_count = pending.get("count", 0)
            p_size = self._format_size(int(pending.get("size") or 0))
            p_mode = "全部删除" if pending.get("mode") == "all" else "删除选中"
            if stage <= 1:
                head_alerts.append({
                    "component": "VAlert",
                    "props": {"type": "warning", "variant": "flat", "class": "mb-4"},
                    "content": [
                        {"component": "div", "props": {"class": "text-subtitle-2 mb-1"}, "text": f"待确认：{p_mode}"},
                        {"component": "div", "props": {"class": "text-body-2 mb-2"},
                         "text": f"将删除 {p_count} 项，预计释放 {p_size}。此操作不可撤销，请再次确认。"},
                        {"component": "div", "props": {"class": "d-flex ga-2"}, "content": [
                            {"component": "VBtn", "props": {"size": "small", "color": "warning", "variant": "flat"},
                             "text": "继续确认",
                             "events": {"click": {"api": advance_delete_api, "method": "get"}}},
                            {"component": "VBtn", "props": {"size": "small", "color": "grey", "variant": "text"},
                             "text": "取消",
                             "events": {"click": {"api": cancel_delete_api, "method": "get"}}},
                        ]},
                    ],
                })
            else:
                head_alerts.append({
                    "component": "VAlert",
                    "props": {"type": "error", "variant": "flat", "class": "mb-4"},
                    "content": [
                        {"component": "div", "props": {"class": "text-subtitle-2 mb-1"}, "text": f"最后确认：{p_mode}"},
                        {"component": "div", "props": {"class": "text-body-2 mb-2"},
                         "text": f"即将永久删除 {p_count} 项（{p_size}）。确认后无法恢复！"},
                        {"component": "div", "props": {"class": "d-flex ga-2"}, "content": [
                            {"component": "VBtn", "props": {"size": "small", "color": "error", "variant": "flat",
                                                             "prepend-icon": "mdi-delete-alert"},
                             "text": "确认删除（不可撤销）",
                             "events": {"click": {"api": confirm_delete_api, "method": "get"}}},
                            {"component": "VBtn", "props": {"size": "small", "color": "grey", "variant": "text"},
                             "text": "取消",
                             "events": {"click": {"api": cancel_delete_api, "method": "get"}}},
                        ]},
                    ],
                })
        if selected_count:
            try:
                sel_size = self._format_size(self._sum_items_size(self._resolve_selected_items()))
            except Exception:
                sel_size = "未知"
            head_alerts.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mb-4"},
                "text": f"已选中 {selected_count} 项，预计释放 {sel_size}。点击「删除选中」后需经过二次确认才会执行。",
            })

        page: List[dict] = head_alerts + [
            # 顶部说明
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mb-4"},
             "text": "扫描媒体库中的断链软链接、硬链接、重复文件、空目录与失败整理记录。每一类可单独「全选/反选」，选中后批量清理或逐项删除。删除后会自动刷新并推送通知。"},
            # 安全说明
            {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "density": "compact", "class": "mb-4"},
             "text": "保护：命中「勿删/保种/契约」等标记的路径不扫描不删除；下载器仍在做种或下载的文件不会列为未整理资源；删除走回收站可恢复（可在设置页关闭）。"},
            # 统计卡片（对齐运维助手 tonal 卡片）
            {"component": "VRow", "content": [
                self._stat_card("断链软链接", str(summary.get("broken_symlinks", 0)), "mdi-link-variant-off", "error", "指向已丢失的目标"),
                self._stat_card("硬链接", str(summary.get("hardlinks", 0)), "mdi-link-variant", "secondary", "可清理的冗余硬链"),
                self._stat_card("重复文件", str(summary.get("duplicates", 0)), "mdi-file-compare", "deep-purple", "内容相同的独立副本"),
                self._stat_card("空目录", str(summary.get("empty_dirs", 0)), "mdi-folder-remove-outline", "warning", "无内容的目录"),
            ]},
            # 动作按钮（网格块级按钮）
            {"component": "VRow", "content": [
                self._action_button("开始扫描", "mdi-magnify-scan", "primary", scan_api, method="get"),
                self._action_button(f"删除选中 ({selected_count})" if selected_count else "删除选中", "mdi-delete", "error", request_selected_api, method="get", disabled=selected_count == 0),
                self._action_button(f"全部删除 ({total})" if total else "全部删除", "mdi-delete-alert", "error", request_all_api, method="get", disabled=not has_results),
                self._action_button("清空选择", "mdi-close-circle", "grey", select_clear_api, method="get", disabled=selected_count == 0),
            ]},
            # 第二行：刷新
            {"component": "VRow", "content": [
                self._action_button("刷新视图", "mdi-refresh", "info", refresh_api, method="get", disabled=not has_results),
            ]},
        ]

        # 断链软链接
        if broken:
            rows = []
            for i, item in enumerate(broken[:100]):
                path = item.get("path", "")
                key = f"b:{i}"
                display = path if len(path) <= 80 else "…" + path[-77:]
                rows.append(self._item_row(
                    key=key, title=display, subtitle=f"目标：{item.get('target', '')}" if item.get("target") else "",
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "broken_symlink", "path": path},
                ))
            page.append(self._section_card("断链软链接", "mdi-link-variant-off", "error", len(broken), rows, header_actions=cat_sel("b")))

        # 硬链接
        hardlinks = results.get("hardlinks", [])
        if hardlinks:
            rows = []
            for i, item in enumerate(hardlinks[:100]):
                path = item.get("path", "")
                key = f"h:{i}"
                display = path if len(path) <= 80 else "…" + path[-77:]
                sub = f"inode {item.get('inode', '')}｜共 {item.get('link_count', 1)} 个链接"
                rows.append(self._item_row(
                    key=key, title=display, subtitle=sub,
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "hardlink", "path": path},
                ))
            page.append(self._section_card("硬链接（可清理冗余）", "mdi-link-variant", "secondary", len(hardlinks), rows, header_actions=cat_sel("h")))

        # 重复文件
        duplicates = results.get("duplicates", [])
        if duplicates:
            rows = []
            for i, item in enumerate(duplicates[:100]):
                path = item.get("path", "")
                key = f"d:{i}"
                display = path if len(path) <= 80 else "…" + path[-77:]
                group = item.get("group_count", 1)
                keep = item.get("keep")
                sub = f"同组共 {group} 份" + ("｜建议保留" if keep else "｜可清理")
                rows.append(self._item_row(
                    key=key, title=display, subtitle=sub,
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "duplicate", "path": path},
                ))
            page.append(self._section_card("重复文件（内容相同）", "mdi-file-compare", "deep-purple", len(duplicates), rows, header_actions=cat_sel("d")))

        # 空目录
        if empty:
            rows = []
            for i, item in enumerate(empty[:100]):
                path = item.get("path", "")
                key = f"e:{i}"
                display = path if len(path) <= 80 else "…" + path[-77:]
                rows.append(self._item_row(
                    key=key, title=display, subtitle="",
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "empty_dir", "path": path},
                ))
            page.append(self._section_card("空目录", "mdi-folder-remove-outline", "warning", len(empty), rows, header_actions=cat_sel("e")))

        # 失败整理记录
        if failed:
            rows = []
            for i, item in enumerate(failed[:100]):
                key = f"f:{i}"
                title = f"{item.get('title', '未知')}（{item.get('year', '')}）"
                if item.get("dest"):
                    title = f"{title}\n→ {item.get('dest')}"
                rows.append(self._item_row(
                    key=key, title=title, subtitle=(item.get("errmsg", "") or "")[:80],
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "failed_transfer", "id": item.get("id")},
                    image=item.get("image", ""),
                ))
            page.append(self._section_card("失败整理记录", "mdi-alert-circle-outline", "info", len(failed), rows, header_actions=cat_sel("f")))

        # 孤儿 strm（源目录已删但媒体库仍有残留）
        orphans = results.get("orphan_streams", [])
        if orphans:
            rows = []
            for i, item in enumerate(orphans[:100]):
                path = item.get("path", "")
                key = f"s:{i}"
                display = path if len(path) <= 80 else "…" + path[-77:]
                disk = item.get("disk", "")
                sub = f"网盘: {disk}" if disk else ""
                rows.append(self._item_row(
                    key=key, title=display, subtitle=sub,
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "orphan_stream", "path": path},
                ))
            page.append(self._section_card("孤儿 strm（源目录已删）", "mdi-cloud-off", "deep-orange", len(orphans), rows, header_actions=cat_sel("s")))

        # 下载目录未整理资源
        untransferred = results.get("untransferred", [])
        if untransferred:
            rows = []
            for i, item in enumerate(untransferred[:100]):
                path = item.get("path", "")
                key = f"u:{i}"
                display = path if len(path) <= 80 else "…" + path[-77:]
                size_str = self._format_size(item.get("size", 0))
                sub = f"目录: {item.get('parent', '')}｜大小: {size_str}"
                rows.append(self._item_row(
                    key=key, title=display, subtitle=sub,
                    is_selected=key in selected, toggle_api=toggle_api,
                    delete_api=delete_api, delete_params={"type": "untransferred", "path": path},
                ))
            page.append(self._section_card("下载目录未整理资源", "mdi-download-off", "cyan", len(untransferred), rows, header_actions=cat_sel("u")))

        # 未扫描占位
        if not has_results:
            page.append({
                "component": "VCard", "props": {"variant": "outlined", "class": "mb-4"},
                "content": [{
                    "component": "VCardText", "props": {"class": "text-center py-8"},
                    "content": [
                        {"component": "VIcon", "props": {"size": "48", "color": "grey", "class": "mb-2"}, "text": "mdi-magnify-scan"},
                        {"component": "div", "props": {"class": "text-subtitle-1 mt-2"}, "text": "点击上方「开始扫描」按钮"},
                    ],
                }],
            })

        return page

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件后台定时服务列表（已移除定时扫描功能）。"""
        return []

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        try:
            self._scan_results = {}
        except Exception:
            pass

    # ==================== 扫描逻辑 ====================

    def _scan_all(self) -> dict:
        """执行全量扫描，返回所有垃圾项。

        每个分类独立捕获异常：单个扫描方法失败只降级该分类为空列表，
        不影响其它分类，也不让整次扫描返回 500 导致用户误以为什么都没扫到。
        """
        scanners = (
            ("broken_symlinks", self._scan_broken_symlinks),
            ("hardlinks", self._scan_hardlinks),
            ("duplicates", self._scan_duplicates),
            ("empty_dirs", self._scan_empty_dirs),
            ("failed_transfers", self._scan_failed_transfers),
            ("orphan_streams", self._scan_orphan_streams),
            ("untransferred", self._scan_untransferred),
        )
        results = {}
        for key, fn in scanners:
            try:
                results[key] = fn()
            except Exception as e:
                logger.error(f"扫描分类 {key} 出错，已降级为空列表: {e}")
                results[key] = []
        results["summary"] = {
            "broken_symlinks": len(results["broken_symlinks"]),
            "hardlinks": len(results["hardlinks"]),
            "duplicates": len(results["duplicates"]),
            "empty_dirs": len(results["empty_dirs"]),
            "failed_transfers": len(results["failed_transfers"]),
            "orphan_streams": len(results["orphan_streams"]),
            "untransferred": len(results["untransferred"]),
            "total": len(results["broken_symlinks"]) + len(results["hardlinks"]) + len(results["duplicates"])
            + len(results["empty_dirs"]) + len(results["failed_transfers"]) + len(results["orphan_streams"])
            + len(results["untransferred"]),
        }
        self._scan_results = results
        # 重新扫描后旧的选中/待确认状态已失效：选中项存的是「分类:序号」，
        # 结果列表一旦变化，同一序号指向的就是另一个文件，必须清空，否则
        # 「删除选中」会删到用户根本没有勾过的新文件。
        self._selected = {}
        self._pending_delete = None
        self._delete_item_confirmed = False
        self.save_data("scan_results", results)
        return results

    def _get_results(self) -> dict:
        """获取缓存的扫描结果。"""
        return self._scan_results or {
            "broken_symlinks": [], "hardlinks": [], "duplicates": [], "empty_dirs": [], "failed_transfers": [],
            "orphan_streams": [],
            "untransferred": self._scan_untransferred(),
            "summary": {"broken_symlinks": 0, "hardlinks": 0, "duplicates": 0, "empty_dirs": 0,
                        "failed_transfers": 0, "orphan_streams": 0, "untransferred": 0, "total": 0},
        }

    def _scan_broken_symlinks(self) -> List[Dict[str, Any]]:
        """扫描媒体库中的断链软链接。"""
        broken = []
        for lib_dir in self._get_library_dirs():
            if not os.path.isdir(lib_dir):
                continue
            for root, dirs, files in os.walk(lib_dir):
                if self._is_excluded(root) or self._is_reserved(root):
                    continue
                dirs[:] = [d for d in dirs if not (self._is_excluded(os.path.join(root, d)) or self._is_reserved(os.path.join(root, d)))]
                for name in files:
                    if self._name_protected(name):
                        continue
                    filepath = os.path.join(root, name)
                    if os.path.islink(filepath) and not os.path.exists(filepath):
                        try:
                            target = os.readlink(filepath)
                        except OSError:
                            target = "未知"
                        broken.append({"path": filepath, "target": target, "item_type": "broken_symlink"})
        return broken

    def _scan_hardlinks(self) -> List[Dict[str, Any]]:
        """扫描媒体库中的硬链接。

        普通的、非符号链接的文件，若 ``st_nlink > 1`` 表示同一 inode 在其它位置还有
        至少一条硬链接，删除其中任意一条都不会真正丢失数据（数据仍由其余链接持有）。
        这类硬链常是辅种/整理过程产生的冗余副本，可安全清理。
        """
        found = []
        for lib_dir in self._get_library_dirs():
            if not os.path.isdir(lib_dir):
                continue
            for root, dirs, files in os.walk(lib_dir):
                if self._is_excluded(root) or self._is_reserved(root):
                    continue
                dirs[:] = [d for d in dirs if not (self._is_excluded(os.path.join(root, d)) or self._is_reserved(os.path.join(root, d)))]
                for name in files:
                    if self._name_protected(name):
                        continue
                    filepath = os.path.join(root, name)
                    if os.path.islink(filepath):
                        continue
                    try:
                        st = os.stat(filepath)
                    except OSError:
                        continue
                    if st.st_nlink and st.st_nlink > 1:
                        found.append({
                            "path": filepath,
                            "inode": st.st_ino,
                            "link_count": st.st_nlink,
                            "item_type": "hardlink",
                        })
        return found

    def _content_signature(self, filepath: str, head_tail: int = 65536) -> Optional[str]:
        """计算文件内容签名：头部 + 尾部 + 大小。

        用于重复文件快速聚类：同尺寸文件若头部+尾部+大小一致，极可能内容相同，
        不必对整文件做全量哈希，避免大媒体文件拖垮 IO。失败时返回 None。
        """
        try:
            size = os.path.getsize(filepath)
            h = hashlib.md5()
            with open(filepath, "rb") as f:
                head = f.read(head_tail)
                h.update(head)
                if size > head_tail * 2:
                    f.seek(-head_tail, os.SEEK_END)
                    tail = f.read(head_tail)
                else:
                    f.seek(0, os.SEEK_END)
                    tail = b""
                h.update(tail)
            h.update(str(size).encode())
            return h.hexdigest()
        except OSError:
            return None

    def _scan_duplicates(self) -> List[Dict[str, Any]]:
        """扫描媒体库中的重复文件（内容相同、但各自独立占空间的冗余副本）。

        与硬链接区别：硬链接共享 inode、删一条不丢数据；重复副本是各自独立拷贝，
        占双倍空间。检测分两阶段：
          1) 按字节大小分组，仅同尺寸的候选进入下一阶段（体积差异即非重复）；
          2) 对同尺寸候选用 头部+尾部+大小 的内容签名聚类，签名相同的视为重复组。
        每个重复文件作为独立可删项列出，并标注所属重复组的份数，便于你保留至少一份。

        范围控制：``_dup_only_video`` 为 True 时只纳入视频类资源（占空间的才是真垃圾，
        如 .mkv/.mp4），跳过图片/字幕等小文件（海报、背景图等刮削副本量大但几乎不占空间，
        用户明确不希望它们混入重复清理）。该开关可在插件配置中关闭以覆盖所有类型。
        """
        only_video = self._dup_only_video
        video_exts = self._video_exts or set()

        # 1) 收集所有普通文件（非符号链接），排除硬链接（st_nlink>1 已在硬链接分组处理）
        by_size: Dict[int, List[str]] = defaultdict(list)
        for lib_dir in self._get_library_dirs():
            if not os.path.isdir(lib_dir):
                continue
            for root, dirs, files in os.walk(lib_dir):
                if self._is_excluded(root) or self._is_reserved(root):
                    continue
                dirs[:] = [d for d in dirs if not (self._is_excluded(os.path.join(root, d)) or self._is_reserved(os.path.join(root, d)))]
                for name in files:
                    if self._name_protected(name):
                        continue
                    filepath = os.path.join(root, name)
                    if os.path.islink(filepath):
                        continue
                    if only_video and video_exts:
                        # 仅视频类：扩展名不在视频集合里直接跳过
                        if os.path.splitext(name)[1].lower() not in video_exts:
                            continue
                    try:
                        st = os.stat(filepath)
                    except OSError:
                        continue
                    if st.st_nlink and st.st_nlink > 1:
                        # 硬链接另行处理，避免与重复副本重复计数
                        continue
                    if st.st_size <= 0:
                        continue
                    by_size[st.st_size].append(filepath)

        duplicates: List[Dict[str, Any]] = []
        # 2) 仅对同尺寸的候选组做内容签名聚类
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            groups: Dict[str, List[str]] = defaultdict(list)
            for p in paths:
                sig = self._content_signature(p)
                if sig is None:
                    continue
                groups[sig].append(p)
            for sig, members in groups.items():
                if len(members) < 2:
                    continue
                members_sorted = sorted(members)
                for idx, p in enumerate(members_sorted):
                    duplicates.append({
                        "path": p,
                        "size": size,
                        "group_id": sig,
                        "group_count": len(members_sorted),
                        "item_type": "duplicate",
                        "keep": idx == 0,  # 每组第一份默认建议保留
                    })
        return duplicates

    def _scan_empty_dirs(self) -> List[Dict[str, Any]]:
        """扫描媒体库中的空目录。"""
        empty = []
        for lib_dir in self._get_library_dirs():
            if not os.path.isdir(lib_dir):
                continue
            for root, dirs, files in os.walk(lib_dir, topdown=False):
                if self._is_excluded(root) or self._is_reserved(root):
                    continue
                if root == lib_dir:
                    continue
                if self._name_protected(os.path.basename(root)):
                    continue
                if not os.listdir(root):
                    empty.append({"path": root, "item_type": "empty_dir"})
        return empty

    def _scan_failed_transfers(self) -> List[Dict[str, Any]]:
        """扫描失败的整理记录。"""
        failed = []
        try:
            from app.db import ScopedSession
            from app.db.models.transferhistory import TransferHistory

            db = ScopedSession()
            try:
                for item in db.query(TransferHistory).filter(
                    TransferHistory.status.is_(False)
                ).limit(1000).all():
                    title = item.title or "未知"
                    if self._name_protected(title):
                        continue
                    failed.append({
                        "id": item.id, "title": title, "year": item.year or "",
                        "src": item.src or "", "dest": item.dest or "", "errmsg": item.errmsg or "",
                        "date": str(item.date) if item.date else "", "item_type": "failed_transfer",
                        "image": item.image or "", "tmdbid": item.tmdbid or 0,
                    })
            finally:
                db.close()
        except Exception as e:
            logger.error(f"扫描失败整理记录出错: {e}")
        return failed

    def _scan_untransferred(self) -> List[Dict[str, Any]]:
        """扫描下载目录中从未被整理过的媒体文件。

        遍历所有配置的下载目录，查找媒体文件（视频/音频），
        检查是否存在成功的 TransferHistory 记录。
        没有记录的 = 从未被整理过的文件，可作为清理候选。
        """
        if not self._untransfer_scan_enabled:
            return []

        # 获取所有下载目录（来自系统目录配置）
        download_dirs = set()
        for d in self._load_dir_configs():
            dp = getattr(d, "download_path", None)
            if dp and str(dp).strip():
                download_dirs.add(str(dp).strip())
        # fallback: 常见下载目录
        for fallback in ["/media/downloads/BT下载", "/media/downloads"]:
            if os.path.isdir(fallback):
                download_dirs.add(fallback)

        if not download_dirs:
            return []

        # 收集所有出现在 TransferHistory 中的文件路径（任何状态：成功/失败）。
        # 只要文件曾在整理记录里出现过，就不是"未整理资源"候选。
        # 这样即使数据库 status 字段类型变化（int/str/enum），也不会误把已整理文件标成垃圾。
        transferred_paths: set = set()
        try:
            from app.db import ScopedSession
            from app.db.models.transferhistory import TransferHistory

            db = ScopedSession()
            try:
                # 按主键游标分页取全量，不设行数上限：limit 上限一旦超过表内记录数，
                # 会被静默丢弃且丢弃范围不确定，被丢掉的已整理文件就会重新变成
                # "未整理"候选，这正是历史上误删事故的同一误判形态。
                page_size = 5000
                offset = 0
                transferred_count = 0
                while True:
                    page = db.query(TransferHistory.src).order_by(
                        TransferHistory.id.asc()
                    ).offset(offset).limit(page_size).all()
                    if not page:
                        break
                    for record in page:
                        if record[0]:
                            transferred_paths.add(os.path.normpath(record[0]))
                    transferred_count += len(page)
                    if len(page) < page_size:
                        break
                    offset += page_size
                logger.info(
                    f"整理记录路径收集完成：表内 {transferred_count} 条，"
                    f"去重后 {len(transferred_paths)} 个唯一源路径"
                )
            finally:
                db.close()
        except Exception as e:
            logger.error(f"查询整理记录出错: {e}")
        # 二次校验：显式统计 status 字段实际存储类型，帮助诊断潜在类型不匹配
        try:
            from app.db import ScopedSession
            from app.db.models.transferhistory import TransferHistory
            db = ScopedSession()
            try:
                type_probe = db.query(
                    TransferHistory.status,
                    TransferHistory.id,
                ).limit(1).first()
                if type_probe is not None:
                    logger.info(
                        f"TransferHistory.status 字段实际类型={type(type_probe[0]).__name__}，"
                        f"值示例={type_probe[0]!r}"
                    )
            finally:
                db.close()
        except Exception:
            pass  # 诊断信息仅辅助，失败不影响主流程

        # 下载器在册文件：仍在做种/下载，无整理记录也不能算垃圾
        active_downloads = self._active_downloader_paths()
        logger.info(
            f"未整理扫描：下载目录 {len(download_dirs)} 个，已整理记录 {len(transferred_paths)} 条，"
            f"下载器在册文件 {len(active_downloads)} 个"
        )

        # 视频/音频扩展名
        media_exts = self._video_exts or {".mkv", ".mp4", ".avi", ".ts", ".flv", ".rmvb", ".wmv", ".m4v", ".mp3", ".flac", ".wav", ".aac"}
        media_exts = media_exts | {".iso", ".bdmv"}

        untransferred: List[Dict[str, Any]] = []
        skipped_reserved = 0
        skipped_active = 0
        for dl_dir in download_dirs:
            if not os.path.isdir(dl_dir):
                continue
            try:
                for root, dirs, files in os.walk(dl_dir):
                    # 跳过排除目录
                    if self._is_excluded(root) or self._is_reserved(root):
                        continue
                    dirs[:] = [d for d in dirs if not (self._is_excluded(os.path.join(root, d)) or self._is_reserved(os.path.join(root, d)))]
                    for name in files:
                        # 跳过非媒体文件
                        ext = os.path.splitext(name)[1].lower()
                        if ext not in media_exts:
                            continue
                        # 跳过保护的名称
                        if self._name_protected(name):
                            continue
                        filepath = os.path.join(root, name)
                        # 跳过符号链接
                        if os.path.islink(filepath):
                            continue
                        # 跳过保留路径（勿删/保种/契约等标记）
                        if self._is_reserved(filepath):
                            skipped_reserved += 1
                            continue
                        # 跳过排除目录/排除关键词
                        if self._is_untransfer_excluded(filepath):
                            continue
                        # 跳过下载器在册文件（仍在做种/下载）
                        if os.path.normpath(filepath) in active_downloads:
                            skipped_active += 1
                            continue
                        # 检查是否已整理
                        if os.path.normpath(filepath) in transferred_paths:
                            continue
                        # 获取文件信息
                        try:
                            st = os.stat(filepath)
                            size = st.st_size
                            mtime = st.st_mtime
                        except OSError:
                            continue
                        # 跳过过小的文件（<1MB，可能是样本/预告）
                        if size < 1024 * 1024:
                            continue
                        untransferred.append({
                            "path": filepath,
                            "size": size,
                            "mtime": mtime,
                            "parent": os.path.basename(root),
                            "item_type": "untransferred",
                        })
            except Exception as e:
                logger.error(f"扫描下载目录出错 ({dl_dir}): {e}")

        # 按修改时间排序，最旧的在前（更可能是垃圾）
        untransferred.sort(key=lambda x: x.get("mtime", 0))
        return untransferred

    def _scan_orphan_streams(self) -> List[Dict[str, Any]]:
        """扫描媒体库中源目录已删除、但整理目录仍残留的 strm 文件。"""
        if not self._orphan_scan_enabled:
            return []
        if not (self._orphan_scan_keep_disks or self._orphan_scan_source_dirs):
            return []

        orphans: List[Dict[str, Any]] = []
        keep_disks = self._orphan_scan_keep_disks
        use_parent_name_match = (not keep_disks) and bool(self._orphan_scan_source_dirs)
        parent_name_set = self._collect_source_dir_names(self._orphan_scan_source_dirs) if use_parent_name_match else set()

        for lib_dir in self._get_library_dirs():
            if not os.path.isdir(lib_dir):
                continue
            try:
                for root, dirs, files in os.walk(lib_dir):
                    if self._is_excluded(root) or self._is_reserved(root):
                        continue
                    dirs[:] = [d for d in dirs if not (self._is_excluded(os.path.join(root, d)) or self._is_reserved(os.path.join(root, d)))]
                    for name in files:
                        if not name.endswith(".strm"):
                            continue
                        if self._name_protected(name):
                            continue
                        filepath = os.path.join(root, name)
                        if os.path.islink(filepath):
                            continue
                        try:
                            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read().strip()
                        except OSError:
                            continue

                        disk = self._detect_disk_from_strm(content)
                        is_orphan = False
                        reason = ""

                        if keep_disks:
                            if not disk or not any(kw.lower() in disk.lower() for kw in keep_disks):
                                is_orphan = True
                                reason = f"网盘类型 '{disk or '未知'}' 不在保留白名单中"
                        elif use_parent_name_match and parent_name_set:
                            parent_basename = os.path.basename(root)
                            if parent_basename not in parent_name_set:
                                is_orphan = True
                                reason = f"父目录 '{parent_basename}' 在源目录中不存在"
                        else:
                            continue

                        if is_orphan:
                            orphans.append({
                                "path": filepath,
                                "parent": os.path.dirname(filepath),
                                "parent_name": os.path.basename(root),
                                "disk": disk or "未知",
                                "reason": reason,
                                "item_type": "orphan_stream",
                            })
            except Exception as e:
                logger.error(f"扫描孤儿 strm 出错 ({lib_dir}): {e}")

        # 为每个孤儿标记同父目录是否有有效 strm（供删除时判断是否可安全联动清理）
        orphan_parents = {}
        for o in orphans:
            pp = o["parent"]
            if pp not in orphan_parents:
                orphan_parents[pp] = self._has_valid_strm_in_dir(pp, exclude_orphans=[x["path"] for x in orphans])
        for o in orphans:
            o["has_valid_sibling"] = orphan_parents.get(o["parent"], False)

        return orphans

    def _has_valid_strm_in_dir(self, dir_path: str, exclude_orphans: Optional[List[str]] = None) -> bool:
        """检查目录下是否存在有效（非孤儿）的 strm 文件。

        有效判定：strm 文件不在 exclude_orphans 列表中，且内容指向的网盘在保留白名单内
        （或白名单为空时只要不在孤儿列表中即视为有效）。
        """
        if not dir_path or not os.path.isdir(dir_path):
            return False
        exclude_set = set(exclude_orphans or [])
        try:
            for name in os.listdir(dir_path):
                if not name.endswith(".strm"):
                    continue
                fp = os.path.join(dir_path, name)
                if fp in exclude_set:
                    continue
                if not os.path.isfile(fp):
                    continue
                # 能读取内容就认为有效
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read().strip()
                    if content:
                        return True
                except OSError:
                    pass
        except OSError:
            pass
        return False

    def _is_untransfer_excluded(self, path: str) -> bool:
        """检查路径是否在未整理资源排除列表中。

        排除逻辑：
        1) 路径前缀匹配排除目录
        2) 文件名或父目录名包含排除关键词（不区分大小写，| 分隔）
        """
        # 排除目录
        norm = os.path.normpath(path)
        for exclude in self._untransfer_exclude_dirs:
            e = os.path.normpath(exclude)
            if norm.startswith(e + os.sep) or norm == e:
                return True
        # 排除关键词
        if self._untransfer_exclude_keywords:
            name = os.path.basename(path)
            parent = os.path.basename(os.path.dirname(path))
            combined = (name + "|" + parent).lower()
            for kw in self._untransfer_exclude_keywords.split("|"):
                kw = kw.strip().lower()
                if kw and kw in combined:
                    return True
        return False

    def _get_library_dirs(self) -> List[str]:
        """获取所有媒体库目录。

        来源：系统目录配置中的媒体库路径 → 常见媒体库根兜底。
        结果会剔除被上级目录覆盖的子目录，避免重复扫描。
        """
        raw: List[str] = []

        for d in self._load_dir_configs():
            lp = getattr(d, "library_path", None)
            if lp:
                raw.append(str(lp))

        for fallback in ("/media/movie", "/media/tv", "/media/music"):
            if os.path.isdir(fallback):
                raw.append(fallback)

        norm: List[str] = []
        for p in raw:
            s = str(p).strip()
            if not s:
                continue
            s = os.path.normpath(s)
            if s not in norm:
                norm.append(s)

        result: List[str] = []
        for p in sorted(norm, key=len):
            if any(p == q or p.startswith(q + os.sep) for q in result):
                continue
            result.append(p)
        return result

    def _is_excluded(self, path: str) -> bool:
        """检查路径是否在排除列表中。"""
        p = os.path.normpath(path)
        for exclude in self._exclude_dirs:
            e = os.path.normpath(exclude)
            if p == e or p.startswith(e + os.sep):
                return True
        return False

    def _name_protected(self, name: str) -> bool:
        """检查名称是否命中保护名单（喜欢的电影/剧集等）。

        命中保护名单的作品整项不扫描、不展示、不参与删除。
        不区分大小写，支持子串匹配（如「寻梦环游记」可保护该作品所有相关文件）。
        """
        if not self._protect_name_keywords:
            return False
        nl = (name or "").lower()
        if not nl:
            return False
        return any(k in nl for k in self._protect_name_keywords)

    # ==================== 安全兜底 ====================

    def _is_reserved(self, path: str) -> bool:
        """判断路径是否命中保留标记（内置 + 用户配置关键词）。

        逐段检查路径组件，任一段命中即视为「勿删」：扫描不出候选，删除直接拒绝。
        用于保种目录、契约目录、用户上传区等明确不应参与清理的位置。
        """
        p = os.path.normpath(path or "")
        if not p:
            return True
        markers = set(PATH_RESERVED_MARKERS) | {k for k in (self._reserved_keywords or []) if k}
        if not markers:
            return False
        parts = p.strip(os.sep).split(os.sep)
        for seg in parts:
            seg_l = seg.lower()
            if any(m in seg_l for m in markers):
                return True
        return False

    def _active_downloader_paths(self) -> set:
        """返回下载器中仍在做种/下载的文件路径集合。

        这些文件属于活跃种子，即使没有 TransferHistory 记录也不能视为垃圾，
        否则会把正在保种的资源误判为「未整理资源」并删除，进而导致关联的
        媒体库软链接（整理模式为软链接时）全部失效。
        查询失败时返回空集合并记录日志，不影响主流程。
        """
        active: set = set()
        try:
            from app.helper.downloader import DownloaderHelper
            for t in DownloaderHelper.list_files() or []:
                dp = getattr(t, "download_path", None)
                fp = getattr(t, "path", None)
                if not dp or not fp:
                    continue
                full = fp if os.path.isabs(fp) else os.path.join(str(dp).strip(), fp)
                active.add(os.path.normpath(full))
        except Exception as e:
            logger.debug(f"查询下载器在册文件失败: {e}")
        return active

    def _remove_to_trash(self, path: str) -> Tuple[bool, str]:
        """把文件移入回收站（可恢复）。失败时返回 (False, 原因)。

        回收站不可用（无 GUI 桌面环境等）时由调用方回退到直接删除。
        """
        try:
            import send2trash
            send2trash.send2trash(path)
            return True, ""
        except Exception as e:
            return False, str(e)

    # ==================== 清理操作 ====================

    def _remove_file(self, path: str) -> Tuple[str, str]:
        """按配置的删除方式移除文件，返回 (实际方式, 消息)。

        - 回收站模式：优先软删除，成功后可恢复；send2trash 不可用时回退直接删除。
        - 直接删除模式：os.remove。
        """
        if self._delete_mode == _DELETE_MODE_TRASH:
            ok, err = self._remove_to_trash(path)
            if ok:
                return _DELETE_MODE_TRASH, "已移入回收站"
            logger.warning(f"回收站删除失败，回退直接删除: {path} ({err})")
            os.remove(path)
            return _DELETE_MODE_DELETE, "已删除"
        os.remove(path)
        return _DELETE_MODE_DELETE, "已删除"

    def _is_path_in_transfer_history(self, path: str) -> Optional[str]:
        """反查某个文件路径是否出现在 TransferHistory 表里（任何状态）。

        这是"未整理"删除的最后一道防线：即使 _scan_untransferred 逻辑再次出 bug
        （例如 status 字段类型不匹配、路径规范化不一致），只要该文件在历史上曾
        经被整理过（无论成功或失败），删除时都会被拦截。

        返回命中记录的 status（字符串），未命中返回 None。
        """
        if not path:
            return None
        try:
            from app.db import ScopedSession
            from app.db.models.transferhistory import TransferHistory
            db = ScopedSession()
            try:
                norm = os.path.normpath(path)
                # 用 LIKE 前缀匹配以兼容路径规范化差异（尾部斜杠、大小写等）
                like = norm + "%"
                row = db.query(TransferHistory.src, TransferHistory.status).filter(
                    TransferHistory.src.like(like)
                ).first()
                if not row:
                    # 兜底：完全匹配（防御 SQL LIKE 通配符干扰）
                    row = db.query(TransferHistory.src, TransferHistory.status).filter(
                        TransferHistory.src == norm
                    ).first()
                if row:
                    return str(row[1]) if row[1] is not None else ""
                return None
            finally:
                db.close()
        except Exception as e:
            # 查库失败时保守拒绝删除，宁可漏删也不能误删
            logger.error(f"反查 TransferHistory 失败，保守拒绝删除: {path}, err={e}")
            return "query_error"

    def _delete_item(self, data: dict, silent: bool = False) -> dict:
        """删除单个垃圾项。"""
        item_type = data.get("type") or data.get("item_type")
        path = data.get("path", "")
        item_id = data.get("id")

        # 统一守卫：保留路径不参与任何清理（保种/契约/勿删等标记目录）
        if item_type != "failed_transfer" and path and self._is_reserved(path):
            logger.warning(f"拒绝删除保留路径下的项目: {path}")
            msg = f"保留路径，已拒绝删除: {os.path.basename(path)}"
            if not silent:
                self._notify_result("删除被拒绝", msg, fail=True)
            return {"success": False, "message": msg}

        # 页面单条直删必须经过两级确认：仅当 _confirm_delete 设置了确认令牌时放行。
        # silent=True 表示由 _batch_delete_selected / _delete_all 在确认流程内调用，同样放行。
        if not silent and not self._delete_item_confirmed:
            logger.warning(f"拒绝未经确认的单条删除: {path}")
            msg = f"未经确认流程，已拒绝删除: {os.path.basename(path)}"
            self._notify_result("删除被拒绝", msg, fail=True)
            return {"success": False, "message": msg}

        try:
            if item_type == "broken_symlink" and os.path.islink(path):
                # 只删除断链本体：数据库记录与刮削残留由「清理媒体文件」插件负责
                os.remove(path)
                self._scan_results["broken_symlinks"] = [x for x in self._scan_results.get("broken_symlinks", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"已删除: {os.path.basename(path)}"
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            elif item_type == "empty_dir" and os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
                self._scan_results["empty_dirs"] = [x for x in self._scan_results.get("empty_dirs", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"已删除: {os.path.basename(path)}"
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            elif item_type == "hardlink" and os.path.isfile(path) and not os.path.islink(path):
                # 硬链为普通文件（非符号链接），删除一条不影响其余持有同 inode 的链接
                mode_used, removed_msg = self._remove_file(path)
                self._scan_results["hardlinks"] = [x for x in self._scan_results.get("hardlinks", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"{removed_msg}硬链接: {os.path.basename(path)}"
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            elif item_type == "duplicate" and os.path.isfile(path) and not os.path.islink(path):
                # 重复文件：内容相同的独立副本，删除不影响同组其它副本
                mode_used, removed_msg = self._remove_file(path)
                self._scan_results["duplicates"] = [x for x in self._scan_results.get("duplicates", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"{removed_msg}重复文件: {os.path.basename(path)}"
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            elif item_type == "orphan_stream" and os.path.isfile(path) and not os.path.islink(path):
                # 只删除孤儿 strm 本体：同目录刮削残留、空目录与数据库记录由「清理媒体文件」插件负责
                os.remove(path)
                self._scan_results["orphan_streams"] = [x for x in self._scan_results.get("orphan_streams", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"已删除孤儿 strm: {os.path.basename(path)}"
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            elif item_type == "failed_transfer" and item_id:
                from app.db import ScopedSession
                from app.db.models.transferhistory import TransferHistory
                db = ScopedSession()
                try:
                    record = db.query(TransferHistory).filter(TransferHistory.id == item_id).first()
                    if record:
                        db.delete(record)
                        db.commit()
                        self._scan_results["failed_transfers"] = [x for x in self._scan_results.get("failed_transfers", []) if x.get("id") != item_id]
                        self._update_summary()
                        self.save_data("scan_results", self._scan_results)
                        msg = f"已删除记录: {item_id}"
                        if not silent:
                            self._notify_result("删除完成", msg)
                        return {"success": True, "message": msg}
                finally:
                    db.close()

            elif item_type == "untransferred" and os.path.isfile(path) and not os.path.islink(path):
                # 保留路径强制拒绝：保种/契约等目录不参与清理
                if self._is_reserved(path):
                    logger.warning(f"拒绝删除保留路径下的文件: {path}")
                    msg = f"保留路径，已拒绝删除: {os.path.basename(path)}"
                    self._notify_result("删除被拒绝", msg, fail=True)
                    return {"success": False, "message": msg}
                # 下载器在册豁免：仍在做种/下载的文件即使无整理记录也不可视为垃圾
                if os.path.normpath(path) in self._active_downloader_paths():
                    logger.warning(f"拒绝删除下载器在册文件: {path}")
                    msg = f"仍在做种/下载，已拒绝删除: {os.path.basename(path)}"
                    self._notify_result("删除被拒绝", msg, fail=True)
                    return {"success": False, "message": msg}
                # 转移历史反查（最后一道防线）：只要文件曾在 TransferHistory 里出现过，
                # 无论成功或失败，都拒绝删除。即使 _scan_untransferred 再出 bug，这里也能兜底。
                hist_status = self._is_path_in_transfer_history(path)
                if hist_status is not None:
                    logger.warning(
                        f"拒绝删除：文件曾在 TransferHistory 中出现（status={hist_status}）: {path}"
                    )
                    msg = (
                        f"文件曾出现在整理记录中（status={hist_status}），"
                        f"已拒绝删除: {os.path.basename(path)}"
                    )
                    if not silent:
                        self._notify_result("删除被拒绝", msg, fail=True)
                    return {"success": False, "message": msg}
                # 只删除文件本体：空目录与数据库记录由「清理媒体文件」插件负责
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                mode_used, removed_msg = self._remove_file(path)
                self._scan_results["untransferred"] = [x for x in self._scan_results.get("untransferred", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                size_str = self._format_size(size) if size else ""
                msg = f"{removed_msg}未整理文件: {os.path.basename(path)}" + (f"（{size_str}）" if size_str else "")
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            msg = "无法删除（项目可能已不存在或参数不匹配）"
            if not silent:
                self._notify_result("删除失败", msg, fail=True)
            return {"success": False, "message": msg}

        except Exception as e:
            logger.error(f"删除失败: {e}")
            msg = f"删除异常: {e}"
            if not silent:
                self._notify_result("删除失败", msg, fail=True)
            return {"success": False, "message": msg}

    def _delete_all(self, data: dict = None, silent: bool = False) -> dict:
        """删除所有扫描到的垃圾项。"""
        if not silent and not self._delete_item_confirmed:
            logger.warning("拒绝未经确认的全部删除")
            return {"success": False, "message": "未经确认流程，已拒绝全部删除"}
        results = self._scan_results
        if not results:
            return {"success": False, "message": "请先扫描"}

        items = []
        for item in results.get("broken_symlinks", []):
            items.append({"type": "broken_symlink", "path": item["path"]})
        for item in results.get("hardlinks", []):
            items.append({"type": "hardlink", "path": item["path"]})
        for item in results.get("duplicates", []):
            items.append({"type": "duplicate", "path": item["path"]})
        for item in results.get("empty_dirs", []):
            items.append({"type": "empty_dir", "path": item["path"]})
        for item in results.get("failed_transfers", []):
            items.append({"type": "failed_transfer", "id": item["id"]})
        for item in results.get("orphan_streams", []):
            items.append({"type": "orphan_stream", "path": item["path"]})
        for item in results.get("untransferred", []):
            items.append({"type": "untransferred", "path": item["path"]})

        if not items:
            return {"success": True, "message": "没有需要删除的项目"}

        success_count = 0
        fail_count = 0
        for item in items:
            result = self._delete_item(item, silent=True)
            if result.get("success"):
                success_count += 1
            else:
                fail_count += 1

        msg = f"成功删除 {success_count} 项" + (f"，失败 {fail_count} 项" if fail_count else "")
        self._notify_result("全部删除完成", msg, fail=bool(fail_count))
        return {"success": fail_count == 0, "message": msg}

    def _update_summary(self):
        """更新统计信息。"""
        results = self._scan_results
        results["summary"] = {
            "broken_symlinks": len(results.get("broken_symlinks", [])),
            "hardlinks": len(results.get("hardlinks", [])),
            "duplicates": len(results.get("duplicates", [])),
            "empty_dirs": len(results.get("empty_dirs", [])),
            "failed_transfers": len(results.get("failed_transfers", [])),
            "orphan_streams": len(results.get("orphan_streams", [])),
            "untransferred": len(results.get("untransferred", [])),
            "total": len(results.get("broken_symlinks", [])) + len(results.get("hardlinks", []))
            + len(results.get("duplicates", [])) + len(results.get("empty_dirs", []))
            + len(results.get("failed_transfers", [])) + len(results.get("orphan_streams", []))
            + len(results.get("untransferred", [])),
        }

    # ==================== 交互端点（选中 / 批量 / 刷新） ====================

    def _toggle_select(self, data: dict) -> dict:
        """切换单个项目的选中状态。key 形如 b:0 / e:3 / f:12。"""
        key = data.get("key", "")
        if not key:
            return {"success": False, "message": "缺少 key"}
        if key in self._selected:
            self._selected.pop(key, None)
        else:
            # 记录选中项的类型，便于批量删除时定位
            kind = key.split(":", 1)[0]
            self._selected[key] = kind
        return {"success": True, "selected": len(self._selected)}

    def _select_clear(self) -> dict:
        """清空所有选中。"""
        self._selected = {}
        return {"success": True, "selected": 0}

    def _visible_keys(self, category: Optional[str] = None) -> List[str]:
        """返回当前可见（页面展示的）项目的 key，顺序与 get_page 渲染一致。

        页面每类最多展示前 100 条，key 形如 b:<i> / h:<i> / e:<i> / f:<i>。
        category 为 None 时返回所有分类。category 取值：b/h/e/f。
        """
        mapping = (("broken_symlinks", "b"), ("hardlinks", "h"), ("duplicates", "d"), ("empty_dirs", "e"), ("orphan_streams", "s"), ("failed_transfers", "f"), ("untransferred", "u"))
        if category:
            mapping = [m for m in mapping if m[1] == category]
        keys: List[str] = []
        for cat, prefix in mapping:
            for i in range(min(100, len((self._scan_results or {}).get(cat, [])))):
                keys.append(f"{prefix}:{i}")
        return keys

    def _select_category(self, category: str = None, mode: str = "all") -> dict:
        """按分类全选/反选当前可见项目（每类独立，不互相干扰）。

        category: b(断链软链) / h(硬链) / e(空目录) / f(失败记录)
        mode: all(全选) / invert(反选)
        """
        if category not in ("b", "h", "e", "f", "s", "u"):
            return {"success": False, "message": "无效的分类"}
        keys = self._visible_keys(category)
        if mode == "invert":
            for key in keys:
                if key in self._selected:
                    self._selected.pop(key, None)
                else:
                    self._selected[key] = category
        else:  # all
            for key in keys:
                self._selected[key] = category
        return {"success": True, "selected": len(self._selected)}

    # ==================== 两级删除确认 ====================

    def _resolve_selected_items(self) -> List[dict]:
        """把当前选中的 key 解析为可删除 item 列表。"""
        items: List[dict] = []
        kind_map = {
            "b": ("broken_symlinks", "broken_symlink", "path"),
            "h": ("hardlinks", "hardlink", "path"),
            "d": ("duplicates", "duplicate", "path"),
            "e": ("empty_dirs", "empty_dir", "path"),
            "s": ("orphan_streams", "orphan_stream", "path"),
            "u": ("untransferred", "untransferred", "path"),
        }
        for key, kind in list((self._selected or {}).items()):
            idx = self._key_index(key)
            if kind == "f":
                item = self._item_by_index("failed_transfers", idx)
                if item:
                    items.append({"type": "failed_transfer", "id": item.get("id")})
                continue
            spec = kind_map.get(kind)
            if not spec:
                continue
            cat, item_type, field = spec
            item = self._item_by_index(cat, idx)
            if item and item.get(field):
                items.append({"type": item_type, field: item.get(field)})
        return items

    def _collect_all_items(self) -> List[dict]:
        """收集当前扫描结果中的全部可删除项。"""
        results = self._scan_results or {}
        items: List[dict] = []
        for it in results.get("broken_symlinks", []):
            items.append({"type": "broken_symlink", "path": it.get("path", "")})
        for it in results.get("hardlinks", []):
            items.append({"type": "hardlink", "path": it.get("path", "")})
        for it in results.get("duplicates", []):
            items.append({"type": "duplicate", "path": it.get("path", "")})
        for it in results.get("empty_dirs", []):
            items.append({"type": "empty_dir", "path": it.get("path", "")})
        for it in results.get("failed_transfers", []):
            items.append({"type": "failed_transfer", "id": it.get("id")})
        for it in results.get("orphan_streams", []):
            items.append({"type": "orphan_stream", "path": it.get("path", "")})
        for it in results.get("untransferred", []):
            items.append({"type": "untransferred", "path": it.get("path", "")})
        return [i for i in items if i.get("path") or i.get("id")]

    @staticmethod
    def _sum_items_size(items: List[dict]) -> int:
        """累加待删除项占用的字节数（目录/记录按 0 计）。"""
        total = 0
        for it in items:
            p = it.get("path")
            if not p:
                continue
            try:
                total += os.path.getsize(p)
            except OSError:
                try:
                    total += os.lstat(p).st_size
                except OSError:
                    continue
        return total

    def _request_delete(self, mode: str = "selected") -> dict:
        """第一步：发起删除请求，仅计算摘要并进入待确认状态。"""
        if mode == "all":
            items = self._collect_all_items()
        else:
            items = self._resolve_selected_items()
        if not items:
            return {"success": False, "message": "没有可删除的项目"}
        size = self._sum_items_size(items)
        self._pending_delete = {
            "mode": "all" if mode == "all" else "selected",
            "count": len(items), "size": size, "stage": 1,
        }
        return {"success": True, "count": len(items), "size": size, "stage": 1}

    def _advance_delete(self) -> dict:
        """第二步：进入最终确认。"""
        if not self._pending_delete:
            return {"success": False, "message": "没有待确认的删除请求"}
        self._pending_delete["stage"] = 2
        return {"success": True, "stage": 2}

    def _cancel_delete(self) -> dict:
        """取消待确认的删除请求。"""
        self._pending_delete = None
        return {"success": True}

    def _confirm_delete(self) -> dict:
        """最终执行删除。"""
        pending = self._pending_delete
        if not pending:
            return {"success": False, "message": "没有待确认的删除请求"}
        mode = pending.get("mode")
        self._pending_delete = None
        # 一次性放行令牌：允许确认后的单条删除通过 _delete_item 守卫，执行完立即复位
        self._delete_item_confirmed = True
        try:
            if mode == "all":
                return self._delete_all(silent=True)
            return self._batch_delete_selected(silent=True)
        finally:
            self._delete_item_confirmed = False

    # ==================== 批量删除 ====================

    def _batch_delete_selected(self, data: dict = None, silent: bool = False) -> dict:
        """删除所有已选中的项目（已修复硬链/重复文件批量删除缺失的 bug）。"""
        if not silent and not self._delete_item_confirmed:
            logger.warning("拒绝未经确认的批量删除")
            return {"success": False, "message": "未经确认流程，已拒绝批量删除"}
        if not self._selected:
            return {"success": False, "message": "没有选中的项目"}
        items = self._resolve_selected_items()
        if not items:
            return {"success": False, "message": "选中的项目已失效"}
        success_count = 0
        fail_count = 0
        for item in items:
            res = self._delete_item(item, silent=True)
            if res.get("success"):
                success_count += 1
            else:
                fail_count += 1
        self._selected = {}
        msg = f"已删除 {success_count} 项" + (f"，失败 {fail_count} 项" if fail_count else "")
        self._notify_result("批量删除完成", msg, fail=bool(fail_count))
        return {"success": fail_count == 0, "message": msg}

    def _refresh(self) -> dict:
        """刷新视图：以当前缓存重渲染页面（不重新扫描磁盘）。"""
        # 仅返回当前缓存摘要，前端会在 events.click 后重拉 get_page
        summary = (self._scan_results or {}).get("summary", {})
        return {"success": True, "message": "已刷新", "summary": summary}

    # ==================== 选中项辅助 ====================

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小为人类可读字符串。"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _key_index(key: str) -> int:
        """从 key（如 b:3）提取序号。"""
        try:
            return int(str(key).split(":", 1)[1])
        except (ValueError, IndexError):
            return -1

    def _item_by_index(self, category: str, idx: int) -> Optional[dict]:
        """按序号取缓存里某分类的项目（与页面展示顺序一致，仅取前 100）。"""
        items = (self._scan_results or {}).get(category, [])[:100]
        if 0 <= idx < len(items):
            return items[idx]
        return None

    # ==================== 通知 ====================

    def _notify_result(self, title: str, text: str, fail: bool = False) -> None:
        """删除结果推送通知，便于用户确认是否真正生效。"""
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"媒体垃圾扫描 - {title}",
                text=text,
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[MediaGarbageCleaner] 通知发送失败（忽略）: {err}")