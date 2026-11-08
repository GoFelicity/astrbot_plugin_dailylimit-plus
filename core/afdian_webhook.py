"""
爱发电(afdian) Webhook 处理模块

功能:
- 解析爱发电 Webhook 回调 JSON
- 提取备注中的 QQ 号(格式: qq=123456789)
- 幂等处理订单(基于 order_id)
- 调用 VipManager 升级月度/季度会员
- 同时提供 Flask 视图函数(挂载到现有 web_server)
  和独立 aiohttp 服务的启动方式(可选)
"""

import asyncio
import datetime
import hmac
import hashlib
import json
import re
import time
from typing import Any

try:
    import aiohttp
except ImportError:
    aiohttp = None  # 仅有 query_order 会用到,Webhook 主体处理不依赖

QQ_RE = re.compile(r"qq(?:\s*=|%3D)\s*(\d+)", re.IGNORECASE)


def extract_qq(remark: str) -> str | None:
    """从爱发电订单备注中提取 QQ 号"""
    if not remark:
        return None
    m = QQ_RE.search(remark)
    if m:
        return m.group(1)
    # 兜底: 纯数字视为 QQ
    stripped = remark.strip()
    if stripped.isdigit() and 5 <= len(stripped) <= 12:
        return stripped
    return None


class AfdianWebhookHandler:
    """
    爱发电 Webhook 处理器

    调用方需传入:
        vip_manager: VipManager 实例
        config: 插件配置(用于读取 plan_id 映射与 token)
    """

    AIFADIAN_API_BASE = "https://ifdian.net/api/open"

    def __init__(self, plugin, vip_manager):
        """
        Args:
            plugin: 插件实例
            vip_manager: VipManager 实例
        """
        self.plugin = plugin
        self.logger = plugin.logger
        self.config = plugin.config
        self.vip = vip_manager
        # 主动核验缓存: qq -> last_check_timestamp
        # 避免同一用户每次调用都打爱发电 API
        self._check_cache: dict[str, float] = {}
        # 主动核验结果日志: qq -> {new_orders: int, upgraded: bool, time: float}
        self._last_result: dict[str, dict[str, Any]] = {}

    # ---------- 工具: API 签名 ----------

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    @classmethod
    def _sign(cls, token: str, params: str, ts: int, user_id: str) -> str:
        return cls._md5(f"{token}params{params}ts{ts}user_id{user_id}")

    async def query_order(
        self, out_trade_no: str
    ) -> dict[str, Any] | None:
        """
        通过爱发电开放 API 查询订单(用于 webhook 之外的人工补单场景)

        Returns:
            订单字典,失败返回 None
        """
        afdian_cfg = self.config.get("afdian", {})
        token = afdian_cfg.get("user_token", "")
        user_id = str(afdian_cfg.get("user_id", ""))
        if not token or not user_id:
            self.logger.log_warning("爱发电 API 配置不完整,无法查询订单")
            return None
        if aiohttp is None:
            self.logger.log_warning("aiohttp 未安装,无法调用爱发电 API")
            return None
        params_obj = {"out_trade_no": out_trade_no}
        # 与爱发电官方/TS 客户端一致: 不传 ensure_ascii=False,默认 ASCII 转义
        # (中文会变 \uXXXX),否则服务端签名校验会因字符串比对失败
        params = json.dumps(params_obj)
        ts = int(time.time())
        sign = self._sign(token, params, ts, user_id)
        body = json.dumps(
            {"user_id": user_id, "params": params, "ts": ts, "sign": sign},
        )
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.AIFADIAN_API_BASE}/query-order",
                    data=body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    data = await resp.json()
            if data.get("ec") != 200:
                self.logger.log_warning("爱发电查询订单失败: {}", data)
                return None
            items = data.get("data", {}).get("list", []) or []
            for item in items:
                if item.get("out_trade_no") == out_trade_no:
                    return item
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.logger.log_error("爱发电 API 请求异常: {}", str(e))
            return None

    # ---------- Webhook 入口 ----------

    def verify_signature(self, payload: dict[str, Any]) -> bool:
        """
        校验爱发电 Webhook 签名(若配置了 token 则启用校验)

        爱发电 v1 Webhook 直接以明文 JSON 推送,不做服务端签名,
        推荐做法是: 仅暴露 Webhook URL(地址保密) + 配置 token 二次校验。
        此处保留扩展位。
        """
        afdian_cfg = self.config.get("afdian", {})
        token = afdian_cfg.get("webhook_token", "")
        if not token:
            return True  # 未配置 token, 跳过校验
        # 兼容爱发电新版可能带的 sign 字段
        if "sign" in payload:
            provided = str(payload.get("sign", ""))
            # 这里按约定把 token 当作简易 HMAC(若有自定义需求可改)
            expected = hmac.new(
                token.encode("utf-8"),
                json.dumps(payload, sort_keys=True).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(provided, expected)
        return True

    def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        处理 Webhook 负载(同步版本,内部加锁)

        Args:
            payload: 爱发电 Webhook POST 的 JSON

        Returns:
            处理结果 {"ok": bool, "reason": str, "data": dict | None}
        """
        try:
            if not self.verify_signature(payload):
                return {"ok": False, "reason": "签名校验失败"}

            # Plus 版: 爱发电 v1 实际 payload 是嵌套结构 {data: {type, order: {...}}},
            # 但老版本/部分场景下也可能是扁平字段,做兼容。
            data_obj = payload.get("data") or {}
            order_obj = (
                data_obj.get("order")
                if isinstance(data_obj, dict)
                else {}
            ) or {}

            # 优先读嵌套 (爱发电官方格式),再退回顶层 (兼容老/自定义)
            order_id = str(
                order_obj.get("out_trade_no")
                or order_obj.get("order_id")
                or payload.get("order_id")
                or payload.get("out_trade_no")
                or ""
            )
            remark = str(
                order_obj.get("remark")
                or order_obj.get("custom_order_id")
                or payload.get("remark")
                or payload.get("custom_order_id")
                or ""
            )
            plan_id = str(
                order_obj.get("plan_id")
                or payload.get("plan_id")
                or ""
            )
            custom_order_id = str(
                order_obj.get("custom_order_id")
                or payload.get("custom_order_id")
                or ""
            )
            try:
                status = int(order_obj.get("status", payload.get("status", 2)))
            except (TypeError, ValueError):
                status = 2  # 2 = 已支付

            if not order_id:
                return {"ok": False, "reason": "缺少 order_id"}
            if status != 2:
                return {"ok": True, "reason": "非已支付订单,忽略"}

            # QQ 号双保险: 优先 remark,fallback 到 custom_order_id
            qq = extract_qq(remark) or extract_qq(custom_order_id)
            if not qq:
                # 探测请求兜底: 爱发电后台"开发者发送测试"发的 payload
                # remark/custom_order_id 都为空,直接返 ec: 200 让 URL 通过保存
                if not remark and not custom_order_id:
                    return {
                        "ok": True,
                        "reason": "探测通过:空 remark 视为后台测试",
                        "data": {"order_id": order_id, "probe": True},
                    }
                return {"ok": False, "reason": f"备注中未找到 QQ 号(remark={remark!r}, custom_order_id={custom_order_id!r})"}

            # 幂等: 同一用户同一订单只处理一次
            if self.vip.store.is_order_processed(qq, order_id):
                return {"ok": True, "reason": "订单已处理(幂等)", "data": {"qq": qq, "order_id": order_id}}

            # 标记订单已处理
            self.vip.store.add_processed_order(qq, order_id)

            # 映射 plan_id -> 月度 / 季度
            afdian_cfg = self.config.get("afdian", {})
            plan_map = {
                str(afdian_cfg.get("monthly_plan_id", "")): ("monthly", 30),
                str(afdian_cfg.get("quarterly_plan_id", "")): ("quarterly", 90),
            }
            mapping = plan_map.get(plan_id)

            # 自动识别: 备注包含 "季度" 字样时升级为季度
            is_quarterly_kw = "季度" in remark

            if mapping is None:
                if is_quarterly_kw:
                    level = "quarterly"
                else:
                    # 默认按月度处理
                    level = "monthly"
            else:
                level, days = mapping

            if level == "quarterly":
                user = self.vip.upgrade_to_quarterly(qq, days=days or 90)
            else:
                user = self.vip.upgrade_to_monthly(qq, days=days or 30)

            self.logger.log_info(
                "爱发电订单处理成功: qq={} order={} plan={} -> level={} expire={}",
                qq,
                order_id,
                plan_id,
                user.get("vip_level"),
                user.get("vip_expire"),
            )
            return {
                "ok": True,
                "reason": "ok",
                "data": {
                    "qq": qq,
                    "order_id": order_id,
                    "vip_level": user.get("vip_level"),
                    "vip_expire": user.get("vip_expire"),
                    "daily_limit": user.get("daily_limit"),
                },
            }
        except Exception as e:
            self.logger.log_error("爱发电 Webhook 处理异常: {}", str(e))
            return {"ok": False, "reason": f"内部错误: {e}"}

    # ------------------------------------------------------------------
    # 主动核验模式(用户付款后,下次调用时主动拉订单核验)
    # ------------------------------------------------------------------

    async def _api_query_orders(self, page: int = 1, per_page: int = 50) -> dict[str, Any] | None:
        """
        调爱发电 query-order API 拉一页订单

        Returns:
            解析后的 data 字典(含 list/total_page/total_count),失败返回 None
        """
        afdian_cfg = self.config.get("afdian", {})
        token = afdian_cfg.get("user_token", "")
        user_id = str(afdian_cfg.get("user_id", ""))
        if not token or not user_id:
            return None
        if aiohttp is None:
            self.logger.log_warning("aiohttp 未安装,无法调用爱发电 API")
            return None

        params_obj = {"page": page, "per_page": max(1, min(100, per_page))}
        # 与爱发电官方/TS 客户端一致: 默认 ASCII 转义 (中文 → \uXXXX)
        params = json.dumps(params_obj)
        ts = int(time.time())
        sign = self._sign(token, params, ts, user_id)
        body = json.dumps(
            {"user_id": user_id, "params": params, "ts": ts, "sign": sign},
        )
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.AIFADIAN_API_BASE}/query-order",
                    data=body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    data = await resp.json()
            if data.get("ec") != 200:
                self.logger.log_warning("爱发电 query-order 失败: ec={} em={}",
                                        data.get("ec"), data.get("em"))
                return None
            return data.get("data") or {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.logger.log_error("爱发电 query-order 网络异常: {}", str(e))
            return None
        except Exception as e:
            self.logger.log_error("爱发电 query-order 异常: {}", str(e))
            return None

    @staticmethod
    def _order_matches_qq(order: dict[str, Any], qq: str) -> bool:
        """
        判断订单是否属于该 QQ 号

        订单的 custom_order_id 由 URL 自动填充,通常形如:
            "qq=123456789"
        也支持旧版 remark 字段(防呆)
        """
        target = f"qq={qq}"
        custom = str(order.get("custom_order_id") or "")
        remark = str(order.get("remark") or "")
        return target in custom or target in remark

    async def verify_user_payment(
        self,
        qq: str,
        force: bool = False,
        cache_seconds: int | None = None,
    ) -> dict[str, Any]:
        """
        主动核验用户是否已支付爱发电订单(无 Webhook 模式)

        流程:
        1. 拉最近 1 页订单(默认 50 条,够 24 小时内的新订单)
        2. 过滤: status=2(已支付) && custom_order_id 包含 qq={qq}
        3. 去重: processed_orders 列表中已有的跳过
        4. 升级: 月度/永久(根据 plan_id 映射)
        5. 缓存: 同一用户默认 5 分钟内不重复查

        Args:
            qq: 用户 QQ 号
            force: 强制核验,忽略缓存
            cache_seconds: 自定义缓存秒数(None 用配置)

        Returns:
            {
                "checked": bool,        # 本次是否真的打了 API
                "upgraded": bool,       # 是否触发了升级
                "new_orders": int,      # 本次处理的新订单数
                "reason": str,          # 描述
            }
        """
        afdian_cfg = self.config.get("afdian", {})
        if not afdian_cfg.get("enabled", True):
            return {"checked": False, "upgraded": False, "new_orders": 0,
                    "reason": "爱发电未启用"}

        if cache_seconds is None:
            cache_seconds = int(afdian_cfg.get("verify_cache_seconds", 300))

        qq = str(qq).strip()
        if not qq:
            return {"checked": False, "upgraded": False, "new_orders": 0,
                    "reason": "空 QQ"}

        # 缓存
        now = time.time()
        if not force:
            last = self._check_cache.get(qq, 0.0)
            if now - last < cache_seconds:
                last_res = self._last_result.get(qq, {})
                return {
                    "checked": False,
                    "upgraded": False,
                    "new_orders": 0,
                    "reason": f"缓存命中(距上次 {int(now-last)}s)",
                    "last_result": last_res,
                }

        # 调 API 拉订单
        data = await self._api_query_orders(page=1, per_page=50)
        self._check_cache[qq] = now
        if data is None:
            result = {"checked": True, "upgraded": False, "new_orders": 0,
                      "reason": "API 调用失败"}
            self._last_result[qq] = result
            return result

        # 过滤该用户的新订单
        new_orders = 0
        upgraded = False
        items = data.get("list", []) or []
        for order in items:
            if int(order.get("status", 0)) != 2:
                continue
            if not self._order_matches_qq(order, qq):
                continue
            order_id = str(order.get("out_trade_no", "") or "")
            if not order_id:
                continue
            if self.vip.store.is_order_processed(qq, order_id):
                continue
            # 标记 + 升级
            self.vip.store.add_processed_order(qq, order_id)
            self._upgrade_from_order(qq, order)
            new_orders += 1
            upgraded = True

        result = {
            "checked": True,
            "upgraded": upgraded,
            "new_orders": new_orders,
            "reason": "ok" if upgraded else "无新订单",
        }
        self._last_result[qq] = result
        if upgraded:
            self.logger.log_info("主动核验升级成功: qq={} new_orders={}", qq, new_orders)
        return result

    def _upgrade_from_order(self, qq: str, order: dict[str, Any]) -> None:
        """
        根据订单 plan_id 决定升级月度/季度

        优先级:
        1. plan_id 命中配置中的 quarterly_plan_id -> 季度(90 天)
        2. plan_id 命中配置中的 monthly_plan_id -> 月度(30 天)
        3. 订单 sku 名称包含 "季度" -> 季度
        4. 订单 month 字段(爱发电返回 3 表示 3 个月) -> 自动换算成天数
        5. 默认月度
        """
        afdian_cfg = self.config.get("afdian", {})
        plan_id = str(order.get("plan_id", "") or "")

        is_quarterly = False
        if afdian_cfg.get("quarterly_plan_id", "") and plan_id == str(afdian_cfg.get("quarterly_plan_id", "")):
            is_quarterly = True
        elif afdian_cfg.get("monthly_plan_id", "") and plan_id == str(afdian_cfg.get("monthly_plan_id", "")):
            is_quarterly = False
        else:
            # sku 名称含"季度"则季度
            sku_detail = order.get("sku_detail") or []
            for sku in sku_detail:
                if "季度" in str(sku.get("name", "")):
                    is_quarterly = True
                    break
            # 备注含"季度"也兜底
            if not is_quarterly and "季度" in str(order.get("remark", "")):
                is_quarterly = True

        if is_quarterly:
            # 季度: 用 order 的 month 字段(爱发电月数)换算成天数
            try:
                months = int(order.get("month", 3))
            except (TypeError, ValueError):
                months = 3
            days = max(1, months) * 30
            self.vip.upgrade_to_quarterly(qq, days=days)
        else:
            # 月度: 用 order 的 month 字段(爱发电月数)换算成天数
            try:
                months = int(order.get("month", 1))
            except (TypeError, ValueError):
                months = 1
            days = max(1, months) * 30
            self.vip.upgrade_to_monthly(qq, days=days)

    def build_sponsor_url(
        self,
        base_url: str,
        qq: str | int,
        sku_id: str = "",
        product_type: int = 1,
    ) -> str:
        """
        动态生成爱发电"最终付款页" URL,带 QQ 号标识

        base_url 可以是:
        - /item/{id}    商品详情页(爱发电后台复制得到)
        - /p/{id}       方案介绍页
        - /order/create?plan_id=xxx  已经是订单创建页
        任意形式,插件都会自动转成 /order/create?plan_id=...&product_type=1&sku=...&custom_order_id=qq%3D{qq}

        sku_id 是该商品下的具体型号 ID(从爱发电后台商品详情页 URL 里取),
        按月/季分别配置,每个套餐自己挂一个独立商品。

        参考爱发电官方文档
        https://ifdian.net/p/010ff078177211eca44f52540025c377
        的"前端嵌入/按钮"章节第 60-75 行。

        示例(月度商品,item URL 形式):
            base_url = "https://ifdian.net/item/bef6243e680a11f1826c52540025c377"
            sku_id = "98655f88680a11f1ace052540025c377"
            qq = 2536843865
            返回: "https://ifdian.net/order/create?plan_id=bef6243e680a11f1826c52540025c377&product_type=1&sku=%5B%7B%22sku_id%22%3A%2298655f88680a11f1ace052540025c377%22%2C%22count%22%3A1%7D%5D&custom_order_id=qq%3D2536843865"
        """
        import re as _re
        import urllib.parse as _u
        import json as _json

        if not base_url:
            return ""
        qq = str(qq).strip()
        if not qq:
            return base_url

        # 详情页 → 自动转 /order/create
        if "/order/create" not in base_url:
            m = _re.search(r"/(?:item|p)/([0-9a-fA-F]+)", base_url)
            if m:
                plan_id = m.group(1)
                params = [f"plan_id={plan_id}", f"product_type={product_type}"]
                if sku_id:
                    sku_json = _json.dumps(
                        [{"sku_id": sku_id, "count": 1}],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    params.append("sku=" + _u.quote(sku_json, safe=""))
                base_url = "https://ifdian.net/order/create?" + "&".join(params)

        sep = "&" if "?" in base_url else "?"
        # 链接里直接放裸 QQ 号(爱发电前端解码后留言框 = QQ 号本身,更直观);
        # extract_qq 有"纯数字兜底"逻辑,即便不带 qq= 前缀也能识别
        return f"{base_url}{sep}remark={qq}&custom_order_id={qq}"

    def register_flask_route(self, app, path: str = "/afdian_webhook"):
        """
        注册 Flask 路由(挂载到 web_server 的 Flask app)

        响应格式按爱发电协议: {"ec": 200, "em": "ok", "data": {...}},
        业务失败也用 ec != 200, 至少 ec 字段必须存在,
        否则爱发电代理按 "response error" 判失败 (ec: 500)。
        """
        @app.route(path, methods=["POST"])
        def afdian_webhook():
            from flask import jsonify, request
            try:
                payload = request.get_json(silent=True) or {}
            except Exception:
                payload = {}
            result = self.handle_payload(payload)
            # 统一爱发电协议格式: ec / em / data
            if result.get("ok"):
                body = {
                    "ec": 200,
                    "em": "ok",
                    "data": result.get("data") or {},
                }
                return jsonify(body), 200
            # 业务失败: 仍然按爱发电约定的 ec != 200 形式返回,
            # 这样代理 / 后台能看到具体原因
            body = {
                "ec": 400,
                "em": result.get("reason") or "处理失败",
                "data": result.get("data") or {},
            }
            return jsonify(body), 200

        @app.route(path + "/test", methods=["GET"])
        def afdian_webhook_test():
            from flask import jsonify
            return jsonify({"ok": True, "msg": "afdian webhook endpoint ready"})
