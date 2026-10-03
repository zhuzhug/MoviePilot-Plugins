"""
AIPair（AI双引擎识别）插件

整合「AI识别增强」与「AI识别词」两个插件的互补能力，单事件入口、单写入方：
1. 原生识别失败 → AI 结构化兜底识别（救当次），注入当前识别事件让整理能过；
2. 兜底成功后 → 沉淀窄作用域识别词（救以后），同类文件以后原生就能认出；
3. 兜底失败 → 记录失败样本，支持人工生成识别词建议并写入。

核心设计原则：识别词写入前全量快照比对、只增不删、写后逐行校验，杜绝清空用户自带识别词的风险。
识别词只有本插件一个写入方，不存在并发覆盖风险。
"""

import hmac
import asyncio
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.chain.media import MediaChain
from app.core.config import settings
from app.core.event import eventmanager
from app.core.meta.words import WordsMatcher
from app.core.metainfo import MetaInfo, clear_rust_parse_options_cache
from app.db.systemconfig_oper import SystemConfigOper
try:
    from app.helper.llm import LLMHelper
except ImportError:
    from app.agent.llm import LLMHelper
from app.log import logger
from app.plugins import _PluginBase
from app.utils import rust_accel
from app.schemas.types import ChainEventType, MediaType, SystemConfigKey


# ==================== 数据模型 ====================

class AIRecognitionGuess(BaseModel):
    """大模型结构化猜测结果。"""
    name: str = Field(default="", description="标准化后的影视标题；无法判断时返回空字符串")
    year: str = Field(default="", description="四位年份；无法判断时返回空字符串")
    media_type: str = Field(default="unknown", description="movie、tv 或 unknown")
    season: int = Field(default=0, description="剧集季号，电影填 0")
    episode: int = Field(default=0, description="剧集集号，电影或未知填 0")
    confidence: float = Field(default=0.0, description="0 到 1 之间的置信度")
    reason: str = Field(default="", description="简短说明为什么这样判断")


class IdentifierSuggestion(BaseModel):
    """识别词建议候选规则。"""
    comment: str = Field(default="", description="可选注释，不带 #")
    rule: str = Field(default="", description="一条 MoviePilot 自定义识别词规则")
    confidence: float = Field(default=0.0, description="0 到 1 之间的置信度")
    reason: str = Field(default="", description="为什么建议这条规则")


class IdentifierSuggestionBundle(BaseModel):
    """识别词建议规则集合。"""
    summary: str = Field(default="", description="整体建议摘要")
    suggestions: List[IdentifierSuggestion] = Field(default_factory=list, description="建议规则列表")


# ==================== 插件主体 ====================

class AIPair(_PluginBase):
    """AI双引擎识别插件：识别兜底 + 识别词沉淀，互补一体化。"""

    plugin_name = "AI双引擎识别"
    plugin_desc = "整合 AI 识别增强与 AI 识别词：原生识别失败时做结构化兜底（救当次），成功后沉淀窄作用域识别词（救以后）。识别词写入走全量快照比对、只增不删、写后逐行校验，杜绝清空用户识别词。致谢 liuyuexi1987 的开源识别增强实现。"
    plugin_icon = "mdi-robot-outline"
    plugin_version = "1.0.2"
    plugin_label = "识别,增强"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "aipair_"
    plugin_order = 41
    auth_level = 1

    # 运行状态
    _enabled: bool = False
    _debug: bool = False
    # 识别增强部分
    _confidence_threshold: float = 0.65
    _request_timeout: int = 25
    _max_retries: int = 2
    _save_failed_samples: bool = True
    _save_title_only_samples: bool = False
    _max_failed_samples: int = 200
    _auto_remove_applied_sample: bool = True
    _clear_failed_samples_once: bool = False
    # 识别词部分（唯一写入方）
    _write_identifier: bool = True
    _write_min_confidence: float = 0.8
    _cooldown_hours: int = 24
    _max_rule_lines: int = 5
    _max_rule_length: int = 200
    _review_mode: bool = False
    _notify_on_write: bool = False

    # 识别词写入锁（本插件是唯一写入方，单锁足够）
    _identifier_lock = threading.Lock()
    # 冷却记录：文件名+规则 -> 上次处理时间戳
    _cooldown_records: Dict[str, float] = {}
    # 已成功沉淀识别词的标题键（title|year|season），用于同名去重跳过重复 LLM 生成
    _settled_titles: set = set()
    # LLM 调用审计计数（_recognize 兜底 / _invoke_identifier_llm 沉淀），监控 token 消耗去向
    _llm_usage: Dict[str, int] = {}
    # 插件归属标记前缀，必须作为独立注释行写入，禁止拼接在规则行上
    _AI_MARK = "# [AI双引擎] "
    _systemconfig: Optional[SystemConfigOper] = None

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._debug = bool(config.get("debug", False))
        self._confidence_threshold = self._safe_float(config.get("confidence_threshold"), 0.65)
        self._request_timeout = self._safe_int(config.get("request_timeout"), 25)
        self._max_retries = max(1, min(5, self._safe_int(config.get("max_retries"), 2)))
        self._save_failed_samples = bool(config.get("save_failed_samples", True))
        self._save_title_only_samples = bool(config.get("save_title_only_samples", False))
        self._max_failed_samples = max(20, min(1000, self._safe_int(config.get("max_failed_samples"), 200)))
        self._auto_remove_applied_sample = bool(config.get("auto_remove_applied_sample", True))
        self._clear_failed_samples_once = bool(config.get("clear_failed_samples_once", False))
        self._write_identifier = bool(config.get("write_identifier", True))
        self._write_min_confidence = self._safe_float(config.get("write_min_confidence"), 0.8)
        self._cooldown_hours = int(config.get("cooldown_hours") or 24)
        self._max_rule_lines = int(config.get("max_rule_lines") or 5)
        self._max_rule_length = int(config.get("max_rule_length") or 200)
        self._review_mode = bool(config.get("review_mode", False))
        self._notify_on_write = bool(config.get("notify_on_write", False))
        self._systemconfig = SystemConfigOper()
        self._load_cooldown_records()
        self._load_settled_titles()
        self._load_llm_usage()
        self._register_events()
        if self._clear_failed_samples_once:
            cleared = self._clear_failed_samples()
            self._clear_failed_samples_once = False
            self._persist_config({"clear_failed_samples_once": False})
            logger.info(f"[AI双引擎] 已按配置清空失败样本 {cleared} 条")
        if self._enabled:
            logger.info(f"[AI双引擎] 插件已启用（调试={self._debug}，写识别词={self._write_identifier}）")

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        try:
            eventmanager.unregister(self.on_chain_name_recognize)
        except Exception:
            pass
        self._save_cooldown_records()

    # ==================== 通用工具 ====================

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        """安全取整数。"""
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        """安全取浮点数。"""
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _extract_apikey(request: Request, body: Optional[Dict[str, Any]] = None) -> str:
        """从请求中提取 API Token。"""
        header = str(request.headers.get("Authorization") or "").strip()
        if header.lower().startswith("bearer "):
            return header.split(" ", 1)[1].strip()
        if body:
            for key in ("apikey", "api_key", "token"):
                token = str(body.get(key) or "").strip()
                if token:
                    return token
        return str(request.query_params.get("apikey") or request.query_params.get("token") or "").strip()

    def _check_api_access(self, request: Request, body: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """校验 API 访问权限。"""
        expected = str(getattr(settings, "API_TOKEN", "") or "").strip()
        if not expected:
            return False, "服务端未配置 API Token"
        actual = self._extract_apikey(request, body)
        if not hmac.compare_digest(actual, expected):
            return False, "API Token 无效"
        return True, ""

    @staticmethod
    def _extract_title_path(event_data: Any) -> Tuple[str, str]:
        """从事件数据中提取标题和路径。"""
        title = ""
        path = ""
        if isinstance(event_data, dict):
            title = event_data.get("title") or event_data.get("name") or event_data.get("org_string") or ""
            path = event_data.get("path") or event_data.get("file_path") or event_data.get("org_string") or ""
        else:
            title = getattr(event_data, "title", "") or getattr(event_data, "name", "") or getattr(event_data, "org_string", "") or ""
            path = getattr(event_data, "path", "") or getattr(event_data, "file_path", "") or getattr(event_data, "org_string", "") or ""
        return str(title or "").strip(), str(path or "").strip()

    @staticmethod
    def _normalize_media_type(value: Any) -> str:
        """规范化媒体类型。"""
        if value == MediaType.MOVIE:
            return "movie"
        if value == MediaType.TV:
            return "tv"
        text = str(value or "").strip().lower()
        if text in {"movie", "movies", "电影"}:
            return "movie"
        if text in {"tv", "电视剧", "剧集"}:
            return "tv"
        return "unknown"

    @staticmethod
    def _clean_guess_name(name: str) -> str:
        """清理猜测标题中的噪点。"""
        text = str(name or "").strip()
        if not text:
            return ""
        text = text.split("/")[0].strip().replace(".", " ")
        return " ".join(text.split())

    def _normalize_guess(self, guess: AIRecognitionGuess) -> AIRecognitionGuess:
        """规范化猜测结果。"""
        name = self._clean_guess_name(guess.name)
        year = str(guess.year or "").strip()
        if not (len(year) == 4 and year.isdigit()):
            year = ""
        media_type = str(guess.media_type or "unknown").strip().lower()
        if media_type not in {"movie", "tv"}:
            media_type = "unknown"
        season = max(0, self._safe_int(guess.season, 0))
        episode = max(0, self._safe_int(guess.episode, 0))
        confidence = min(1.0, max(0.0, self._safe_float(guess.confidence, 0.0)))
        reason = str(guess.reason or "").strip()
        return AIRecognitionGuess(
            name=name, year=year, media_type=media_type,
            season=season, episode=episode, confidence=confidence, reason=reason,
        )

    @staticmethod
    def _build_meta_hint(raw_text: str) -> Dict[str, Any]:
        """构建 MoviePilot 基础解析提示。"""
        try:
            meta = MetaInfo(raw_text)
        except Exception:
            return {}
        return {
            "name": getattr(meta, "name", "") or "",
            "year": getattr(meta, "year", "") or "",
            "type": getattr(getattr(meta, "type", None), "to_agent", lambda: None)() or "",
            "season": getattr(meta, "begin_season", None) or 0,
            "episode": getattr(meta, "begin_episode", None) or 0,
            "org_string": getattr(meta, "org_string", "") or "",
        }

    @staticmethod
    def _run_async_compatible(value: Any) -> Any:
        """兼容 LLM 异步返回。"""
        if asyncio.iscoroutine(value):
            async def _worker():
                return await value
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, _worker()).result()
        return value

    def _get_llm(self):
        """获取 LLM 实例。"""
        return self._run_async_compatible(LLMHelper.get_llm(streaming=False))

    @staticmethod
    def _extract_json_text(content: Any) -> str:
        """从模型响应中提取 JSON 文本，兼容 markdown 代码块包裹。"""
        try:
            text = LLMHelper.extract_text_content(content, fallback_to_string=True)
        except Exception:
            text = str(content or "")
        text = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        return text

    @staticmethod
    def _parse_json_model(model_cls: Any, content: Any) -> Any:
        """把模型输出解析成 Pydantic 模型。"""
        text = AIPair._extract_json_text(content)
        if not text:
            raise ValueError("模型未返回任何内容")
        try:
            return model_cls.model_validate_json(text)
        except Exception:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer) if start >= 0 else -1
            if end <= start:
                continue
            try:
                return model_cls.model_validate_json(text[start:end + 1])
            except Exception:
                continue
        raise ValueError(f"模型输出无法解析为 JSON: {text[:200]}")

    def _persist_config(self, overrides: Dict[str, Any]) -> None:
        """持久化当前运行配置。"""
        try:
            config = {
                "enabled": self._enabled,
                "debug": self._debug,
                "confidence_threshold": self._confidence_threshold,
                "request_timeout": self._request_timeout,
                "max_retries": self._max_retries,
                "save_failed_samples": self._save_failed_samples,
                "save_title_only_samples": self._save_title_only_samples,
                "max_failed_samples": self._max_failed_samples,
                "auto_remove_applied_sample": self._auto_remove_applied_sample,
                "clear_failed_samples_once": self._clear_failed_samples_once,
                "write_identifier": self._write_identifier,
                "write_min_confidence": self._write_min_confidence,
                "cooldown_hours": self._cooldown_hours,
                "max_rule_lines": self._max_rule_lines,
                "max_rule_length": self._max_rule_length,
                "review_mode": self._review_mode,
                "notify_on_write": self._notify_on_write,
            }
            config.update(overrides or {})
            self.update_config(config)
        except Exception as exc:
            logger.warning(f"[AI双引擎] 持久化配置失败: {exc}")

    # ==================== 事件注册与处理 ====================

    def _register_events(self) -> None:
        """注册识别事件。"""
        try:
            eventmanager.register(ChainEventType.NameRecognize)(self.on_chain_name_recognize)
            if self._enabled:
                eventmanager.enable_event_handler(self.on_chain_name_recognize)
            else:
                eventmanager.disable_event_handler(self.on_chain_name_recognize)
        except Exception as exc:
            logger.warning(f"[AI双引擎] 注册识别事件失败: {exc}")

    def on_chain_name_recognize(self, event) -> None:
        """识别事件回调：只在原生识别失败时介入。"""
        if not self._enabled:
            return
        event_data = getattr(event, "event_data", None) or {}
        # 已有 mediainfo 或 source_plugin 说明识别成功或已被处理，跳过
        if isinstance(event_data, dict):
            if event_data.get("mediainfo") or event_data.get("media_info"):
                return
            if event_data.get("source_plugin"):
                return
        title, path = self._extract_title_path(event_data)
        if not title and not path:
            return
        # 异步执行，不阻塞原生链路
        threading.Thread(
            target=self._handle_recognition_failure,
            args=(event_data, title, path),
            daemon=True
        ).start()

    # ==================== 核心处理链路 ====================

    def _handle_recognition_failure(self, event_data: Any, title: str, path: str) -> None:
        """识别失败主链路：冷却检查 → AI 兜底 → 注入事件 → 沉淀识别词 → 重新整理。"""
        try:
            sample_key = f"{title}|{path}"
            # 1. 冷却检查
            if self._is_cooled_down(sample_key):
                self._record_abandon("冷却中")
                if self._debug:
                    logger.info(f"[AI双引擎] 样本在冷却期，跳过: {title or path}")
                return

            # 2. AI 兜底识别
            result = self._recognize(title=title, path=path)
            if not result.get("success"):
                self._record_abandon(result.get("message", "识别失败"))
                return
            guess = result.get("guess") or {}
            verified = result.get("verified_media_info") or {}

            # 3. 注入当前识别事件（救当次）
            self._inject_guess(event_data, guess)

            # 4. 沉淀识别词（救以后）
            if self._write_identifier:
                self._settle_identifier(title, path, guess, verified)

            # 5. 重新触发整理（带回识别结果的完整整理）
            transfer_success = self._retrigger_transfer(path, verified, guess)
            if transfer_success:
                self._cleanup_failed_history(path)
                self._remove_failed_sample_by_title_path(title, path)

            # 6. 更新冷却记录
            self._update_cooldown(sample_key)

            # 7. 通知
            if self._notify_on_write:
                self._notify_result(title, path, guess, transfer_success)

        except Exception as exc:
            logger.error(f"[AI双引擎] 处理识别失败时发生异常: {exc}", exc_info=True)
            self._record_abandon("内部异常")

    @staticmethod
    def _inject_guess(event_data: Any, guess: Dict[str, Any]) -> None:
        """把 AI 猜测结果注入识别事件，让 MoviePilot 原生链路继续二次识别。"""
        if not isinstance(event_data, dict):
            return
        if event_data.get("source_plugin"):
            return
        event_data["name"] = guess.get("name", "")
        event_data["year"] = guess.get("year", "")
        event_data["season"] = guess.get("season", 0)
        event_data["episode"] = guess.get("episode", 0)
        event_data["source_plugin"] = "AIPair"
        event_data["confidence"] = guess.get("confidence", 0)
        event_data["reason"] = guess.get("reason", "")

    def _settle_identifier(self, title: str, path: str, guess: Dict[str, Any], verified: Dict[str, Any]) -> None:
        """兜底成功后沉淀识别词规则。"""
        try:
            # A. 同名去重：同一标题键已沉淀过识别词，直接跳过，不重复调 LLM 生成
            settle_key = self._settle_key(verified, guess)
            if self._is_settled(settle_key):
                if self._debug:
                    logger.info(f"[AI双引擎] 标题已沉淀过识别词，跳过: {settle_key}")
                self._record_abandon("已沉淀过")
                return
            suggested = self._suggest_identifiers({
                "title": title, "path": path,
                "desired_name": verified.get("title") or guess.get("name"),
                "desired_year": verified.get("year") or guess.get("year"),
                "desired_media_type": self._normalize_media_type(verified.get("type") or guess.get("media_type")),
                "desired_season": guess.get("season", 0),
                "desired_episode": guess.get("episode", 0),
                "desired_tmdb_id": verified.get("tmdb_id"),
                "auto_write": True,
            })
            if not suggested.get("success"):
                if self._debug:
                    logger.info(f"[AI双引擎] 识别词建议生成失败: {suggested.get('message')}")
                return
            data = suggested.get("data") or {}
            target = data.get("target") or {}
            ok, reason = self._target_ok(target)
            if not ok:
                self._record_abandon(f"目标不明确:{reason}")
                if self._debug:
                    logger.info(f"[AI双引擎] 目标不明确，跳过沉淀: {reason}")
                return
            candidates = [item for item in (data.get("suggestions") or []) if item.get("lines")]
            min_conf = self._write_min_confidence
            strong = [item for item in candidates if self._safe_float(item.get("confidence"), 0.0) >= min_conf]
            chosen = (strong or candidates)[0] if (strong or candidates) else None
            if not chosen:
                self._record_abandon("无可用规则")
                return

            # 宽泛规则拒绝防线：过宽规则直接放弃，不进入沉淀/待确认
            if self._is_rule_too_broad(chosen.get("rule") or "", target):
                self._record_abandon("规则过宽")
                if self._debug:
                    logger.info(f"[AI双引擎] 规则作用域过宽，已拒绝沉淀: {chosen.get('rule')}")
                return

            # 先审后写模式：进待确认队列，不直接写入
            if self._review_mode:
                self._add_pending_rules(title, path, chosen, target)
                self._record_abandon("待确认")
                return

            # 写入
            apply_result = self._append_custom_identifiers(chosen.get("lines") or [])
            added = list(apply_result.get("added") or [])
            if not added:
                self._record_abandon("规则已存在")
                if self._debug:
                    logger.info(f"[AI双引擎] 规则已存在或重复，未写入: {chosen.get('rule')}")
                return

            # 写后回放验证，不命中自动回滚
            verify = self._preview_current_identifiers(title=title, target=target)
            if not (verify.get("applied") and verify.get("matched_target")):
                rolled = self._remove_custom_identifiers(added)
                self._record_abandon("回放失败已回滚")
                logger.warning(f"[AI双引擎] 写入后回放未命中目标，已回滚 {rolled.get('removed_count', 0)} 行: {chosen.get('rule')}")
                return

            if self._notify_on_write:
                self._notify_identifier_written(title, chosen, target, apply_result.get("total_count"))
            # 标记已沉淀，避免后续同名重复调 LLM
            self._mark_settled(settle_key)
            if self._debug:
                logger.info(f"[AI双引擎] 识别词沉淀成功: {title} -> {chosen.get('rule')}")

        except Exception as exc:
            logger.warning(f"[AI双引擎] 沉淀识别词异常: {exc}")
            self._record_abandon("沉淀异常")

    @staticmethod
    def _target_ok(target: Dict[str, Any]) -> Tuple[bool, str]:
        """目标必须足够明确才允许写入识别词。"""
        target = target or {}
        if not str(target.get("name") or "").strip():
            return False, "缺少标题"
        if not str(target.get("year") or "").strip():
            return False, "缺少年份"
        media_type = AIPair._normalize_media_type(target.get("media_type"))
        if media_type == "unknown":
            return False, "类型不明"
        if not AIPair._safe_int(target.get("tmdb_id"), 0):
            return False, "缺少 TMDB ID"
        if media_type == "tv" and not AIPair._safe_int(target.get("season"), 0):
            return False, "剧集缺少季号"
        if media_type == "tv" and not AIPair._safe_int(target.get("episode"), 0):
            return False, "剧集缺少集号"
        return True, ""

    # ==================== AI 兜底识别 ====================

    def _recognize(self, title: str, path: str = "", record_failed_sample: bool = True) -> Dict[str, Any]:
        """AI 结构化兜底识别：LLM 猜测 → TMDB 硬校验。"""
        title = str(title or "").strip()
        path = str(path or "").strip()
        if not title and path:
            title = Path(path).name
        if not title:
            return {"success": False, "message": "标题为空"}
        is_title_only = not path
        try:
            guess = self._invoke_llm(title, path)
        except Exception as exc:
            if record_failed_sample:
                if is_title_only and not self._save_title_only_samples:
                    if self._debug:
                        logger.info(f"[AI双引擎] 跳过保存仅标题 LLM 错误: {title}")
                else:
                    self._record_llm_error(title, path, self._build_meta_hint(path or title), exc)
            return {"success": False, "message": f"LLM 调用失败: {exc}"}

        verified = self._verify_guess(title, path, guess)
        passed = bool(guess.name and guess.confidence >= self._confidence_threshold)
        if not passed and record_failed_sample:
            if is_title_only and not self._save_title_only_samples:
                if self._debug:
                    logger.info(f"[AI双引擎] 跳过保存仅标题样本: {title}")
            else:
                self._record_failed_sample({
                    "title": title,
                    "path": path,
                    "meta_hint": self._build_meta_hint(path or title),
                    "guess": guess.model_dump(),
                    "verified_media_info": self._compact_verified_summary(verified),
                    "reason": "low_confidence_or_empty_name",
                    "sample_source_kind": "path_backed" if path else "title_only",
                })
        return {
            "success": passed,
            "message": "success" if passed else "识别结果置信度不足，已放弃注入",
            "guess": guess.model_dump(),
            "verified_media_info": verified,
        }

    def _invoke_llm(self, title: str, path: str) -> AIRecognitionGuess:
        """调用 LLM 做结构化猜测。"""
        self._record_llm_usage("recognize")
        raw_text = path or title
        meta_hint = self._build_meta_hint(raw_text)
        llm = self._get_llm()
        prompt = self._build_prompt()
        chain = prompt | llm
        last_error: Optional[BaseException] = None
        for _ in range(max(1, self._max_retries)):
            try:
                response = chain.invoke(
                    {"title": title, "path": path, "meta_hint": meta_hint},
                    config={"configurable": {"timeout": self._request_timeout}},
                )
                guess = self._parse_json_model(AIRecognitionGuess, response.content)
                return self._normalize_guess(guess)
            except Exception as exc:
                last_error = exc
        raise last_error or ValueError("LLM 调用失败")

    @staticmethod
    def _build_prompt() -> ChatPromptTemplate:
        """构建识别兜底提示词。"""
        return ChatPromptTemplate.from_messages([
            (
                "system",
                """你是 MoviePilot 的影视文件名识别增强助手。

你的任务不是搜索 TMDB，也不是编造结果，而是根据文件名、路径和已有解析提示，尽量提炼出更适合 MoviePilot 二次识别的结构化信息。

规则：
1. 只依据输入内容推断，不要臆造不存在的信息。
2. 如果不确定，请返回空标题，并把 media_type 设为 unknown，confidence 降低。
3. title/name 只保留作品名，不要包含分辨率、制作组、音频编码、网盘标记等噪音。
4. year 只有在比较确定时才给四位年份。
5. 电影 season/episode 必须为 0。
6. 剧集如果能确定季集就填写，否则保持 0。
7. media_type 只能是 movie、tv、unknown。
8. confidence 范围为 0 到 1。
9. 只输出 JSON，不要使用 markdown 代码块（```json ... ```）包裹。
""",
            ),
            (
                "human",
                """原始标题：
{title}

原始路径：
{path}

MoviePilot 当前基础解析提示：
{meta_hint}
""",
            ),
        ])

    def _verify_guess(self, title: str, path: str, guess: AIRecognitionGuess) -> Optional[Dict[str, Any]]:
        """回查 TMDB 硬校验，未命中返回 None。"""
        if not guess.name:
            return None
        try:
            raw_text = path or title or guess.name
            meta = MetaInfo(raw_text)
            meta.name = guess.name
            meta.year = guess.year or None
            meta.begin_season = guess.season or None
            meta.begin_episode = guess.episode or None
            if guess.media_type == "tv" or meta.begin_season or meta.begin_episode:
                meta.type = MediaType.TV
            elif guess.media_type == "movie":
                meta.type = MediaType.MOVIE
            mediainfo = MediaChain().recognize_media(meta=meta, cache=False)
            if not mediainfo:
                return None
            return mediainfo.to_dict()
        except Exception as exc:
            if self._debug:
                logger.warning(f"[AI双引擎] TMDB 校验失败: {exc}")
            return None

    @staticmethod
    def _compact_verified_summary(verified: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """压缩 TMDB 校验结果摘要。"""
        verified = verified or {}
        return {
            "title": verified.get("title"),
            "year": verified.get("year"),
            "type": verified.get("type"),
            "tmdb_id": verified.get("tmdb_id"),
            "title_year": verified.get("title_year"),
            "season_years": verified.get("season_years"),
            "seasons": verified.get("seasons"),
            "names": (verified.get("names") or [])[:8],
        }

    # ==================== 识别词建议与写入 ====================

    def _suggest_identifiers(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """生成识别词建议：LLM 生成 + 本地回放预演 + 精确规则兜底。"""
        body = dict(body or {})
        title = str(body.get("title") or "").strip()
        path = str(body.get("path") or "").strip()
        if not title and path:
            title = Path(path).name
        if not title:
            return {"success": False, "message": "标题为空"}

        # 复用已有识别结果，避免重复调用 LLM
        result = {
            "success": True,
            "guess": {
                "name": str(body.get("desired_name") or "").strip(),
                "year": str(body.get("desired_year") or "").strip(),
                "media_type": self._normalize_media_type(body.get("desired_media_type")),
                "season": self._safe_int(body.get("desired_season"), 0),
                "episode": self._safe_int(body.get("desired_episode"), 0),
            },
            "verified_media_info": {
                "title": str(body.get("desired_name") or "").strip(),
                "year": str(body.get("desired_year") or "").strip(),
                "type": self._normalize_media_type(body.get("desired_media_type")),
                "tmdb_id": self._safe_int(body.get("desired_tmdb_id"), 0),
            },
        }
        target = self._build_target(body, result=result)
        invoke_error = ""
        try:
            bundle = self._invoke_identifier_llm(title=title, path=path, result=result, target=target)
        except Exception as exc:
            bundle = IdentifierSuggestionBundle(
                summary="识别词建议模型暂不可用，已自动回退到精确规则兜底。",
                suggestions=[],
            )
            invoke_error = str(exc)

        cleaned: List[Dict[str, Any]] = []
        for item in bundle.suggestions:
            rule = self._enrich_identifier_rule(item.rule, target=target)
            if not self._validate_identifier_rule(rule):
                continue
            comment_line = self._clean_comment_line(item.comment)
            preview = self._preview_identifier_rule(title=title, rule=rule, target=target)
            if not preview.get("applied"):
                continue
            if target and any(target.values()) and preview.get("matched_target") is False:
                continue
            cleaned.append({
                "comment": item.comment.strip(),
                "comment_line": comment_line,
                "rule": rule,
                "confidence": min(1.0, max(0.0, self._safe_float(item.confidence, 0.0))),
                "reason": str(item.reason or "").strip(),
                "preview": preview,
                "lines": [line for line in [comment_line, rule] if line],
            })

        if not cleaned:
            fallback = self._build_exact_identifier_fallback(title=title, target=target)
            if fallback:
                if invoke_error:
                    fallback["reason"] = f"{fallback.get('reason', '')} 当前识别词建议模型不可用，已自动切到精确规则兜底。".strip()
                cleaned.append(fallback)

        if not cleaned:
            return {
                "success": False,
                "message": f"识别词建议生成失败: {invoke_error}" if invoke_error else "没有生成可直接使用的识别词规则",
                "data": {"summary": bundle.summary, "target": target, "recognize_result": result},
            }
        return {
            "success": True,
            "message": "success",
            "data": {
                "summary": bundle.summary,
                "target": target,
                "recognize_result": result,
                "suggestions": cleaned,
            },
        }

    def _invoke_identifier_llm(self, title: str, path: str, result: Dict[str, Any], target: Dict[str, Any]) -> IdentifierSuggestionBundle:
        """调用 LLM 生成识别词建议。"""
        self._record_llm_usage("settle")
        llm = self._get_llm()
        prompt = self._build_identifier_prompt()
        chain = prompt | llm
        last_error: Optional[BaseException] = None
        for _ in range(max(1, self._max_retries)):
            try:
                response = chain.invoke(
                    {
                        "title": title,
                        "path": path,
                        "meta_hint": self._build_meta_hint(path or title),
                        "guess": result.get("guess") or {},
                        "verified_summary": self._compact_verified_summary(result.get("verified_media_info")),
                        "target": target,
                    },
                    config={"configurable": {"timeout": self._request_timeout}},
                )
                return self._parse_json_model(IdentifierSuggestionBundle, response.content)
            except Exception as exc:
                last_error = exc
        raise last_error or ValueError("LLM 调用失败")

    @staticmethod
    def _build_identifier_prompt() -> ChatPromptTemplate:
        """构建识别词建议提示词。"""
        return ChatPromptTemplate.from_messages([
            (
                "system",
                """你是 MoviePilot 自定义识别词规则助手。

你的任务是根据错误标题、当前解析结果和目标结果，生成尽量窄作用域、可直接用于 MoviePilot CustomIdentifiers 的规则。

支持格式只有四种：
1. 屏蔽词
2. 替换词：被替换词 => 替换词
3. 集偏移：前定位词 <> 后定位词 >> EP±N
4. 组合规则：被替换词 => 替换词 && 前定位词 <> 后定位词 >> EP±N

硬性要求：
1. 运算符两侧必须保留空格： => 、 <> 、 >> 、 &&
2. 优先生成窄作用域规则，尽量带发布组、年份、季集、分辨率等锚点
3. 不要生成过宽的裸屏蔽词，比如 1080p、WEB-DL、字幕
4. 如果需要强制绑 TMDB，可使用 {{[tmdbid=xxx;type=tv/movies;s=1;e=14]}} 这种替换词
5. comment 不带 #，rule 里不要再包 markdown 或代码块
6. 如果没有把握，请返回空 suggestions
7. 只输出 JSON，不要使用 markdown 代码块（```json ... ```）包裹。
""",
            ),
            (
                "human",
                """原始标题：
{title}

原始路径：
{path}

MoviePilot 当前基础解析：
{meta_hint}

AI 识别增强结果：
{guess}

二次校验到的媒体信息摘要：
{verified_summary}

希望修正成的目标结果：
{target}
""",
            ),
        ])

    def _build_target(self, body: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构建识别词目标结果。"""
        body = body or {}
        result = result or {}
        guess = result.get("guess") or {}
        verified = result.get("verified_media_info") or {}
        verified_type = self._normalize_media_type(verified.get("type"))
        target = {
            "name": str(body.get("desired_name") or verified.get("title") or guess.get("name") or "").strip(),
            "year": str(body.get("desired_year") or verified.get("year") or guess.get("year") or "").strip(),
            "media_type": self._normalize_media_type(
                body.get("desired_media_type") or verified_type or guess.get("media_type")
            ),
            "season": self._safe_int(body.get("desired_season"), self._safe_int(guess.get("season"), 0)),
            "episode": self._safe_int(body.get("desired_episode"), self._safe_int(guess.get("episode"), 0)),
            "tmdb_id": self._safe_int(body.get("desired_tmdb_id") or verified.get("tmdb_id"), 0),
        }
        if len(target["year"]) != 4 or not target["year"].isdigit():
            target["year"] = ""
        return target

    @staticmethod
    def _normalize_identifier_line(value: Any) -> str:
        """规范化识别词行。"""
        return " ".join(str(value or "").strip().split())

    def _validate_identifier_rule(self, rule: str) -> bool:
        """校验识别词规则格式。"""
        rule = self._normalize_identifier_line(rule)
        if not rule or rule.startswith("#"):
            return False
        if " => " in rule and " && " in rule and " >> " in rule and " <> " in rule:
            return True
        if " => " in rule:
            return True
        if " >> " in rule and " <> " in rule:
            return True
        return len(rule) >= 4

    def _is_rule_too_broad(self, rule: str, target: Dict[str, Any]) -> bool:
        """判断识别词规则是否作用域过宽，过宽则拒绝沉淀，防止污染识别词库。

        策略：抽取规则左侧的"匹配部分"（去掉替换目标、扩展段、集数偏移段），
        判断其中是否含"结构化锚点"（年份、季集号、分辨率、发布组、标题分片等）。
        只含孤立短数字/裸格式词的规则视为过宽并拒绝。
        """
        rule = self._normalize_identifier_line(rule)
        if not rule:
            return True
        left = rule
        # 取 ' => ' 之前的匹配段
        if " => " in left:
            left = left.split(" => ", 1)[0]
        # 去掉 ' && ' 扩展段与 ' <> ... >> ' 偏移段
        for sep in (" && ", " >> "):
            if sep in left:
                left = left.split(sep, 1)[0]
        if " <> " in left:
            left = left.split(" <> ", 1)[0]
        left = left.strip()
        if not left:
            return True

        has_anchor = False
        # 1) 结构化季集标记：S01E01 / 第3季 / 第08集
        if re.search(r"S\d{1,2}E\d{1,2}|第[\d一二三四五六七八九十百千]+[季集]", left):
            has_anchor = True
        # 2) 结构化年份（四位数字且非孤立短词）：2024.xxx / xxx(2024)
        year_tokens = re.findall(r"\b\d{4}\b", left)
        if year_tokens and any(len(t) == 4 for t in year_tokens):
            has_anchor = True
        # 3) 分辨率/格式锚点（与其它内容共存，非孤立裸词）
        format_anchors = [
            "1080p", "720p", "2160p", "4k", "uhd", "bluray", "blu-ray",
            "web-dl", "hdtv", "webrip", "x264", "x265", "hevc", "avc",
            "remux", "hdr", "diy", "strm", "dmhy", "chinese", "chs", "cht",
        ]
        if any(anchor in left.lower() for anchor in format_anchors):
            # 要求该格式词不是整个匹配段唯一的裸 token
            bare = left.lower().strip()
            if not (re.fullmatch(anchor, bare) for anchor in format_anchors):
                has_anchor = True
        if not has_anchor:
            return True  # 无任何结构化锚点 → 过宽

        left_lower = left.lower()

        # —— 第一道：匹配段足够具体（含较长语义 token）→ 直接视为窄作用域 ——
        # 排除纯数字、纯季集结构符、裸格式词形态的 token，它们不算"语义锚点"
        _concrete_re = re.compile(
            r"^\d{2,4}$|^s\d{1,2}e\d{1,2}$|^s\d{1,2}$|"
            r"^(?:1080p|720p|2160p|4k|uhd|bluray|blu-ray|web-dl|hdtv|webrip|"
            r"x264|x265|hevc|avc|remux|hdr|diy|strm|dmhy|chinese|chs|cht)$"
        )
        long_tokens = [
            t for t in re.split(r"[\s._\-\[\]（）()【】]+", left_lower)
            if len(t) >= (4 if re.fullmatch(r"[\u4e00-\u9fff]+", t) else 6)
            and not _concrete_re.match(t)
        ]
        if long_tokens:
            return False

        # — 第二道：结构化锚点（季集 / 年份 / 格式词，且非孤立裸 token）——
        purged = left_lower.strip()
        # 纯结构符形态（只有 SxxExx / 纯数字 / 裸格式词）视为过宽
        if re.fullmatch(r"s\d{1,2}e\d{1,2}|\d{2,4}|[a-z0-9.-]+p", purged):
            return True
        # 季集标记 || 四位年份 || 格式词（格式词不与匹配段等长=非孤立）
        has_anchor = bool(
            re.search(r"S\d{1,2}E\d{1,2}|第[\d一二三四五六七八九十百千]+[季集]", left_lower)
            or re.search(r"\b\d{4}\b", left_lower)
        )
        if not has_anchor:
            for anchor in ("1080p", "720p", "2160p", "4k", "uhd", "bluray", "blu-ray",
                           "web-dl", "hdtv", "webrip", "x264", "x265", "hevc", "avc",
                           "remux", "hdr", "diy", "strm", "dmhy", "chinese", "chs", "cht"):
                if anchor in left_lower and not re.fullmatch(anchor, purged):
                    has_anchor = True
                    break
        # 无结构锚点 → 过宽
        if not has_anchor:
            return True

        # 有结构锚点但无长语义 token 且匹配段整体仍很短 → 偏向过宽
        if len(left_lower) <= 6:
            return True
        return False

    def _enrich_identifier_rule(self, rule: str, target: Dict[str, Any]) -> str:
        """补全识别词规则中的目标标题。"""
        rule = self._normalize_identifier_line(rule)
        target_name = str((target or {}).get("name") or "").strip()
        if not target_name or " => " not in rule:
            return rule
        left, right = rule.split(" => ", 1)
        suffix = ""
        replace_part = right
        if " && " in right:
            replace_part, extra = right.split(" && ", 1)
            suffix = f" && {extra}"
        if replace_part.startswith("{["):
            replace_part = f"{target_name}{replace_part}"
        return f"{left} => {replace_part}{suffix}"

    @staticmethod
    def _clean_comment_line(comment: str) -> str:
        """清理注释行。"""
        text = str(comment or "").strip()
        if not text:
            return ""
        return f"#{text.lstrip('#').strip()}"

    def _preview_custom_words(self, title: str, custom_words: List[str], target: Dict[str, Any]) -> Dict[str, Any]:
        """本地回放预演：不落库，验证规则是否命中目标。"""
        prepared_title, apply_words = WordsMatcher().prepare(title, custom_words=custom_words)
        meta = MetaInfo(title=title, custom_words=custom_words)
        preview = {
            "prepared_title": prepared_title,
            "applied_words": apply_words or [],
            "applied": bool(apply_words),
            "name": getattr(meta, "name", "") or "",
            "year": getattr(meta, "year", "") or "",
            "media_type": self._normalize_media_type(getattr(meta, "type", None)),
            "season": getattr(meta, "begin_season", None) or 0,
            "episode": getattr(meta, "begin_episode", None) or 0,
        }
        if target:
            matched = True
            if target.get("name"):
                matched = matched and (preview["name"].strip().lower() == str(target["name"]).strip().lower())
            if target.get("year"):
                matched = matched and (preview["year"] == target["year"])
            if target.get("media_type") and target.get("media_type") != "unknown":
                matched = matched and (preview["media_type"] == target["media_type"])
            if target.get("season"):
                matched = matched and (preview["season"] == target["season"])
            if target.get("episode"):
                matched = matched and (preview["episode"] == target["episode"])
            preview["matched_target"] = matched
        return preview

    def _preview_identifier_rule(self, title: str, rule: str, target: Dict[str, Any]) -> Dict[str, Any]:
        """预探单条规则。"""
        preview = self._preview_custom_words(title=title, custom_words=[rule], target=target)
        preview["applied"] = rule in (preview.get("applied_words") or [])
        return preview

    def _preview_current_identifiers(self, title: str, target: Dict[str, Any]) -> Dict[str, Any]:
        """用当前全部识别词回放预演。"""
        custom_words = self._get_custom_identifiers()
        preview = self._preview_custom_words(title=title, custom_words=custom_words, target=target)
        preview["custom_identifier_count"] = len(custom_words)
        preview["applied_count"] = len(preview.get("applied_words") or [])
        return preview

    def _build_exact_identifier_fallback(self, title: str, target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """精确规则兜底：LLM 建议不可用时，用精确标题绑定 TMDB。"""
        target_name = str((target or {}).get("name") or "").strip()
        tmdb_id = self._safe_int((target or {}).get("tmdb_id"), 0)
        media_type = self._normalize_media_type((target or {}).get("media_type"))
        if not title or not target_name or not tmdb_id or media_type == "unknown":
            return None
        replace = target_name
        target_year = str((target or {}).get("year") or "").strip()
        if len(target_year) == 4 and target_year.isdigit():
            replace += f".{target_year}"
        replace += f"{{[tmdbid={tmdb_id};type={'tv' if media_type == 'tv' else 'movie'}"
        if media_type == "tv" and self._safe_int(target.get("season"), 0):
            replace += f";s={self._safe_int(target.get('season'), 0)}"
        if media_type == "tv" and self._safe_int(target.get("episode"), 0):
            replace += f";e={self._safe_int(target.get('episode'), 0)}"
        replace += "]}"
        rule = f"{re.escape(title)} => {replace}"
        preview = self._preview_identifier_rule(title=title, rule=rule, target=target)
        if not preview.get("applied"):
            return None
        comment = "AI 建议无法稳定通过本地预演时，使用精确标题绑定规则固定到目标 TMDB 与季集"
        return {
            "comment": comment,
            "comment_line": f"#{comment}",
            "rule": rule,
            "confidence": 0.95,
            "reason": "精确匹配当前标题并强制绑定目标 TMDB / 季集，作用域最窄，稳定性最高。",
            "preview": preview,
            "lines": [f"#{comment}", rule],
        }

    def _get_custom_identifiers(self) -> List[str]:
        """获取自定义识别词全量列表。"""
        if not self._systemconfig:
            self._systemconfig = SystemConfigOper()
        return self._systemconfig.get(SystemConfigKey.CustomIdentifiers) or []

    def _append_custom_identifiers(self, lines: List[str]) -> Dict[str, Any]:
        """追加识别词规则，锁内重读合并写回。

        标记只能写成独立的注释行，绝不能拼接到规则行上。
        """
        with self._identifier_lock:
            existing = self._get_custom_identifiers()
            added: List[str] = []
            for line in lines:
                normalized = str(line or "").rstrip()
                if not normalized:
                    continue
                if normalized.startswith(self._AI_MARK):
                    normalized = normalized[len(self._AI_MARK):].lstrip()
                if normalized in existing or normalized in added:
                    continue
                added.extend(self._mark_identifier_line(normalized))
            if added:
                merged = existing + added
                self._systemconfig.set(SystemConfigKey.CustomIdentifiers, merged)
                clear_rust_parse_options_cache()
                # 写后校验：原有规则必须一字未变
                after = self._get_custom_identifiers()
                if len(after) < len(existing):
                    self._systemconfig.set(SystemConfigKey.CustomIdentifiers, existing)
                    clear_rust_parse_options_cache()
                    return {"added": [], "added_count": 0, "total_count": len(existing), "message": "写后校验失败，已回滚"}
                for line in existing:
                    if line not in after:
                        self._systemconfig.set(SystemConfigKey.CustomIdentifiers, existing)
                        clear_rust_parse_options_cache()
                        return {"added": [], "added_count": 0, "total_count": len(existing), "message": "写后校验失败，已回滚"}
            return {
                "added": added,
                "added_count": len(added),
                "total_count": len(self._get_custom_identifiers()),
            }

    def _mark_identifier_line(self, line: str) -> List[str]:
        """生成带插件标记的识别词行列表。"""
        if line.startswith("#"):
            return [line]
        return [f"{self._AI_MARK}{line}", line]

    def _remove_custom_identifiers(self, lines: List[str]) -> Dict[str, Any]:
        """移除指定识别词行，用于写入回放失败后的回滚。"""
        with self._identifier_lock:
            existing = self._get_custom_identifiers()
            targets = {str(line or "").rstrip() for line in lines if str(line or "").strip()}
            kept = [line for line in existing if str(line or "").rstrip() not in targets]
            removed_count = len(existing) - len(kept)
            if removed_count:
                self._systemconfig.set(SystemConfigKey.CustomIdentifiers, kept)
                clear_rust_parse_options_cache()
            return {"removed_count": removed_count, "total_count": len(self._get_custom_identifiers())}

    # ==================== 重新整理 ====================

    def _retrigger_transfer(self, path: str, verified: Dict[str, Any], guess: Dict[str, Any]) -> bool:
        """重新触发整理，带回 AI 识别出的完整媒体身份。"""
        try:
            if not path or not Path(path).exists():
                if self._debug:
                    logger.info(f"[AI双引擎] 路径不存在，跳过重新整理: {path}")
                return False
            from app.chain.transfer import TransferChain
            from app.schemas import FileItem, MediaType as _MT

            fileitem = FileItem(path=path, storage="local")
            tmdb_id = self._safe_int(verified.get("tmdb_id"), 0)
            if not tmdb_id:
                if self._debug:
                    logger.info(f"[AI双引擎] 无 TMDB ID，跳过重新整理: {path}")
                return False
            media_type = self._normalize_media_type(verified.get("type") or guess.get("media_type"))
            mtype = _MT.TV if media_type == "tv" else _MT.MOVIE
            season = self._safe_int(guess.get("season"), 0) or None
            state, message = TransferChain().manual_transfer(
                fileitem=fileitem,
                tmdbid=tmdb_id,
                mtype=mtype,
                season=season,
                reorganize=True
            )
            if state:
                logger.info(f"[AI双引擎] 重新整理成功: {path}")
                return True
            logger.warning(f"[AI双引擎] 重新整理失败: {path} -> {message}")
            return False
        except Exception as exc:
            logger.error(f"[AI双引擎] 重新整理异常: {exc}")
            return False

    def _cleanup_failed_history(self, path: str) -> None:
        """整理成功后删除同源路径下的旧失败记录。"""
        try:
            from app.db import TransferHistoryOper
            transfer_oper = TransferHistoryOper()
            records = transfer_oper.get_by_src(path, "local")
            if records and not records.status:
                transfer_oper.delete(records.id)
                logger.info(f"[AI双引擎] 已删除旧失败记录: {path}")
        except Exception as exc:
            logger.error(f"[AI双引擎] 清理失败记录异常: {exc}")

    @staticmethod
    def _is_trash_sample(title: str, path: str) -> bool:
        """判断样本是否不值得调用 AI 兜底（省 token）。

        只做两类明确安全的判定，避免误伤正常媒体：
        1. 纯字幕/索引类文件（非视频正片，字幕名乱码是常见坑）。
        2. 强乱码文件名（大量非 Latin、非中日韩的可打印符号，如倒L、方框符）。
        """
        import unicodedata
        p = str(path or "").strip()
        ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
        subtitle_exts = {"srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "stl"}
        if ext in subtitle_exts:
            return True

        raw = (title or p if not title else title).strip()
        if not raw:
            return True
        # 计算"乱码符号"占比：非 Latin 字母、非数字、非常见标点、非中日韩的字符
        garbage = 0
        total = 0
        for ch in raw:
            if ch.isspace():
                continue
            total += 1
            try:
                cat = unicodedata.category(ch)
                name = unicodedata.name(ch, "")
            except Exception:
                cat, name = "", ""
            # 判定为正常可读：拉丁字母/数字/中日韩/常见标点
            is_common = (
                "LATIN" in name or "DIGIT" in name
                or "CJK" in name or "FULLWIDTH" in name
                or "HIRAGANA" in name or "KATAKANA" in name
                or ("PUNCTUATION" in name and "BOX" not in name)
                or cat in {"Po", "Sc", "Sm", "Zs"}
            )
            if not is_common:
                garbage += 1
        if total == 0:
            return True
        return (garbage / total) > 0.5

    # ==================== 冷却机制 ====================

    def _is_cooled_down(self, sample_key: str) -> bool:
        """检查样本是否在冷却期。"""
        if sample_key not in self._cooldown_records:
            return False
        last_time = self._cooldown_records[sample_key]
        cooldown_seconds = self._cooldown_hours * 3600
        return (time.time() - last_time) < cooldown_seconds

    def _update_cooldown(self, sample_key: str) -> None:
        """更新冷却记录。"""
        self._cooldown_records[sample_key] = time.time()
        self._save_cooldown_records()

    def _load_cooldown_records(self) -> None:
        """加载冷却记录。"""
        try:
            data = self.get_data("cooldown_records")
            if data and isinstance(data, dict):
                self._cooldown_records = data
            else:
                self._cooldown_records = {}
        except Exception:
            self._cooldown_records = {}

    def _save_cooldown_records(self) -> None:
        """保存冷却记录。"""
        try:
            self.save_data("cooldown_records", self._cooldown_records)
        except Exception as exc:
            logger.error(f"[AI双引擎] 保存冷却记录失败: {exc}")

    # ==================== 失败样本与统计 ====================

    def _sample_path(self) -> Path:
        """失败样本文件路径。"""
        return self.get_data_path() / "failed_samples.jsonl"

    def _llm_errors_path(self) -> Path:
        """LLM 错误诊断文件路径。"""
        return self.get_data_path() / "llm_errors.jsonl"

    def _failed_sample_cap(self) -> int:
        """失败样本保留上限。"""
        return max(20, min(1000, self._safe_int(self._max_failed_samples, 200)))

    @staticmethod
    def _sample_identity(payload: Dict[str, Any]) -> str:
        """样本唯一标识。"""
        return json.dumps(
            {
                "title": str(payload.get("title") or "").strip(),
                "path": str(payload.get("path") or "").strip(),
                "reason": str(payload.get("reason") or "").strip(),
            },
            ensure_ascii=False, sort_keys=True,
        )

    def _write_failed_samples(self, rows: List[Dict[str, Any]]) -> None:
        """写入失败样本文件。"""
        sample_path = self._sample_path()
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        filtered = [row for row in rows if not str(row.get("reason") or "").startswith("llm_error:")]
        trimmed = filtered[-self._failed_sample_cap():]
        with sample_path.open("w", encoding="utf-8") as f:
            for row in trimmed:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _record_failed_sample(self, payload: Dict[str, Any]) -> None:
        """记录失败样本（去重 + 保留上限）。"""
        if not self._save_failed_samples:
            return
        try:
            rows = self._read_failed_samples(limit=1000)
            rows.reverse()
            identity = self._sample_identity(payload)
            filtered = [row for row in rows if self._sample_identity(row) != identity]
            filtered.append(payload)
            self._write_failed_samples(filtered)
        except Exception as exc:
            logger.warning(f"[AI双引擎] 写入失败样本失败: {exc}")

    def _record_llm_error(self, title: str, path: str, meta_hint: Dict[str, Any], error: Any) -> None:
        """记录 LLM 调用错误诊断。"""
        try:
            error_path = self._llm_errors_path()
            error_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "title": title,
                "path": path,
                "meta_hint": meta_hint,
                "reason": f"llm_error:{error}",
                "timestamp": __import__("datetime").datetime.now().isoformat(),
            }
            existing = self._read_llm_errors(limit=1000)
            existing.reverse()
            new_identity = {"title": title, "path": path, "reason": entry["reason"]}
            existing = [
                row for row in existing
                if {"title": row.get("title"), "path": row.get("path"), "reason": row.get("reason")} != new_identity
            ]
            existing.append(entry)
            trimmed = existing[-self._failed_sample_cap():]
            with error_path.open("w", encoding="utf-8") as f:
                for row in trimmed:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(f"[AI双引擎] 写入 LLM 错误诊断记录失败: {exc}")

    def _read_llm_errors(self, limit: int = 20) -> List[Dict[str, Any]]:
        """读取 LLM 错误诊断记录。"""
        error_path = self._llm_errors_path()
        if not error_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with error_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning(f"[AI双引擎] 读取 LLM 错误诊断记录失败: {exc}")
            return []
        if limit > 0:
            rows = rows[-limit:]
        rows.reverse()
        return rows

    def _clear_llm_errors(self) -> int:
        """清空 LLM 错误诊断记录。"""
        rows = self._read_llm_errors(limit=10000)
        error_path = self._llm_errors_path()
        if error_path.exists():
            error_path.unlink()
        return len(rows)

    def _read_failed_samples(self, limit: int = 20) -> List[Dict[str, Any]]:
        """读取失败样本。"""
        sample_path = self._sample_path()
        if not sample_path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with sample_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning(f"[AI双引擎] 读取失败样本失败: {exc}")
            return []
        if limit > 0:
            rows = rows[-limit:]
        rows.reverse()
        return rows

    def _clear_failed_samples(self) -> int:
        """清空失败样本。"""
        rows = self._read_failed_samples(limit=1000)
        sample_path = self._sample_path()
        if sample_path.exists():
            sample_path.unlink()
        return len(rows)

    def _remove_failed_sample(self, sample_index: Optional[Any], limit: int = 1000) -> Dict[str, Any]:
        """按索引移除单条失败样本。"""
        rows = self._read_failed_samples(limit=max(1, min(limit, 1000)))
        if not rows:
            return {"removed": False, "message": "暂无失败样本", "removed_count": 0}
        index = self._safe_int(sample_index, 0)
        if index < 0:
            index = 0
        if index >= len(rows):
            return {"removed": False, "message": f"失败样本索引超出范围，当前共有 {len(rows)} 条", "removed_count": 0}
        removed_sample = dict(rows[index] or {})
        del rows[index]
        if rows:
            rows.reverse()
            self._write_failed_samples(rows)
        else:
            self._clear_failed_samples()
        return {
            "removed": True, "message": "success", "removed_count": 1,
            "remaining_count": len(rows), "removed_sample": removed_sample, "removed_sample_index": index,
        }

    def _remove_failed_samples(self, sample_indexes: List[Any], limit: int = 1000) -> Dict[str, Any]:
        """批量移除失败样本。"""
        rows = self._read_failed_samples(limit=max(1, min(limit, 1000)))
        if not rows:
            return {"removed": False, "message": "暂无失败样本", "removed_count": 0, "remaining_count": 0}
        normalized_indexes = sorted(
            {max(0, min(self._safe_int(i, 0), len(rows) - 1)) for i in (sample_indexes or [])},
            reverse=True,
        )
        removed_samples = []
        for idx in normalized_indexes:
            if idx < len(rows):
                removed_samples.insert(0, dict(rows.pop(idx)))
        if rows:
            rows.reverse()
            self._write_failed_samples(rows)
        else:
            self._clear_failed_samples()
        return {
            "removed": bool(normalized_indexes), "message": "success",
            "removed_count": len(removed_samples), "remaining_count": len(rows),
            "removed_sample_indexes": normalized_indexes, "removed_samples": removed_samples,
        }

    def _remove_failed_sample_by_title_path(self, title: str, path: str) -> None:
        """按标题和路径移除失败样本（整理成功后自动出队）。"""
        try:
            if not self._auto_remove_applied_sample:
                return
            rows = self._read_failed_samples(limit=1000)
            if not rows:
                return
            rows.reverse()
            identity = {"title": str(title or "").strip(), "path": str(path or "").strip()}
            filtered = [
                row for row in rows
                if {"title": str(row.get("title") or "").strip(), "path": str(row.get("path") or "").strip()} != identity
            ]
            if len(filtered) != len(rows):
                self._write_failed_samples(filtered)
        except Exception as exc:
            logger.warning(f"[AI双引擎] 移除失败样本失败: {exc}")

    def _settle_key(self, verified: Dict[str, Any], guess: Dict[str, Any]) -> str:
        """构造已沉淀标题的归一化键（标题|年份|季号）。"""
        title = str((verified or {}).get("title") or (guess or {}).get("name") or "").strip()
        year = str((verified or {}).get("year") or "").strip() or str((guess or {}).get("year") or "").strip()
        key = f"{title}|{year}"
        if (verified or {}).get("type") == "tv" or (guess or {}).get("media_type") == "tv":
            key += f"|S{self._safe_int((guess or {}).get('season'), 0)}"
        return key

    def _is_settled(self, key: str) -> bool:
        """判断该标题键是否已沉淀过识别词。"""
        if not key:
            return False
        try:
            return key in self._settled_titles
        except Exception:
            return False

    def _mark_settled(self, key: str) -> None:
        """标记该标题键已沉淀并发起持久化。"""
        if not key:
            return
        self._settled_titles.add(key)
        self._save_settled_titles()

    def _load_settled_titles(self) -> None:
        """加载已沉淀标题集合。"""
        try:
            data = self.get_data("settled_titles")
            if data and isinstance(data, (list, set, tuple)):
                self._settled_titles = set(str(x) for x in data)
            else:
                self._settled_titles = set()
        except Exception:
            self._settled_titles = set()

    def _save_settled_titles(self) -> None:
        """保存已沉淀标题集合。"""
        try:
            self.save_data("settled_titles", sorted(self._settled_titles))
        except Exception as exc:
            logger.warning(f"[AI双引擎] 保存已沉淀标题集合失败: {exc}")

    # ==================== LLM 调用审计 ====================

    def _record_llm_usage(self, kind: str) -> None:
        """记录一次 LLM 调用，用于监控 token 消耗去向。

        kind 取 'recognize'（兜底猜测）或 'settle'（沉淀识别词生成）。
        """
        try:
            self._llm_usage[kind] = self._llm_usage.get(kind, 0) + 1
            self.save_data("llm_usage", dict(self._llm_usage))
        except Exception as exc:
            logger.warning(f"[AI双引擎] 记录 LLM 调用失败: {exc}")

    def _get_llm_usage(self) -> Dict[str, int]:
        """读取 LLM 调用审计计数。"""
        try:
            return {k: int(v) for k, v in (self._llm_usage or {}).items()}
        except Exception:
            return {}

    def _load_llm_usage(self) -> None:
        """加载 LLM 调用审计计数。"""
        try:
            data = self.get_data("llm_usage")
            if data and isinstance(data, dict):
                self._llm_usage = {str(k): int(v) for k, v in data.items()}
            else:
                self._llm_usage = {}
        except Exception:
            self._llm_usage = {}
    def _record_abandon(self, reason: str) -> None:
        """记录放弃原因统计。"""
        try:
            stats = self.get_data("abandon_stats") or {}
            stats[reason] = stats.get(reason, 0) + 1
            self.save_data("abandon_stats", stats)
        except Exception as exc:
            logger.error(f"[AI双引擎] 记录放弃原因失败: {exc}")

    def _get_stats(self) -> Dict[str, Any]:
        """获取统计信息。"""
        abandon_stats = self.get_data("abandon_stats") or {}
        ai_rules_count = 0
        try:
            rules = self._get_custom_identifiers()
            ai_rules_count = len([r for r in rules if r.startswith(self._AI_MARK)])
        except Exception:
            pass
        return {
            "abandon_stats": abandon_stats,
            "ai_rules_count": ai_rules_count,
            "cooldown_count": len(self._cooldown_records),
        }

    # ==================== 待确认规则（先审后写模式） ====================

    def _add_pending_rules(self, title: str, path: str, chosen: Dict[str, Any], target: Dict[str, Any]) -> None:
        """添加待确认规则到队列。"""
        try:
            pending = self.get_data("pending_rules") or []
            pending.append({
                "title": title,
                "path": path,
                "rule": chosen.get("rule"),
                "confidence": chosen.get("confidence", 0),
                "reason": chosen.get("reason", ""),
                "target": target,
                "lines": chosen.get("lines") or [],
                "timestamp": time.time(),
            })
            self.save_data("pending_rules", pending)
        except Exception as exc:
            logger.error(f"[AI双引擎] 添加待确认规则失败: {exc}")

    def _get_pending_rules(self) -> List[Dict[str, Any]]:
        """获取待确认规则列表。"""
        try:
            return self.get_data("pending_rules") or []
        except Exception:
            return []

    # ==================== 通知 ====================

    def _notify_identifier_written(self, title: str, chosen: Dict[str, Any], target: Dict[str, Any], total: int) -> None:
        """发送识别词写入通知。"""
        try:
            self.post_message(
                title="AI双引擎：识别词已沉淀",
                text=(
                    f"标题：{title}\n"
                    f"规则：{chosen.get('rule')}\n"
                    f"目标：{target.get('name')}（{target.get('year')}）\n"
                    f"当前识别词总数：{total}"
                ),
            )
        except Exception as exc:
            logger.warning(f"[AI双引擎] 发送通知失败: {exc}")

    def _notify_result(self, title: str, path: str, guess: Dict[str, Any], transfer_success: bool) -> None:
        """发送兜底识别结果通知。"""
        try:
            self.post_message(
                title="AI双引擎：识别兜底完成",
                text=(
                    f"标题：{title}\n"
                    f"识别结果：{guess.get('name')}（{guess.get('year')}）置信度 {guess.get('confidence')}\n"
                    f"重新整理：{'成功' if transfer_success else '未执行或失败'}"
                ),
            )
        except Exception as exc:
            logger.warning(f"[AI双引擎] 发送通知失败: {exc}")

    # ==================== 失败样本摘要 ====================

    @staticmethod
    def _inject_sample_indices(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """给失败样本注入索引。"""
        indexed: List[Dict[str, Any]] = []
        for idx, sample in enumerate(samples):
            row = dict(sample or {})
            row["sample_index"] = idx
            indexed.append(row)
        return indexed

    def _summarize_sample(self, sample: Dict[str, Any]) -> Dict[str, Any]:
        """摘要化单条失败样本。"""
        sample = dict(sample or {})
        guess = sample.get("guess") or {}
        verified = sample.get("verified_media_info") or {}
        inferred_target = {
            "name": verified.get("title") or guess.get("name") or "",
            "year": verified.get("year") or guess.get("year") or "",
            "media_type": self._normalize_media_type(verified.get("type") or guess.get("media_type")),
            "season": self._safe_int(guess.get("season"), 0),
            "episode": self._safe_int(guess.get("episode"), 0),
            "tmdb_id": self._safe_int(verified.get("tmdb_id"), 0),
        }
        return {
            "sample_index": sample.get("sample_index"),
            "title": sample.get("title"),
            "path": sample.get("path"),
            "reason": sample.get("reason"),
            "guess_name": guess.get("name"),
            "guess_confidence": self._safe_float(guess.get("confidence"), 0.0),
            "verified_title": verified.get("title"),
            "verified_year": verified.get("year"),
            "verified_tmdb_id": verified.get("tmdb_id"),
            "inferred_target": inferred_target,
            "can_auto_suggest": bool(inferred_target["name"]),
        }

    @staticmethod
    def _render_sample_brief(samples: List[Dict[str, Any]], top: int = 5) -> str:
        """渲染失败样本精简摘要。"""
        if not samples:
            return "当前没有失败样本。"
        lines = [f"失败样本 {len(samples)} 条，展示前 {min(len(samples), top)} 条："]
        for sample in samples[:top]:
            idx = sample.get("sample_index")
            label = sample.get("title") or "未命名样本"
            confidence = round(float(sample.get("guess_confidence") or 0), 2)
            can_suggest = "可建议" if sample.get("can_auto_suggest") else "需人工"
            lines.append(f"{idx}. {label} | 置信度 {confidence} | {can_suggest}")
        lines.append("下一步：可调用批量建议或批量复查接口。")
        return "\n".join(lines)

    def _build_sample_insights(self, samples: List[Dict[str, Any]], top: int = 10) -> Dict[str, Any]:
        """汇总失败样本洞察。"""
        summaries = [self._summarize_sample(sample) for sample in samples]
        reason_counter = Counter()
        title_counter = Counter()
        for summary in summaries:
            reason = str(summary.get("reason") or "unknown")
            if ":" in reason:
                reason = reason.split(":", 1)[0].strip() or "unknown"
            reason_counter[reason] += 1
            title_counter[str(summary.get("title") or "未命名")] += 1
        return {
            "total_count": len(summaries),
            "reason_counts": [{"reason": r, "count": c} for r, c in reason_counter.most_common(top)],
            "top_titles": [{"title": t, "count": c} for t, c in title_counter.most_common(top) if c >= 2],
            "priority_samples": summaries[:top],
        }

    # ==================== API 端点 ====================

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {
                "path": "/health",
                "endpoint": self.api_health,
                "methods": ["GET"],
                "summary": "检查 AI双引擎 的运行状态",
            },
            {
                "path": "/recognize",
                "endpoint": self.api_recognize,
                "methods": ["POST"],
                "summary": "用当前 LLM 对失败标题做一次本地结构化识别测试",
            },
            {
                "path": "/failed_samples",
                "endpoint": self.api_failed_samples,
                "methods": ["GET"],
                "summary": "查看最近保存的低置信度失败样本",
            },
            {
                "path": "/sample_insights",
                "endpoint": self.api_sample_insights,
                "methods": ["GET"],
                "summary": "汇总失败样本原因和优先处理样本",
            },
            {
                "path": "/sample_brief",
                "endpoint": self.api_sample_brief,
                "methods": ["GET"],
                "summary": "返回适合智能体低 token 消费的失败样本精简摘要",
            },
            {
                "path": "/suggest_identifiers",
                "endpoint": self.api_suggest_identifiers,
                "methods": ["POST"],
                "summary": "根据标题和目标结果生成 MoviePilot 自定义识别词建议",
            },
            {
                "path": "/apply_identifiers",
                "endpoint": self.api_apply_identifiers,
                "methods": ["POST"],
                "summary": "将确认后的自定义识别词追加写入系统 CustomIdentifiers",
            },
            {
                "path": "/clear_failed_samples",
                "endpoint": self.api_clear_failed_samples,
                "methods": ["POST"],
                "summary": "清空失败样本文件",
            },
            {
                "path": "/llm_errors",
                "endpoint": self.api_llm_errors,
                "methods": ["GET"],
                "summary": "查看 LLM 调用失败的诊断记录",
            },
            {
                "path": "/clear_llm_errors",
                "endpoint": self.api_clear_llm_errors,
                "methods": ["POST"],
                "summary": "清空 LLM 错误诊断记录",
            },
            {
                "path": "/remove_failed_sample",
                "endpoint": self.api_remove_failed_sample,
                "methods": ["POST"],
                "summary": "按索引移除单条失败样本",
            },
            {
                "path": "/stats",
                "endpoint": self.api_stats,
                "methods": ["GET"],
                "summary": "获取识别词统计",
                "auth": "bear",
            },
            {
                "path": "/pending",
                "endpoint": self.api_pending,
                "methods": ["GET"],
                "summary": "获取待确认规则",
                "auth": "bear",
            },
            {
                "path": "/confirm",
                "endpoint": self.api_confirm,
                "methods": ["POST"],
                "summary": "确认写入规则",
                "auth": "bear",
            },
        ]

    async def api_health(self, request: Request):
        """检查插件运行状态。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        llm_ready = bool(getattr(settings, "LLM_API_KEY", None))
        return {
            "success": True,
            "data": {
                "plugin_version": self.plugin_version,
                "enabled": self._enabled,
                "llm_ready": llm_ready,
                "llm_provider": getattr(settings, "LLM_PROVIDER", ""),
                "llm_model": getattr(settings, "LLM_MODEL", ""),
                "confidence_threshold": self._confidence_threshold,
                "write_identifier": self._write_identifier,
                "review_mode": self._review_mode,
            },
        }

    async def api_recognize(self, request: Request):
        """测试性识别一次。"""
        body = await request.json()
        ok, message = self._check_api_access(request, body)
        if not ok:
            return {"success": False, "message": message}
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        title = str(body.get("title") or "").strip()
        path = str(body.get("path") or "").strip()
        result = self._recognize(title=title, path=path)
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "data": {"guess": result.get("guess"), "verified_media_info": result.get("verified_media_info")},
        }

    async def api_failed_samples(self, request: Request):
        """查看失败样本。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        limit = self._safe_int(request.query_params.get("limit"), 20)
        limit = max(1, min(limit, 100))
        samples = self._inject_sample_indices(self._read_failed_samples(limit=limit))
        return {"success": True, "data": {"count": len(samples), "samples": samples}}

    async def api_sample_insights(self, request: Request):
        """查看失败样本洞察。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        limit = self._safe_int(request.query_params.get("limit"), 50)
        limit = max(1, min(limit, self._failed_sample_cap()))
        top = self._safe_int(request.query_params.get("top"), 10)
        top = max(1, min(top, 20))
        samples = self._inject_sample_indices(self._read_failed_samples(limit=limit))
        return {"success": True, "data": self._build_sample_insights(samples, top=top)}

    async def api_sample_brief(self, request: Request):
        """查看失败样本精简摘要。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        limit = self._safe_int(request.query_params.get("limit"), 5)
        limit = max(1, min(limit, 20))
        samples = self._inject_sample_indices(self._read_failed_samples(limit=self._failed_sample_cap()))
        return {
            "success": True,
            "data": {"count": len(samples), "text": self._render_sample_brief(samples, top=limit)},
        }

    async def api_suggest_identifiers(self, request: Request):
        """生成识别词建议。"""
        body = await request.json()
        ok, message = self._check_api_access(request, body)
        if not ok:
            return {"success": False, "message": message}
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        return self._suggest_identifiers(body)

    async def api_apply_identifiers(self, request: Request):
        """写入识别词。"""
        body = await request.json()
        ok, message = self._check_api_access(request, body)
        if not ok:
            return {"success": False, "message": message}
        identifiers = body.get("identifiers") or []
        if not isinstance(identifiers, list):
            return {"success": False, "message": "identifiers 必须是数组"}
        result = self._append_custom_identifiers([str(line or "") for line in identifiers])
        return {"success": True, "message": "success", "data": result}

    async def api_clear_failed_samples(self, request: Request):
        """清空失败样本。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        cleared = self._clear_failed_samples()
        return {"success": True, "message": "success", "data": {"cleared_count": cleared}}

    async def api_llm_errors(self, request: Request):
        """查看 LLM 错误诊断。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        limit = self._safe_int(request.query_params.get("limit"), 20)
        limit = max(1, min(limit, 100))
        errors = self._read_llm_errors(limit=limit)
        return {"success": True, "data": {"count": len(errors), "errors": errors}}

    async def api_clear_llm_errors(self, request: Request):
        """清空 LLM 错误诊断。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        cleared = self._clear_llm_errors()
        return {"success": True, "message": "success", "data": {"cleared_count": cleared}}

    async def api_remove_failed_sample(self, request: Request):
        """移除失败样本。"""
        body = await request.json()
        ok, message = self._check_api_access(request, body)
        if not ok:
            return {"success": False, "message": message}
        result = self._remove_failed_sample(body.get("sample_index"), limit=1000)
        if not result.get("removed"):
            return {"success": False, "message": result.get("message", "移除失败"), "data": result}
        return {"success": True, "message": "success", "data": result}

    async def api_stats(self, request):
        """获取识别词统计。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        return {"success": True, "data": self._get_stats()}

    async def api_pending(self, request):
        """获取待确认规则。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        pending = self._get_pending_rules()
        return {"success": True, "data": {"count": len(pending), "pending": pending}}

    async def api_confirm(self, request):
        """确认写入待确认规则。"""
        ok, message = self._check_api_access(request)
        if not ok:
            return {"success": False, "message": message}
        body = await request.json()
        rule = str(body.get("rule") or "").strip()
        if not rule:
            return {"success": False, "message": "缺少 rule 参数"}
        pending = self._get_pending_rules()
        target_rule = None
        for i, p in enumerate(pending):
            if p.get("rule") == rule:
                target_rule = pending.pop(i)
                break
        if not target_rule:
            return {"success": False, "message": "未找到待确认规则"}
        result = self._append_custom_identifiers(target_rule.get("lines") or [rule])
        self.save_data("pending_rules", pending)
        if result.get("added_count", 0) > 0:
            return {"success": True, "message": "已写入"}
        return {"success": False, "message": result.get("message", "写入失败或规则已存在")}

    # ==================== 详情页面 ====================

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None

        llm_ready = bool(getattr(settings, "LLM_API_KEY", None))
        llm_provider = getattr(settings, "LLM_PROVIDER", "—")
        llm_model = getattr(settings, "LLM_MODEL", "—")
        failed_samples_count = len(self._read_failed_samples(limit=self._failed_sample_cap()))
        llm_errors_count = len(self._read_llm_errors(limit=self._max_failed_samples))
        stats = self._get_stats()
        ai_rules_count = stats.get("ai_rules_count", 0)
        cooldown_count = len(self._cooldown_records)
        custom_identifiers_count = len(self._get_custom_identifiers())
        pending = self._get_pending_rules() if self._review_mode else []

        def stat_card(title: str, value: Any, subtitle: str = "") -> dict:
            content = [
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-1"}, "text": title},
                {"component": "div", "props": {"class": "text-h6 font-weight-bold"}, "text": str(value)},
            ]
            if subtitle:
                content.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"}, "text": subtitle})
            return {"component": "VCard", "props": {"variant": "tonal", "class": "pa-4 h-100"}, "content": content}

        page = [
            {
                "component": "VContainer",
                "props": {"fluid": True, "class": "pa-0"},
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info", "variant": "tonal", "class": "mb-4",
                            "title": "AI 双引擎识别",
                            "text": "原生识别失败时：AI 结构化兜底（救当次）→ 沉淀窄作用域识别词（救以后）→ 重新整理。识别词由本插件独占写入，写前全量快照比对、只增不删、写后逐行校验。",
                        },
                    },
                    {
                        "component": "VRow",
                        "props": {"dense": True, "class": "mb-2"},
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("当前状态", "已启用" if self._enabled else "未启用")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [stat_card("LLM 可用", "是" if llm_ready else "否", f"{llm_provider} / {llm_model}")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("失败样本", f"{failed_samples_count} 条", f"上限 {self._max_failed_samples} 条")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("LLM 错误", f"{llm_errors_count} 条", "诊断记录")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [stat_card("自定义识别词", f"{custom_identifiers_count} 条", f"AI 沉淀 {ai_rules_count} 条")]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"dense": True, "class": "mb-2"},
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("写识别词", "开" if self._write_identifier else "关", f"阈值 {self._write_min_confidence}")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("先审后写", "开" if self._review_mode else "关")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("冷却时长", f"{self._cooldown_hours} 小时")]},
                            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 2}, "content": [stat_card("冷却记录", f"{cooldown_count} 条")]},
                        ],
                    },
                ],
            }
        ]

        # 放弃原因统计
        abandon_stats = stats.get("abandon_stats", {})
        if abandon_stats:
            stat_cards = []
            for reason, count in abandon_stats.items():
                stat_cards.append({
                    "component": "VCard",
                    "props": {"class": "ma-2", "style": "min-width: 120px;"},
                    "content": [{
                        "component": "VCardText",
                        "props": {"class": "text-center"},
                        "content": [
                            {"component": "div", "props": {"class": "text-h4"}, "text": str(count)},
                            {"component": "div", "props": {"class": "text-caption"}, "text": reason},
                        ],
                    }],
                })
            page[0]["content"].append({
                "component": "VRow",
                "content": [{"component": "VCol", "props": {"cols": 12}, "content": stat_cards}],
            })

        # 待确认规则
        if self._review_mode and pending:
            page[0]["content"].append({
                "component": "VAlert",
                "props": {"type": "warning", "text": f"有 {len(pending)} 条规则待确认"},
            })

        return page

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """返回渲染模式。"""
        return "vuetify", None

    # ==================== 配置表单 ====================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        failed_samples_count = len(self._read_failed_samples(limit=self._failed_sample_cap()))
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info", "variant": "tonal", "class": "mb-2",
                            "text": "AI 双引擎识别：原生识别失败时做结构化兜底（救当次），成功后沉淀窄作用域识别词（救以后）。识别词由本插件独占写入。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning", "variant": "tonal", "class": "mb-2",
                            "text": f"当前累计 {failed_samples_count} 条失败样本。如需重置噪音数据，请勾选下方“一次性清空”开关后点击保存。该操作只清空失败样本，不会删除已写入的识别词。",
                        },
                    },
                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                    {"component": "VSwitch", "props": {"model": "debug", "label": "调试模式（写入详细日志）"}},
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "confidence_threshold", "label": "兜底注入置信度阈值", "type": "number", "hint": "低于该值的结果不注入，默认 0.65", "persistent-hint": True}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "request_timeout", "label": "LLM 请求超时（秒）", "type": "number", "hint": "默认 25 秒", "persistent-hint": True}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "max_retries", "label": "结构化输出重试次数", "type": "number", "hint": "默认 2 次", "persistent-hint": True}},
                            ]},
                        ],
                    },
                    {"component": "VTextField", "props": {"model": "max_failed_samples", "label": "失败样本保留上限", "type": "number", "hint": "默认保留最近 200 条，并对重复样本自动去重", "persistent-hint": True}},
                    {"component": "VSwitch", "props": {"model": "save_failed_samples", "label": "保存低置信度样本"}},
                    {"component": "VSwitch", "props": {"model": "save_title_only_samples", "label": "保存仅标题样本"}},
                    {"component": "VSwitch", "props": {"model": "auto_remove_applied_sample", "label": "写入识别词后自动移除对应失败样本"}},
                    {"component": "VSwitch", "props": {"model": "clear_failed_samples_once", "label": "保存时清空失败样本（一次性）"}},
                    {"component": "VSwitch", "props": {"model": "write_identifier", "label": "兜底成功后自动沉淀识别词（推荐开启，同类文件以后原生即可识别）"}},
                    {"component": "VSlider", "props": {"model": "write_min_confidence", "label": "识别词沉淀置信度阈值（低于此值不写入）", "min": 0.3, "max": 1.0, "step": 0.05, "thumb-label": "always"}},
                    {"component": "VTextField", "props": {"model": "cooldown_hours", "label": "冷却时长（小时）", "type": "number", "hint": "同一文件名在此时间内只处理一次", "persistent-hint": True}},
                    {"component": "VTextField", "props": {"model": "max_rule_lines", "label": "单次写入规则行数上限", "type": "number", "hint": "防止异常情况下批量写入污染识别词库", "persistent-hint": True}},
                    {"component": "VTextField", "props": {"model": "max_rule_length", "label": "单条规则长度上限（字符）", "type": "number", "hint": "超长规则直接放弃并记入失败样本", "persistent-hint": True}},
                    {"component": "VSwitch", "props": {"model": "review_mode", "label": "先审后写模式（规则进待确认队列，人工确认后写入）"}},
                    {"component": "VSwitch", "props": {"model": "notify_on_write", "label": "写入/兜底成功时发送通知"}},
                ],
            }
        ]
        defaults = {
            "enabled": False,
            "debug": False,
            "confidence_threshold": 0.65,
            "request_timeout": 25,
            "max_retries": 2,
            "save_failed_samples": True,
            "save_title_only_samples": False,
            "max_failed_samples": 200,
            "auto_remove_applied_sample": True,
            "clear_failed_samples_once": False,
            "write_identifier": True,
            "write_min_confidence": 0.8,
            "cooldown_hours": 24,
            "max_rule_lines": 5,
            "max_rule_length": 200,
            "review_mode": False,
            "notify_on_write": False,
        }
        return form, defaults
