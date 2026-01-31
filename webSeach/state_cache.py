import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
from playwright.async_api import BrowserContext


class StateCacheManager:
    """
    浏览器状态缓存管理器
    负责保存、加载、验证和清理浏览器存储状态
    """

    # 引擎域名白名单：只保存/恢复这些域名相关的状态
    ENGINE_DOMAINS = {
        "bing": ["bing.com", "www.bing.com"],
        "duckduckgo": ["duckduckgo.com", "www.duckduckgo.com"],
        "baidu": ["baidu.com", "www.baidu.com"],
        "yandex": ["yandex.com", "yandex.ru", "www.yandex.com", "www.yandex.ru"],
    }

    def __init__(
        self,
        cache_dir: Path = None,
        ttl_seconds: int = 7200,  # 默认2小时
    ):
        """
        :param cache_dir: 缓存目录路径，默认为 .cache/browser_states/
        :param ttl_seconds: 状态有效期（秒）
        """
        if cache_dir is None:
            cache_dir = Path(__file__).parent / ".cache"

        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.metadata_file = self.cache_dir / "metadata.json"

        # 确保缓存目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 每个引擎一个独立的锁，防止并发写入冲突
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, engine_name: str) -> asyncio.Lock:
        """获取指定引擎的锁（懒初始化）"""
        if engine_name not in self._locks:
            self._locks[engine_name] = asyncio.Lock()
        return self._locks[engine_name]

    def _get_state_path(self, engine_name: str) -> Path:
        """获取指定引擎的状态文件路径"""
        return self.cache_dir / f"{engine_name}_state.json"

    async def _load_metadata(self) -> Dict:
        """加载元数据文件"""
        if not self.metadata_file.exists():
            return {}
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    async def _save_metadata(self, metadata: Dict) -> None:
        """保存元数据文件"""
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _is_expired(self, engine_meta: Dict) -> bool:
        """检查状态是否已过期"""
        if "expires_at" not in engine_meta:
            return True
        try:
            expires_at = datetime.fromisoformat(engine_meta["expires_at"])
            return datetime.now() > expires_at
        except (ValueError, KeyError):
            return True

    async def is_state_valid(self, engine_name: str) -> bool:
        """
        检查指定引擎的缓存状态是否有效
        :param engine_name: 引擎名称（如 "bing", "duckduckgo", "baidu"）
        :return: True 如果状态存在且未过期
        """
        metadata = await self._load_metadata()
        if engine_name not in metadata:
            return False

        state_path = self._get_state_path(engine_name)
        if not state_path.exists():
            return False

        return not self._is_expired(metadata[engine_name])

    async def load_state(self, engine_name: str) -> Optional[dict]:
        """
        加载指定引擎的缓存状态
        :param engine_name: 引擎名称
        :return: 状态字典，如果无效则返回 None
        """
        if not await self.is_state_valid(engine_name):
            # 过期则清理
            await self.invalidate_state(engine_name)
            return None

        state_path = self._get_state_path(engine_name)
        async with self._get_lock(engine_name):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)

                # 更新最后使用时间
                metadata = await self._load_metadata()
                metadata[engine_name]["last_used"] = datetime.now().isoformat()
                await self._save_metadata(metadata)

                return state
            except (json.JSONDecodeError, IOError):
                # 文件损坏，清理
                await self.invalidate_state(engine_name)
                return None

    async def save_state(self, engine_name: str, state: dict) -> bool:
        """
        保存指定引擎的缓存状态
        :param engine_name: 引擎名称
        :param state: Playwright storage_state 字典
        :return: True 如果保存成功
        """
        state_path = self._get_state_path(engine_name)
        async with self._get_lock(engine_name):
            try:
                # 过滤：只保存白名单域名的 cookies 和 origins
                allowed_domains = self.ENGINE_DOMAINS.get(engine_name, [])

                # 过滤 cookies
                filtered_cookies = []
                if "cookies" in state:
                    for cookie in state["cookies"]:
                        domain = cookie.get("domain", "").lstrip(".")
                        if any(domain == ad or domain.endswith("." + ad) for ad in allowed_domains):
                            filtered_cookies.append(cookie)

                # 过滤 origins
                filtered_origins = []
                if "origins" in state:
                    for origin in state["origins"]:
                        origin_str = origin.get("origin", "")
                        if any(origin_str == f"https://{ad}" or origin_str == f"http://{ad}" for ad in allowed_domains):
                            filtered_origins.append(origin)

                # 只保存过滤后的状态
                filtered_state = {
                    "cookies": filtered_cookies,
                    "origins": filtered_origins,
                }

                # 写入状态文件
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(filtered_state, f, ensure_ascii=False, indent=2)

                # 更新元数据
                metadata = await self._load_metadata()
                now = datetime.now()
                metadata[engine_name] = {
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
                    "last_used": now.isoformat(),
                }
                await self._save_metadata(metadata)

                return True
            except IOError as e:
                print(f"[warn] Failed to save state for {engine_name}: {e}")
                return False

    async def save_context_state(
        self, context: BrowserContext, engine_name: str
    ) -> bool:
        """
        从浏览器上下文保存状态
        :param context: Playwright BrowserContext 对象
        :param engine_name: 引擎名称
        :return: True 如果保存成功
        """
        try:
            state = await context.storage_state()
            return await self.save_state(engine_name, state)
        except Exception as e:
            print(f"[warn] Failed to extract storage state for {engine_name}: {e}")
            return False

    async def load_merged_state(self, engine_names: List[str]) -> Optional[dict]:
        """
        加载多个引擎的状态并合并
        :param engine_names: 引擎名称列表
        :return: 合并后的状态字典，如果都无效则返回 None
        """
        merged = {"cookies": [], "origins": []}

        has_valid = False
        for engine_name in engine_names:
            state = await self.load_state(engine_name)
            if state:
                has_valid = True
                # 合并 cookies
                if "cookies" in state:
                    merged["cookies"].extend(state["cookies"])
                # 合并 origins
                if "origins" in state:
                    merged["origins"].extend(state["origins"])

        return merged if has_valid else None

    async def invalidate_state(self, engine_name: str) -> None:
        """
        使指定引擎的缓存状态失效（删除文件）
        :param engine_name: 引擎名称
        """
        state_path = self._get_state_path(engine_name)
        async with self._get_lock(engine_name):
            try:
                if state_path.exists():
                    state_path.unlink()
            except IOError as e:
                print(f"[warn] Failed to delete state file for {engine_name}: {e}")

            # 从元数据中移除
            metadata = await self._load_metadata()
            metadata.pop(engine_name, None)
            await self._save_metadata(metadata)

    async def cleanup_expired_states(self) -> int:
        """
        清理所有过期的缓存状态
        :return: 清理的引擎数量
        """
        metadata = await self._load_metadata()
        expired = []

        for engine_name, engine_meta in list(metadata.items()):
            if self._is_expired(engine_meta):
                expired.append(engine_name)

        for engine_name in expired:
            await self.invalidate_state(engine_name)

        return len(expired)

    async def get_cache_info(self) -> Dict:
        """
        获取缓存信息摘要
        :return: 包含各引擎状态信息的字典
        """
        metadata = await self._load_metadata()
        info = {}

        for engine_name, engine_meta in metadata.items():
            state_path = self._get_state_path(engine_name)
            info[engine_name] = {
                "exists": state_path.exists(),
                "is_valid": not self._is_expired(engine_meta),
                "created_at": engine_meta.get("created_at"),
                "expires_at": engine_meta.get("expires_at"),
                "last_used": engine_meta.get("last_used"),
            }

        return info
