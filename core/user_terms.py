"""
用户条款签署控制(内置子插件)

移植自 astrbot_plugin_user_terms(AlanBacker),改为内置模块 + JSON 存储,
不再依赖 SQLite 与独立插件目录。行为对齐上游:

- 用户作用域(私聊)与群组作用域各有一套生效范围(全部 / 手动 SID 列表)
- 判定顺序: 禁用列表 → 豁免列表 → 手动 SID 列表 → 生效范围
- 首次触发只发条款并记 prompted;第二次发送同意语才真正签署
- 条款正文变更(发布新条款)后需要重新签署
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

DEFAULT_TERMS_TEXT = (
    "1. 使用机器人时请遵守所在平台与群组规则。\n"
    "2. 不要发送违法、侵权、骚扰或滥用内容。\n"
    "3. 管理员可根据实际情况停止服务或清理违规使用记录。"
)
DEFAULT_ACCEPT_PHRASE = "我已知晓并同意遵守条款所有内容"
DEFAULT_PROMPT = (
    "请同意以下条款来得到使用权限：\n{terms}\n\n"
    "当前需要签署：{scopes}\n请阅读后回复“{accept_phrase}”；回复“拒绝”将无法继续使用。"
)
DEFAULT_PREMATURE = (
    "请先阅读以下用户条款，再回复“{accept_phrase}”完成签署：\n{terms}\n\n"
    "当前需要签署：{scopes}"
)
DEFAULT_ACCEPTED = "已记录你对 {scopes} 的用户条款同意状态，现在可以继续使用。"
DEFAULT_REJECTED = "你已拒绝用户条款，将无法继续使用机器人。"
DEFAULT_INVALID = "请回复“{accept_phrase}”完成签署，或回复“拒绝”放弃使用。"
DEFAULT_DISABLED = "该用户或群组已被禁用，请联系开发者处理。"

STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"


class UserTerms:
    """条款签署状态管理 + 请求拦截判定"""

    STORE_KEY = "__terms__"

    def __init__(self, plugin, data_file: str | None = None):
        self.plugin = plugin
        self.logger = getattr(plugin, "logger", None)
        self.config = plugin.config
        self._lock = threading.Lock()
        if data_file is None:
            base = os.path.join(os.path.dirname(__file__), "..", "data")
            if hasattr(plugin, "get_data_path"):
                try:
                    base = plugin.get_data_path()
                except Exception:
                    pass
            data_file = os.path.join(base, "user_terms.json")
        self.data_file = data_file
        self._cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # 配置与存储
    # ------------------------------------------------------------------ #
    def _cfg(self, key: str, default: Any = None) -> Any:
        section = self.config.get("user_terms", {}) or {}
        value = section.get(key, default)
        return default if value is None else value

    def _sid_list(self, key: str) -> list[str]:
        raw = self._cfg(key, [])
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.replace(",", "\n").split("\n")]
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def terms_text(self) -> str:
        return str(self._cfg("terms_text", DEFAULT_TERMS_TEXT) or DEFAULT_TERMS_TEXT)

    def accept_phrase(self) -> str:
        return str(self._cfg("accept_phrase", DEFAULT_ACCEPT_PHRASE))

    def revision(self) -> str:
        """条款正文指纹: 正文一改就要重新签署"""
        return hashlib.sha1(self.terms_text().encode("utf-8")).hexdigest()[:16]

    def _load(self) -> dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            if not os.path.exists(self.data_file):
                self._cache = {"revision": self.revision(), "records": {}}
                return self._cache
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data.setdefault("records", {})
            data.setdefault("revision", self.revision())
            self._cache = data
            return self._cache

    def _flush(self) -> None:
        data = self._load()
        directory = os.path.dirname(self.data_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.data_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.data_file)
        except OSError as exc:
            if self.logger:
                self.logger.log_error("user_terms 写入失败: {}", str(exc))

    # ------------------------------------------------------------------ #
    # 作用域判定
    # ------------------------------------------------------------------ #
    def _applicable_scope(self, user_id: str, group_id: str | None) -> tuple[str, str] | None:
        """返回 (scope_id, scope_label);None 表示不需要签署"""
        if group_id is not None:
            scope_id = f"group:{group_id}"
            mode = str(self._cfg("group_scope_mode", "all"))
            if scope_id in self._sid_set("disabled_group_sids"):
                return ("disabled", scope_id)
            if scope_id in self._sid_set("exempt_group_sids"):
                return None
            listed = scope_id in self._sid_set("group_sids")
            if mode == "all" or listed:
                return (scope_id, f"群组 {group_id}")
            return None

        scope_id = f"user:{user_id}"
        mode = str(self._cfg("user_scope_mode", "all"))
        if scope_id in self._sid_set("disabled_user_sids"):
            return ("disabled", scope_id)
        if scope_id in self._sid_set("exempt_user_sids"):
            return None
        listed = scope_id in self._sid_set("user_sids")
        if mode == "all" or listed:
            return (scope_id, f"用户 {user_id}")
        return None

    def _sid_set(self, key: str) -> set[str]:
        return {sid for sid in self._sid_list(key)}

    def _record(self, scope_id: str) -> dict[str, Any]:
        return self._load()["records"].get(scope_id, {})

    def _mark(self, scope_id: str, **updates: Any) -> None:
        records = self._load()["records"]
        record = records.setdefault(scope_id, {})
        record.update(updates)
        self._flush()

    def _render(self, template: str, scope_label: str, **extra: Any) -> str:
        values = {
            "terms": self.terms_text(),
            "scopes": scope_label,
            "accept_phrase": self.accept_phrase(),
        }
        values.update(extra)
        try:
            return template.format(**values)
        except (KeyError, ValueError):
            return template

    # ------------------------------------------------------------------ #
    # 拦截判定
    # ------------------------------------------------------------------ #
    def check(self, event) -> tuple[bool, str | None]:
        """返回 (是否放行, 需要回复的提示文本)"""
        if not self._cfg("enabled", True):
            return True, None

        user_id = str(event.get_sender_id() or "")
        group_id = event.get_group_id()
        group_id = str(group_id) if group_id is not None else None
        if not user_id:
            return True, None

        scope = self._applicable_scope(user_id, group_id)
        if scope is None:
            return True, None

        scope_id, scope_label = scope
        if scope_id == "disabled":
            text = self._render(
                str(self._cfg("disabled_message", DEFAULT_DISABLED)), scope_label
            )
            return False, text

        record = self._record(scope_id)
        message = str(getattr(event, "message_str", "") or "").strip()
        accept_words = self._sid_list("accept_keywords") or [self.accept_phrase()]
        reject_words = self._sid_list("reject_keywords") or ["拒绝"]

        accepted = (
            record.get("status") == STATUS_ACCEPTED
            and record.get("revision") == self.revision()
        )
        if accepted:
            return True, None

        prompted = bool(record.get("prompted"))

        if message in accept_words:
            if not prompted:
                # 首次直接发同意语: 先发条款,不直接签署
                self._mark(scope_id, prompted=True, revision=self.revision())
                text = self._render(
                    str(self._cfg("premature_accept_message", DEFAULT_PREMATURE)),
                    scope_label,
                )
                return False, text
            self._mark(
                scope_id,
                status=STATUS_ACCEPTED,
                revision=self.revision(),
                prompted=True,
            )
            text = self._render(
                str(self._cfg("accepted_message", DEFAULT_ACCEPTED)), scope_label
            )
            return False, text

        if message in reject_words:
            self._mark(scope_id, status=STATUS_REJECTED, prompted=True)
            text = self._render(
                str(self._cfg("rejected_message", DEFAULT_REJECTED)), scope_label
            )
            return False, text

        template = (
            str(self._cfg("invalid_message", DEFAULT_INVALID))
            if prompted
            else str(self._cfg("prompt_message", DEFAULT_PROMPT))
        )
        self._mark(scope_id, prompted=True, revision=self.revision())
        return False, self._render(template, scope_label)

    # ------------------------------------------------------------------ #
    # 管理接口
    # ------------------------------------------------------------------ #
    def describe(self, scope_id: str) -> dict[str, Any]:
        record = self._record(scope_id)
        return {
            "scope": scope_id,
            "status": record.get("status", "unsigned"),
            "prompted": bool(record.get("prompted")),
            "signed": (
                record.get("status") == STATUS_ACCEPTED
                and record.get("revision") == self.revision()
            ),
            "revision": record.get("revision", ""),
        }

    def reset(self, scope_id: str) -> bool:
        records = self._load()["records"]
        if scope_id in records:
            records.pop(scope_id)
            self._flush()
            return True
        return False

    def publish(self, terms_text: str) -> None:
        """发布新条款: 更新正文 + 记录新版本指纹(所有已签署记录需重新确认)"""
        self.config.setdefault("user_terms", {})["terms_text"] = terms_text
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001 - 保存失败不影响内存生效
            if self.logger:
                self.logger.log_error("user_terms 保存配置失败: {}", str(exc))
        self._load()["revision"] = self.revision()
        self._flush()

    def stats(self) -> dict[str, int]:
        records = self._load()["records"]
        signed = sum(
            1
            for record in records.values()
            if record.get("status") == STATUS_ACCEPTED
            and record.get("revision") == self.revision()
        )
        return {"total": len(records), "signed": signed}
