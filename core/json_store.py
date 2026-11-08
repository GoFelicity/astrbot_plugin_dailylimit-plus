"""
JSON 文件存储模块

替代 Redis 的持久化存储层,负责:
- 用户额度数据(user_data.json)
- 跨群/私聊共享用户额度
- 文件级加锁(线程 + 协程)确保并发安全
- 懒加载 + 立即落盘策略
"""

import asyncio
import datetime
import json
import os
import threading
from typing import Any


class JsonStore:
    """JSON 文件存储管理器"""

    # 群组共享计数存放在同一个 JSON 的保留键下,避免再引一份存储
    GROUPS_KEY = "__groups__"
    # 通用日计数器(Redis 不可用时的持久化后端),键名与 Redis 计数键一致
    COUNTERS_KEY = "__counters__"

    def __init__(self, plugin, file_path: str = "data/user_data.json"):
        """
        初始化 JSON 存储

        Args:
            plugin: 插件实例
            file_path: 存储文件相对路径
        """
        self.plugin = plugin
        self.logger = plugin.logger
        self.file_path = file_path
        # 线程锁,保护同步 IO
        self._thread_lock = threading.Lock()
        # 协程锁,保护跨事件循环
        self._async_lock: asyncio.Lock | None = None
        # 内存中的数据缓存
        self._data: dict[str, Any] = {}
        self._loaded = False

    def _ensure_async_lock(self) -> asyncio.Lock:
        """懒创建协程锁"""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _ensure_dir(self) -> None:
        """确保目录存在"""
        directory = os.path.dirname(self.file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    def _default_user(self) -> dict[str, Any]:
        """生成新用户的默认数据"""
        return {
            "daily_limit": self.plugin.config["limits"].get("default_daily_limit", 25),
            "remaining": self.plugin.config["limits"].get("default_daily_limit", 25),
            "last_reset": self._today_str(),
            "vip_level": "normal",
            "vip_expire": None,
            "processed_orders": [],
        }

    @staticmethod
    def _today_str() -> str:
        """获取今日日期字符串(YYYY-MM-DD)"""
        return datetime.date.today().strftime("%Y-%m-%d")

    def load(self) -> dict[str, Any]:
        """
        从文件加载数据(同步)
        """
        with self._thread_lock:
            if self._loaded:
                return self._data
            self._ensure_dir()
            if not os.path.exists(self.file_path):
                self._data = {}
                self._loaded = True
                return self._data
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raw = {}
                # 兼容旧数据,补全缺失字段
                for uid, info in raw.items():
                    raw[uid] = self._migrate_user(info)
                self._data = raw
            except (json.JSONDecodeError, OSError) as e:
                self.logger.log_warning("读取用户数据文件失败,使用空数据: {}", str(e))
                self._data = {}
            self._loaded = True
            return self._data

    def _migrate_user(self, info: dict[str, Any]) -> dict[str, Any]:
        """补全/迁移用户字段,确保结构完整"""
        default = self._default_user()
        if not isinstance(info, dict):
            return default
        for key, val in default.items():
            info.setdefault(key, val if not isinstance(val, list) else list(val))
        # 强制 last_reset 是字符串
        if not isinstance(info.get("last_reset"), str):
            info["last_reset"] = self._today_str()
        return info

    def save(self) -> None:
        """将数据写回文件(同步,带临时文件 + 原子替换)"""
        with self._thread_lock:
            self._ensure_dir()
            tmp_path = f"{self.file_path}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.file_path)
            except OSError as e:
                self.logger.log_error("写入用户数据文件失败: {}", str(e))

    def get_user(self, user_id: str | int) -> dict[str, Any]:
        """
        获取用户数据(不存在则初始化)

        Args:
            user_id: 用户 ID(QQ 号)

        Returns:
            用户数据字典
        """
        data = self.load()
        uid = str(user_id)
        if uid not in data:
            data[uid] = self._default_user()
            self.save()
        return data[uid]

    def update_user(self, user_id: str | int, updates: dict[str, Any]) -> dict[str, Any]:
        """
        更新用户数据(并落盘)

        Args:
            user_id: 用户 ID
            updates: 要更新的字段

        Returns:
            更新后的完整用户数据
        """
        data = self.load()
        uid = str(user_id)
        user = data.setdefault(uid, self._default_user())
        user.update(updates)
        self.save()
        return user

    def all_users(self) -> dict[str, Any]:
        """获取所有用户数据(浅拷贝),不含内部保留键"""
        reserved = {self.GROUPS_KEY, self.COUNTERS_KEY}
        return {
            uid: info for uid, info in self.load().items() if uid not in reserved
        }

    # ---------- 通用日计数器(Redis 的 JSON 回退后端) ----------

    def counter_period_date(self) -> str:
        """计数器的归属日期(与 Redis 键一致,遵循自定义重置时间)"""
        try:
            return self.plugin.redis_keys.get_reset_period_date()
        except Exception:
            return self._today_str()

    def today_counters(self) -> dict[str, int]:
        """返回当前重置周期内所有计数(key -> 次数,只含 >0 的)"""
        prefix = f"astrbot:daily_limit:{self.counter_period_date()}:"
        counters = self.load().get(self.COUNTERS_KEY) or {}
        result: dict[str, int] = {}
        for key, record in counters.items():
            if not str(key).startswith(prefix):
                continue
            if not isinstance(record, dict):
                continue
            try:
                value = int(record.get("count", 0))
            except (TypeError, ValueError):
                continue
            if value > 0:
                result[str(key)] = value
        return result

    def _prune_counters(self, data: dict[str, Any]) -> None:
        """只保留最近 7 个周期的计数,避免文件无限增长"""
        counters = data.get(self.COUNTERS_KEY)
        if not isinstance(counters, dict) or len(counters) <= 200:
            return
        def period_of(key: str) -> str:
            parts = str(key).split(":")
            return parts[2] if len(parts) > 2 else ""
        periods = sorted({period_of(k) for k in counters}, reverse=True)[:7]
        keep = set(periods)
        data[self.COUNTERS_KEY] = {
            k: v for k, v in counters.items() if period_of(k) in keep
        }

    def get_counter(self, key: str) -> int:
        """读取当日计数器(跨天自动归零)"""
        record = (self.load().get(self.COUNTERS_KEY) or {}).get(str(key))
        if not isinstance(record, dict) or record.get("date") != self._today_str():
            return 0
        try:
            return max(0, int(record.get("count", 0)))
        except (TypeError, ValueError):
            return 0

    def incr_counter(self, key: str) -> int:
        """当日计数器 +1 并返回新值"""
        data = self.load()
        counters = data.setdefault(self.COUNTERS_KEY, {})
        today = self.counter_period_date()
        record = counters.get(str(key))
        if not isinstance(record, dict) or record.get("date") != today:
            record = {"date": today, "count": 0}
            counters[str(key)] = record
        try:
            record["count"] = max(0, int(record.get("count", 0))) + 1
        except (TypeError, ValueError):
            record["count"] = 1
        self._prune_counters(data)
        self.save()
        return record["count"]

    def clear_counters(self) -> None:
        """清空所有日计数器(重置用)"""
        data = self.load()
        if data.get(self.COUNTERS_KEY):
            data[self.COUNTERS_KEY] = {}
            self.save()

    # ---------- 群组共享计数(Redis 不可用时的持久化回退) ----------

    def get_group_usage(self, group_id: str | int) -> int:
        """获取群组当日共享使用次数"""
        return self.get_counter(self._group_counter_key(group_id))

    def increment_group_usage(self, group_id: str | int) -> int:
        """增加群组当日共享使用次数,返回增加后的次数"""
        return self.incr_counter(self._group_counter_key(group_id))

    @staticmethod
    def _group_counter_key(group_id: str | int) -> str:
        return f"group:{group_id}"

    async def aget_user(self, user_id: str | int) -> dict[str, Any]:
        """异步获取用户数据(异步锁)"""
        async with self._ensure_async_lock():
            return self.get_user(user_id)

    async def aupdate_user(self, user_id: str | int, updates: dict[str, Any]) -> dict[str, Any]:
        """异步更新用户数据"""
        async with self._ensure_async_lock():
            return self.update_user(user_id, updates)

    def add_processed_order(self, user_id: str | int, order_id: str) -> bool:
        """
        记录已处理订单(用于爱发电 Webhook 幂等)

        Returns:
            True 表示新加入,False 表示已存在
        """
        data = self.load()
        uid = str(user_id)
        user = data.setdefault(uid, self._default_user())
        orders: list = user.setdefault("processed_orders", [])
        if order_id in orders:
            return False
        orders.append(order_id)
        # 限制数量,防止无限增长
        if len(orders) > 500:
            user["processed_orders"] = orders[-500:]
        self.save()
        return True

    def is_order_processed(self, user_id: str | int, order_id: str) -> bool:
        """检查订单是否已处理"""
        data = self.load()
        uid = str(user_id)
        user = data.get(uid)
        if not user:
            return False
        return order_id in user.get("processed_orders", [])
