# -*- coding: utf-8 -*-
"""
配置管理模块
统一管理所有环境变量和配置项
"""
import os
import logging
from typing import List
from dotenv import load_dotenv
from src.token_manager import TokenManager
from src.token_updater import TokenUpdater

# 加载环境变量
load_dotenv()

class Config:
    """应用配置类"""
    
    # API认证配置
    VALID_API_KEY: str = os.getenv("VALID_API_KEY", "")
    K2THINK_API_URL: str = os.getenv("K2THINK_API_URL", "https://www.k2think.ai/api/chat/completions")
    
    # Token管理配置
    MAX_TOKEN_FAILURES: int = int(os.getenv("MAX_TOKEN_FAILURES", "3"))
    
    # Token自动更新配置
    ENABLE_TOKEN_AUTO_UPDATE: bool = os.getenv("ENABLE_TOKEN_AUTO_UPDATE", "true").lower() == "true"
    TOKEN_UPDATE_INTERVAL: int = int(os.getenv("TOKEN_UPDATE_INTERVAL", "86400"))  # 默认24小时
    ACCOUNTS_FILE: str = os.getenv("ACCOUNTS_FILE", "accounts.txt")
    TOKEN_MAX_WORKERS: int = int(os.getenv("TOKEN_MAX_WORKERS", "4"))  # 并发获取token的线程数
    
    # Token管理器实例（延迟初始化）
    _token_manager: TokenManager = None
    _token_updater: TokenUpdater = None
    
    # 服务器配置
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8001"))
    
    # 功能开关
    DEBUG_LOGGING: bool = os.getenv("DEBUG_LOGGING", "false").lower() == "true"
    ENABLE_ACCESS_LOG: bool = os.getenv("ENABLE_ACCESS_LOG", "true").lower() == "true"
    
    # 性能配置
    REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "60"))
    MAX_KEEPALIVE_CONNECTIONS: int = int(os.getenv("MAX_KEEPALIVE_CONNECTIONS", "20"))
    MAX_CONNECTIONS: int = int(os.getenv("MAX_CONNECTIONS", "100"))
    STREAM_DELAY: float = float(os.getenv("STREAM_DELAY", "0.05"))
    STREAM_CHUNK_SIZE: int = int(os.getenv("STREAM_CHUNK_SIZE", "50"))
    MAX_STREAM_TIME: float = float(os.getenv("MAX_STREAM_TIME", "10.0"))
    
    # 日志配置
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    
    # CORS配置
    CORS_ORIGINS: List[str] = (
        os.getenv("CORS_ORIGINS", "*").split(",") 
        if os.getenv("CORS_ORIGINS", "*") != "*" 
        else ["*"]
    )
    

    
    @classmethod
    def validate(cls) -> None:
        """验证必需的配置项"""
        if not cls.VALID_API_KEY:
            raise ValueError("错误：VALID_API_KEY 环境变量未设置。请在 .env 文件中提供一个安全的API密钥。")
        
        # 检查账户文件是否存在（启动时刷新需要）
        if cls.ENABLE_TOKEN_AUTO_UPDATE:
            if not os.path.exists(cls.ACCOUNTS_FILE):
                raise ValueError(f"错误：账户文件 {cls.ACCOUNTS_FILE} 不存在。请创建账户文件。")
            print(f"✓ 账户文件已找到: {cls.ACCOUNTS_FILE}")
        else:
            raise ValueError("错误：必须启用 ENABLE_TOKEN_AUTO_UPDATE=true，因为现在完全依赖内存中的tokens。")
        
        # 验证数值范围
        if cls.PORT < 1 or cls.PORT > 65535:
            raise ValueError(f"错误：PORT 值 {cls.PORT} 不在有效范围内 (1-65535)")
        
        if cls.REQUEST_TIMEOUT <= 0:
            raise ValueError(f"错误：REQUEST_TIMEOUT 必须大于0，当前值: {cls.REQUEST_TIMEOUT}")
        
        if cls.STREAM_DELAY < 0:
            raise ValueError(f"错误：STREAM_DELAY 不能为负数，当前值: {cls.STREAM_DELAY}")
    
    @classmethod
    def setup_logging(cls) -> None:
        """设置日志配置"""
        import sys
        
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR
        }
        
        log_level = level_map.get(cls.LOG_LEVEL, logging.INFO)
        
        # 确保日志输出使用UTF-8编码
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        
        # 确保标准输出使用UTF-8编码
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
    
    @classmethod
    def get_token_manager(cls) -> TokenManager:
        """获取token管理器实例（单例模式）"""
        if cls._token_manager is None:
            # 创建TokenManager，允许空启动（等待刷新）
            cls._token_manager = TokenManager(
                max_failures=cls.MAX_TOKEN_FAILURES,
                allow_empty=True  # 启动时允许空，等待刷新
            )
        return cls._token_manager
    
    @classmethod
    def get_token_updater(cls) -> TokenUpdater:
        """获取token更新器实例（单例模式）"""
        if cls._token_updater is None:
            cls._token_updater = TokenUpdater(
                update_interval=cls.TOKEN_UPDATE_INTERVAL,
                accounts_file=cls.ACCOUNTS_FILE,
                max_workers=cls.TOKEN_MAX_WORKERS
            )
        return cls._token_updater
    
    @classmethod
    def initialize_tokens(cls) -> bool:
        """
        初始化tokens - 启动时执行
        
        Returns:
            成功返回True，否则返回False
        """
        logger = logging.getLogger(__name__)
        
        logger.info("🚀 启动时执行token刷新...")
        
        # 获取更新器和管理器
        token_updater = cls.get_token_updater()
        token_manager = cls.get_token_manager()
        
        # 关联更新器和管理器
        token_updater.set_token_manager(token_manager)
        
        # 设置内存刷新回调
        token_manager.set_memory_refresh_callback(token_updater.refresh_tokens)
        
        # 执行初始刷新
        success = token_updater.initial_refresh()
        
        if success:
            logger.info(f"✅ Token初始化成功，共 {len(token_manager.get_tokens_list())} 个token可用")
        else:
            logger.error("❌ Token初始化失败，请检查accounts.txt文件")
        
        # 设置强制刷新回调
        cls._setup_force_refresh_callback()
        
        return success
    
    @classmethod
    def reload_tokens(cls) -> None:
        """重新加载token"""
        if cls._token_manager is not None:
            cls._token_manager.reload_tokens()
    
    @classmethod
    def _setup_force_refresh_callback(cls) -> None:
        """设置强制刷新回调函数"""
        if cls._token_manager is None or cls._token_updater is None:
            return
        
        def force_refresh_callback():
            try:
                logger = logging.getLogger(__name__)
                logger.info("🔄 检测到token问题，启动自动刷新")
                success = cls._token_updater.force_update()
                if success:
                    cls._token_manager.reset_consecutive_failures()
                    logger.info("✅ 自动刷新完成，token池已更新")
                else:
                    logger.error("❌ 自动刷新失败，请检查accounts.txt文件")
            except Exception as e:
                logging.getLogger(__name__).error(f"❌ 自动刷新回调执行失败: {e}")
        
        cls._token_manager.set_force_refresh_callback(force_refresh_callback)
        logging.getLogger(__name__).info("已设置连续失效自动强制刷新机制")