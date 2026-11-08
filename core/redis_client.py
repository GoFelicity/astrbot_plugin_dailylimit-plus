"""
Redis 连接管理模块(可选)

提供 Redis 连接的初始化、验证、重连和状态查询功能。

Plus 版特性:
- Redis 是可选项: 插件默认走 JSON 存储,无需 Redis
- 配置 redis.enabled=true 才会尝试连接 Redis
- 配置缺失 / 连接失败 / 包未安装 都不会让插件崩溃
"""


try:
    import redis
    import redis.exceptions
    _REDIS_AVAILABLE = True
except ImportError:
    redis = None
    _REDIS_AVAILABLE = False


class RedisClient:
    """Redis 连接管理类(可选)"""

    def __init__(self, plugin):
        """
        初始化 Redis 客户端

        Args:
            plugin: 插件实例
        """
        self.plugin = plugin
        self.logger = plugin.logger
        self.config = plugin.config
        self.redis_client = None
        self._enabled = False
        self._last_error = None

    def is_available(self) -> bool:
        """Redis 是否可用(已启用且连接成功)"""
        return self._enabled and self.redis_client is not None

    def init_redis(self):
        """初始化Redis连接(软启动)

        Plus 版语义:
        - 如果 redis.enabled != true: 直接跳过,redis_client 保持 None
        - 如果未安装 redis 包: 记录 warning,redis_client 保持 None
        - 如果 host/port 等配置缺失: 记录 warning,redis_client 保持 None
        - 如果连接失败: 记录 error,redis_client 保持 None
        主流程不会因 Redis 不可用而中断。
        """
        redis_cfg = self.config.get("redis", {}) or {}
        if not isinstance(redis_cfg, dict):
            self._last_error = "redis 配置项必须是 dict"
            self.logger.log_warning("{}", self._last_error)
            return
        # Plus 版: 检查 enabled 开关
        self._enabled = bool(redis_cfg.get("enabled", False))
        if not self._enabled:
            self._last_error = "Redis 未启用(redis.enabled = false)"
            self.logger.log_info(
                "Redis 未启用,使用 JSON 文件存储。如需启用请在配置中设置 redis.enabled = true"
            )
            return

        if not _REDIS_AVAILABLE:
            self._last_error = "redis 包未安装,请 pip install redis 后再启用"
            self.logger.log_warning("{}", self._last_error)
            self._enabled = False
            return

        # 检查必要配置
        host = redis_cfg.get("host")
        port = redis_cfg.get("port")
        if not host or not port:
            self._last_error = "Redis 配置不完整(需 host 和 port)"
            self.logger.log_warning("{}", self._last_error)
            self._enabled = False
            return

        try:
            # 获取连接池大小配置
            pool_size = redis_cfg.get("connection_pool_size", 10)

            self.redis_client = redis.Redis(
                host=host,
                port=port,
                db=redis_cfg.get("db", 0),
                password=redis_cfg.get("password") or None,
                decode_responses=True,  # 自动将响应解码为字符串
                max_connections=pool_size,  # 使用配置的连接池大小
                socket_connect_timeout=redis_cfg.get("connect_timeout", 3),
                socket_timeout=redis_cfg.get("socket_timeout", 3),
            )
            # 测试连接
            self.redis_client.ping()
            self.logger.log_info("Redis 连接成功,连接池大小: {}", pool_size)
        except Exception as e:
            self._last_error = f"Redis 连接失败: {e}"
            self.logger.log_error("{}", self._last_error)
            self.redis_client = None
            self._enabled = False

    def validate_redis_connection(self) -> bool:
        """
        验证Redis连接状态

        检查Redis连接是否可用，包括连接状态和响应能力。

        返回：
            bool: Redis连接是否可用
        """
        if not self._enabled:
            # 未启用时直接返回 False,日志减少
            return False
        if not self.redis_client:
            self.logger.log_error("Redis连接未初始化")
            return False

        try:
            # 发送ping命令验证连接
            response = self.redis_client.ping()
            if not response:
                self.logger.log_warning("Redis ping响应异常: {}", response)
                return False

            return True

        except Exception as e:
            # 兼容 redis 缺失场景
            conn_err = getattr(
                getattr(redis, "exceptions", None), "ConnectionError", Exception
            ) if redis else Exception
            timeout_err = getattr(
                getattr(redis, "exceptions", None), "TimeoutError", Exception
            ) if redis else Exception
            if isinstance(e, conn_err):
                self.logger.log_error("Redis连接错误: {}", str(e))
            elif isinstance(e, timeout_err):
                self.logger.log_error("Redis连接超时: {}", str(e))
            else:
                self._last_error = str(e)
            return False

    def get_redis_status(self):
        """
        获取Redis连接状态信息

        返回：
            dict: Redis连接状态信息字典
        """
        if not self._enabled:
            return {
                "connected": False,
                "status": "未启用",
                "mode": "json",
                "error": self._last_error or "Redis 未启用(redis.enabled = false)",
            }
        if not self.redis_client:
            return {
                "connected": False,
                "status": "未初始化",
                "mode": "json",
                "error": self._last_error or "Redis连接未初始化",
            }

        try:
            # 检查连接状态
            self.redis_client.ping()

            # 获取Redis服务器信息
            info = self.redis_client.info()

            return {
                "connected": True,
                "status": "正常",
                "mode": "redis",
                "response_time": "正常",
                "server_version": info.get("redis_version", "未知"),
                "used_memory": info.get("used_memory_human", "未知"),
                "connected_clients": info.get("connected_clients", 0),
            }

        except Exception as e:
            return {"connected": False, "status": "异常", "mode": "json", "error": str(e)}

    def reconnect_redis(self):
        """
        重新连接Redis

        当Redis连接断开时，尝试重新建立连接。

        返回：
            bool: 重连成功返回True，失败返回False
        """
        if not self._enabled:
            return False
        if not self.redis_client:
            self.logger.log_error("Redis连接未初始化，无法重连")
            return False

        try:
            # 关闭现有连接
            if (
                hasattr(self.redis_client, "connection_pool")
                and self.redis_client.connection_pool
            ):
                self.redis_client.connection_pool.disconnect()

            # 重新连接
            self.redis_client.connection_pool.reset()

            # 验证新连接
            if self.validate_redis_connection():
                self.logger.log_info("Redis重连成功")
                return True
            else:
                self.logger.log_error("Redis重连失败")
                return False

        except Exception as e:
            self.logger.log_error("Redis重连过程中出错: {}", str(e))
            return False

    @property
    def redis(self):
        """获取 Redis 客户端实例"""
        return self.redis_client
