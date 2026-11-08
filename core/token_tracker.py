"""
Plus 版: 单用户 token 消耗跟踪

存到 JsonStore (data/token_stats.json),与 _get_user_usage 用的
同源 JSON 文件保持风格一致(不依赖 Redis)。

数据结构:
{
  "users": {
    "<user_id>": {
      "daily": {
        "<YYYY-MM-DD>": {
          "input": 0,           # 输入 token 总数 (input_other + input_cached)
          "output": 0,          # 输出 token
          "total": 0,           # 总 token
          "calls": 0            # LLM 调用次数
        }
      },
      "monthly": {
        "<YYYY-MM>": {          # 整月汇总
          "input": 0,
          "output": 0,
          "total": 0,
          "calls": 0
        }
      }
    }
  }
}
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path
from typing import Any


class TokenTracker:
    """单用户 token 跟踪器 (文件持久化,线程安全)"""

    DEFAULT_FILENAME = "token_stats.json"

    def __init__(self, plugin, data_dir: str | None = None):
        self.plugin = plugin
        self.logger = plugin.logger if hasattr(plugin, "logger") else None
        self._lock = threading.Lock()

        # 解析数据文件路径
        if data_dir is None:
            data_dir = self._resolve_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        self.data_file = os.path.join(data_dir, self.DEFAULT_FILENAME)

        # 内存缓存(避免每次都读文件)
        self._cache: dict[str, Any] | None = None

    def _resolve_data_dir(self) -> str:
        """优先用 AstrBot 提供的 get_data_path(),否则回退到插件内 data/"""
        try:
            if hasattr(self.plugin, "get_data_path"):
                return self.plugin.get_data_path()
        except Exception:
            pass
        return os.path.join(os.path.dirname(__file__), "..", "data")

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _load(self) -> dict[str, Any]:
        """读文件 → dict(线程安全)"""
        with self._lock:
            if self._cache is not None:
                return self._cache
            if not os.path.exists(self.data_file):
                self._cache = {"users": {}}
                return self._cache
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or "users" not in data:
                    data = {"users": {}}
            except (OSError, json.JSONDecodeError) as e:
                if self.logger:
                    self.logger.log_warning("token_stats 读取失败,使用空数据: {}", str(e))
                data = {"users": {}}
            self._cache = data
            return data

    def _flush(self) -> None:
        """写回磁盘(线程安全)"""
        with self._lock:
            if self._cache is None:
                return
            tmp = self.data_file + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.data_file)
            except OSError as e:
                if self.logger:
                    self.logger.log_error("token_stats 写入失败: {}", str(e))

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def record(
        self,
        user_id: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> None:
        """记录一次 LLM 调用的 token 消耗(单用户维度)"""
        if not user_id:
            return
        try:
            input_total = int(prompt_tokens) + int(cached_tokens)
            output_total = int(completion_tokens)
            if input_total < 0:
                input_total = 0
            if output_total < 0:
                output_total = 0
            call_total = input_total + output_total
            if call_total == 0:
                # 没有 token 也要记 calls,避免 /my 显示"今日 0 次"
                pass
        except (TypeError, ValueError):
            return

        data = self._load()
        users = data.setdefault("users", {})
        user = users.setdefault(str(user_id), {"daily": {}, "monthly": {}})

        today = datetime.date.today().strftime("%Y-%m-%d")
        month = datetime.date.today().strftime("%Y-%m")

        # 今日
        d = user["daily"].setdefault(
            today, {"input": 0, "output": 0, "total": 0, "calls": 0}
        )
        d["input"] += input_total
        d["output"] += output_total
        d["total"] += call_total
        d["calls"] += 1

        # 本月
        m = user["monthly"].setdefault(
            month, {"input": 0, "output": 0, "total": 0, "calls": 0}
        )
        m["input"] += input_total
        m["output"] += output_total
        m["total"] += call_total
        m["calls"] += 1

        self._flush()

    def get_today(self, user_id: str) -> dict[str, int]:
        """取今日 token,缺失字段返回 0"""
        return self._get_bucket(user_id, datetime.date.today().strftime("%Y-%m"), "daily")

    def get_month(self, user_id: str) -> dict[str, int]:
        """取本月 token,缺失字段返回 0"""
        return self._get_bucket(user_id, datetime.date.today().strftime("%Y-%m"), "monthly")

    def _get_bucket(self, user_id: str, key: str, scope: str) -> dict[str, int]:
        data = self._load()
        bucket = (
            data.get("users", {})
            .get(str(user_id), {})
            .get(scope, {})
            .get(key, {})
        )
        return {
            "input": int(bucket.get("input", 0)),
            "output": int(bucket.get("output", 0)),
            "total": int(bucket.get("total", 0)),
            "calls": int(bucket.get("calls", 0)),
        }

    def reset_user(self, user_id: str) -> bool:
        """重置某用户所有 token 记录(管理员命令)"""
        with self._lock:
            data = self._load()
            if str(user_id) in data.get("users", {}):
                del data["users"][str(user_id)]
                self._flush()
                return True
            return False

    def stats_summary(self) -> dict[str, int]:
        """全局统计概览(展示在管理面板)"""
        data = self._load()
        users = data.get("users", {})
        total_input = 0
        total_output = 0
        total_calls = 0
        for u in users.values():
            today = datetime.date.today().strftime("%Y-%m-%d")
            d = u.get("daily", {}).get(today, {})
            total_input += int(d.get("input", 0))
            total_output += int(d.get("output", 0))
            total_calls += int(d.get("calls", 0))
        return {
            "active_users": len(users),
            "today_input": total_input,
            "today_output": total_output,
            "today_total": total_input + total_output,
            "today_calls": total_calls,
        }
