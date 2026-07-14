from collections import defaultdict
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app import schemas
from app.chain import ChainBase
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, Response
from app.schemas.types import EventType


class SaveFiltersParams(BaseModel):
    """详情页筛选条件保存参数。"""

    min_count: Optional[int] = Field(default=None, description="最小辅种数")
    max_count: Optional[int] = Field(default=None, description="最大辅种数(0=不限)")
    downloader_filter: Optional[str] = Field(default=None, description="仅显示下载器名称")
    name_keyword: Optional[str] = Field(default=None, description="名称关键词(|分隔)")
    path_keywords: Optional[List[str]] = Field(default=None, description="保存路径关键词列表（多选，精确匹配）")
    size_min_gb: Optional[float] = Field(default=None, description="大小下限(GB)")
    size_max_gb: Optional[float] = Field(default=None, description="大小上限(GB,0=不限)")
    sort_by: Optional[str] = Field(default=None, description="排序字段:count/size/name/seeding_time/uploaded")
    sort_order: Optional[str] = Field(default=None, description="排序方向:desc/asc")
    view_mode: Optional[str] = Field(default=None, description="视图模式:group(按分组)/downloader(按下载器)")
    page: Optional[int] = Field(default=None, description="分页页码(1-based,不持久化)")


class DeleteTorrentParams(BaseModel):
    """删除单个种子参数。"""

    hash: str = Field(..., description="种子 hash")
    downloader: str = Field(..., description="下载器名称")
    delete_files: bool = Field(default=False, description="是否同时删除文件")


class SetFilterTextParams(BaseModel):
    """手动输入筛选文本。支持路径或名称关键词。"""

    value: str = Field(default="", description="手动输入的路径或关键词")


class ToggleSelectParams(BaseModel):
    """切换单个种子的选中状态。"""

    hash: str = Field(..., description="种子 hash")
    downloader: str = Field(..., description="下载器名称")


class ToggleSelectGroupParams(BaseModel):
    """切换一组种子（分组内所有种子）的选中状态。"""

    torrents: List[Dict[str, str]] = Field(..., description="种子列表 [{hash, downloader}]")


class BatchDeleteParams(BaseModel):
    """批量删除已选中的种子。"""

    delete_files: bool = Field(default=False, description="是否同时删除文件")


class CrossSeedView(_PluginBase):
    """辅种查看插件：扫描下载器中的种子，按 name+size 分组识别辅种，用于清理孤种。"""

    # region 插件元数据
    plugin_name = "辅种查看"
    plugin_desc = "扫描所有下载器种子，按“种子名+大小”识别辅种关系，用可折叠卡片展示辅种数量、保存路径与明细，支持交互筛选与可选删除。"
    plugin_icon = "seed.png"
    plugin_version = "1.2.0"
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
    _path_keywords: List[str] = None
    _size_min_gb: float = 0.0
    _size_max_gb: float = 0.0  # 0 = 不限
    _allow_delete: bool = False  # 安全开关：详情页是否显示删除按钮
    _notify: bool = False  # 删种时是否发送通知
    _sort_by: str = "count"  # 排序字段: count/size/name/seeding_time/uploaded
    _sort_order: str = "desc"  # 排序方向: desc/asc
    _view_mode: str = "group"  # 视图模式: group(按分组) / downloader(按下载器聚合)

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
    # 选中集合：{hash: downloader}，非持久化，重启即空
    _selected: Dict[str, str] = {}
    # 上次渲染时的可见种子列表 [(hash, downloader)]，用于全选/反选
    _last_visible: List[Tuple[str, str]] = []
    # 详情页当前页码（1-based，非持久化，重启即回到 1）
    _current_page: int = 1
    # 每页分组数（详情页分页大小）
    PAGE_SIZE: int = 50
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
            pk = config.get("path_keywords") or []
            if isinstance(pk, str):
                # 兼容旧版 path_keyword 字符串
                pk = [pk.strip()] if pk.strip() else []
            elif not isinstance(pk, list):
                pk = []
            self._path_keywords = [str(p).strip() for p in pk if str(p).strip()]
            try:
                self._size_min_gb = max(0.0, float(config.get("size_min_gb") or 0))
            except (TypeError, ValueError):
                self._size_min_gb = 0.0
            try:
                self._size_max_gb = max(0.0, float(config.get("size_max_gb") or 0))
            except (TypeError, ValueError):
                self._size_max_gb = 0.0
            self._allow_delete = bool(config.get("allow_delete", False))
            self._notify = bool(config.get("notify", False))
            self._sort_by = str(config.get("sort_by") or "count").strip() or "count"
            self._sort_order = str(config.get("sort_order") or "desc").strip() or "desc"
            self._view_mode = str(config.get("view_mode") or "group").strip() or "group"

        # 每次初始化/重载后页码回到第一页
        self._current_page = 1

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
                "path": "/set_filter_text",
                "endpoint": self.set_filter_text,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "设置文本筛选（手动输入路径或关键词）",
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
            {
                "path": "/toggle_select",
                "endpoint": self.toggle_select,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换单个种子的选中状态",
            },
            {
                "path": "/toggle_select_group",
                "endpoint": self.toggle_select_group,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换一组种子的选中状态",
            },
            {
                "path": "/select_all",
                "endpoint": self.select_all,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "全选当前可见种子",
            },
            {
                "path": "/select_invert",
                "endpoint": self.select_invert,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "反选当前可见种子",
            },
            {
                "path": "/select_clear",
                "endpoint": self.select_clear,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "清空选择",
            },
            {
                "path": "/batch_delete",
                "endpoint": self.batch_delete,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量删除已选中的种子",
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
        page 字段特殊处理：只更新 self._current_page（不写入 config，重启即回到 1）。
        其他筛选条件变更时页码回到 1，避免定位到不存在的页。
        """
        raw: Dict[str, Any] = params.dict(exclude_none=True)
        # page 是非持久化的翻页状态，单独处理
        page_val = raw.pop("page", None)
        page_only = (page_val is not None) and (not raw)

        if raw:
            # 其他筛选条件变化时页码回到第 1 页
            self._current_page = 1
            try:
                current = self.get_config() or {}
                current.update(raw)
                self.update_config(current)
                # 同步回内存实例变量，避免下次 render 前需要重载
                for key, value in raw.items():
                    attr = f"_{key}"
                    if hasattr(self, attr):
                        setattr(self, attr, value)
            except Exception as err:  # noqa: BLE001
                logger.error(f"[CrossSeedView] 保存筛选条件失败：{err}")
                return Response(success=False, message=f"保存失败：{err}")

        if page_val is not None:
            try:
                self._current_page = max(1, int(page_val))
            except (TypeError, ValueError):
                self._current_page = 1

        if not raw and page_val is None:
            return Response(success=True, message="无变更")
        if page_only:
            return Response(success=True, message=f"已切换到第 {self._current_page} 页")
        return Response(success=True, message="已保存")

    def clear_filters(self) -> Response:
        """重置详情页筛选条件为默认值。"""
        defaults = {
            "min_count": 2,
            "max_count": 0,
            "downloader_filter": "",
            "name_keyword": "",
            "path_keywords": [],
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
            # 页码回到第一页
            self._current_page = 1
        except Exception as err:  # noqa: BLE001
            logger.error(f"[CrossSeedView] 重置筛选条件失败：{err}")
            return Response(success=False, message=f"重置失败：{err}")
        return Response(success=True, message="已重置")

    def set_filter_text(self, params: SetFilterTextParams) -> Response:
        val = (params.value or "").strip()
        current = self.get_config() or {}
        if "/" in val or "\\" in val:
            current["path_keywords"] = [val] if val else []
            current["name_keyword"] = ""
        else:
            current["name_keyword"] = val or ""
            current["path_keywords"] = []
        try:
            self.update_config(current)
            self._path_keywords = current.get("path_keywords") or []
            self._name_keyword = current.get("name_keyword") or ""
            self._current_page = 1
        except Exception as err:
            logger.error(f"[CrossSeedView] 设置文本筛选失败：{err}")
            return Response(success=False, message=f"设置失败：{err}")
        return Response(success=True, message=f"筛选已应用：{val or chr(65288)}全部{chr(65289)}")

    def _cleanup_scrape_leftover_dirs(self, fileitem, storage_chain) -> int:
        """兜底清理刮削残留目录。

        场景：`StorageChain.delete_media_file` 删完源文件后，会尝试逐级向上清理
        空的父目录，但存在两处保守分支：
        1) 当父目录匹配到 DirectoryHelper 的资源目录/媒体库根（library_path）时，
           条件 `associated_dir.is_relative_to(dir_item.path)` 为真直接 break；
        2) 目录内残留 .nfo / .jpg / .srt 等非视频文件时，`list_files(recursion=False)`
           非空也会 break。

        结果就是电影的刮削文件夹（比如 `外语电影/XX电影/`）保留了 poster/nfo 等
        残留，用户视觉上"没删干净"。

        这里做兜底：从被删文件的父目录起向上探测最多 2 层，仅当整棵子树里没有
        任何视频扩展（`settings.RMT_MEDIAEXT`）时，直接 `storage_chain.delete_file`
        整个目录。本地存储对 dir 走 `shutil.rmtree`，能连带 .nfo/.jpg 清干净。

        约束：
        - 只处理路径深度 > 2 的目录，避免误删根或一级目录；
        - 只用纯视频扩展，避免字幕/音频文件反过来保住空壳目录；
        - 只处理与 fileitem 同一 storage 的层级；
        - 全流程 try/except，失败不阻断主删除。

        返回实际删除的目录数量。
        """
        from pathlib import Path  # 局部导入避免顶部污染

        removed = 0
        src_path = getattr(fileitem, "path", None) or ""
        logger.info(f"[CrossSeedView] 兜底清理入口 src={src_path}")
        try:
            parent = storage_chain.get_parent_item(fileitem)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 取父目录失败 src={src_path}: {err}")
            return 0

        # 最多向上探测 2 层：一般刮削目录结构为 <媒体库根>/<分类>/<影片名>/<文件>
        for depth in range(2):
            if not parent:
                break
            parent_path = getattr(parent, "path", None) or ""
            if not parent_path:
                logger.debug("[CrossSeedView] 父目录路径为空，停止兜底")
                break
            logger.info(f"[CrossSeedView] 兜底探测第 {depth + 1} 层父目录：{parent_path}")
            # 安全边界：路径过浅一律不动
            try:
                if len(Path(parent_path).parts) <= 2:
                    logger.info(f"[CrossSeedView] 父目录过浅，停止兜底：{parent_path}")
                    break
            except Exception:  # noqa: BLE001
                break

            # 是否还有真实视频文件
            try:
                has_video = storage_chain.any_files(
                    parent, extensions=settings.RMT_MEDIAEXT
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    f"[CrossSeedView] any_files 检查失败 path={parent_path}: {err}"
                )
                break

            if has_video is not False:
                # True 或 None（不支持的存储）都不动
                logger.info(
                    f"[CrossSeedView] 目录仍存在视频文件或无法确认(has_video={has_video})，停止兜底：{parent_path}"
                )
                break

            # 无视频文件，整目录删掉（残留 .nfo/.jpg 一并清）
            try:
                ok = storage_chain.delete_file(parent)
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    f"[CrossSeedView] 兜底删除目录异常 path={parent_path}: {err}"
                )
                break
            if not ok:
                logger.warning(
                    f"[CrossSeedView] 兜底删除目录返回失败 path={parent_path}"
                )
                break
            removed += 1
            logger.info(f"[CrossSeedView] 已清理刮削残留目录：{parent_path}")

            # 继续向上一层
            try:
                parent = storage_chain.get_parent_item(parent)
            except Exception as err:  # noqa: BLE001
                logger.debug(f"[CrossSeedView] 继续上探父目录失败：{err}")
                break

        return removed

    def _lookup_content_paths(self, hashes: List[str]) -> Dict[str, str]:
        """从缓存 groups 中按 hash 取 save_path（content_path）。

        v1.1.3 起：必须在 remove_torrents 之前调用，用返回的 path 作为
        _cleanup_links_for_hash(content_path=...) 的兜底查询依据。种子从下载器
        移除后，缓存最终会被 _refresh_cache 刷掉，届时 save_path 就永远拿不到了。
        """
        result: Dict[str, str] = {}
        if not hashes:
            return result
        wanted = set(h for h in hashes if h)
        if not wanted:
            return result
        try:
            with self._cache_lock:
                groups = list((self._cache or {}).get("groups") or [])
            for g in groups:
                if not wanted:
                    break
                for t in (g.get("torrents") or []):
                    th = t.get("hash")
                    if th and th in wanted:
                        sp = t.get("save_path") or ""
                        if sp and th not in result:
                            result[th] = sp
                        wanted.discard(th)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 查询 content_path 失败 hashes={hashes}: {err}")
        return result

    def _lookup_torrent_names(self, hashes: List[str]) -> Dict[str, str]:
        """从缓存 groups 中按 hash 取所属分组的种子名称。

        v1.1.4：用于在通知/日志里显示"删除的是哪个种子"，与 _lookup_content_paths
        同样必须在 remove_torrents 之前调用。
        """
        result: Dict[str, str] = {}
        if not hashes:
            return result
        wanted = set(h for h in hashes if h)
        if not wanted:
            return result
        try:
            with self._cache_lock:
                groups = list((self._cache or {}).get("groups") or [])
            for g in groups:
                if not wanted:
                    break
                gname = str(g.get("name") or "")
                for t in (g.get("torrents") or []):
                    th = t.get("hash")
                    if th and th in wanted:
                        if gname and th not in result:
                            result[th] = gname
                        wanted.discard(th)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 查询种子名称失败 hashes={hashes}: {err}")
        return result

    def _find_related_hashes(self, download_hash: str) -> List[str]:
        """从当前缓存中查找与该 hash 同组（相同 name+size）的所有辅种 hash。

        辅种场景下 TransferHistory 只会关联"最先入库"那个 hash。用户删除的
        是另一个辅种 hash 时，list_by_hash 会直接返空，看起来"无媒体库关联"，
        但媒体库文件其实还在。此函数按缓存里的分组扩展 hash 列表，供
        _cleanup_links_for_hash 兜底查询用。返回值包含自身 hash。
        """
        result: List[str] = [download_hash]
        try:
            with self._cache_lock:
                groups = list((self._cache or {}).get("groups") or [])
            for g in groups:
                tors = g.get("torrents") or []
                hashes = [t.get("hash") for t in tors if t.get("hash")]
                if download_hash in hashes:
                    for h in hashes:
                        if h and h != download_hash and h not in result:
                            result.append(h)
                    break
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 查找同组辅种 hash 失败 {download_hash}: {err}")
        return result

    def _cleanup_links_for_hash(
        self,
        download_hash: str,
        content_path: Optional[str] = None,
    ) -> Tuple[int, int, List[str], List[str]]:
        """联动 MoviePilot 原生"删除源文件和媒体库文件"流程清理辅种残留。

        对齐原生入口 `/api/v1/history/transfer?deletesrc=true&deletedest=true`
        （`app/api/endpoints/history.py:delete_transfer_history`），每条 TransferHistory 执行：
          1) dest_fileitem → StorageChain().delete_media_file 清媒体库软/硬链接 + 空目录
          2) 兜底 _cleanup_scrape_leftover_dirs 清 .nfo/.jpg/字幕 等刮削残留
          3) src_fileitem.path → DownloadFiles.delete_by_fullpath 清下载文件表
             （物理源文件由下载器 remove_torrents(delete_file=True) 负责，不再重复 delete_media_file）
          4) eventmanager.send_event(EventType.DownloadFileDeleted, {src: history.src, hash: history.download_hash})
          5) TransferHistoryOper().delete(history.id)

        辅种三级回退查询 TransferHistory：
          A) 主 hash list_by_hash
          B) 缓存分组内同组辅种 hash 逐个 list_by_hash（辅种只会关联最先入库的 hash）
          C) content_path（种子 save_path）→ get_by_src(path, "local") 兜底
             覆盖"缓存里同组辅种已被删光但 TransferHistory 仍在"的场景

        content_path 必须由调用方在 remove_torrents 之前从缓存 group 中取出，
        否则种子从下载器移除后此路径无法再获取。

        返回 (成功数, 失败数, 已删除的媒体库路径列表, 删除失败的媒体库路径列表)。
        仅处理本地存储链接，其他存储遇错不阻断主删除流程。
        """
        if not download_hash:
            return 0, 0, [], []
        try:
            transfer_oper = getattr(self, "_transferhistory_oper", None)
            if transfer_oper is None:
                transfer_oper = TransferHistoryOper()
                self._transferhistory_oper = transfer_oper
            storage_chain = getattr(self, "_storage_chain", None)
            if storage_chain is None:
                storage_chain = StorageChain()
                self._storage_chain = storage_chain
        except Exception as err:  # noqa: BLE001
            logger.error(f"[CrossSeedView] 初始化清理组件失败 hash={download_hash}: {err}")
            return 0, 0, [], []

        # 汇总所有相关 TransferHistory：主 hash + 同组辅种 hash + content_path 兜底
        related_hashes = self._find_related_hashes(download_hash)
        histories_map: Dict[int, Any] = {}
        for h in related_hashes:
            try:
                for hist in (transfer_oper.list_by_hash(h) or []):
                    histories_map[hist.id] = hist
            except Exception as err:  # noqa: BLE001
                logger.debug(f"[CrossSeedView] 查询 TransferHistory 失败 hash={h}: {err}")

        # content_path 兜底：hash 查空时，用种子 save_path 走 src 查询
        # 覆盖"缓存里同组辅种已被删光但 TransferHistory 仍在"的场景
        if not histories_map and content_path:
            try:
                hist = transfer_oper.get_by_src(content_path, "local")
                if hist:
                    histories_map[hist.id] = hist
                    logger.info(
                        f"[CrossSeedView] content_path 兜底命中 TransferHistory "
                        f"id={hist.id} src={content_path} hash={download_hash}"
                    )
            except Exception as err:  # noqa: BLE001
                logger.debug(
                    f"[CrossSeedView] content_path 兜底查询失败 src={content_path}: {err}"
                )

        histories = list(histories_map.values())

        if not histories:
            logger.info(
                f"[CrossSeedView] 未找到关联 TransferHistory hash={download_hash} "
                f"（已尝试同组辅种 {len(related_hashes)} 个 hash"
                f"{'+ content_path 兜底' if content_path else ''}）"
            )
            return 0, 0, [], []
        if len(related_hashes) > 1:
            logger.info(
                f"[CrossSeedView] 辅种回退命中 TransferHistory {len(histories)} 条 "
                f"（原 hash={download_hash}，扩展至同组 {len(related_hashes)} 个 hash）"
            )

        success = 0
        fail = 0
        success_paths: List[str] = []
        fail_paths: List[str] = []
        for history in histories:
            history_id = getattr(history, "id", None)
            history_src = getattr(history, "src", None) or ""
            history_dl_hash = getattr(history, "download_hash", None) or download_hash

            dest_fileitem = getattr(history, "dest_fileitem", None)
            src_fileitem = getattr(history, "src_fileitem", None)

            # ---- 1) 删媒体库软/硬链接（dest_fileitem）----
            dest_ok = False
            dest_path = ""
            dest_item: Optional[schemas.FileItem] = None
            if dest_fileitem:
                try:
                    dest_item = schemas.FileItem(**dest_fileitem)
                    dest_path = getattr(dest_item, "path", None) or ""
                except Exception as err:  # noqa: BLE001
                    logger.warning(
                        f"[CrossSeedView] 解析 dest_fileitem 失败 id={history_id} "
                        f"hash={download_hash}: {err}"
                    )
                    dest_item = None

            if dest_item is not None:
                try:
                    dest_ok = bool(storage_chain.delete_media_file(dest_item))
                except Exception as err:  # noqa: BLE001
                    logger.error(
                        f"[CrossSeedView] 删除媒体链接异常 path={dest_path} "
                        f"hash={download_hash}: {err}"
                    )
                    dest_ok = False

                if dest_ok:
                    success += 1
                    if dest_path:
                        success_paths.append(dest_path)
                    logger.info(f"[CrossSeedView] 已删除媒体链接：{dest_path}")
                else:
                    fail += 1
                    if dest_path:
                        fail_paths.append(dest_path)
                    logger.warning(f"[CrossSeedView] 删除媒体链接失败：{dest_path}")

                # 兜底刮削残留目录（无论 dest 删除是否成功都跑，v1.1.1 教训）
                try:
                    self._cleanup_scrape_leftover_dirs(dest_item, storage_chain)
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        f"[CrossSeedView] 清理刮削残留目录异常 path={dest_path}: {err}"
                    )

            # ---- 2) 清 DownloadFiles 表（用 src 路径，对齐原生流程）----
            # 注意：v1.1.2 之前误用了 dest_path，DownloadFiles 表存的是源路径，删不掉。
            # 物理源文件已由 remove_torrents(delete_file=True) 交给下载器删，
            # 这里不再重复调用 delete_media_file(src_fileitem)。
            src_path = ""
            if src_fileitem:
                try:
                    src_item = schemas.FileItem(**src_fileitem)
                    src_path = getattr(src_item, "path", None) or ""
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        f"[CrossSeedView] 解析 src_fileitem 失败 id={history_id}: {err}"
                    )

            if src_path:
                try:
                    from pathlib import Path as _P
                    DownloadHistoryOper().delete_file_by_fullpath(_P(src_path).as_posix())
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        f"[CrossSeedView] 清理 DownloadFiles 失败 src={src_path}: {err}"
                    )

            # ---- 3) 发 DownloadFileDeleted 事件（对齐原生流程）----
            try:
                eventmanager.send_event(
                    EventType.DownloadFileDeleted,
                    {"src": history_src or src_path, "hash": history_dl_hash},
                )
            except Exception as err:  # noqa: BLE001
                logger.debug(f"[CrossSeedView] 发送 DownloadFileDeleted 事件失败：{err}")

            # ---- 4) 删 TransferHistory 行（无论上面是否成功都清脏数据）----
            if history_id is not None:
                try:
                    transfer_oper.delete(history_id)
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        f"[CrossSeedView] 删除 TransferHistory 失败 id={history_id}: {err}"
                    )

        return success, fail, success_paths, fail_paths

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
        # v1.1.3：在 remove_torrents 之前抢救 content_path，供后续兜底查询
        content_path = self._lookup_content_paths([params.hash]).get(params.hash, "")
        # v1.1.4：在 remove_torrents 之前抢救种子名，供通知使用
        torrent_name = self._lookup_torrent_names([params.hash]).get(params.hash, "")
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
        # 连带清理媒体库软/硬链接（仅在“删种+文件”时）
        link_msg = ""
        cleaned_success_paths: List[str] = []
        cleaned_fail_paths: List[str] = []
        cleaned_success = 0
        cleaned_fail = 0
        if params.delete_files:
            try:
                cleaned_success, cleaned_fail, cleaned_success_paths, cleaned_fail_paths = (
                    self._cleanup_links_for_hash(params.hash, content_path=content_path)
                )
                logger.info(
                    f"[CrossSeedView] 媒体链接清理完成 hash={params.hash} "
                    f"成功={cleaned_success} 失败={cleaned_fail}"
                )
                if cleaned_fail:
                    link_msg = f"，媒体库链接清理 成功{cleaned_success}/失败{cleaned_fail}"
                elif cleaned_success:
                    link_msg = f"，已同步清理媒体库链接 {cleaned_success} 个"
                else:
                    link_msg = "，无关联媒体库链接"
            except Exception as err:  # noqa: BLE001
                logger.error(f"[CrossSeedView] 清理媒体链接异常 hash={params.hash}: {err}")
                link_msg = f"，媒体库链接清理异常：{err}"
        # 后台重新扫描，刷新分组
        try:
            self._refresh_cache(source="delete")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 删除后刷新缓存失败（忽略）：{err}")
        # 发送通知
        if self._notify:
            try:
                mode_txt = "删种+文件+清库" if params.delete_files else "仅删种"
                lines = [mode_txt]
                if torrent_name:
                    lines.append(f"种子: {torrent_name}")
                lines.append(f"hash: {params.hash[:8]}")
                lines.append(f"下载器: {params.downloader}")
                if params.delete_files:
                    lines.append(
                        f"媒体库链接: 清理 {cleaned_success} 个"
                        + (f"，失败 {cleaned_fail} 个" if cleaned_fail else "")
                    )
                    if cleaned_success_paths:
                        preview = cleaned_success_paths[:5]
                        lines.append("已清理路径:")
                        for p in preview:
                            lines.append(f"  · {_short_path(p, keep=40)}")
                        if len(cleaned_success_paths) > 5:
                            lines.append(f"  … 共 {len(cleaned_success_paths)} 项")
                    if cleaned_fail_paths:
                        lines.append("失败路径:")
                        for p in cleaned_fail_paths[:5]:
                            lines.append(f"  · {_short_path(p, keep=40)}")
                        if len(cleaned_fail_paths) > 5:
                            lines.append(f"  … 共 {len(cleaned_fail_paths)} 项")
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="【辅种查看-单条删除】",
                    text="\n".join(lines),
                )
            except Exception as err:  # noqa: BLE001
                logger.debug(f"[CrossSeedView] 发送删除通知失败（忽略）：{err}")
        return Response(success=True, message=f"已删除{link_msg}")

    # ---------- 多选相关 API ----------

    def toggle_select(self, params: ToggleSelectParams) -> Response:
        """切换单个种子的选中状态。"""
        if not self._enabled:
            return Response(success=False, message="插件未启用")
        if not params.hash or not params.downloader:
            return Response(success=False, message="参数不完整")
        h = params.hash
        if h in self._selected:
            del self._selected[h]
        else:
            self._selected[h] = params.downloader
        return Response(success=True, message=f"已选 {len(self._selected)} 项")

    def toggle_select_group(self, params: ToggleSelectGroupParams) -> Response:
        """切换一组种子的选中状态。

        规则：若组内所有种子已全部选中 -> 全部取消；否则 -> 全部添加。
        """
        if not self._enabled:
            return Response(success=False, message="插件未启用")
        pairs: List[Tuple[str, str]] = []
        for t in params.torrents or []:
            h = str(t.get("hash") or "")
            dl = str(t.get("downloader") or "")
            if h and dl:
                pairs.append((h, dl))
        if not pairs:
            return Response(success=False, message="参数不完整")
        all_selected = all(h in self._selected for h, _ in pairs)
        if all_selected:
            for h, _ in pairs:
                self._selected.pop(h, None)
        else:
            for h, dl in pairs:
                self._selected[h] = dl
        return Response(success=True, message=f"已选 {len(self._selected)} 项")

    def select_all(self) -> Response:
        """全选当前可见种子（基于上次 get_page 渲染时的可见集合）。"""
        if not self._enabled:
            return Response(success=False, message="插件未启用")
        added = 0
        for h, dl in self._last_visible:
            if h and dl and h not in self._selected:
                self._selected[h] = dl
                added += 1
        return Response(success=True, message=f"已选 {len(self._selected)} 项（新增 {added}）")

    def select_invert(self) -> Response:
        """反选当前可见种子。"""
        if not self._enabled:
            return Response(success=False, message="插件未启用")
        for h, dl in self._last_visible:
            if not h or not dl:
                continue
            if h in self._selected:
                del self._selected[h]
            else:
                self._selected[h] = dl
        return Response(success=True, message=f"已选 {len(self._selected)} 项")

    def select_clear(self) -> Response:
        """清空选择。"""
        self._selected.clear()
        return Response(success=True, message="已清空选择")

    def batch_delete(self, params: BatchDeleteParams) -> Response:
        """批量删除已选中的种子。按下载器分组批量调用。"""
        if not self._enabled:
            return Response(success=False, message="插件未启用")
        if not self._allow_delete:
            return Response(success=False, message="删除功能未启用，请在插件设置中开启")
        if not self._selected:
            return Response(success=False, message="尚未选择任何种子")

        # 按下载器分组
        by_dl: Dict[str, List[str]] = defaultdict(list)
        for h, dl in self._selected.items():
            if h and dl:
                by_dl[dl].append(h)

        # v1.1.3：在 remove_torrents 之前抢救 content_path，供后续兜底查询
        all_hashes = [h for hs in by_dl.values() for h in hs]
        hash_to_path = self._lookup_content_paths(all_hashes)
        # v1.1.4：同步抢救种子名，供通知使用
        hash_to_name = self._lookup_torrent_names(all_hashes)

        total = sum(len(v) for v in by_dl.values())
        succeeded = 0
        succeeded_hashs: List[str] = []
        failed_dls: List[str] = []
        for dl, hashs in by_dl.items():
            try:
                ok = ChainBase().remove_torrents(
                    hashs=hashs,
                    delete_file=bool(params.delete_files),
                    downloader=dl,
                )
            except Exception as err:  # noqa: BLE001
                logger.error(f"[CrossSeedView] 批量删除失败 downloader={dl} 数量={len(hashs)}: {err}")
                failed_dls.append(f"{dl}({len(hashs)}):{err}")
                continue
            if ok:
                succeeded += len(hashs)
                succeeded_hashs.extend(hashs)
            else:
                failed_dls.append(f"{dl}({len(hashs)})")

        # 连带清理媒体库软/硬链接（仅在“删种+文件”时，且仅对下载器删除成功的种子）
        link_success = 0
        link_fail = 0
        all_success_paths: List[str] = []
        all_fail_paths: List[str] = []
        per_hash_cleaned: Dict[str, int] = {}
        if params.delete_files and succeeded_hashs:
            for h in succeeded_hashs:
                try:
                    s, f, s_paths, f_paths = self._cleanup_links_for_hash(
                        h, content_path=hash_to_path.get(h, "")
                    )
                    link_success += s
                    link_fail += f
                    all_success_paths.extend(s_paths)
                    all_fail_paths.extend(f_paths)
                    per_hash_cleaned[h] = s
                except Exception as err:  # noqa: BLE001
                    logger.error(f"[CrossSeedView] 批量清理媒体链接异常 hash={h}: {err}")
                    link_fail += 1
            logger.info(
                f"[CrossSeedView] 批量媒体链接清理完成 成功={link_success} 失败={link_fail}"
            )

        logger.info(
            f"[CrossSeedView] 批量删除完成 总数={total} 成功={succeeded} "
            f"delete_files={params.delete_files} 失败下载器={failed_dls}"
        )

        # 无论部分成败都清空选择并刷新
        self._selected.clear()
        try:
            self._refresh_cache(source="batch_delete")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 批量删除后刷新缓存失败（忽略）：{err}")

        link_msg = ""
        if params.delete_files and (link_success or link_fail):
            if link_fail:
                link_msg = f"，媒体库链接清理 成功{link_success}/失败{link_fail}"
            elif link_success:
                link_msg = f"，已同步清理媒体库链接 {link_success} 个"
        elif params.delete_files and succeeded_hashs:
            link_msg = "，无关联媒体库链接"

        # 发送通知
        if self._notify:
            try:
                mode_txt = "删种+文件+清库" if params.delete_files else "仅删种"
                lines = [mode_txt, f"成功 {succeeded}/{total} 项"]
                # 每个种子简报：名字 + 清理数
                named_hashs = [h for h in succeeded_hashs if hash_to_name.get(h)]
                if named_hashs:
                    lines.append("涉及种子:")
                    preview_n = named_hashs[:5]
                    for h in preview_n:
                        name = hash_to_name.get(h, "")
                        cleaned_n = per_hash_cleaned.get(h, 0)
                        suffix = f"（清理 {cleaned_n}）" if params.delete_files else ""
                        lines.append(f"  · {name}{suffix}")
                    if len(named_hashs) > 5:
                        lines.append(f"  … 共 {len(named_hashs)} 个")
                if params.delete_files:
                    lines.append(
                        f"媒体库链接: 清理 {link_success} 个"
                        + (f"，失败 {link_fail} 个" if link_fail else "")
                    )
                    if all_success_paths:
                        lines.append("已清理路径:")
                        for p in all_success_paths[:5]:
                            lines.append(f"  · {_short_path(p, keep=40)}")
                        if len(all_success_paths) > 5:
                            lines.append(f"  … 共 {len(all_success_paths)} 项")
                    if all_fail_paths:
                        lines.append("失败路径:")
                        for p in all_fail_paths[:5]:
                            lines.append(f"  · {_short_path(p, keep=40)}")
                        if len(all_fail_paths) > 5:
                            lines.append(f"  … 共 {len(all_fail_paths)} 项")
                if failed_dls:
                    lines.append("失败：" + "; ".join(failed_dls))
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="【辅种查看-批量删除】",
                    text="\n".join(lines),
                )
            except Exception as err:  # noqa: BLE001
                logger.debug(f"[CrossSeedView] 发送批量删除通知失败（忽略）：{err}")

        if failed_dls:
            return Response(
                success=succeeded > 0,
                message=f"批量删除 {succeeded}/{total} 成功，失败：{'; '.join(failed_dls)}{link_msg}",
            )
        return Response(success=True, message=f"已批量删除 {succeeded} 项{link_msg}")


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

        # 获取保存路径选项（从缓存中获取，如果没有则从下载器实时获取）
        save_path_options: List[Dict[str, str]] = [{"title": "全部路径", "value": ""}]
        try:
            # 优先从缓存中获取
            saved_cache = self.get_data("cache")
            if isinstance(saved_cache, dict) and saved_cache.get("groups"):
                path_counter: Dict[str, int] = {}
                for g in saved_cache["groups"]:
                    for t in (g.get("torrents") or []):
                        sp = str((t or {}).get("save_path") or "").strip()
                        if sp:
                            path_counter[sp] = path_counter.get(sp, 0) + 1
                # 按出现次数排序，取前20个
                top_paths = sorted(path_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
                save_path_options = [{"title": "全部路径", "value": ""}] + [
                    {"title": p, "value": p} for p, _ in top_paths
                ]
            else:
                # 缓存中没有，尝试从下载器实时获取
                from app.helper.downloader import DownloaderHelper
                dl_helper = DownloaderHelper()
                for dl_name, dl in dl_helper.get_downloader().items():
                    try:
                        torrents = dl.get_torrents() or []
                        path_counter: Dict[str, int] = {}
                        for t in torrents:
                            sp = str((t or {}).get("save_path") or "").strip()
                            if sp:
                                path_counter[sp] = path_counter.get(sp, 0) + 1
                        for p, c in sorted(path_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
                            title = f"{p} ({c}个种子)"
                            if not any(opt["value"] == p for opt in save_path_options):
                                save_path_options.append({"title": title, "value": p})
                    except Exception:
                        pass
        except Exception as err:  # noqa: BLE001
            logger.debug(f"[CrossSeedView] 读取保存路径列表失败：{err}")

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
                                            "label": "允许在详情页删除种子（含媒体库链接，危险）",
                                            "color": "error",
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
                                            "model": "notify",
                                            "label": "删种时发送通知",
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
                                        "component": "VCombobox",
                                        "props": {
                                            "model": "path_keywords",
                                            "label": "保存路径关键词(多选,精确匹配)",
                                            "items": save_path_options,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "placeholder": "选择保存路径或手动输入，留空查看全部",
                                            "delimiters": [","],
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
            "notify": False,
            "cron": "0 4 * * *",
            "min_count": 2,
            "max_count": 0,
            "downloader_filter": "",
            "name_keyword": "",
            "path_keywords": [],
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

        # 路径关键词展示用变量（用于日志和筛选状态提示，与 name_kw 对称）
        path_kw = "|".join(self._path_keywords) if self._path_keywords else ""

        # 5) 保存路径关键词（多选精确匹配，OR 逻辑）
        if self._path_keywords:
            filtered = [
                g for g in filtered
                if any(
                    sp in self._path_keywords
                    for sp in (g.get("save_paths") or [])
                )
            ]

        # 6) 大小区间（GB）
        gb = 1024.0 ** 3
        if self._size_min_gb > 0:
            min_bytes = self._size_min_gb * gb
            filtered = [g for g in filtered if float(g.get("size") or 0) >= min_bytes]
        if self._size_max_gb > 0:
            max_bytes = self._size_max_gb * gb
            filtered = [g for g in filtered if float(g.get("size") or 0) <= max_bytes]

        # 排序（动态）
        sort_by = (self._sort_by or "count").lower()
        sort_desc = (self._sort_order or "desc").lower() != "asc"

        def _sort_key(g: Dict[str, Any]):
            if sort_by == "size":
                return (int(g.get("size") or 0), str(g.get("name") or ""))
            if sort_by == "name":
                return (str(g.get("name") or "").lower(),)
            if sort_by == "seeding_time":
                return (int(g.get("max_seeding_time") or 0), str(g.get("name") or ""))
            if sort_by == "uploaded":
                return (int(g.get("total_uploaded") or 0), str(g.get("name") or ""))
            # 默认 count
            return (int(g.get("count") or 0), str(g.get("name") or ""))

        filtered.sort(key=_sort_key, reverse=sort_desc)

        # 视图模式：按下载器聚合（把每个下载器的所有种子作为一个「组」呈现）
        view_mode = (self._view_mode or "group").lower()
        if view_mode == "downloader":
            dl_map: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {
                    "name": "",
                    "size": 0,
                    "count": 0,
                    "downloaders": set(),
                    "save_paths": set(),
                    "torrents": [],
                    "max_seeding_time": 0,
                    "total_uploaded": 0,
                }
            )
            for g in filtered:
                for t in (g.get("torrents") or []):
                    dl = str((t or {}).get("downloader") or "-")
                    e = dl_map[dl]
                    e["name"] = f"[下载器] {dl}"
                    e["size"] += int(g.get("size") or 0)
                    e["count"] += 1
                    e["downloaders"].add(dl)
                    sp = (t or {}).get("save_path")
                    if sp:
                        e["save_paths"].add(sp)
                    e["torrents"].append({
                        **t,
                        "_group_name": g.get("name") or "",
                        "_group_size": g.get("size") or 0,
                    })
                    st = int((t or {}).get("seeding_time") or 0)
                    if st > e["max_seeding_time"]:
                        e["max_seeding_time"] = st
                    e["total_uploaded"] += int((t or {}).get("uploaded") or 0)
            filtered = []
            for dl, e in dl_map.items():
                filtered.append({
                    "name": e["name"],
                    "size": e["size"],
                    "count": e["count"],
                    "downloaders": sorted(e["downloaders"]),
                    "save_paths": sorted(e["save_paths"]),
                    "torrents": e["torrents"],
                    "max_seeding_time": e["max_seeding_time"],
                    "total_uploaded": e["total_uploaded"],
                    "state": "",
                })
            filtered.sort(key=_sort_key, reverse=sort_desc)

        items = [
            {
                "name": g.get("name") or "-",
                "count": g.get("count", 0),
                "size": g.get("size") or 0,
                "size_text": self._fmt_size(g.get("size") or 0),
                "save_paths_text": "\n".join(g.get("save_paths") or []) or "-",
                "downloaders_text": "、".join(g.get("downloaders") or []) or "-",
                "torrents": list(g.get("torrents") or []),
                "max_seeding_time": g.get("max_seeding_time", 0),
                "total_uploaded": g.get("total_uploaded", 0),
                "state": g.get("state") or "",
            }
            for g in filtered
        ]

        # -------- 分页切片：只渲染当前页 --------
        page_size = self.PAGE_SIZE
        total_items = len(items)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        page = self._current_page if isinstance(self._current_page, int) else 1
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        # 若因外部变化触发页码越界，回写内存以保持一致
        self._current_page = page
        page_start = (page - 1) * page_size
        page_end = page_start + page_size
        page_items = items[page_start:page_end]

        # 记录当前可见且可勾选的 (hash, downloader) 集合，供 select_all/select_invert 使用
        # 全选/反选只作用于当前页
        visible: List[Tuple[str, str]] = []
        if self._allow_delete:
            for it in page_items:
                for t in it.get("torrents") or []:
                    h = str(t.get("hash") or "")
                    dl = str(t.get("downloader") or "")
                    if h and dl:
                        visible.append((h, dl))
        self._last_visible = visible

        logger.info(
            f"[CrossSeedView] get_page: 总分组={len(groups)}，"
            f"筛选后={total_items}，本页={len(page_items)}(第{page}/{total_pages}页)，"
            f"下载器={selected_dl or '全部'}，"
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
        summary_row_extra = {
            "component": "VRow",
            "props": {"class": "mb-2"},
            "content": [
                self._stat_card("孤种组数", snapshot.get("orphan_groups", 0), "warning"),
                self._stat_card("辅种组数(≥2)", snapshot.get("cross_groups", 0), "success"),
                self._stat_card(
                    "冗余占用",
                    self._fmt_size(snapshot.get("redundant_bytes", 0) or 0),
                    "error",
                ),
                self._stat_card(
                    "累计上传量",
                    self._fmt_size(snapshot.get("total_uploaded", 0) or 0),
                    "primary",
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
        refresh_api = f"plugin/CrossSeedView/refresh?apikey={settings.API_TOKEN}"
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

        # 聚合 Top 8 常见保存路径（基于全量分组的 torrents 出现次数）
        path_counter: Dict[str, int] = {}
        for g in groups:
            for t in (g.get("torrents") or []):
                sp = str((t or {}).get("save_path") or "").strip()
                if sp:
                    path_counter[sp] = path_counter.get(sp, 0) + 1
        top_paths = sorted(path_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:8]

        def _short_path(p: str, keep: int = 24) -> str:
            if len(p) <= keep:
                return p
            # 保留最后 keep 字符，前面用 …
            return "…" + p[-keep:]

        def _common_dir_prefix(paths: List[str]) -> str:
            """求路径列表的公共目录前缀（按 / 分段，至少留 1 段完整目录，末尾带 /）。"""
            if len(paths) < 2:
                return ""
            # 统一分隔符
            segs_list = [p.replace("\\", "/").split("/") for p in paths]
            common: List[str] = []
            for parts in zip(*segs_list):
                if len(set(parts)) == 1:
                    common.append(parts[0])
                else:
                    break
            # 至少要有两段（比如 "" + "media"），且不能把整条路径都吃掉
            if len(common) < 2:
                return ""
            if any(len(segs) == len(common) for segs in segs_list):
                # 有路径本身就等于公共前缀，别裁到只剩空
                common = common[:-1]
                if len(common) < 2:
                    return ""
            prefix = "/".join(common)
            return prefix + "/" if prefix else ""

        # directory dropdown menu (VMenu + VList, replaces old chips)
        set_filter_text_api = f"plugin/CrossSeedView/set_filter_text?apikey={settings.API_TOKEN}"
        paths_only = [p for p, _ in top_paths]
        common_prefix = _common_dir_prefix(paths_only) if top_paths else ""
        dir_menu_items: List[dict] = []
        dir_menu_items.append({
            "component": "VListItem",
            "props": {"title": "（全部目录）", "active": not bool(self._path_keywords), "color": "primary"},
            "events": {"click": {"api": save_api, "method": "post", "params": {"path_keywords": []}}},
        })
        if top_paths:
            dir_menu_items.append({"component": "VDivider", "props": {"class": "my-1"}})
            for p, cnt in top_paths:
                short = p[len(common_prefix):] if common_prefix and p.startswith(common_prefix) else p
                short = short or "."
                active = p in (self._path_keywords or [])
                dir_menu_items.append({
                    "component": "VListItem",
                    "props": {"title": f"{_short_path(short, 40)} ({cnt})", "subtitle": p, "active": active, "color": "success" if active else None},
                    "events": {"click": {"api": save_api, "method": "post", "params": {"path_keywords": [p]}}},
                })
        current_dir_label = "目录筛选 ▾"
        if self._path_keywords:
            first = self._path_keywords[0]
            current_dir_label = f"📁 {_short_path(first, 28)} ▾"
        dir_dropdown = {
            "component": "VBtn",
            "props": {"color": "success" if self._path_keywords else "grey", "variant": "tonal" if not self._path_keywords else "flat", "size": "small", "class": "mr-2"},
            "text": current_dir_label,
            "content": [{"component": "VMenu", "props": {"activator": "parent", "close-on-content-click": True}, "content": [{"component": "VList", "props": {"density": "compact", "maxHeight": 300, "class": "overflow-y-auto"}, "content": dir_menu_items}]}],
        }

        # 关键词下拉菜单
        keyword_presets = [
            ("(清除关键词)", "", "grey"),
            ("4K/2160p", "4K|2160p", "primary"),
            ("1080p", "1080p", "primary"),
            ("BluRay", "BluRay", "primary"),
            ("WEB-DL", "WEB-DL", "primary"),
            ("HDR/DV", "HDR|DV|Dolby", "primary"),
            ("国语音轨", "国语|国语音轨|普通话", "primary"),
            ("中文字幕", "中文字幕|CHS|CHT|简繁", "primary"),
        ]
        kw_menu_items = []
        for label, kw, color in keyword_presets:
            active = bool(self._name_keyword) and (self._name_keyword == kw or (kw and self._name_keyword in kw))
            kw_menu_items.append({
                "component": "VListItem",
                "props": {"title": label, "active": active, "color": "success" if active else None},
                "events": {"click": {"api": save_api, "method": "post", "params": {"name_keyword": kw}}},
            })
        current_kw_label = "关键词 ▾"
        if self._name_keyword:
            current_kw_label = f"🏷 {self._name_keyword[:20]} ▾"
        kw_dropdown = {
            "component": "VBtn",
            "props": {"color": "success" if self._name_keyword else "grey", "variant": "tonal" if not self._name_keyword else "flat", "size": "small", "class": "mr-2"},
            "text": current_kw_label,
            "content": [{"component": "VMenu", "props": {"activator": "parent", "close-on-content-click": True}, "content": [{"component": "VList", "props": {"density": "compact"}, "content": kw_menu_items}]}],
        }


        preset_row_children: List[dict] = [
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
        ]

        # 排序 & 视图模式
        sort_options = [
            ("count", "辅种数"),
            ("size", "大小"),
            ("name", "名称"),
            ("seeding_time", "做种时间"),
            ("uploaded", "上传量"),
        ]
        order_options = [("desc", "降序"), ("asc", "升序")]
        view_options = [("group", "按分组"), ("downloader", "按下载器")]

        def _dropdown_btn(label: str, value: str, current: str, field: str) -> dict:
            active = value == current
            return {
                "component": "VBtn",
                "props": {
                    "size": "x-small",
                    "variant": "flat" if active else "tonal",
                    "color": "primary" if active else "grey",
                    "class": "mr-1 mb-1",
                },
                "text": label,
                "events": {"click": {"api": save_api, "method": "post", "params": {field: value}}},
            }

        control_row_content: List[dict] = [
            {"component": "span", "props": {"class": "text-caption mr-2"}, "text": "排序："},
        ]
        for v, lbl in sort_options:
            control_row_content.append(_dropdown_btn(lbl, v, self._sort_by or "count", "sort_by"))
        control_row_content.append({"component": "VDivider", "props": {"vertical": True, "class": "mx-2"}})
        for v, lbl in order_options:
            control_row_content.append(_dropdown_btn(lbl, v, self._sort_order or "desc", "sort_order"))
        control_row_content.append({"component": "VDivider", "props": {"vertical": True, "class": "mx-2"}})
        control_row_content.append({"component": "span", "props": {"class": "text-caption mr-2"}, "text": "视图："})
        for v, lbl in view_options:
            control_row_content.append(_dropdown_btn(lbl, v, self._view_mode or "group", "view_mode"))

        preset_row_children.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center mt-1"},
                "content": control_row_content,
            }
        )
        # 目录下拉 + 手动输入行
        filter_row_children: List[dict] = [dir_dropdown, kw_dropdown]
        preset_row_children.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center mt-1"},
                "content": filter_row_children,
            }
        )

        preset_row = {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-2"},
            "content": [
                {
                    "component": "VCardText",
                    "props": {"class": "py-2"},
                    "content": preset_row_children,
                }
            ],
        }

                # 构建卡片列表
        # PageRender events.click 不会自动带 Bearer 头，必须在 URL 上拼 ?apikey= 走 apikey 分支
        delete_api = f"plugin/CrossSeedView/delete_torrent?apikey={settings.API_TOKEN}"
        toggle_select_api = f"plugin/CrossSeedView/toggle_select?apikey={settings.API_TOKEN}"
        toggle_select_group_api = f"plugin/CrossSeedView/toggle_select_group?apikey={settings.API_TOKEN}"
        batch_delete_api = f"plugin/CrossSeedView/batch_delete?apikey={settings.API_TOKEN}"
        select_all_api = f"plugin/CrossSeedView/select_all?apikey={settings.API_TOKEN}"
        select_invert_api = f"plugin/CrossSeedView/select_invert?apikey={settings.API_TOKEN}"
        select_clear_api = f"plugin/CrossSeedView/select_clear?apikey={settings.API_TOKEN}"

        def _torrent_row(t: dict, show_delete: bool) -> dict:
            thash = str(t.get("hash") or "")
            dl_name = str(t.get("downloader") or "")
            save_path = str(t.get("save_path") or "")
            state = str(t.get("state") or "")
            seeding_time = int(t.get("seeding_time") or 0)
            uploaded = int(t.get("uploaded") or 0)
            display_path = save_path or "-"
            # 命中当前 path_keywords 时高亮该行（精确匹配）
            hit_path = bool(save_path and save_path in (self._path_keywords or []))
            is_selected = bool(show_delete and thash and thash in self._selected)
            row_props = {"dense": True, "class": "align-center py-1"}
            # 选中态优先蓝色高亮，其次是路径命中的绿色
            if is_selected:
                row_props["style"] = "background-color: rgba(33,150,243,0.14); border-left: 3px solid #2196f3;"
            elif hit_path:
                row_props["style"] = "background-color: rgba(76,175,80,0.10); border-left: 3px solid #4caf50;"
            # 附加：所属分组信息（仅按下载器视图时可用）
            group_hint = ""
            if t.get("_group_name"):
                gs = int(t.get("_group_size") or 0)
                group_hint = f"{t.get('_group_name')} · {self._fmt_size(gs)}"
            path_col_content = []
            if group_hint:
                path_col_content.append({
                    "component": "div",
                    "props": {"class": "text-caption text-primary text-truncate", "style": "user-select: text;"},
                    "text": group_hint,
                })
            path_col_content.extend([
                {
                    "component": "div",
                    "props": {
                        "class": "text-caption text-medium-emphasis text-truncate",
                        "style": "user-select: text;",
                    },
                    "text": display_path,
                },
                {
                    "component": "div",
                    "props": {"class": "text-caption text-disabled", "style": "user-select: text;"},
                    "text": f"hash: {thash[:16]}..." if thash else "hash: -",
                },
            ])
            # 「筛选此路径」按钮：将此行 save_path 加入 path_keywords
            if save_path:
                path_col_content.append(
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "secondary",
                            "variant": "text",
                            "size": "x-small",
                            "prepend-icon": "mdi-filter-variant",
                            "class": "mt-1 px-2",
                        },
                        "text": "筛选此路径",
                        "events": {
                            "click": {
                                "api": save_api,
                                "method": "post",
                                "params": {"path_keywords": [save_path]},
                            }
                        },
                    }
                )
            # 元数据 chip 列（状态 / 做种时间 / 上传量）
            meta_chips: List[dict] = []
            if state:
                meta_chips.append({
                    "component": "VChip",
                    "props": {"size": "x-small", "color": "secondary", "variant": "tonal", "class": "mr-1"},
                    "text": state,
                })
            if seeding_time > 0:
                meta_chips.append({
                    "component": "VChip",
                    "props": {"size": "x-small", "color": "info", "variant": "tonal", "class": "mr-1"},
                    "text": self._fmt_duration(seeding_time),
                })
            if uploaded > 0:
                meta_chips.append({
                    "component": "VChip",
                    "props": {"size": "x-small", "color": "success", "variant": "tonal"},
                    "text": f"↑{self._fmt_size(uploaded)}",
                })
            dl_col_content: List[dict] = []
            if show_delete and thash and dl_name:
                row_selected = thash in self._selected
                dl_col_content.append({
                    "component": "VBtn",
                    "props": {
                        "icon": (
                            "mdi-checkbox-marked"
                            if row_selected
                            else "mdi-checkbox-blank-outline"
                        ),
                        "size": "small",
                        # v0.5.13: 未选=outlined 有可见边框，已选=flat 实心绿色
                        "variant": "flat" if row_selected else "outlined",
                        "color": "success" if row_selected else "grey-darken-1",
                        "class": "mr-1",
                    },
                    "events": {
                        "click": {
                            "api": toggle_select_api,
                            "method": "post",
                            "params": {"hash": thash, "downloader": dl_name},
                        }
                    },
                })
            dl_col_content.append({
                "component": "VChip",
                "props": {"size": "x-small", "color": "primary", "variant": "tonal"},
                "text": dl_name or "-",
            })
            row_content = [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 2},
                    "content": dl_col_content,
                },
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 5},
                    "content": path_col_content,
                },
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 2},
                    "content": [{
                        "component": "div",
                        "props": {"class": "d-flex flex-wrap"},
                        "content": meta_chips,
                    }] if meta_chips else [],
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
                                "content": [{
                                    "component": "VMenu",
                                    "props": {"activator": "parent", "close-on-content-click": True},
                                    "content": [{
                                        "component": "VList",
                                        "props": {"density": "compact"},
                                        "content": [{
                                            "component": "VListItem",
                                            "props": {"prepend-icon": "mdi-check", "title": "确认仅删种（保留文件）"},
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
                                        }],
                                    }],
                                }],
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "error",
                                    "variant": "flat",
                                    "size": "x-small",
                                },
                                "text": "删种+文件+清库",
                                "content": [{
                                    "component": "VMenu",
                                    "props": {"activator": "parent", "close-on-content-click": True},
                                    "content": [{
                                        "component": "VList",
                                        "props": {"density": "compact"},
                                        "content": [{
                                            "component": "VListItem",
                                            "props": {"prepend-icon": "mdi-alert", "title": "确认删种+源文件+媒体库链接（不可撤销）", "class": "text-error"},
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
                                        }],
                                    }],
                                }],
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
                "props": row_props,
                "content": row_content,
            }

        card_list_content: List[dict] = []

        # 批量选择工具栏：仅在允许删除时展示
        if self._allow_delete:
            selected_count = len(self._selected)
            toolbar_children: List[dict] = [
                {
                    "component": "VChip",
                    "props": {
                        "size": "small",
                        "color": "primary" if selected_count > 0 else "default",
                        "variant": "tonal",
                        "class": "mr-2",
                        "prepend-icon": "mdi-checkbox-multiple-marked-outline",
                    },
                    "text": f"已选 {selected_count} 项 / 本页可勾选 {len(visible)} 条",
                },
            ]
            visible_count = len(visible)
            # 全选可见 / 反选可见 / 清空选择
            toolbar_children.append(
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "color": "primary",
                        "variant": "tonal",
                        "prepend-icon": "mdi-checkbox-multiple-marked",
                        "class": "mr-1",
                        "disabled": visible_count == 0,
                    },
                    "text": "全选本页",
                    "events": {
                        "click": {
                            "api": select_all_api,
                            "method": "get",
                        }
                    },
                }
            )
            toolbar_children.append(
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "color": "primary",
                        "variant": "tonal",
                        "prepend-icon": "mdi-select-inverse",
                        "class": "mr-1",
                        "disabled": visible_count == 0,
                    },
                    "text": "反选本页",
                    "events": {
                        "click": {
                            "api": select_invert_api,
                            "method": "get",
                        }
                    },
                }
            )
            toolbar_children.append(
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "color": "default",
                        "variant": "text",
                        "prepend-icon": "mdi-close-circle-outline",
                        "class": "mr-3",
                        "disabled": selected_count == 0,
                    },
                    "text": "清空选择",
                    "events": {
                        "click": {
                            "api": select_clear_api,
                            "method": "get",
                        }
                    },
                }
            )
            toolbar_children.append(
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "color": "warning",
                        "variant": "outlined",
                        "prepend-icon": "mdi-trash-can-outline",
                        "class": "mr-1",
                        "disabled": selected_count == 0,
                    },
                    "text": "批量仅删种",
                    "content": [{
                        "component": "VMenu",
                        "props": {"activator": "parent", "close-on-content-click": True},
                        "content": [{
                            "component": "VList",
                            "props": {"density": "compact"},
                            "content": [{
                                "component": "VListItem",
                                "props": {
                                    "prepend-icon": "mdi-check",
                                    "title": f"确认批量仅删种（{selected_count} 项，保留文件）",
                                },
                                "events": {
                                    "click": {
                                        "api": batch_delete_api,
                                        "method": "post",
                                        "params": {"delete_files": False},
                                    }
                                },
                            }],
                        }],
                    }],
                }
            )
            toolbar_children.append(
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "color": "error",
                        "variant": "flat",
                        "prepend-icon": "mdi-delete-alert",
                        "disabled": selected_count == 0,
                    },
                    "text": "批量删种+文件+清库",
                    "content": [{
                        "component": "VMenu",
                        "props": {"activator": "parent", "close-on-content-click": True},
                        "content": [{
                            "component": "VList",
                            "props": {"density": "compact"},
                            "content": [{
                                "component": "VListItem",
                                "props": {
                                    "prepend-icon": "mdi-alert",
                                    "title": f"确认批量删种+源文件+媒体库链接（{selected_count} 项，不可撤销）",
                                    "class": "text-error",
                                },
                                "events": {
                                    "click": {
                                        "api": batch_delete_api,
                                        "method": "post",
                                        "params": {"delete_files": True},
                                    }
                                },
                            }],
                        }],
                    }],
                }
            )
            card_list_content.append(
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "class": "mb-2 pa-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-wrap align-center"},
                            "content": toolbar_children,
                        }
                    ],
                }
            )

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
            group_cards: List[dict] = []
            for idx, it in enumerate(page_items):
                show_delete = self._allow_delete
                torrents = it.get("torrents") or []
                name_text = it["name"]
                # 组头复选框：计算组的选中状态
                group_pairs = [
                    {"hash": str(t.get("hash") or ""), "downloader": str(t.get("downloader") or "")}
                    for t in torrents
                    if t.get("hash") and t.get("downloader")
                ]
                sel_count = sum(1 for p in group_pairs if p["hash"] in self._selected)
                # v0.5.13: 提升可见度——outlined/flat 变体，避免 text 变体在浅色背景下几乎透明
                if sel_count == 0:
                    group_icon = "mdi-checkbox-blank-outline"
                    group_icon_color = "grey-darken-1"
                    group_icon_variant = "outlined"
                elif sel_count == len(group_pairs):
                    group_icon = "mdi-checkbox-marked"
                    group_icon_color = "success"
                    group_icon_variant = "flat"
                else:
                    group_icon = "mdi-checkbox-intermediate"
                    group_icon_color = "warning"
                    group_icon_variant = "tonal"
                group_checkbox_btn: List[dict] = []
                if show_delete and group_pairs:
                    if len(group_pairs) == 1:
                        # 单种子组：直接切换该种子
                        p = group_pairs[0]
                        group_checkbox_btn.append({
                            "component": "VBtn",
                            "props": {
                                "icon": group_icon,
                                "size": "default",
                                "variant": group_icon_variant,
                                "color": group_icon_color,
                                "density": "comfortable",
                            },
                            "events": {
                                "click": {
                                    "api": toggle_select_api,
                                    "method": "post",
                                    "params": {"hash": p["hash"], "downloader": p["downloader"]},
                                }
                            },
                        })
                    else:
                        # 多种子组：一次切换全组
                        group_checkbox_btn.append({
                            "component": "VBtn",
                            "props": {
                                "icon": group_icon,
                                "size": "default",
                                "variant": group_icon_variant,
                                "color": group_icon_color,
                                "density": "comfortable",
                            },
                            "events": {
                                "click": {
                                    "api": toggle_select_group_api,
                                    "method": "post",
                                    "params": {"torrents": group_pairs},
                                }
                            },
                        })
                # 卡片头：名称 + 概要 chips（名称可选中复制，不触发展开）
                # v0.5.12: header_row 内只放名称+chips；复选框剥离到 VCard 左侧独立列，
                # 折叠时也能稳定可见，绝不会被 flex-wrap 挤到隐藏位置。
                header_row = {
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap align-center px-4 pt-3 pb-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "class": "text-subtitle-2 mr-3",
                                "style": "flex: 1 1 auto; min-width: 0; user-select: text; cursor: text; word-break: break-all;",
                            },
                            "text": name_text,
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
                    ] + (
                        [{
                            "component": "VChip",
                            "props": {"size": "x-small", "color": "secondary", "variant": "tonal", "class": "ml-2"},
                            "text": it.get("state") or "",
                        }] if it.get("state") else []
                    ) + (
                        [{
                            "component": "VChip",
                            "props": {"size": "x-small", "color": "info", "variant": "tonal", "class": "ml-2"},
                            "text": "⏱ " + self._fmt_duration(it.get("max_seeding_time", 0)),
                        }] if (it.get("max_seeding_time") or 0) > 0 else []
                    ) + (
                        [{
                            "component": "VChip",
                            "props": {"size": "x-small", "color": "success", "variant": "tonal", "class": "ml-2"},
                            "text": "↑ " + self._fmt_size(it.get("total_uploaded", 0)),
                        }] if (it.get("total_uploaded") or 0) > 0 else []
                    ),
                }
                torrent_rows = [_torrent_row(t, show_delete) for t in torrents]
                expand_content = torrent_rows or [
                    {
                        "component": "div",
                        "props": {"class": "text-caption text-disabled"},
                        "text": "无种子明细",
                    }
                ]
                expansion_panel = {
                    "component": "VExpansionPanels",
                    "props": {"variant": "accordion", "flat": True, "class": "cross-seed-inner-panels"},
                    "content": [
                        {
                            "component": "VExpansionPanel",
                            "content": [
                                {
                                    "component": "VExpansionPanelTitle",
                                    "props": {"class": "py-1 text-caption text-medium-emphasis", "static": False},
                                    "content": [
                                        {
                                            "component": "span",
                                            "text": f"点击展开 {int(it['count']) or len(torrents)} 个副本明细",
                                        }
                                    ],
                                },
                                {
                                    "component": "VExpansionPanelText",
                                    "content": expand_content,
                                },
                            ],
                        }
                    ],
                }
                group_card_props = {"variant": "outlined", "class": "mb-2"}
                if show_delete and group_pairs:
                    if sel_count == len(group_pairs) and sel_count > 0:
                        # 全选：深蓝色边框 + 中等蓝底
                        group_card_props["style"] = (
                            "background-color: rgba(33,150,243,0.10); "
                            "border-color: #1976d2; border-width: 2px;"
                        )
                    elif sel_count > 0:
                        # 部分选中：浅蓝底 + 蓝色边框
                        group_card_props["style"] = (
                            "background-color: rgba(33,150,243,0.05); "
                            "border-color: #64b5f6;"
                        )
                # v0.5.12: VCard 内部横向双列
                #   左列：复选框（固定 44px 宽，flex 不压缩，折叠时也稳定可见）
                #   右列：header_row + expansion_panel（原内容）
                if group_checkbox_btn:
                    card_body = {
                        "component": "div",
                        "props": {"class": "d-flex align-start", "style": "width: 100%;"},
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "class": "d-flex align-center justify-center pl-2 pt-3",
                                    "style": "flex: 0 0 44px; min-width: 44px;",
                                },
                                "content": group_checkbox_btn,
                            },
                            {
                                "component": "div",
                                "props": {"class": "flex-grow-1", "style": "min-width: 0;"},
                                "content": [header_row, expansion_panel],
                            },
                        ],
                    }
                    group_cards.append(
                        {
                            "component": "VCard",
                            "props": group_card_props,
                            "content": [card_body],
                        }
                    )
                else:
                    group_cards.append(
                        {
                            "component": "VCard",
                            "props": group_card_props,
                            "content": [header_row, expansion_panel],
                        }
                    )
            card_list_content.extend(group_cards)
            # 分页控件：仅当筛选后总页数 > 1 时显示
            if total_pages > 1:
                # 上一页 / 下一页按钮的 params，直接把目标页码写在静态 payload 里
                prev_page = max(1, page - 1)
                next_page = min(total_pages, page + 1)
                pagination_row = {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-center flex-wrap ga-2 my-3"},
                    "content": [
                        {
                            "component": "VBtn",
                            "props": {
                                "prependIcon": "mdi-chevron-left",
                                "variant": "tonal",
                                "size": "small",
                                "disabled": page <= 1,
                            },
                            "text": "上一页",
                            "events": {
                                "click": {
                                    "api": save_api,
                                    "method": "post",
                                    "params": {"page": prev_page},
                                }
                            },
                        },
                        {
                            "component": "VChip",
                            "props": {"color": "primary", "size": "small", "variant": "tonal"},
                            "text": f"第 {page} / {total_pages} 页 · 共 {total_items} 组",
                        },
                        {
                            "component": "VBtn",
                            "props": {
                                "appendIcon": "mdi-chevron-right",
                                "variant": "tonal",
                                "size": "small",
                                "disabled": page >= total_pages,
                            },
                            "text": "下一页",
                            "events": {
                                "click": {
                                    "api": save_api,
                                    "method": "post",
                                    "params": {"page": next_page},
                                }
                            },
                        },
                    ],
                }
                # 首页 / 末页快捷（仅在页数较多时提示）
                if total_pages > 2:
                    pagination_row["content"].insert(
                        0,
                        {
                            "component": "VBtn",
                            "props": {
                                "prependIcon": "mdi-page-first",
                                "variant": "text",
                                "size": "small",
                                "disabled": page <= 1,
                            },
                            "text": "首页",
                            "events": {
                                "click": {
                                    "api": save_api,
                                    "method": "post",
                                    "params": {"page": 1},
                                }
                            },
                        },
                    )
                    pagination_row["content"].append(
                        {
                            "component": "VBtn",
                            "props": {
                                "appendIcon": "mdi-page-last",
                                "variant": "text",
                                "size": "small",
                                "disabled": page >= total_pages,
                            },
                            "text": "末页",
                            "events": {
                                "click": {
                                    "api": save_api,
                                    "method": "post",
                                    "params": {"page": total_pages},
                                }
                            },
                        }
                    )
                card_list_content.append(pagination_row)
            if not self._allow_delete:
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
                                            "props": {"class": "mb-2"},
                                            "content": [
                                                {"component": "strong", "text": "分页"},
                                                {"component": "span", "text": "：分组较多时按每页 50 组分页展示。翻页保留当前筛选条件，「全选本页 / 反选本页」只会作用于当前页的分组。"},
                                            ],
                                        },
                                        {
                                            "component": "p",
                                            "props": {"class": "mb-0"},
                                            "content": [
                                                {"component": "strong", "text": "删除按钮"},
                                                {"component": "span", "text": "：默认关闭。到插件设置里开启「允许在详情页删除种子（危险）」后，每张分组卡片上都会出现两个按钮——「仅删种」保留文件仅从下载器移除；"},
                                                {"component": "strong", "props": {"class": "text-error"}, "text": "「删种+文件+清库」"},
                                                {"component": "span", "text": "在下载器删除种子和源文件的同时，会顺带清理媒体库里指向这些源文件的软/硬链接（读 TransferHistory 反向定位），操作不可撤销。"},
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

        # ── 顶部工具栏：状态行（紧凑，取代旧版 VAlert） + 刷新按钮 ──
        info_bits = [f"扫描：{snapshot.get('updated_at') or '尚未扫描'}"]
        dl_list = snapshot.get("downloaders") or []
        if dl_list:
            info_bits.append(f"下载器：{'、'.join(dl_list)}")
        if filter_bits:
            info_bits.append(f"筛选：{'、'.join(filter_bits)}（命中 {len(items)} 组）")
        if snapshot.get("error"):
            info_bits.append(f"⚠ {snapshot['error']}")
        info_line = "｜".join(info_bits)

        # 帮助下拉（取代旧的臃肿 VExpansionPanels）
        help_items: List[dict] = [
            {"component": "VListItem", "props": {"title": "种子（Torrent）：下载器里的一条任务，同一片资源来自不同站点即为不同种子", "density": "compact"}},
            {"component": "VDivider", "props": {"class": "my-1"}},
            {"component": "VListItem", "props": {"title": "分组（Group）：按「种子名+大小」聚合，同组种子即同资源在不同站的辅种", "density": "compact"}},
            {"component": "VListItem", "props": {"title": "辅种数=1 → 孤种；≥2 → 跨站辅种", "density": "compact"}},
            {"component": "VDivider", "props": {"class": "my-1"}},
            {"component": "VListItem", "props": {"title": "用法：找孤种→仅孤种 / 清冗余→多辅种 / 排大文件→大文件", "density": "compact"}},
            {"component": "VListItem", "props": {"title": "删除默认关闭，需在插件设置中开启", "density": "compact"}},
        ]
        help_tooltip = {
            "component": "VBtn",
            "props": {"icon": "mdi-help-circle-outline", "size": "x-small", "variant": "text", "color": "medium-emphasis", "class": "ml-1"},
            "content": [{"component": "VMenu", "props": {"activator": "parent", "close-on-content-click": True}, "content": [{"component": "VCard", "props": {"maxWidth": 420}, "content": [{"component": "VList", "props": {"density": "compact"}, "content": help_items}]}]}],
        }

        toolbar_row = {
            "component": "VRow",
            "props": {"class": "mb-2 align-center"},
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": True},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "text-caption text-medium-emphasis text-truncate"},
                            "content": [
                                {"component": "span", "text": info_line},
                                help_tooltip,
                            ],
                        }
                    ],
                },
                {
                    "component": "VCol",
                    "props": {"cols": "auto"},
                    "content": [
                        {
                            "component": "VBtn",
                            "props": {"color": "primary", "variant": "tonal", "prepend-icon": "mdi-refresh", "size": "x-small"},
                            "text": "刷新",
                            "events": {"click": {"api": refresh_api, "method": "get"}},
                        }
                    ],
                },
            ],
        }

        return [
            toolbar_row,
            summary_row,
            summary_row_extra,
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
                "max_seeding_time": 0,
                "total_uploaded": 0,
                "states": [],
            }
        )
        total_torrents = 0
        for dl_name, tors in torrents_by_dl.items():
            for t in tors:
                try:
                    name, size, save_path, thash, state, seeding_time, uploaded = self._extract_torrent_meta(t)
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
                    "state": state,
                    "seeding_time": seeding_time,
                    "uploaded": uploaded,
                })
                if seeding_time > entry["max_seeding_time"]:
                    entry["max_seeding_time"] = seeding_time
                entry["total_uploaded"] += uploaded
                if state:
                    entry["states"].append(state)

        groups: List[Dict[str, Any]] = []
        cross_groups = 0
        orphan_groups = 0
        total_uploaded_all = 0
        redundant_bytes = 0  # 冗余占用：辅种组内多余副本占用的空间 = size * (count-1)
        for entry in groups_map.values():
            # 代表状态：取出现次数最多的
            rep_state = ""
            if entry["states"]:
                from collections import Counter as _Counter
                rep_state = _Counter(entry["states"]).most_common(1)[0][0]
            g = {
                "name": entry["name"],
                "size": entry["size"],
                "count": entry["count"],
                "downloaders": sorted(entry["downloaders"]),
                "save_paths": sorted(entry["save_paths"]),
                "torrents": entry["torrents"],
                "max_seeding_time": entry["max_seeding_time"],
                "total_uploaded": entry["total_uploaded"],
                "state": rep_state,
            }
            groups.append(g)
            total_uploaded_all += entry["total_uploaded"]
            if g["count"] >= 2:
                cross_groups += 1
                redundant_bytes += g["size"] * (g["count"] - 1)
            else:
                orphan_groups += 1

        snapshot = {
            "groups": groups,
            "downloaders": downloader_names,
            "total_torrents": total_torrents,
            "total_groups": len(groups),
            "cross_groups": cross_groups,
            "orphan_groups": orphan_groups,
            "redundant_bytes": redundant_bytes,
            "total_uploaded": total_uploaded_all,
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
    def _extract_torrent_meta(t: Any) -> Tuple[str, int, str, str, str, int, int]:
        """从下载器返回的种子对象中提取 (name, size, save_path, hash, state, seeding_time_sec, uploaded_bytes)。
        兼容 qb/tr 两种对象。"""
        name = ""
        size = 0
        save_path = ""
        thash = ""
        state = ""
        seeding_time = 0
        uploaded = 0

        def _get(obj: Any, key: str) -> Any:
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        for attr in ("name",):
            v = _get(t, attr)
            if v:
                name = str(v)
                break

        for attr in ("size", "total_size", "totalSize"):
            v = _get(t, attr)
            if v is not None:
                try:
                    size = int(v)
                    break
                except (TypeError, ValueError):
                    continue

        for attr in ("save_path", "download_dir", "downloadDir", "content_path"):
            v = _get(t, attr)
            if v:
                save_path = str(v)
                break

        for attr in ("hash", "hashString", "hash_string"):
            v = _get(t, attr)
            if v:
                thash = str(v)
                break

        for attr in ("state", "status"):
            v = _get(t, attr)
            if v is not None and v != "":
                state = str(v)
                break

        for attr in ("seeding_time", "seedingTime", "time_active", "timeActive"):
            v = _get(t, attr)
            if v is not None:
                try:
                    seeding_time = int(v)
                    break
                except (TypeError, ValueError):
                    continue

        for attr in ("uploaded", "total_uploaded", "totalUploaded", "uploadedEver"):
            v = _get(t, attr)
            if v is not None:
                try:
                    uploaded = int(v)
                    break
                except (TypeError, ValueError):
                    continue

        return name, size, save_path, thash, state, seeding_time, uploaded

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
    def _fmt_duration(seconds: int) -> str:
        try:
            s = int(seconds)
        except (TypeError, ValueError):
            return "-"
        if s <= 0:
            return "-"
        days, rem = divmod(s, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days > 0:
            return f"{days}d{hours}h"
        if hours > 0:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"

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
