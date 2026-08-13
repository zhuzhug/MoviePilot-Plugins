"""
媒体垃圾扫描插件

扫描媒体库中的垃圾文件：断链软链接、空目录、失败整理记录。
支持手动清理和定时扫描。
"""

import os
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType



class MediaGarbageCleaner(_PluginBase):
    """资源清理插件（原名：媒体垃圾扫描）。"""

    plugin_name = "资源清理"
    plugin_desc = "扫描媒体库中的断链软链接、硬链接、重复文件、空目录与失败整理记录，支持按地址与名称保护喜欢的作品，手动或批量清理。"
    plugin_icon = "mdi-broom"
    plugin_version = "1.6.1"
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
    _scan_results: Dict[str, Any] = {}
    _selected: Dict[str, str] = {}  # 已选中的项目 key -> 标识符

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._exclude_dirs = []
        self._scan_results = {}
        self._selected = {}
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
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
                ],
            }
        ], {"enabled": False, "exclude_dirs": [], "dup_only_video": True, "protect_name_keywords": ""}

    def _exclude_dir_options(self) -> List[Dict[str, str]]:
        """构建排除目录下拉选项：媒体库根目录 + 已扫描结果中出现过的父目录。"""
        candidates: List[str] = []
        try:
            for d in self._get_library_dirs():
                candidates.append(d)
        except Exception:
            pass
        try:
            results = self._scan_results or {}
            for cat in ("broken_symlinks", "hardlinks", "duplicates", "empty_dirs"):
                for item in results.get(cat, [])[:200]:
                    p = item.get("path", "")
                    if p:
                        candidates.append(os.path.dirname(p))
        except Exception:
            pass
        seen = set()
        opts = []
        for c in candidates:
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                opts.append({"title": c, "value": c})
        return opts

    # ==================== 详情页仪表盘样式辅助方法 ====================
    # 视觉风格对齐「MP 运维助手」：tonal 统计卡 + 网格动作按钮 + 圆角描边列表行

    @staticmethod
    def _normalize_path_list(raw: Any) -> List[str]:
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
                if val and isinstance(val, str) and val.strip():
                    result.append(val.strip())
        seen = set()
        out = []
        for p in result:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    @staticmethod
    def _stat_card(title: str, value: str, icon: str, color: str, subtitle: str) -> dict:
        """对齐 MP 运维助手 的 _status_card 风格。"""
        return {
            "component": "VCol", "props": {"cols": 6, "md": 3},
            "content": [{
                "component": "VCard", "props": {"variant": "tonal", "color": color, "class": "h-100 mb-4"},
                "content": [{
                    "component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "d-flex align-center justify-space-between mb-2"}, "content": [
                            {"component": "div", "props": {"class": "text-caption"}, "text": title},
                            {"component": "VIcon", "props": {"icon": icon, "size": "28"}},
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

        # 每个分类各自的全选/反选按钮（按分组独立选择，不互相干扰）
        cat_sel = lambda prefix: [
            self._section_header_btn("全选", "mdi-select-all",
                                     f"plugin/MediaGarbageCleaner/select_category?category={prefix}&mode=all&token={api_token}"),
            self._section_header_btn("反选", "mdi-select-inverse",
                                     f"plugin/MediaGarbageCleaner/select_category?category={prefix}&mode=invert&token={api_token}"),
        ]

        selected = self._selected or {}
        selected_count = len(selected)

        page: List[dict] = [
            # 顶部说明
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mb-4"},
             "text": "扫描媒体库中的断链软链接、硬链接、重复文件、空目录与失败整理记录。每一类可单独「全选/反选」，选中后批量清理或逐项删除。删除后会自动刷新并推送通知。"},
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
                self._action_button(f"删除选中 ({selected_count})" if selected_count else "删除选中", "mdi-delete", "error", batch_delete_api, disabled=selected_count == 0),
                self._action_button(f"全部删除 ({total})" if total else "全部删除", "mdi-delete-alert", "error", delete_all_api, disabled=not has_results),
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
        """执行全量扫描，返回所有垃圾项。"""
        results = {
            "broken_symlinks": self._scan_broken_symlinks(),
            "hardlinks": self._scan_hardlinks(),
            "duplicates": self._scan_duplicates(),
            "empty_dirs": self._scan_empty_dirs(),
            "failed_transfers": self._scan_failed_transfers(),
        }
        results["summary"] = {
            "broken_symlinks": len(results["broken_symlinks"]),
            "hardlinks": len(results["hardlinks"]),
            "duplicates": len(results["duplicates"]),
            "empty_dirs": len(results["empty_dirs"]),
            "failed_transfers": len(results["failed_transfers"]),
            "total": len(results["broken_symlinks"]) + len(results["hardlinks"]) + len(results["duplicates"])
            + len(results["empty_dirs"]) + len(results["failed_transfers"]),
        }
        self._scan_results = results
        self.save_data("scan_results", results)
        return results

    def _get_results(self) -> dict:
        """获取缓存的扫描结果。"""
        return self._scan_results or {
            "broken_symlinks": [], "hardlinks": [], "duplicates": [], "empty_dirs": [], "failed_transfers": [],
            "summary": {"broken_symlinks": 0, "hardlinks": 0, "duplicates": 0, "empty_dirs": 0, "failed_transfers": 0, "total": 0},
        }

    def _scan_broken_symlinks(self) -> List[Dict[str, Any]]:
        """扫描媒体库中的断链软链接。"""
        broken = []
        for lib_dir in self._get_library_dirs():
            if not os.path.isdir(lib_dir):
                continue
            for root, dirs, files in os.walk(lib_dir):
                if self._is_excluded(root):
                    continue
                dirs[:] = [d for d in dirs if not self._is_excluded(os.path.join(root, d))]
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
                if self._is_excluded(root):
                    continue
                dirs[:] = [d for d in dirs if not self._is_excluded(os.path.join(root, d))]
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
                if self._is_excluded(root):
                    continue
                dirs[:] = [d for d in dirs if not self._is_excluded(os.path.join(root, d))]
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
                if self._is_excluded(root):
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
                for item in db.query(TransferHistory).filter(TransferHistory.status == "失败").limit(1000).all():
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

    def _get_library_dirs(self) -> List[str]:
        """获取所有媒体库目录。"""
        dirs = []
        try:
            from app.db.directory_oper import DirectoryOper
            for d in DirectoryOper().list():
                if d.library_path and d.library_path not in dirs:
                    dirs.append(d.library_path)
        except Exception:
            pass
        for fallback in ["/media/movie", "/media/tv"]:
            if os.path.isdir(fallback) and fallback not in dirs:
                dirs.append(fallback)
        return dirs

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

    # ==================== 清理操作 ====================

    def _delete_item(self, data: dict, silent: bool = False) -> dict:
        """删除单个垃圾项。"""
        item_type = data.get("type") or data.get("item_type")
        path = data.get("path", "")
        item_id = data.get("id")

        try:
            if item_type == "broken_symlink" and os.path.islink(path):
                os.remove(path)
                parent = os.path.dirname(path)
                if parent and os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
                # 从缓存中移除
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
                os.remove(path)
                self._scan_results["hardlinks"] = [x for x in self._scan_results.get("hardlinks", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"已删除硬链接: {os.path.basename(path)}"
                if not silent:
                    self._notify_result("删除完成", msg)
                return {"success": True, "message": msg}

            elif item_type == "duplicate" and os.path.isfile(path) and not os.path.islink(path):
                # 重复文件：内容相同的独立副本，删除不影响同组其它副本
                os.remove(path)
                self._scan_results["duplicates"] = [x for x in self._scan_results.get("duplicates", []) if x.get("path") != path]
                self._update_summary()
                self.save_data("scan_results", self._scan_results)
                msg = f"已删除重复文件: {os.path.basename(path)}"
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

    def _delete_all(self, data: dict = None) -> dict:
        """删除所有扫描到的垃圾项。"""
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
            "total": len(results.get("broken_symlinks", [])) + len(results.get("hardlinks", []))
            + len(results.get("duplicates", [])) + len(results.get("empty_dirs", [])) + len(results.get("failed_transfers", [])),
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
        mapping = (("broken_symlinks", "b"), ("hardlinks", "h"), ("duplicates", "d"), ("empty_dirs", "e"), ("failed_transfers", "f"))
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
        if category not in ("b", "h", "e", "f"):
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

    def _batch_delete_selected(self, data: dict = None) -> dict:
        """删除所有已选中的项目。"""
        if not self._selected:
            return {"success": False, "message": "没有选中的项目"}
        success_keys = []
        fail_keys = []
        for key in list(self._selected.keys()):
            kind = self._selected[key]
            if kind == "b":
                # 断链软链接：需要在缓存里找到对应 path
                idx = self._key_index(key)
                item = self._item_by_index("broken_symlinks", idx)
                if item:
                    res = self._delete_item({"type": "broken_symlink", "path": item.get("path", "")})
                else:
                    res = {"success": False, "message": "找不到对应项"}
            elif kind == "e":
                idx = self._key_index(key)
                item = self._item_by_index("empty_dirs", idx)
                if item:
                    res = self._delete_item({"type": "empty_dir", "path": item.get("path", "")})
                else:
                    res = {"success": False, "message": "找不到对应项"}
            elif kind == "f":
                idx = self._key_index(key)
                item = self._item_by_index("failed_transfers", idx)
                if item:
                    res = self._delete_item({"type": "failed_transfer", "id": item.get("id")})
                else:
                    res = {"success": False, "message": "找不到对应项"}
            else:
                res = {"success": False, "message": "未知类型"}
            if res.get("success"):
                success_keys.append(key)
            else:
                fail_keys.append(key)
        # 清除已成功删除的选中项
        for k in success_keys:
            self._selected.pop(k, None)
        msg = f"已删除 {len(success_keys)} 项" + (f"，失败 {len(fail_keys)} 项" if fail_keys else "")
        self._notify_result("批量删除完成", msg, fail=bool(fail_keys))
        return {"success": not fail_keys, "message": msg}

    def _refresh(self) -> dict:
        """刷新视图：以当前缓存重渲染页面（不重新扫描磁盘）。"""
        # 仅返回当前缓存摘要，前端会在 events.click 后重拉 get_page
        summary = (self._scan_results or {}).get("summary", {})
        return {"success": True, "message": "已刷新", "summary": summary}

    # ==================== 选中项辅助 ====================

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