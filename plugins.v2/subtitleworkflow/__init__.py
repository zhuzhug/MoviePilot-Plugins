from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.plugin import PluginManager
from app.helper.plugin import PluginHelper
from app.log import logger
from app.plugins import _PluginBase


class SubtitleWorkflow(_PluginBase):
    """
    字幕工作流引导插件。

    化繁为简的引导工具：检测并一键安装/配置「海拉鲁字幕大师」与「AI字幕生成(联动版)」，
    把两者的协同链路（在线搜索 -> AI翻译兜底）设置到位，并提供一键验证就绪状态。
    插件本身不含任何翻译或搜索引擎，只做编排与引导。
    """

    plugin_name = "字幕工作流引导"
    plugin_desc = "检测并一键配置「海拉鲁字幕大师」与「AI字幕生成(联动版)」，建立在线搜索+AI翻译协同链路，支持一键验证就绪状态。"
    plugin_icon = ""
    plugin_version = "1.0.0"
    plugin_label = "字幕工具"
    plugin_author = "local"
    plugin_config_prefix = "subtitleworkflow_"
    plugin_order = 100
    auth_level = 1

    # 依赖插件的仓库地址与 ID
    _HAILALU_ID = "SubtitleManualUpload"
    _AISUB_ID = "AutoSubv3"
    _DEP_REPO_URL = "https://github.com/ifsherlock/MoviePilot-Plugins"

    _enabled = False

    # 海拉鲁推荐配置
    _HAILALU_RECOMMEND = {
        "enabled": True,
        "auto_search_on_transfer": True,
        "auto_transfer_subtitle_strategy": "online_then_ai_source",
        "ai_link_enabled": True,
        "auto_skip_chinese_media_on_transfer": True,
        "online_providers": ["subhd", "subtitlecat", "opensubtitles", "assrt", "subdl"],
    }

    def init_plugin(self, config: dict = None) -> None:
        """
        根据插件配置初始化运行状态。
        """
        self._enabled = False
        if config:
            self._enabled = bool(config.get("enabled"))

    def get_state(self) -> bool:
        """
        获取插件启用状态。
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        返回插件远程命令列表。
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        返回插件 API 列表。
        """
        return [
            {
                "path": "/verify",
                "endpoint": self.verify_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "验证字幕工作流各环节就绪状态",
            },
            {
                "path": "/install_subtitle",
                "endpoint": self.install_subtitle,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "安装/更新海拉鲁字幕大师",
            },
            {
                "path": "/install_ai",
                "endpoint": self.install_ai,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "安装/更新AI字幕生成(联动版)",
            },
            {
                "path": "/apply_config",
                "endpoint": self.apply_config,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "应用推荐配置到两个字幕插件",
            },
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        返回插件配置表单与默认配置。
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": "本插件是引导工具，本身不含翻译/搜索引擎，只负责检测、安装并配置「海拉鲁字幕大师」与「AI字幕生成(联动版)」。",
                            "dismissible": False,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "dep_repo_url",
                            "label": "依赖插件仓库地址",
                            "placeholder": "https://github.com/ifsherlock/MoviePilot-Plugins",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "dep_repo_url": self._DEP_REPO_URL,
        }

    def get_page(self) -> Optional[List[dict]]:
        """
        返回插件详情页面：显示两个字幕插件的安装/配置状态与一键验证入口。
        """
        if not self._enabled:
            return None
        return [
            {
                "component": "VCard",
                "props": {
                    "title": "字幕工作流引导",
                },
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": "点击「验证就绪状态」检查两个字幕插件的安装、启用与配置；未就绪项可按提示一键安装或应用推荐配置。",
                            "dismissible": False,
                        },
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "model": "verify",
                            "color": "primary",
                            "text": "验证就绪状态",
                            "events": {
                                "click": {
                                    "api": f"plugin/SubtitleWorkflow/verify?apikey={settings.API_TOKEN}",
                                    "method": "get",
                                }
                            },
                        },
                    },
                ],
            }
        ]

    def stop_service(self) -> None:
        """
        停止插件后台服务并释放资源。
        """
        return None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _pm(self) -> PluginManager:
        """
        获取插件管理器单例。
        """
        return PluginManager()

    def _get_config(self, pid: str) -> dict:
        """
        读取指定插件的当前配置。
        """
        return self._pm().get_plugin_config(pid) or {}

    def _get_running(self, pid: str) -> bool:
        """
        判断插件是否处于运行态。
        """
        return pid in self._pm().get_running_plugin_ids()

    def _get_state(self, pid: str) -> bool:
        """
        判断插件是否启用。
        """
        return bool(self._pm().get_plugin_state(pid))

    def _get_repo_url(self) -> str:
        """
        读取依赖插件仓库地址（支持配置覆盖）。
        """
        try:
            conf = self._pm().get_plugin_config(self.__class__.__name__) or {}
            return str(conf.get("dep_repo_url") or self._DEP_REPO_URL)
        except Exception:
            return self._DEP_REPO_URL

    def _read_system_llm(self) -> Dict[str, str]:
        """
        读取系统大模型配置（API 地址/密钥/模型），用于填入AI字幕联动版。
        """
        return {
            "url": settings.LLM_BASE_URL or "",
            "key": settings.LLM_API_KEY or "",
            "model": settings.LLM_MODEL or "",
        }

    # ------------------------------------------------------------------
    # 验证就绪状态
    # ------------------------------------------------------------------

    def _hailalu_items(self) -> List[Dict[str, Any]]:
        """
        生成海拉鲁字幕大师的就绪项列表。
        """
        conf = self._get_config(self._HAILALU_ID)
        running = self._get_running(self._HAILALU_ID)
        state = self._get_state(self._HAILALU_ID)
        providers = conf.get("online_providers") or []
        strategy = conf.get("auto_transfer_subtitle_strategy") or ""
        ai_link = conf.get("ai_link_enabled")
        auto_search = conf.get("auto_search_on_transfer")
        # 无需 API Key 的源视为可用
        key_free_ok = any(p in providers for p in ("subhd", "subtitlecat"))
        return [
            {
                "key": "hailalu_installed",
                "label": "海拉鲁字幕大师已安装",
                "ok": running or state,
            },
            {
                "key": "hailalu_enabled",
                "label": "海拉鲁字幕大师已启用",
                "ok": state,
            },
            {
                "key": "hailalu_auto_search",
                "label": "入库自动搜索已开启",
                "ok": bool(auto_search),
            },
            {
                "key": "hailalu_strategy",
                "label": "字幕策略为「在线优先，AI兜底」",
                "ok": strategy == "online_then_ai_source",
            },
            {
                "key": "hailalu_ai_link",
                "label": "AI联动已开启",
                "ok": bool(ai_link),
            },
            {
                "key": "hailalu_providers",
                "label": "已配置免Key在线字幕源",
                "ok": key_free_ok,
            },
        ]

    def _ai_items(self) -> List[Dict[str, Any]]:
        """
        生成AI字幕联动版的就绪项列表。
        """
        conf = self._get_config(self._AISUB_ID)
        running = self._get_running(self._AISUB_ID)
        state = self._get_state(self._AISUB_ID)
        listen = conf.get("listen_transfer_event")
        translate = conf.get("translate_zh")
        asr = conf.get("enable_asr")
        api_key = conf.get("openai_key") or ""
        api_url = conf.get("openai_url") or ""
        return [
            {
                "key": "ai_installed",
                "label": "AI字幕生成(联动版)已安装",
                "ok": running or state,
            },
            {
                "key": "ai_enabled",
                "label": "AI字幕生成(联动版)已启用",
                "ok": state,
            },
            {
                "key": "ai_listen",
                "label": "入库监听已开启",
                "ok": bool(listen),
            },
            {
                "key": "ai_translate",
                "label": "翻译为中文已开启",
                "ok": bool(translate),
            },
            {
                "key": "ai_asr",
                "label": "Whisper语音识别已开启",
                "ok": bool(asr),
            },
            {
                "key": "ai_api",
                "label": "AI翻译API已配置",
                "ok": bool(api_key and api_url),
            },
        ]

    def verify_status(self) -> Dict[str, Any]:
        """
        验证字幕工作流各环节就绪状态。
        """
        items = self._hailalu_items() + self._ai_items()
        ok_count = sum(1 for it in items if it["ok"])
        ready = ok_count == len(items)
        return {
            "success": True,
            "data": {
                "ready": ready,
                "ok_count": ok_count,
                "total": len(items),
                "items": items,
            },
        }

    # ------------------------------------------------------------------
    # 安装 / 配置
    # ------------------------------------------------------------------

    def install_subtitle(self) -> Dict[str, Any]:
        """
        安装/更新海拉鲁字幕大师。
        """
        repo_url = self._get_repo_url()
        try:
            state, msg = PluginHelper().install(
                pid=self._HAILALU_ID, repo_url=repo_url, force_install=True
            )
            if state:
                return {"success": True, "message": "海拉鲁字幕大师安装成功"}
            return {"success": False, "message": f"安装失败：{msg}"}
        except Exception as e:
            logger.error(f"[字幕工作流引导] 安装海拉鲁失败：{e}")
            return {"success": False, "message": f"安装异常：{e}"}

    def install_ai(self) -> Dict[str, Any]:
        """
        安装/更新AI字幕生成(联动版)。
        """
        repo_url = self._get_repo_url()
        try:
            state, msg = PluginHelper().install(
                pid=self._AISUB_ID, repo_url=repo_url, force_install=True
            )
            if state:
                return {"success": True, "message": "AI字幕生成(联动版)安装成功"}
            return {"success": False, "message": f"安装失败：{msg}"}
        except Exception as e:
            logger.error(f"[字幕工作流引导] 安装AI联动版失败：{e}")
            return {"success": False, "message": f"安装异常：{e}"}

    def apply_config(self) -> Dict[str, Any]:
        """
        应用推荐配置到两个字幕插件。

        海拉鲁：开启入库自动搜索、在线优先AI兜底策略、AI联动、免Key在线源。
        AI联动版：开启入库监听、翻译为中文、ASR，并复用系统大模型配置。
        """
        pm = self._pm()
        results: List[Dict[str, Any]] = []

        # 海拉鲁：合并推荐配置到现有配置
        hailalu_conf = self._get_config(self._HAILALU_ID)
        new_hailalu = {**hailalu_conf, **self._HAILALU_RECOMMEND}
        saved_h = pm.save_plugin_config(self._HAILALU_ID, new_hailalu, force=True)
        results.append({"plugin": "海拉鲁字幕大师", "saved": saved_h})

        # AI联动版：合并推荐配置 + 系统大模型
        ai_conf = self._get_config(self._AISUB_ID)
        llm = self._read_system_llm()
        new_ai = {
            **ai_conf,
            "enabled": True,
            "listen_transfer_event": True,
            "translate_zh": True,
            "enable_asr": True,
            "send_notify": ai_conf.get("send_notify", True),
        }
        if llm.get("url"):
            new_ai["openai_url"] = llm["url"]
        if llm.get("key"):
            new_ai["openai_key"] = llm["key"]
        if llm.get("model"):
            new_ai["openai_model"] = llm["model"]
        saved_a = pm.save_plugin_config(self._AISUB_ID, new_ai, force=True)
        results.append({"plugin": "AI字幕生成(联动版)", "saved": saved_a})

        # 尝试重载两个插件使配置生效
        reload_notes: List[str] = []
        for pid, name in ((self._HAILALU_ID, "海拉鲁"), (self._AISUB_ID, "AI联动版")):
            try:
                pm.reload_plugin(pid)
                reload_notes.append(f"{name}已重载")
            except Exception as e:
                reload_notes.append(f"{name}重载失败：{e}")

        return {
            "success": True,
            "message": "推荐配置已应用，请点击「验证就绪状态」确认",
            "data": {
                "results": results,
                "llm_model": llm.get("model") or "（未读取到系统大模型）",
                "reload_notes": reload_notes,
            },
        }
