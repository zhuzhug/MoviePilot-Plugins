import io
import os
import re
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, SystemConfigKey

# 插件维护的文件标识
_MARK_BEGIN = "# >>> MoviePilot EmbyExtPlayer BEGIN >>>"
_MARK_END = "# <<< MoviePilot EmbyExtPlayer END <<<"
_ASSET_JS = "embyLaunchPotplayer.js"
_DEPLOY_JS = "embyLaunchPotplayer.js"
_CONTAINER_UI_DIR = "/system/dashboard-ui"
_MODULE_NAME = "embyLaunchPotplayer"

# 允许的播放器按钮键（防止表单传入非法值）
_ALLOWED_PLAYERS = ["Pot", "VLC", "IINA", "NPlayer", "MX", "Infuse", "Stellar", "MPV", "Dandanplay", "Copy"]
_ALLOWED_TYPES = ["Movie", "Episode", "Series", "Season", "Video", "MusicVideo", "Trailer"]


class EmbyExtPlayer(_PluginBase):
    """Emby 调用播放器插件：为 Emby 详情页注入调用本地播放器的按钮。"""

    plugin_name = "Emby调用播放器"
    plugin_desc = (
        "一键给 Emby 详情页加上「调用本地播放器」按钮，支持 strm / 网盘直链。"
        "适配 amilys/embyserver:latest 镜像（原镜像作者 @amilys，感谢）。"
    )
    plugin_icon = "mdi-play-box-multiple"
    plugin_version = "1.0.0"
    plugin_label = "增强"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "embyextplayer_"
    plugin_order = 50
    auth_level = 1

    _enabled: bool = False
    _config_dir: str = ""
    _container_name: str = "emby"
    _players: List[str] = []
    _playable_types: List[str] = []
    _strm_host_rewrite: str = ""
    _watch_ms: int = 700
    _auto_restart: bool = True
    _direct_write: bool = False
    _heal_hours: int = 0
    _container_ui_dir: str = _CONTAINER_UI_DIR

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._config_dir = ""
        self._container_name = "emby"
        self._players = []
        self._playable_types = []
        self._strm_host_rewrite = ""
        self._watch_ms = 700
        self._auto_restart = True
        self._direct_write = False
        self._heal_hours = 0
        self._container_ui_dir = _CONTAINER_UI_DIR
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._config_dir = str(config.get("config_dir") or "").strip()
        self._container_name = str(config.get("container_name") or "emby").strip() or "emby"
        self._players = [p for p in (config.get("players") or []) if p in _ALLOWED_PLAYERS]
        self._playable_types = [t for t in (config.get("playable_types") or []) if t in _ALLOWED_TYPES]
        rewrite = str(config.get("strm_host_rewrite") or "").strip()
        self._strm_host_rewrite = rewrite if rewrite.startswith(("http://", "https://")) else ""
        try:
            self._watch_ms = max(200, min(int(config.get("watch_ms") or 700), 5000))
        except Exception:
            self._watch_ms = 700
        self._auto_restart = bool(config.get("auto_restart", True))
        self._direct_write = bool(config.get("direct_write", False))
        try:
            self._heal_hours = max(0, min(int(config.get("heal_hours") or 0), 168))
        except Exception:
            self._heal_hours = 0
        self._container_ui_dir = str(config.get("container_ui_dir") or _CONTAINER_UI_DIR).strip() or _CONTAINER_UI_DIR

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return [
            {
                "cmd": "/emby_ext_apply",
                "event": EventType.PluginAction,
                "desc": "立即部署外挂播放器按钮到 Emby",
                "category": "Emby调用播放器",
                "data": {"action": "emby_ext_apply"},
            },
            {
                "cmd": "/emby_ext_status",
                "event": EventType.PluginAction,
                "desc": "查看 Emby 外挂播放器部署状态",
                "category": "Emby调用播放器",
                "data": {"action": "emby_ext_status"},
            },
            {
                "cmd": "/emby_ext_restore",
                "event": EventType.PluginAction,
                "desc": "还原 Emby 外挂播放器改动",
                "category": "Emby调用播放器",
                "data": {"action": "emby_ext_restore"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件定时服务，用于容器重建后自动补写。"""
        if not self._enabled or self._heal_hours <= 0:
            return []
        return [
            {
                "id": "EmbyExtPlayerHeal",
                "name": "Emby外挂播放器自愈巡检",
                "trigger": "interval",
                "func": self.heal_task,
                "kwargs": {"hours": self._heal_hours},
            }
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        player_items = [
            {"title": "PotPlayer", "value": "Pot"},
            {"title": "VLC", "value": "VLC"},
            {"title": "IINA", "value": "IINA"},
            {"title": "NPlayer", "value": "NPlayer"},
            {"title": "MXPlayer", "value": "MX"},
            {"title": "Infuse", "value": "Infuse"},
            {"title": "恒星播放器", "value": "Stellar"},
            {"title": "MPV", "value": "MPV"},
            {"title": "弹弹Play", "value": "Dandanplay"},
            {"title": "复制链接", "value": "Copy"},
        ]
        type_items = [
            {"title": "电影", "value": "Movie"},
            {"title": "剧集单集", "value": "Episode"},
            {"title": "剧集总览", "value": "Series"},
            {"title": "季", "value": "Season"},
            {"title": "其他视频", "value": "Video"},
            {"title": "音乐视频", "value": "MusicVideo"},
            {"title": "预告片", "value": "Trailer"},
        ]
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
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "hint": "启用后点「立即应用」才会写入 Emby",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "direct_write",
                                            "label": "免重启直写容器",
                                            "hint": "MoviePilot 挂载 docker.sock 时可直接写入，无需重启 Emby",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_restart",
                                            "label": "必要时重启 Emby",
                                            "hint": "关闭直写时，必须重启一次 Emby 才会生效",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "config_dir",
                                            "label": "Emby 安装目录（MoviePilot 可见路径）",
                                            "placeholder": "例：/docker/emby，留空自动探测",
                                            "hint": "必须映射进 MoviePilot 且可读写，目录内应有 config/ 与 data/",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "container_name",
                                            "label": "Emby 容器名",
                                            "hint": "用于自动探测与直写",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "players",
                                            "label": "显示的播放器按钮",
                                            "items": player_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "留空 = 显示全部 10 个按钮",
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "playable_types",
                                            "label": "注入按钮的条目类型",
                                            "items": type_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "留空 = 不做类型过滤",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strm_host_rewrite",
                                            "label": "直链域名重写（可选）",
                                            "placeholder": "例：http://192.168.2.60:9527，留空用 strm 原始地址",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "watch_ms",
                                            "label": "按钮巡检间隔(ms)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "heal_hours",
                                            "label": "自愈巡检(小时,0关闭)",
                                            "type": "number",
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
                            "class": "mt-2",
                            "text": (
                                "前置条件：给 MoviePilot 映射 Emby 安装目录并授予读写权限（目录内含 config/ 与 data/）。"
                                "保存配置后点「立即应用」，插件会写入脚本、修改 ext.sh，并按需重启 Emby。"
                            ),
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "direct_write": False,
            "auto_restart": True,
            "config_dir": "",
            "container_name": "emby",
            "players": [],
            "playable_types": [],
            "strm_host_rewrite": "",
            "watch_ms": 700,
            "heal_hours": 0,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面（展示部署状态）。"""
        if not self._enabled:
            return None
        rows = []
        for item in self.build_status():
            rows.append({
                "component": "VAlert",
                "props": {
                    "type": "success" if item["ok"] else "warning",
                    "variant": "tonal",
                    "class": "mb-2",
                    "text": f"{item['name']}：{item['detail']}",
                },
            })
        return rows or None

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        """声明插件首页仪表板小组件。"""
        return [{"key": "default", "name": "Emby外挂播放器"}]

    def get_dashboard(self, key: str = None, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Optional[List[dict]]]]:
        """返回首页仪表板数据与视图。"""
        if not self._enabled:
            return None
        status = self.build_status()
        ok = sum(1 for s in status if s["ok"])
        cols = {"cols": 12, "md": 6, "lg": 4}
        elements = [
            {"component": "VCard", "props": {"variant": "tonal", "color": "primary", "class": "mb-2"},
             "content": [{"component": "VCardText", "props": {"class": "text-center"},
                          "text": f"检查通过 {ok}/{len(status)}"}]},
        ]
        for item in status:
            elements.append({
                "component": "VCard",
                "props": {"variant": "tonal", "color": "success" if item["ok"] else "warning"},
                "content": [{"component": "VCardText", "text": f"{item['name']}：{item['detail']}"}],
            })
        return cols, {"title": "Emby外挂播放器", "refresh": 300}, elements

    # ==================== 事件与定时任务 ====================

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event: Event):
        """处理远程命令触发的动作。"""
        if not event:
            return
        payload = event.event_data or {}
        action = payload.get("action")
        if action == "emby_ext_apply":
            _, message = self.apply_all()
            self.post_message(title="Emby调用播放器", text=message)
        elif action == "emby_ext_status":
            lines = [f"{s['name']}：{s['detail']}" for s in self.build_status()]
            self.post_message(title="Emby调用播放器 状态", text="\n".join(lines))
        elif action == "emby_ext_restore":
            _, message = self.restore_all()
            self.post_message(title="Emby调用播放器 还原", text=message)

    def heal_task(self, hours: int = 6):
        """定时自愈：容器重建或 Emby 升级后自动补写脚本。"""
        if not self._enabled:
            return
        deployed = self.web_deployed_version()
        expect = self.asset_version()
        if deployed and expect and expect in deployed:
            return
        logger.info(f"Emby调用播放器：容器内版本 {deployed}，期望 {expect}，开始自愈")
        self.apply_all()

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None

    # ==================== 路径与状态 ====================

    def ext_sh_candidates(self, root: Path) -> List[Path]:
        """按镜像习惯返回 ext.sh 的可能位置。"""
        return [root / "config" / "ext.sh", root / "ext.sh"]

    def find_ext_sh(self, root: Optional[Path]) -> Optional[Path]:
        """定位实际存在的 ext.sh。"""
        if not root:
            return None
        for candidate in self.ext_sh_candidates(root):
            if candidate.exists():
                return candidate
        return None

    def build_status(self) -> List[Dict[str, Any]]:
        """构建部署状态检查结果。"""
        result = []
        root = self.resolve_config_dir()
        if root:
            result.append({"name": "Emby 目录", "ok": True, "detail": str(root)})
            sh_file = self.find_ext_sh(root)
            if not sh_file:
                result.append({"name": "ext.sh", "ok": False,
                               "detail": "未找到 ext.sh，请确认挂载的是 Emby 安装目录"})
            else:
                patched = _MARK_BEGIN in sh_file.read_text(encoding="utf-8", errors="replace")
                result.append({"name": "ext.sh 钩子", "ok": patched,
                               "detail": f"{sh_file} " + ("已写入" if patched else "未写入")})
                source_ok = (root / _DEPLOY_JS).exists()
                result.append({"name": "持久化脚本", "ok": source_ok,
                               "detail": str(root / _DEPLOY_JS) if source_ok else f"缺少 {root / _DEPLOY_JS}"})
        else:
            result.append({"name": "Emby 目录", "ok": False,
                           "detail": "未找到，请填写 Emby 安装目录或检查映射"})
        server = self.find_emby_server()
        result.append({"name": "Emby API", "ok": bool(server),
                       "detail": server["host"] if server else "未在媒体服务器配置中找到 Emby"})
        if server:
            web_ver = self.web_deployed_version()
            result.append({"name": "容器内生效", "ok": bool(web_ver), "detail": web_ver or "未生效，需重启 Emby"})
        return result

    @staticmethod
    def read_version_of(path: Path) -> Optional[str]:
        """从脚本文件中解析 @version 版本号。"""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        for line in text.splitlines():
            if "@version" in line:
                parts = line.split()
                return parts[2] if len(parts) > 2 else None
        return None

    def asset_path(self) -> Path:
        """获取插件内置脚本文件路径。"""
        return Path(__file__).parent / _ASSET_JS

    def asset_version(self) -> Optional[str]:
        """获取插件内置脚本版本号。"""
        return self.read_version_of(self.asset_path())

    def web_deployed_version(self) -> Optional[str]:
        """检查 Emby Web 上容器内脚本是否已生效及版本。"""
        import requests
        server = self.find_emby_server()
        if not server:
            return None
        try:
            url = f"{server['host'].rstrip('/')}/web/{_MODULE_NAME}.js"
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                return None
            for line in resp.text.splitlines():
                if "@version" in line:
                    parts = line.split()
                    if len(parts) > 2:
                        return f"已生效 {parts[2]}"
        except Exception as e:
            logger.debug(f"Emby调用播放器：检查容器内脚本失败 {e}")
        return None

    # ==================== 环境探测 ====================

    def find_emby_server(self) -> Optional[Dict[str, str]]:
        """从系统媒体服务器配置中找出 Emby 的地址与 API Key。"""
        try:
            servers = self.systemconfig.get(SystemConfigKey.MediaServers) or []
        except Exception as e:
            logger.error(f"Emby调用播放器：读取媒体服务器配置失败 {e}")
            return None
        for item in servers:
            if str(item.get("type") or "").lower() != "emby":
                continue
            if item.get("enabled") is False:
                continue
            conf = item.get("config") or {}
            host = str(conf.get("host") or "").strip()
            apikey = str(conf.get("apikey") or "").strip()
            if host and apikey:
                return {"host": host, "apikey": apikey}
        return None

    def resolve_config_dir(self) -> Optional[Path]:
        """解析 MoviePilot 视角下可读写的 Emby 安装目录。"""
        if self._config_dir:
            path = Path(self._config_dir)
            if self.find_ext_sh(path) or (path / "data").exists():
                return path
            return path if path.exists() else None
        return self.auto_detect_config_dir()

    def auto_detect_config_dir(self) -> Optional[Path]:
        """通过 docker.sock 自动探测并换算 Emby 安装目录在本容器的可见路径。"""
        try:
            import docker
            client = docker.APIClient(base_url="unix://var/run/docker.sock", timeout=10)
        except Exception as e:
            logger.debug(f"Emby调用播放器：docker.sock 不可用 {e}")
            return None
        try:
            target_id = None
            for c in client.containers(all=True):
                if self._container_name in ",".join(c.get("Names") or []):
                    target_id = c["Id"]
                    break
            if not target_id:
                return None
            emby = client.inspect_container(target_id)
            host_config = None
            for mount in emby.get("Mounts") or []:
                if mount.get("Destination") == "/config":
                    host_config = Path(str(mount.get("Source") or ""))
                    break
            if not host_config:
                return None
            for src, dst in self.self_mounts(client):
                try:
                    rel = host_config.relative_to(src)
                except Exception:
                    continue
                candidate = dst / rel
                if candidate.exists():
                    return candidate
            return host_config if host_config.exists() else None
        except Exception as e:
            logger.error(f"Emby调用播放器：自动探测 Emby 目录失败 {e}")
            return None
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def self_mounts(client) -> List[Tuple[Path, Path]]:
        """获取当前容器自身的 (宿主机路径, 容器内路径) 挂载对照表。"""
        pairs: List[Tuple[Path, Path]] = []
        try:
            hostname = os.environ.get("HOSTNAME") or ""
            candidates = []
            for c in client.containers(all=True):
                names = ",".join(c.get("Names") or [])
                if hostname and (c["Id"].startswith(hostname) or hostname in names):
                    candidates.append(c["Id"])
            if not candidates:
                return pairs
            detail = client.inspect_container(candidates[0])
            for mount in detail.get("Mounts") or []:
                pairs.append((Path(str(mount.get("Source") or "")), Path(str(mount.get("Destination") or ""))))
        except Exception as e:
            logger.debug(f"Emby调用播放器：读取自身挂载失败 {e}")
        return pairs

    # ==================== 内容生成 ====================

    def render_js(self) -> str:
        """按当前配置渲染脚本内容（替换内置脚本的配置区块）。"""
        text = self.asset_path().read_text(encoding="utf-8")
        players = "[" + ", ".join(f"'{p}'" for p in self._players) + "]"
        types = "[" + ", ".join(f"'{t}'" for t in self._playable_types) + "]"
        rewrite = self._strm_host_rewrite.replace("'", "")
        block = (
            "    /* ==================== 用户配置 start ==================== */\n"
            "    const Potplayer = 1;\n"
            f"    const WATCH_MS = {self._watch_ms};\n"
            f"    const STRM_HOST_REWRITE = '{rewrite}';\n"
            f"    const PLAYABLE_TYPES = {types};\n"
            f"    const ENABLED_PLAYERS = {players};\n"
            "    /* ==================== 用户配置 end ==================== */"
        )
        start_mark = "    /* ==================== 用户配置 start ==================== */"
        end_mark = "    /* ==================== 用户配置 end ==================== */"
        start = text.find(start_mark)
        end = text.find(end_mark)
        if start < 0 or end < 0:
            raise ValueError("内置脚本缺少配置区块标记，无法渲染")
        return text[:start] + block + text[end + len(end_mark):]

    def render_ext_block(self) -> str:
        """生成 ext.sh 中由插件维护的启动钩子区块（不含前后空行）。"""
        return (
            f"{_MARK_BEGIN}\n"
            "# 由 MoviePilot 插件「Emby调用播放器」自动维护，请勿手动修改本区块\n"
            f"if [ -f /config/{_DEPLOY_JS} ]; then\n"
            f"    cp -f /config/{_DEPLOY_JS} {self._container_ui_dir}/{_DEPLOY_JS}\n"
            f"    if ! grep -q '{_MODULE_NAME}' {self._container_ui_dir}/ext.js 2>/dev/null; then\n"
            f"        sed -i 's/const extmod=\\[/const extmod=[\"{_MODULE_NAME}\",/'"
            f" {self._container_ui_dir}/ext.js 2>/dev/null\n"
            "    fi\n"
            "fi\n"
            f"{_MARK_END}"
        )

    # ==================== ext.sh 维护 ====================

    def patch_ext_sh(self, sh_file: Path) -> Tuple[bool, str]:
        """在 ext.sh 中幂等写入或更新插件维护的启动钩子。"""
        try:
            original = sh_file.read_text(encoding="utf-8", errors="replace") if sh_file.exists() else "#!/bin/sh\n"
        except Exception as e:
            return False, f"读取 ext.sh 失败：{e}"
        block = self.render_ext_block()
        if _MARK_BEGIN in original and _MARK_END in original:
            start = original.find(_MARK_BEGIN)
            end = original.find(_MARK_END) + len(_MARK_END)
            new_text = original[:start] + block + original[end:]
        else:
            backup_dir = sh_file.parent / "backup"
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                name = f"ext.sh.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
                (backup_dir / name).write_text(original, encoding="utf-8")
            except Exception as e:
                logger.warn(f"Emby调用播放器：备份 ext.sh 失败 {e}")
            match = None
            for match in re.finditer(r"^[ \t]*exit[ \t]+0[ \t]*$", original, flags=re.M):
                pass
            if match:
                offset = match.start()
                new_text = original[:offset] + "\n" + block + "\n" + original[offset:]
            else:
                new_text = original.rstrip("\n") + "\n\n" + block + "\n"
        try:
            sh_file.write_text(new_text, encoding="utf-8")
            try:
                sh_file.chmod(0o777)
            except Exception:
                pass
            return True, f"ext.sh 已写入钩子（{sh_file}）"
        except Exception as e:
            return False, f"写入 ext.sh 失败：{e}"

    def remove_ext_block(self, sh_file: Path) -> Tuple[bool, str]:
        """从 ext.sh 中精确移除插件维护的启动钩子区块。"""
        if not sh_file.exists():
            return True, "ext.sh 不存在，无需还原"
        text = sh_file.read_text(encoding="utf-8", errors="replace")
        if _MARK_BEGIN not in text:
            return True, "ext.sh 中没有插件区块"
        pattern = re.compile(r"\n?" + re.escape(_MARK_BEGIN) + r".*?" + re.escape(_MARK_END) + r"\n?", re.S)
        new_text = pattern.sub("", text, count=1)
        try:
            sh_file.write_text(new_text, encoding="utf-8")
            return True, "ext.sh 已移除插件区块"
        except Exception as e:
            return False, f"还原 ext.sh 失败：{e}"

    # ==================== 容器写入与重启 ====================

    def direct_write_container(self) -> Tuple[bool, str]:
        """通过 docker.sock 直接把脚本写入运行中的容器（免重启）。"""
        try:
            import docker
            client = docker.APIClient(base_url="unix://var/run/docker.sock", timeout=20)
        except Exception as e:
            return False, f"docker.sock 不可用：{e}"
        try:
            target = None
            for c in client.containers(all=True):
                if self._container_name in ",".join(c.get("Names") or []):
                    target = c["Id"]
                    break
            if not target:
                return False, f"未找到容器 {self._container_name}"
            payload = self.render_js().encode("utf-8")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(_DEPLOY_JS)
                info.size = len(payload)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(payload))
            client.put_archive(target, self._container_ui_dir, buf.getvalue())
            script = (
                f"if ! grep -q '{_MODULE_NAME}' {self._container_ui_dir}/ext.js 2>/dev/null; then "
                f"sed -i 's/const extmod=\\[/const extmod=[\"{_MODULE_NAME}\",/'"
                f" {self._container_ui_dir}/ext.js; fi"
            )
            exec_id = client.exec_create(container=target, cmd=["sh", "-c", script])
            client.exec_start(exec_id)
            return True, "已直接写入容器，刷新浏览器即可生效"
        except Exception as e:
            return False, f"直接写入容器失败：{e}"
        finally:
            try:
                client.close()
            except Exception:
                pass

    def restart_emby(self) -> Tuple[bool, str]:
        """通过 Emby 接口重启服务使其加载新脚本。"""
        import requests
        server = self.find_emby_server()
        if not server:
            return False, "未找到 Emby 服务器配置，无法自动重启"
        try:
            resp = requests.post(
                f"{server['host'].rstrip('/')}/emby/System/Restart",
                params={"api_key": server["apikey"]},
                timeout=10,
            )
            if resp.status_code in (200, 204):
                return True, "已触发 Emby 重启，约 10 秒后生效"
            return False, f"触发重启失败：HTTP {resp.status_code}"
        except Exception as e:
            return False, f"触发重启失败：{e}"

    # ==================== 主流程 ====================

    def apply_all(self) -> Tuple[bool, str]:
        """执行完整部署：写脚本、打启动钩子、可选直写容器与重启。"""
        root = self.resolve_config_dir()
        if not root:
            return False, "未找到 Emby 安装目录，请先映射并在插件里填写路径"
        messages: List[str] = []
        target_js = root / _DEPLOY_JS
        try:
            target_js.write_text(self.render_js(), encoding="utf-8")
            try:
                target_js.chmod(0o777)
            except Exception:
                pass
            messages.append(f"脚本已写入 {target_js}")
        except Exception as e:
            return False, f"写入脚本失败：{e}"
        sh_file = self.find_ext_sh(root)
        if not sh_file:
            return False, f"未找到 ext.sh，请确认 {root} 是 Emby 安装目录（含 config/ 与 data/）"
        ok, msg = self.patch_ext_sh(sh_file)
        messages.append(msg)
        if not ok:
            return False, "；".join(messages)
        direct_ok = False
        if self._direct_write:
            direct_ok, msg = self.direct_write_container()
            messages.append(msg)
        if self._auto_restart and not direct_ok:
            _, msg = self.restart_emby()
            messages.append(msg)
        text = "\n".join(messages)
        logger.info(f"Emby调用播放器部署完成：{text}")
        return True, text

    def restore_all(self) -> Tuple[bool, str]:
        """还原所有改动：移除启动钩子并删除部署脚本。"""
        root = self.resolve_config_dir()
        if not root:
            return False, "未找到 Emby 安装目录"
        messages: List[str] = []
        sh_file = self.find_ext_sh(root)
        if sh_file:
            _, msg = self.remove_ext_block(sh_file)
            messages.append(msg)
        target_js = root / _DEPLOY_JS
        if target_js.exists():
            try:
                target_js.unlink()
                messages.append("已删除持久化脚本")
            except Exception as e:
                messages.append(f"删除脚本失败：{e}")
        if self._auto_restart:
            _, msg = self.restart_emby()
            messages.append(msg)
        text = "\n".join(messages)
        logger.info(f"Emby调用播放器还原：{text}")
        return True, text
