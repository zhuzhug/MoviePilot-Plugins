"""
AI识别词插件

识别失败兜底 + 安全沉淀识别词 + 重新触发整理。
核心设计原则：写入前全量快照比对、只增不删、写后逐行校验，杜绝清空用户自带识别词的风险。
"""

import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.metainfo import MetaInfo, clear_rust_parse_options_cache
from app.core.event import eventmanager
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import ChainEventType, MediaType, NotificationType, SystemConfigKey
from app.utils import rust_accel

try:
    from app.helper.llm import LLMHelper
except ImportError:
    from app.agent.llm import LLMHelper


# ==================== 数据模型 ====================

class AIGuess(BaseModel):
    """大模型结构化猜测结果。"""
    name: str = Field(default="", description="标准化后的影视标题；无法判断时返回空字符串")
    year: str = Field(default="", description="四位年份；无法判断时返回空字符串")
    media_type: str = Field(default="unknown", description="movie、tv 或 unknown")
    season: Optional[int] = Field(default=None, description="剧集季号，电影或未知填 None")
    episode: Optional[int] = Field(default=None, description="剧集集号，电影或未知填 None")
    confidence: float = Field(default=0.0, description="0 到 1 之间的置信度")
    reason: str = Field(default="", description="简短说明为什么这样判断")


class AICandidate(BaseModel):
    """AI 生成的识别词候选规则。"""
    rule: str = Field(default="", description="一条 MoviePilot 自定义识别词规则")
    confidence: float = Field(default=0.0, description="0 到 1 之间的置信度")
    reason: str = Field(default="", description="为什么建议这条规则")


class AICandidateBundle(BaseModel):
    """AI 生成的识别词候选规则集合。"""
    candidates: List[AICandidate] = Field(default_factory=list, description="候选规则列表")


# ==================== 插件主体 ====================

class AIdentifier(_PluginBase):
    """AI识别词插件：识别失败兜底 + 安全沉淀识别词 + 重新触发整理。"""

    plugin_name = "AI识别词"
    plugin_desc = "原生识别失败时接入 AI 二次识别，TMDB 命中后安全沉淀窄作用域识别词并重新整理。写入前全量快照比对、只增不删、写后逐行校验，杜绝清空用户自带识别词的风险。"
    plugin_icon = "mdi-robot-outline"
    plugin_version = "1.0.3"
    plugin_label = "识别,增强"
    plugin_author = "zhuzhug"
    plugin_config_prefix = "aidentifier_"
    plugin_order = 100
    auth_level = 2

    # 运行状态
    _enabled: bool = False
    _debug: bool = False
    _min_confidence: float = 0.7
    _cooldown_hours: int = 24
    _max_rule_lines: int = 5
    _max_rule_length: int = 200
    _review_mode: bool = False
    _notify_on_write: bool = False
    _track_usage: bool = False

    # 内部锁：保护识别词读-比-写
    _identifier_lock = threading.Lock()
    # 冷却记录：文件名+规则 -> 上次处理时间戳
    _cooldown_records: Dict[str, float] = {}

    # AI 规则标记前缀
    _AI_MARK = "# [AI识别词] "

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._debug = False
        self._min_confidence = 0.7
        self._cooldown_hours = 24
        self._max_rule_lines = 5
        self._max_rule_length = 200
        self._review_mode = False
        self._notify_on_write = False
        self._track_usage = False

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._debug = bool(config.get("debug"))
        self._min_confidence = float(config.get("min_confidence") or 0.7)
        self._cooldown_hours = int(config.get("cooldown_hours") or 24)
        self._max_rule_lines = int(config.get("max_rule_lines") or 5)
        self._max_rule_length = int(config.get("max_rule_length") or 200)
        self._review_mode = bool(config.get("review_mode"))
        self._notify_on_write = bool(config.get("notify_on_write"))
        self._track_usage = bool(config.get("track_usage"))

        # 加载冷却记录
        self._load_cooldown_records()

        # 注册识别事件
        self._register_events()

        if self._enabled:
            logger.info(f"[AI识别词] 插件已启用 (调试模式={self._debug})")

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
            {
                "path": "/stats",
                "endpoint": self.api_stats,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取识别词统计",
                "description": "返回 AI 识别词统计信息，包括写入数、放弃原因分布、冷却记录数等"
            },
            {
                "path": "/pending",
                "endpoint": self.api_pending,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取待确认规则",
                "description": "返回待确认的 AI 识别词规则列表（先审后写模式）"
            },
            {
                "path": "/confirm",
                "endpoint": self.api_confirm,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "确认写入规则",
                "description": "确认写入指定的 AI 识别词规则（先审后写模式）"
            }
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        form = [
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
                        "component": "VSwitch",
                        "props": {
                            "model": "debug",
                            "label": "调试模式（写入详细日志）"
                        }
                    },
                    {
                        "component": "VSlider",
                        "props": {
                            "model": "min_confidence",
                            "label": "置信度阈值（低于此值不写入）",
                            "min": 0.3,
                            "max": 1.0,
                            "step": 0.05,
                            "thumb-label": "always"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "cooldown_hours",
                            "label": "冷却时长（小时）",
                            "type": "number",
                            "hint": "同一文件名+规则在此时间内只处理一次",
                            "persistent-hint": True
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "max_rule_lines",
                            "label": "单次写入规则行数上限",
                            "type": "number",
                            "hint": "防止异常情况下批量写入污染识别词库",
                            "persistent-hint": True
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "max_rule_length",
                            "label": "单条规则长度上限（字符）",
                            "type": "number",
                            "hint": "超长规则直接放弃并记入失败样本",
                            "persistent-hint": True
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "review_mode",
                            "label": "先审后写模式（规则进待确认队列，人工确认后写入）"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "notify_on_write",
                            "label": "写入时发送通知"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "track_usage",
                            "label": "追踪规则使用情况"
                        }
                    }
                ]
            }
        ]
        defaults = {
            "enabled": False,
            "debug": False,
            "min_confidence": 0.7,
            "cooldown_hours": 24,
            "max_rule_lines": 5,
            "max_rule_length": 200,
            "review_mode": False,
            "notify_on_write": False,
            "track_usage": False
        }
        return form, defaults

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None

        stats = self._get_stats()
        cooldown_count = len(self._cooldown_records)
        ai_rules_count = stats.get("ai_rules_count", 0)

        page = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "text": f"AI识别词插件已启用。当前 AI 写入规则 {ai_rules_count} 条，冷却记录 {cooldown_count} 条。"
                }
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
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "text-center"},
                            "content": [
                                {"component": "div", "props": {"class": "text-h4"}, "text": str(count)},
                                {"component": "div", "props": {"class": "text-caption"}, "text": reason}
                            ]
                        }
                    ]
                })
            page.append({
                "component": "VRow",
                "content": [{"component": "VCol", "props": {"cols": "12"}, "content": stat_cards}]
            })

        # 待确认规则（先审后写模式）
        if self._review_mode:
            pending = self._get_pending_rules()
            if pending:
                page.append({
                    "component": "VAlert",
                    "props": {"type": "warning", "text": f"有 {len(pending)} 条规则待确认"}
                })

        return page

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件后台服务列表。"""
        return []

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        try:
            eventmanager.unregister(self.on_chain_name_recognize)
        except Exception:
            pass
        self._save_cooldown_records()

    # ==================== 事件处理 ====================

    def _register_events(self) -> None:
        """注册识别事件。"""
        try:
            eventmanager.register(ChainEventType.NameRecognize)(self.on_chain_name_recognize)
            if self._enabled:
                eventmanager.enable_event_handler(self.on_chain_name_recognize)
            else:
                eventmanager.disable_event_handler(self.on_chain_name_recognize)
        except Exception as exc:
            logger.warning(f"[AI识别词] 注册识别事件失败: {exc}")

    @staticmethod
    def _extract_title_path(event_data: Any) -> Tuple[str, str]:
        """从事件数据中提取标题和路径。"""
        title = ""
        path = ""
        if isinstance(event_data, dict):
            title = (
                event_data.get("title")
                or event_data.get("name")
                or event_data.get("org_string")
                or ""
            )
            path = (
                event_data.get("path")
                or event_data.get("file_path")
                or event_data.get("org_string")
                or ""
            )
        return str(title or "").strip(), str(path or "").strip()

    @staticmethod
    def _is_recognition_failed(event_data: Any) -> bool:
        """判断是否识别失败。"""
        if not isinstance(event_data, dict):
            return False
        # 如果已经有 mediainfo 或者成功标记，说明识别成功
        if event_data.get("mediainfo") or event_data.get("media_info"):
            return False
        # 如果已经有 source_plugin，说明其他插件已处理
        if event_data.get("source_plugin"):
            return False
        return True

    def on_chain_name_recognize(self, event) -> None:
        """识别事件回调：识别失败时介入。"""
        if not self._enabled:
            return

        event_data = getattr(event, "event_data", None) or {}
        title, path = self._extract_title_path(event_data)
        if not title and not path:
            return

        # 只在识别失败时介入
        if not self._is_recognition_failed(event_data):
            return

        # 异步执行，不阻塞原生链路
        threading.Thread(
            target=self._handle_recognition_failure,
            args=(title, path),
            daemon=True
        ).start()

    # ==================== 核心处理逻辑 ====================

    def _handle_recognition_failure(self, title: str, path: str) -> None:
        """处理识别失败：AI 猜测 → TMDB 校验 → 写入识别词 → 重新整理。"""
        try:
            sample_key = f"{title}|{path}"

            # 1. 冷却检查
            if self._is_cooled_down(sample_key):
                self._record_abandon("冷却中")
                if self._debug:
                    logger.info(f"[AI识别词] 样本在冷却期，跳过: {title or path}")
                return

            # 2. AI 猜测
            guess = self._invoke_llm(title, path)
            if not guess or not guess.name:
                self._record_abandon("AI猜测失败")
                self._record_failed_sample(title, path, "AI猜测失败", None)
                return

            # 3. TMDB 硬校验
            mediainfo = self._verify_tmdb(guess)
            if not mediainfo:
                self._record_abandon("TMDB未命中")
                self._record_failed_sample(title, path, "TMDB未命中", guess)
                return

            # 4. 生成候选规则
            candidates = self._generate_candidates(title, path, guess, mediainfo)
            if not candidates:
                self._record_abandon("无候选规则")
                self._record_failed_sample(title, path, "无候选规则", guess)
                return

            # 5. 规则长度和数量检查
            valid_candidates = self._validate_candidates(candidates)
            if not valid_candidates:
                self._record_abandon("规则超限")
                self._record_failed_sample(title, path, "规则超限", guess)
                return

            # 6. 临时演练验证（不落库）
            verified_candidates = self._replay_verify(title, valid_candidates)
            if not verified_candidates:
                self._record_abandon("演练不通过")
                self._record_failed_sample(title, path, "演练不通过", guess)
                return

            # 7. 冲突预检
            non_conflicting = self._conflict_check(verified_candidates)
            if not non_conflicting:
                self._record_abandon("规则冲突")
                self._record_failed_sample(title, path, "规则冲突", guess)
                return

            # 8. 写入识别词（先审后写模式下进待确认队列）
            if self._review_mode:
                self._add_pending_rules(title, path, non_conflicting, guess, mediainfo)
                self._record_abandon("待确认")
                return

            write_result = self._safe_write(non_conflicting)
            if not write_result.get("success"):
                self._record_abandon("写入失败")
                self._record_failed_sample(title, path, f"写入失败: {write_result.get('message')}", guess)
                return

            # 9. 更新冷却记录
            self._update_cooldown(sample_key)

            # 10. 重新整理
            transfer_success = self._retrigger_transfer(path, mediainfo, guess)

            # 11. 整理成功后删除旧失败记录
            if transfer_success:
                self._cleanup_failed_history(path)

            # 12. 通知
            if self._notify_on_write:
                self._notify_write(success=True, title=title, path=path,
                                   rule=non_conflicting[0].rule if non_conflicting else "",
                                   mediainfo=mediainfo, transfer_success=transfer_success)

            # 13. 记录使用情况
            if self._track_usage:
                self._record_usage(title, path, non_conflicting, mediainfo)

        except Exception as exc:
            logger.error(f"[AI识别词] 处理识别失败时发生异常: {exc}", exc_info=True)
            self._record_abandon("内部异常")

    # ==================== AI 调用 ====================

    def _invoke_llm(self, title: str, path: str) -> Optional[AIGuess]:
        """调用大模型进行结构化猜测。"""
        try:
            llm = LLMHelper.get_llm(streaming=False)
            # 兼容新版异步返回
            if asyncio.iscoroutine(llm):
                llm = asyncio.run(llm)

            prompt = self._build_guess_prompt()
            chain = prompt | llm
            response = chain.invoke(
                {"title": title, "path": path},
                config={"configurable": {"timeout": 60}}
            )
            content = self._extract_json_text(response.content)
            return self._parse_json_model(AIGuess, content)
        except Exception as exc:
            logger.error(f"[AI识别词] AI 调用失败: {exc}")
            return None

    @staticmethod
    def _build_guess_prompt():
        """构建猜测提示词。"""
        from langchain_core.prompts import ChatPromptTemplate

        return ChatPromptTemplate.from_messages([
            ("system",
             "你是影视资源识别专家。给定文件名和路径，提炼出标准化的影视标题、年份、季集信息。"
             "只输出 JSON，格式为 "
             '{{"name": "标题", "year": "年份", "media_type": "movie/tv", '
             '"season": 季号, "episode": 集号, "confidence": 置信度, "reason": "原因"}}'
             "。置信度范围 0-1，不确定时降低置信度。"),
            ("human", "文件名: {title}\n路径: {path}")
        ])

    @staticmethod
    def _extract_json_text(content: Any) -> str:
        """从模型响应中提取 JSON 文本。"""
        text = str(content or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        return text

    @staticmethod
    def _parse_json_model(model_cls: Any, content: str) -> Any:
        """把模型输出解析成 Pydantic 模型。"""
        if not content:
            raise ValueError("模型未返回任何内容")
        try:
            return model_cls.model_validate_json(content)
        except Exception:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = content.find(opener)
            end = content.rfind(closer) if start >= 0 else -1
            if end <= start:
                continue
            try:
                return model_cls.model_validate_json(content[start:end + 1])
            except Exception:
                continue
        raise ValueError(f"模型输出无法解析为 JSON: {content[:200]}")

    def _generate_candidates(self, title: str, path: str, guess: AIGuess, mediainfo: Any) -> List[AICandidate]:
        """生成识别词候选规则。"""
        try:
            llm = LLMHelper.get_llm(streaming=False)
            if asyncio.iscoroutine(llm):
                llm = asyncio.run(llm)

            prompt = self._build_candidate_prompt()
            chain = prompt | llm
            response = chain.invoke(
                {
                    "title": title,
                    "path": path,
                    "guess": guess.model_dump_json(),
                    "mediainfo_title": getattr(mediainfo, "title", ""),
                    "mediainfo_year": getattr(mediainfo, "year", "")
                },
                config={"configurable": {"timeout": 60}}
            )
            content = self._extract_json_text(response.content)
            bundle = self._parse_json_model(AICandidateBundle, content)
            return bundle.candidates
        except Exception as exc:
            logger.error(f"[AI识别词] 生成候选规则失败: {exc}")
            return []

    @staticmethod
    def _build_candidate_prompt():
        """构建候选规则提示词。"""
        from langchain_core.prompts import ChatPromptTemplate

        return ChatPromptTemplate.from_messages([
            ("system",
             "你是 MoviePilot 自定义识别词专家。根据给定的文件名、路径、AI 猜测结果和 TMDB 命中的媒体信息，"
             "生成窄作用域的识别词规则。\n"
             "规则格式：`被替换词 => 替换目标{{[tmdbid=xxx;type=movie/tv]}}`\n"
             "要求：\n"
             "1. 左值（被替换词）要窄作用域、样例特定，避免裸通用词\n"
             "2. 优先用文件名中的独特片段作为锚点\n"
             "3. 替换目标要包含正确的标题、季集号和 TMDB ID\n"
             "4. 生成 1-3 条候选规则，按置信度降序\n"
             "只输出 JSON，格式为 "
             '{{"candidates": [{{"rule": "规则", "confidence": 置信度, "reason": "原因"}}]}}'),
            ("human",
             "文件名: {title}\n"
             "路径: {path}\n"
             "AI 猜测: {guess}\n"
             "TMDB 命中标题: {mediainfo_title}\n"
             "TMDB 命中年份: {mediainfo_year}")
        ])

    # ==================== TMDB 校验 ====================

    def _verify_tmdb(self, guess: AIGuess) -> Optional[Any]:
        """回查 TMDB，未命中返回 None。"""
        try:
            from app.chain.media import MediaChain

            raw_text = guess.name
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
            return mediainfo
        except Exception as exc:
            logger.error(f"[AI识别词] TMDB 校验失败: {exc}")
            return None

    # ==================== 规则验证 ====================

    @staticmethod
    def _rust_parse_options(custom_words: List[str] = None) -> dict:
        """构建 Rust 解析选项。"""
        return {"custom_words": custom_words or []}

    def _replay_verify(self, title: str, candidates: List[AICandidate]) -> List[AICandidate]:
        """临时演练验证：不落库，用候选规则直接解析。"""
        verified = []
        for cand in candidates:
            try:
                rules = [cand.rule]
                options = self._rust_parse_options(rules)
                result = rust_accel.parse_metainfo_path(title, options)
                if result:
                    verified.append(cand)
            except Exception as exc:
                logger.debug(f"[AI识别词] 演练失败: {cand.rule} -> {exc}")
        return verified

    def _validate_candidates(self, candidates: List[AICandidate]) -> List[AICandidate]:
        """验证候选规则的长度和数量。"""
        valid = []
        for cand in candidates:
            if not cand.rule or len(cand.rule) > self._max_rule_length:
                continue
            valid.append(cand)
            if len(valid) >= self._max_rule_lines:
                break
        return valid

    def _conflict_check(self, candidates: List[AICandidate]) -> List[AICandidate]:
        """冲突预检：检查新规则是否与已有规则冲突。"""
        existing = self._get_custom_identifiers()
        non_conflicting = []
        for cand in candidates:
            rule = cand.rule
            # 简单冲突检测：检查左值是否与已有规则左值重叠
            left = rule.split("=>")[0].strip() if "=>" in rule else rule
            conflict = False
            for ex in existing:
                if ex.startswith("#"):
                    continue
                ex_left = ex.split("=>")[0].strip() if "=>" in ex else ex
                if left in ex_left or ex_left in left:
                    conflict = True
                    break
            if not conflict:
                non_conflicting.append(cand)
        return non_conflicting

    # ==================== 安全写入 ====================

    def _safe_write(self, candidates: List[AICandidate]) -> Dict[str, Any]:
        """安全写入识别词：锁内重读合并写回 + 写后逐行校验。"""
        with self._identifier_lock:
            try:
                # 1. 重读最新全量
                existing = self._get_custom_identifiers()
                snapshot = list(existing)

                # 2. 准备新增行（带标记）
                added_lines = []
                for cand in candidates:
                    if not cand.rule:
                        continue
                    # 标记行 + 规则行
                    mark = f"{self._AI_MARK}{cand.rule[:50]}..."
                    added_lines.append(mark)
                    added_lines.append(cand.rule)

                if not added_lines:
                    return {"success": False, "message": "没有可写入的规则"}

                # 3. 合并去重
                merged = existing + [l for l in added_lines if l not in existing]

                # 4. 写回
                self._systemconfig.set(SystemConfigKey.CustomIdentifiers, merged)
                clear_rust_parse_options_cache()

                # 5. 写后逐行校验
                after = self._get_custom_identifiers()
                if len(after) < len(snapshot):
                    # 数量变少，立即回滚
                    self._systemconfig.set(SystemConfigKey.CustomIdentifiers, snapshot)
                    clear_rust_parse_options_cache()
                    return {"success": False, "message": "写后校验失败：识别词数量变少，已回滚"}

                # 检查原有规则是否一字未变
                for line in snapshot:
                    if line not in after:
                        self._systemconfig.set(SystemConfigKey.CustomIdentifiers, snapshot)
                        clear_rust_parse_options_cache()
                        return {"success": False, "message": "写后校验失败：原有规则丢失，已回滚"}

                return {"success": True, "added_count": len(added_lines), "total_count": len(after)}

            except Exception as exc:
                logger.error(f"[AI识别词] 写入识别词异常: {exc}")
                # 异常时尝试回滚
                try:
                    self._systemconfig.set(SystemConfigKey.CustomIdentifiers, snapshot)
                    clear_rust_parse_options_cache()
                except Exception:
                    pass
                return {"success": False, "message": f"写入异常: {exc}"}

    def _get_custom_identifiers(self) -> List[str]:
        """获取自定义识别词全量列表。"""
        if not hasattr(self, "_systemconfig"):
            self._systemconfig = SystemConfigOper()
        return self._systemconfig.get(SystemConfigKey.CustomIdentifiers) or []

    # ==================== 重新整理 ====================

    def _retrigger_transfer(self, path: str, mediainfo: Any, guess: AIGuess) -> bool:
        """重新触发整理。"""
        try:
            from app.chain.transfer import TransferChain
            from app.schemas import FileItem

            # 构建 FileItem
            fileitem = FileItem(
                path=path,
                storage="local"
            )

            # 调用手动整理
            transfer_chain = TransferChain()
            season = guess.season if guess.season else None
            state, message = transfer_chain.manual_transfer(
                fileitem=fileitem,
                tmdbid=getattr(mediainfo, "tmdb_id", None),
                mtype=getattr(mediainfo, "type", None),
                season=season,
                reorganize=True
            )

            if state:
                logger.info(f"[AI识别词] 重新整理成功: {path}")
                return True
            else:
                logger.warning(f"[AI识别词] 重新整理失败: {path} -> {message}")
                return False

        except Exception as exc:
            logger.error(f"[AI识别词] 重新整理异常: {exc}")
            return False

    def _cleanup_failed_history(self, path: str) -> None:
        """删除同一源路径下的旧失败记录。"""
        try:
            from app.db import TransferHistoryOper

            transfer_oper = TransferHistoryOper()
            # 获取该源路径的所有记录
            records = transfer_oper.get_by_src(path, "local")
            if records and not records.status:
                # 状态为失败，删除
                transfer_oper.delete(records.id)
                logger.info(f"[AI识别词] 已删除旧失败记录: {path}")
        except Exception as exc:
            logger.error(f"[AI识别词] 清理失败记录异常: {exc}")

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
            logger.error(f"[AI识别词] 保存冷却记录失败: {exc}")

    # ==================== 失败样本与统计 ====================

    def _record_failed_sample(self, title: str, path: str, reason: str, guess: Optional[AIGuess]) -> None:
        """记录失败样本。"""
        try:
            samples = self.get_data("failed_samples") or []
            sample = {
                "title": title,
                "path": path,
                "reason": reason,
                "guess": guess.model_dump() if guess else None,
                "timestamp": time.time()
            }
            samples.append(sample)
            # 只保留最近 1000 条
            if len(samples) > 1000:
                samples = samples[-1000:]
            self.save_data("failed_samples", samples)
        except Exception as exc:
            logger.error(f"[AI识别词] 记录失败样本失败: {exc}")

    def _record_abandon(self, reason: str) -> None:
        """记录放弃原因。"""
        try:
            stats = self.get_data("abandon_stats") or {}
            stats[reason] = stats.get(reason, 0) + 1
            self.save_data("abandon_stats", stats)
        except Exception as exc:
            logger.error(f"[AI识别词] 记录放弃原因失败: {exc}")

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
            "cooldown_count": len(self._cooldown_records)
        }

    def _record_usage(self, title: str, path: str, candidates: List[AICandidate], mediainfo: Any) -> None:
        """记录规则使用情况。"""
        try:
            usage = self.get_data("usage_records") or []
            record = {
                "title": title,
                "path": path,
                "rules": [c.rule for c in candidates],
                "mediainfo_title": getattr(mediainfo, "title", ""),
                "timestamp": time.time()
            }
            usage.append(record)
            if len(usage) > 1000:
                usage = usage[-1000:]
            self.save_data("usage_records", usage)
        except Exception as exc:
            logger.error(f"[AI识别词] 记录使用情况失败: {exc}")

    # ==================== 待确认规则（先审后写模式） ====================

    def _add_pending_rules(self, title: str, path: str, candidates: List[AICandidate], guess: AIGuess, mediainfo: Any) -> None:
        """添加待确认规则。"""
        try:
            pending = self.get_data("pending_rules") or []
            for cand in candidates:
                pending.append({
                    "title": title,
                    "path": path,
                    "rule": cand.rule,
                    "confidence": cand.confidence,
                    "reason": cand.reason,
                    "guess": guess.model_dump(),
                    "mediainfo_title": getattr(mediainfo, "title", ""),
                    "timestamp": time.time()
                })
            self.save_data("pending_rules", pending)
        except Exception as exc:
            logger.error(f"[AI识别词] 添加待确认规则失败: {exc}")

    def _get_pending_rules(self) -> List[Dict[str, Any]]:
        """获取待确认规则列表。"""
        try:
            return self.get_data("pending_rules") or []
        except Exception:
            return []

    # ==================== API 端点 ====================

    async def api_stats(self, request):
        """获取识别词统计。"""
        stats = self._get_stats()
        return {"success": True, "data": stats}

    async def api_pending(self, request):
        """获取待确认规则。"""
        pending = self._get_pending_rules()
        return {"success": True, "data": {"count": len(pending), "pending": pending}}

    async def api_confirm(self, request):
        """确认写入规则。"""
        body = await request.json()
        rule = str(body.get("rule") or "").strip()
        if not rule:
            return {"success": False, "message": "缺少 rule 参数"}

        pending = self._get_pending_rules()
        target = None
        for i, p in enumerate(pending):
            if p.get("rule") == rule:
                target = pending.pop(i)
                break

        if not target:
            return {"success": False, "message": "未找到待确认规则"}

        # 写入
        cand = AICandidate(rule=rule, confidence=target.get("confidence", 0), reason=target.get("reason", ""))
        write_result = self._safe_write([cand])

        # 更新待确认列表
        self.save_data("pending_rules", pending)

        if write_result.get("success"):
            # 重新整理
            self._retrigger_transfer(target.get("path", ""), None, None)
            return {"success": True, "message": "已写入并触发整理"}
        else:
            return {"success": False, "message": write_result.get("message", "写入失败")}
