"""
VIP 订阅管理模块

负责:
- 根据 VIP 等级返回每日 LLM 调用额度
- 跨天懒重置(同时降级过期会员)
- 会员升级/降级接口(供爱发电 Webhook 调用)
- 跨群/私聊共享额度(按用户 QQ 号计算)

Plus 版订阅档: normal / monthly(月度) / quarterly(季度)
"""

import datetime
from typing import Any


class VipManager:
    """VIP 等级与每日额度管理"""

    LEVEL_NORMAL = "normal"
    LEVEL_MONTHLY = "monthly"
    LEVEL_QUARTERLY = "quarterly"  # 90 天

    def __init__(self, plugin, json_store):
        """
        Args:
            plugin: 插件实例
            json_store: JsonStore 实例
        """
        self.plugin = plugin
        self.logger = plugin.logger
        self.config = plugin.config
        self.store = json_store

    # ---------- 额度配置 ----------

    @staticmethod
    def _safe_int(value, default: int) -> int:
        """容错地把任意值转为 int;失败时记录警告并返回 default,避免崩溃"""
        if value is None or value == "":
            return default
        try:
            return max(0, min(99999, int(value)))
        except (TypeError, ValueError):
            return default

    def _limits_config(self) -> dict[str, int]:
        """读取各 VIP 等级额度配置(容错: 任何字段损坏/格式错都回退到默认值)"""
        vip_cfg = self.config.get("vip", {})
        return {
            self.LEVEL_NORMAL: self._safe_int(vip_cfg.get("normal_daily_limit"), 25),
            self.LEVEL_MONTHLY: self._safe_int(vip_cfg.get("monthly_daily_limit"), 150),
            self.LEVEL_QUARTERLY: self._safe_int(vip_cfg.get("quarterly_daily_limit"), 200),
        }

    def get_daily_limit_by_level(self, level: str) -> int:
        """根据等级返回每日额度"""
        limits = self._limits_config()
        return limits.get(level, limits[self.LEVEL_NORMAL])

    # ---------- 核心接口 ----------

    def check_and_reset(self, user_id: str | int) -> dict[str, Any]:
        """
        懒检查并重置用户每日额度

        触发时机: 每次 LLM 请求前。
        行为:
        1. 若 today > last_reset:
            - 月度/季度会员若已过期 -> 降级为普通用户
            - 按当前等级重置 daily_limit 和 remaining
            - 更新 last_reset
        2. 若配置中默认额度变了,VIP 等级变化等 -> 同步 daily_limit 字段
        3. 兼容老数据: 若 vip_level == "permanent" 自动降级为 normal(本版本已不支持永久档)

        Returns:
            最新用户数据字典
        """
        today = datetime.date.today().strftime("%Y-%m-%d")
        user = self.store.get_user(user_id)

        # 兼容老数据: 历史遗留的 permanent 标记全部降级为 normal
        if user.get("vip_level") == "permanent":
            self.logger.log_info(
                "用户 {} 历史永久档已不再支持,自动降级为普通用户", user_id
            )
            user["vip_level"] = self.LEVEL_NORMAL
            user["vip_expire"] = None

        # 月度/季度会员过期检查
        if user.get("vip_level") in (self.LEVEL_MONTHLY, self.LEVEL_QUARTERLY):
            expire = user.get("vip_expire")
            if expire and expire < today:
                level_label = {
                    self.LEVEL_MONTHLY: "月度",
                    self.LEVEL_QUARTERLY: "季度",
                }.get(user["vip_level"], "订阅")
                self.logger.log_info(
                    "用户 {} {}会员已过期(到期:{}),降级为普通用户",
                    user_id, level_label, expire,
                )
                user["vip_level"] = self.LEVEL_NORMAL
                user["vip_expire"] = None

        # 跨天重置
        if user.get("last_reset") != today:
            level = user.get("vip_level", self.LEVEL_NORMAL)
            new_limit = self.get_daily_limit_by_level(level)
            user["daily_limit"] = new_limit
            user["remaining"] = new_limit
            user["last_reset"] = today
            self.store.save()
        else:
            # 当日: 同步额度(若等级变了或配置改了)
            level = user.get("vip_level", self.LEVEL_NORMAL)
            expected = self.get_daily_limit_by_level(level)
            if user.get("daily_limit") != expected:
                # 重新基准化,保留已用次数
                # 修复:用 OLD limit - OLD remaining 算 used(不是 new - old)
                old_limit = int(user.get("daily_limit", expected))
                old_remaining = int(user.get("remaining", 0))
                used = max(0, old_limit - old_remaining)
                user["daily_limit"] = expected
                user["remaining"] = max(0, expected - used)
                self.store.save()
        return user

    def get_user_limit(self, user_id: str | int, fallback: int | None = None) -> int:
        """获取用户当前每日额度(返回 int)"""
        user = self.check_and_reset(user_id)
        return int(user.get("daily_limit", fallback or 25))

    def get_user_remaining(self, user_id: str | int) -> int:
        """获取用户当前剩余额度"""
        user = self.check_and_reset(user_id)
        return int(user.get("remaining", 0))

    def consume(self, user_id: str | int) -> int:
        """
        消耗一次额度

        Returns:
            消费后的剩余次数(< 0 表示已用尽,>= 0 表示剩余)
        """
        user = self.check_and_reset(user_id)
        remaining = int(user.get("remaining", 0))
        if remaining <= 0:
            return remaining
        remaining -= 1
        user["remaining"] = remaining
        self.store.save()
        return remaining

    # ---------- VIP 变更接口(供 Webhook 调用) ----------

    @staticmethod
    def _calc_used(user: dict[str, Any]) -> int:
        """根据旧 daily_limit / remaining 计算已用次数"""
        old_limit = int(user.get("daily_limit", 0))
        remaining = int(user.get("remaining", 0))
        if old_limit <= 0:
            return 0
        used = max(0, old_limit - remaining)
        # 兼容老数据的"超大量"档位(历史 permanent=99999),不视为有已用
        if old_limit >= 1000:
            return 0
        return used

    def _upgrade_to_paid(self, user_id: str | int, level: str, days: int) -> dict[str, Any]:
        """
        通用升级到付费档(月度/季度)

        Args:
            user_id: 用户标识
            level: 目标等级(LEVEL_MONTHLY / LEVEL_QUARTERLY)
            days: 到期天数(30 / 90)
        """
        user = self.store.get_user(user_id)
        today = datetime.date.today()
        current_expire = user.get("vip_expire")
        # 若已有未到期付费会员,在到期日基础上叠加,否则从今天开始
        if current_expire:
            try:
                base = datetime.datetime.strptime(current_expire, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                base = today
            if base < today:
                base = today
        else:
            base = today
        new_expire = (base + datetime.timedelta(days=days)).strftime("%Y-%m-%d")

        new_limit = self.get_daily_limit_by_level(level)
        used = self._calc_used(user)
        return self.store.update_user(
            user_id,
            {
                "vip_level": level,
                "vip_expire": new_expire,
                "daily_limit": new_limit,
                "remaining": max(0, new_limit - used),
                "last_reset": today.strftime("%Y-%m-%d"),
            },
        )

    def upgrade_to_monthly(self, user_id: str | int, days: int = 30) -> dict[str, Any]:
        """升级为月度会员(默认 30 天)"""
        return self._upgrade_to_paid(user_id, self.LEVEL_MONTHLY, days)

    def upgrade_to_quarterly(self, user_id: str | int, days: int = 90) -> dict[str, Any]:
        """升级为季度会员(默认 90 天)"""
        return self._upgrade_to_paid(user_id, self.LEVEL_QUARTERLY, days)

    def downgrade_to_normal(self, user_id: str | int) -> dict[str, Any]:
        """
        降级为普通用户
        """
        user = self.store.get_user(user_id)
        new_limit = self.get_daily_limit_by_level(self.LEVEL_NORMAL)
        used = self._calc_used(user)
        return self.store.update_user(
            user_id,
            {
                "vip_level": self.LEVEL_NORMAL,
                "vip_expire": None,
                "daily_limit": new_limit,
                "remaining": max(0, new_limit - used),
            },
        )

    # ---------- 展示 ----------

    def describe_user(self, user_id: str | int) -> dict[str, Any]:
        """
        描述用户 VIP 与额度信息(/my 命令用)
        """
        user = self.check_and_reset(user_id)
        return {
            "user_id": str(user_id),
            "vip_level": user.get("vip_level", self.LEVEL_NORMAL),
            "vip_expire": user.get("vip_expire"),
            "daily_limit": int(user.get("daily_limit", 0)),
            "remaining": int(user.get("remaining", 0)),
            "last_reset": user.get("last_reset"),
        }
