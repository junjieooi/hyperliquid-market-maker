import asyncio
import html
import json
import logging
import math
import os
import re
import secrets
import time
from collections import deque
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Deque, Dict, List, Optional, Tuple

import aiohttp
from eth_account import Account
from hyperliquid import exchange, info
from hyperliquid.utils.types import Cloid

# Try to load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()  # Load .env file into environment
except ImportError:
    pass  # python-dotenv not installed, rely on system environment variables


# ============================================================================
# Fill Tracking for Adverse Selection Detection
# ============================================================================

@dataclass
class FillRecord:
    """Record of a fill with mark-to-market tracking."""
    timestamp: datetime
    side: str  # "B" or "A"
    price: float
    size: float
    mark_price_at_fill: float

    def unrealized_pnl(self, current_mark: float) -> float:
        """Calculate unrealized P&L from this fill."""
        if self.side == "B":  # Bought - profit if price went up
            return (current_mark - self.price) * self.size
        else:  # Sold - profit if price went down
            return (self.price - current_mark) * self.size


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    @property
    def mid_price(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread_bps(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        if self.asks[0].price == 0:
            return None
        spread = (self.asks[0].price - self.bids[0].price) / self.asks[0].price
        return spread * 10_000

    def imbalance(self, depth: int = 5) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        bids_volume = sum(level.size for level in self.bids[:depth])
        asks_volume = sum(level.size for level in self.asks[:depth])
        total = bids_volume + asks_volume
        if total == 0:
            return None
        return (bids_volume - asks_volume) / total


@dataclass
class MarketMakerConfig:
    base_url: str
    symbol: str
    max_inventory: float  # base max inventory in position units
    session_drawdown_limit: float
    order_size: float
    num_layers: int  # Number of order layers per side for volume generation
    layer_spacing_bps: float  # Price spacing between layers in basis points
    spread_bps: float
    inventory_skew_bps: float
    orderbook_skew_bps: float
    long_bias_bps: float  # base directional bias: positive = favor accumulating longs
    volatility_guard_threshold_bps: float
    volatility_guard_multiplier: float
    funding_flush_lead_seconds: int
    funding_resume_delay_seconds: int
    oracle_deviation_threshold_bps: float
    quote_refresh_threshold_bps: float
    heartbeat_url: str
    heartbeat_interval_seconds: int
    account_address: Optional[str]
    private_key_env: Optional[str]
    private_key: Optional[str]
    perp_dex: Optional[str]
    # === NEW: Adaptive parameters ===
    volatility_lookback: int  # Number of price samples for volatility calc
    baseline_volatility_bps: float  # Expected "normal" volatility in bps
    adverse_selection_threshold: float  # P&L threshold to detect adverse selection
    adverse_selection_lookback: int  # Number of fills to analyze
    us_market_hours_spread_mult: float  # Spread multiplier during US hours (9:30-16:00 ET)
    funding_alpha_threshold_bps: float  # Funding rate threshold to increase bias
    funding_alpha_boost_bps: float  # Extra bias when funding is favorable
    fill_rate_high_threshold: float  # Fill rate above this = widen spread (being picked off)
    fill_rate_low_threshold: float  # Fill rate below this = tighten spread (not competitive)
    fill_rate_lookback_minutes: int  # Minutes to look back for fill rate calculation
    fill_rate_spread_mult_max: float  # Max spread multiplier (when fill rate is very high)
    fill_rate_spread_mult_min: float  # Min spread multiplier (when fill rate is very low)
    # === Risk Management ===
    max_unrealized_loss_pct: float  # Max unrealized loss as % of position before emergency flatten
    emergency_flatten_enabled: bool  # Enable emergency flatten on large losses
    # === Telegram Notifications ===
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_update_interval_seconds: int
    stats_day_tz_offset_hours: float
    # === Isolated Margin Management ===
    auto_add_margin: bool  # Automatically add available balance as margin when opening position
    auto_margin_fraction: float  # Fraction of available balance to add (0.0-1.0)

    @staticmethod
    def from_dict(data: Dict) -> "MarketMakerConfig":
        # Helper to resolve environment variables
        def resolve_env(value: Optional[str], env_key: Optional[str] = None) -> Optional[str]:
            """
            Resolve value from environment variable if needed.
            - If env_key is provided, read from that environment variable
            - Otherwise return value as-is
            """
            if env_key:
                # Try to read from environment
                env_value = os.environ.get(env_key)
                if env_value:
                    return env_value
                # Fallback to original value if env var not set
                return value
            return value

        # Support both old "max_inventory_usd" and new "max_inventory" keys
        max_inv = data.get("max_inventory") or data.get("max_inventory_usd", 100)
        # Get adaptive config with sensible defaults
        adaptive = data.get("adaptive", {})
        return MarketMakerConfig(
            base_url=data["base_url"],
            symbol=data["symbol"],
            max_inventory=max_inv,
            session_drawdown_limit=data["session_drawdown_limit"],
            order_size=data["order_size"],
            num_layers=data.get("num_layers", 1),  # Default to 1 layer for backward compatibility
            layer_spacing_bps=data.get("layer_spacing_bps", 10.0),
            spread_bps=data["spread"]["base_bps"],
            inventory_skew_bps=data["spread"]["inventory_skew_bps"],
            orderbook_skew_bps=data["spread"]["orderbook_skew_bps"],
            long_bias_bps=data["spread"].get("long_bias_bps", 0),
            volatility_guard_threshold_bps=data["spread"]["volatility_guard_threshold_bps"],
            volatility_guard_multiplier=data["spread"]["volatility_guard_multiplier"],
            funding_flush_lead_seconds=data["funding"]["flush_lead_seconds"],
            funding_resume_delay_seconds=data["funding"]["resume_delay_seconds"],
            oracle_deviation_threshold_bps=data["oracle"]["deviation_threshold_bps"],
            quote_refresh_threshold_bps=data["spread"]["quote_refresh_threshold_bps"],
            heartbeat_url=data["heartbeat"]["url"],
            heartbeat_interval_seconds=data["heartbeat"]["interval_seconds"],
            # Resolve from environment variables if specified
            account_address=resolve_env(data.get("account_address"), data.get("account_address_env")),
            private_key_env=data.get("private_key_env"),  # Keep for compatibility
            private_key=resolve_env(data.get("private_key"), data.get("private_key_env")),
            perp_dex=data.get("perp_dex"),
            # === NEW: Adaptive parameters with defaults ===
            volatility_lookback=adaptive.get("volatility_lookback", 100),
            baseline_volatility_bps=adaptive.get("baseline_volatility_bps", 50.0),
            adverse_selection_threshold=adaptive.get("adverse_selection_threshold", -0.002),  # -0.2%
            adverse_selection_lookback=adaptive.get("adverse_selection_lookback", 10),
            us_market_hours_spread_mult=adaptive.get("us_market_hours_spread_mult", 1.5),
            funding_alpha_threshold_bps=adaptive.get("funding_alpha_threshold_bps", 5.0),
            funding_alpha_boost_bps=adaptive.get("funding_alpha_boost_bps", 10.0),
            fill_rate_high_threshold=adaptive.get("fill_rate_high_threshold", 0.8),
            fill_rate_low_threshold=adaptive.get("fill_rate_low_threshold", 0.2),
            fill_rate_lookback_minutes=adaptive.get("fill_rate_lookback_minutes", 15),
            fill_rate_spread_mult_max=adaptive.get("fill_rate_spread_mult_max", 2.0),
            fill_rate_spread_mult_min=adaptive.get("fill_rate_spread_mult_min", 0.7),
            # === Risk Management ===
            max_unrealized_loss_pct=data.get("risk", {}).get("max_unrealized_loss_pct", 0.03),  # 3% default
            emergency_flatten_enabled=data.get("risk", {}).get("emergency_flatten_enabled", True),
            # === Telegram Notifications ===
            telegram_enabled=data.get("telegram", {}).get("enabled", False),
            telegram_bot_token=resolve_env(
                data.get("telegram", {}).get("bot_token", ""),
                data.get("telegram", {}).get("bot_token_env")
            ),
            telegram_chat_id=resolve_env(
                data.get("telegram", {}).get("chat_id", ""),
                data.get("telegram", {}).get("chat_id_env")
            ),
            telegram_update_interval_seconds=data.get("telegram", {}).get("update_interval_seconds", 900),
            stats_day_tz_offset_hours=data.get("stats_day_tz_offset_hours", 0),
            # === Isolated Margin Management ===
            auto_add_margin=data.get("margin", {}).get("auto_add_margin", False),
            auto_margin_fraction=data.get("margin", {}).get("auto_margin_fraction", 1.0),  # Default to 100%
        )


class HyperliquidClient:
    def __init__(self, config: MarketMakerConfig, loggers: Dict[str, logging.Logger],
                 loop: asyncio.AbstractEventLoop) -> None:
        self.config = config
        self.loggers = loggers
        self.loop = loop
        self._perp_dex_catalog: List[Dict] = []
        self.builder_offset: Optional[int] = None
        self.builder_dex_hint = self._resolve_builder_dex_hint()
        self.builder_dex = self._canonicalize_builder_dex(self.builder_dex_hint) if self.builder_dex_hint else None
        self._perp_dexs = self._compute_perp_dexs()
        self.coin_name = self._resolve_coin_name()
        self.exchange: Optional[exchange.Exchange] = None
        self.info: Optional[info.Info] = None
        self.wallet = None
        self._orderbook_handler: Optional[Callable[[OrderBook], None]] = None
        self._fill_handler: Optional[Callable[[List[Dict]], None]] = None
        self._oracle_handler: Optional[Callable[[float], None]] = None
        self._funding_handler: Optional[Callable[[float], None]] = None  # Funding rate in bps/hr
        self.price_increment: Decimal = Decimal("0.0001")
        self.size_increment: Decimal = Decimal("0.001")
        self.size_decimals: int = 3
        self.price_sig_figs: int = 5
        self.max_price_decimals: int = 6
        self._cloid_to_oid: Dict[str, int] = {}
        self._oid_to_cloid: Dict[int, str] = {}

        # Rate limit tracking: Hyperliquid allows ~(base + 1 per $1 taker volume) requests
        self._rate_limit_backoff_until: float = 0.0
        self._rate_limit_base_delay: float = 30.0
        self._rate_limit_current_delay: float = 30.0
        self._rate_limit_max_delay: float = 300.0
        # Parsed from the rate limit error message for preemptive tracking
        self._rl_requests_sent: int = 0
        self._rl_requests_allowed: int = 0
        self._rl_volume_traded: float = 0.0
        self._rl_calibrated: bool = False
        self._rl_session_api_calls: int = 0  # Count our own API calls since calibration
        self._rl_taker_volume_pending: bool = False  # Signal to MarketMaker to place taker orders
        self._margin_rejected: bool = False  # Signal: last order rejected for insufficient margin

    async def initialize(self, *, on_orderbook, on_fill, on_oracle, on_funding=None) -> None:
        pk_env = self.config.private_key_env
        key = None
        if pk_env:
            key = os.environ.get(pk_env)
        if not key:
            key = self.config.private_key
        if not key:
            raise RuntimeError(
                "Private key missing; set private_key_env to a populated environment variable or provide private_key in config.json"
            )

        self.wallet = Account.from_key(key)
        self.info = info.Info(base_url=self.config.base_url, skip_ws=False, spot_meta=self._empty_spot_meta())
        self.exchange = exchange.Exchange(
            self.wallet,
            base_url=self.config.base_url,
            # account_address: the main wallet that orders/positions belong to
            # wallet: the agent/API key that signs transactions on behalf of account_address
            account_address=self.config.account_address or self.wallet.address,
            spot_meta=self._empty_spot_meta(),
        )
        self._ensure_builder_metadata_loaded()
        self._derive_increments()
        self._alias_symbol_if_needed()

        self.loggers["main"].info(
            "Initialized client for %s (dex=%s price_increment=%s size_increment=%s size_decimals=%s)",
            self.config.symbol,
            self.builder_dex or "core",
            self.price_increment,
            self.size_increment,
            self.size_decimals,
        )

        self._orderbook_handler = on_orderbook
        self._fill_handler = on_fill
        self._oracle_handler = on_oracle
        self._funding_handler = on_funding
        self._start_streams()

    def _resolve_builder_dex_hint(self) -> Optional[str]:
        raw = self.config.perp_dex or (self.config.symbol.split(":", 1)[0] if ":" in self.config.symbol else None)
        if not raw:
            return None
        return raw.split(":", 1)[0].strip() or None

    @staticmethod
    def _empty_spot_meta() -> Dict:
        return {"tokens": [], "universe": []}

    def _canonicalize_builder_dex(self, dex: Optional[str]) -> Optional[str]:
        if not dex:
            return None
        dex = dex.split(":", 1)[0]
        if dex.lower() == "xyz":
            self.builder_offset = 110000
            return dex
        try:
            bootstrap = info.Info(base_url=self.config.base_url, skip_ws=True)
            available = bootstrap.perp_dexs() or []
            self._perp_dex_catalog = available
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to fetch perp dex list, using provided builder '%s': %s", dex, exc)
            return dex
        dex_lower = dex.lower()
        for idx, entry in enumerate((available[1:] if len(available) > 1 else [])):
            if isinstance(entry, dict):
                name = entry.get("name") or ""
            else:
                name = str(entry)
            if name.lower() == dex_lower:
                if name != dex:
                    self.loggers["main"].info("Resolved builder dex hint '%s' to canonical '%s'", dex, name)
                self.builder_offset = 110000 + idx * 10000
                return name
        self.loggers["errors"].warning("Builder dex '%s' not found in meta; proceeding anyway", dex)
        return dex

    def _infer_builder_offset(self) -> Optional[int]:
        if self.builder_offset is not None:
            return self.builder_offset
        catalog = self._perp_dex_catalog
        if not catalog:
            try:
                bootstrap = info.Info(base_url=self.config.base_url, skip_ws=True)
                catalog = bootstrap.perp_dexs() or []
                self._perp_dex_catalog = catalog
            except Exception as exc:  # noqa: BLE001
                self.loggers["errors"].warning("Unable to refresh perp dex catalog: %s", exc)
                return None
        dex_lower = (self.builder_dex or "").lower()
        for idx, entry in enumerate((catalog[1:] if len(catalog) > 1 else [])):
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            if name and name.lower() == dex_lower:
                offset = 110000 + idx * 10000
                self.builder_offset = offset
                return offset
        return None

    def _ensure_builder_metadata_loaded(self) -> None:
        if not self.info or not self.builder_dex:
            return
        info_loaded = self.config.symbol in self.info.name_to_coin or self.coin_name in self.info.name_to_coin
        exchange_info = self.exchange.info if self.exchange else None
        exchange_loaded = (
            exchange_info is None
            or self.config.symbol in exchange_info.name_to_coin
            or self.coin_name in exchange_info.name_to_coin
        )
        if info_loaded and exchange_loaded:
            return
        offset = self._infer_builder_offset()
        if offset is None:
            offset = 110000
        try:
            builder_meta = self.info.meta(dex=self.builder_dex)
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to fetch builder meta for %s: %s", self.builder_dex, exc)
            return
        if not info_loaded:
            self.info.set_perp_meta(builder_meta, offset)
        if exchange_info is not None and not exchange_loaded:
            exchange_info.set_perp_meta(builder_meta, offset)

    def _resolve_coin_name(self) -> str:
        if self.builder_dex and ":" in self.config.symbol:
            return self.config.symbol.split(":", 1)[1]
        return self.config.symbol

    def _alias_symbol_if_needed(self) -> None:
        if not self.info:
            return
        alias = self.config.symbol
        canonical = self.coin_name
        try:
            canonical_coin = self.info.name_to_coin[canonical]
        except KeyError:
            return
        self.info.name_to_coin[alias] = canonical_coin

    def _compute_perp_dexs(self) -> Optional[List[str]]:
        if not self.builder_dex:
            return None
        dexes = ["", self.builder_dex]
        seen = set()
        ordered: List[str] = []
        for dex in dexes:
            if dex not in seen:
                ordered.append(dex)
                seen.add(dex)
        return ordered

    def _derive_increments(self) -> None:
        """Attempt to pull price/size increments from the exchange metadata."""
        if not self.info:
            return
        try:
            dex = self.builder_dex or ""
            meta = self.info.meta(dex=dex)
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to fetch meta for increments: %s", exc)
            return

        try:
            entries = meta.get("universe") or meta.get("tokens") or []
            search_names = [self.config.symbol]
            if self.coin_name not in search_names:
                search_names.append(self.coin_name)
            lowered = {name.lower(): name for name in search_names}
            coin_meta = next((t for t in entries if t.get("name", "").lower() in lowered), None)
            if not coin_meta:
                return
            actual_name = coin_meta.get("name")
            if actual_name and actual_name != self.coin_name:
                self.coin_name = actual_name
                self.loggers["main"].info("Canonicalized coin name to %s", self.coin_name)
                self._alias_symbol_if_needed()
            px_decimals = coin_meta.get("pxDecimals") or coin_meta.get("szDecimals")
            if px_decimals is not None:
                px_decimals_int = int(px_decimals)
                self.price_increment = Decimal(1) / (Decimal(10) ** Decimal(px_decimals_int))
            sz_decimals = coin_meta.get("szDecimals")
            if sz_decimals is not None:
                sz_decimals_int = int(sz_decimals)
                self.size_decimals = sz_decimals_int
                self.size_increment = Decimal(1) / (Decimal(10) ** Decimal(sz_decimals_int))
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to parse increments from meta: %s", exc)

    def _start_streams(self) -> None:
        if not self.info:
            return
        try:
            self.loggers["main"].info("Subscribing to l2Book for %s (coin=%s)", self.config.symbol, self.coin_name)
            self.info.subscribe(
                {"type": "l2Book", "coin": self.coin_name},
                lambda msg: self._handle_orderbook_msg(msg),
            )
            self.loggers["main"].info("Subscribing to activeAssetCtx for %s", self.coin_name)
            self.info.subscribe(
                {"type": "activeAssetCtx", "coin": self.coin_name},
                lambda msg: self._handle_asset_ctx(msg),
            )
            account_address = self.config.account_address or self.wallet.address
            if account_address:
                self.loggers["main"].info("Subscribing to userFills for %s", account_address)
                self.info.subscribe(
                    {"type": "userFills", "user": account_address},
                    lambda msg: self._handle_user_fills(msg),
                )
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Websocket subscribe error: %s", exc)

    def new_cloid(self) -> str:
        return f"0x{secrets.token_hex(16)}"

    def _safe_run(self, func, *, label: str) -> Optional[object]:
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("%s failed: %s", label, exc)
            return None

    async def _run(self, func, *, label: str) -> Optional[object]:
        self._rl_session_api_calls += 1
        return await self.loop.run_in_executor(None, lambda: self._safe_run(func, label=label))

    def _check_rate_limit(self, result: object) -> bool:
        """Check if an API response indicates a rate limit error.
        If so, parse the numbers, signal for taker volume, and activate short backoff."""
        if not isinstance(result, dict):
            return False
        response_str = str(result.get("response", ""))
        if "Too many cumulative requests" not in response_str:
            return False

        # Parse: "Too many cumulative requests sent (113625 > 105342) for cumulative volume traded $95343.33"
        m = re.search(r'sent \((\d+) > (\d+)\) for cumulative volume traded \$(\d+\.\d+)', response_str)
        if m:
            self._rl_requests_sent = int(m.group(1))
            self._rl_requests_allowed = int(m.group(2))
            self._rl_volume_traded = float(m.group(3))
            self._rl_calibrated = True
            self._rl_session_api_calls = 0  # Reset counter from this calibration point
            deficit = self._rl_requests_sent - self._rl_requests_allowed
            self.loggers["main"].warning(
                "RATE LIMIT: %d requests sent, %d allowed (deficit=%d), volume=$%.2f — requesting taker orders",
                self._rl_requests_sent, self._rl_requests_allowed, deficit, self._rl_volume_traded,
            )

        # Signal MarketMaker to place taker volume orders
        self._rl_taker_volume_pending = True

        # Short backoff (5s) to let taker orders go through before resuming
        now = time.monotonic()
        self._rate_limit_backoff_until = now + 5.0
        self.loggers["errors"].warning(
            "RATE LIMITED — signaling taker volume orders, pausing 5s",
        )
        return True

    def _is_rate_limited(self) -> bool:
        """Return True if we are currently in rate limit backoff."""
        return time.monotonic() < self._rate_limit_backoff_until

    def _needs_preemptive_taker_volume(self) -> bool:
        """Check if we're approaching 90% of rate limit and should preemptively place taker orders."""
        if not self._rl_calibrated:
            return False
        # Estimate current position: requests_at_calibration + calls_since_then
        estimated_requests = self._rl_requests_sent + self._rl_session_api_calls
        estimated_allowed = self._rl_requests_allowed  # Stays same until more taker volume
        if estimated_allowed <= 0:
            return False
        usage_ratio = estimated_requests / estimated_allowed
        return usage_ratio >= 0.9

    def _sanitize_price(self, price: float) -> float:
        """Clamp price following Hyperliquid precision rules to avoid float_to_wire errors."""
        decimals_budget = max(self.max_price_decimals - self.size_decimals, 0)
        if price > 100_000:
            rounded = round(price)
        else:
            rounded = round(float(f"{price:.{self.price_sig_figs}g}"), decimals_budget)

        precision_increment = Decimal(1).scaleb(-decimals_budget)
        quantize_step = max(self.price_increment, precision_increment)
        quantized = Decimal(str(rounded)).quantize(quantize_step, rounding=ROUND_DOWN)
        return float(quantized)

    def _sanitize_size(self, size: float) -> float:
        quantized = Decimal(str(size)).quantize(self.size_increment, rounding=ROUND_DOWN)
        return float(quantized)

    def _extract_oid(self, order_status: Dict) -> Optional[int]:
        for key in ("resting", "filled"):
            data = order_status.get(key)
            if isinstance(data, dict) and "oid" in data:
                try:
                    return int(data["oid"])
                except (TypeError, ValueError):
                    return None
        return None

    def _record_cloid_oid(self, cloid: str, oid: Optional[int]) -> None:
        if oid is None:
            return
        self._cloid_to_oid[cloid] = oid
        self._oid_to_cloid[oid] = cloid

    def cloid_for_oid(self, oid: int) -> Optional[str]:
        return self._oid_to_cloid.get(oid)

    async def place_post_only(
            self,
            side: str,
            price: float,
            size: float,
            *,
            cloid: str,
            reduce_only: bool = False,
    ) -> bool:
        assert self.exchange is not None
        if self._is_rate_limited():
            return False
        cloid_obj = Cloid.from_str(cloid)
        order_type = {"limit": {"tif": "Alo"}}
        safe_price = self._sanitize_price(price)
        safe_size = self._sanitize_size(size)
        self.loggers["main"].info(
            "Submitting post-only %s sz=%.6f px=%.6f reduce_only=%s", side, safe_size, safe_price, reduce_only
        )
        result = await self._run(
            lambda: self.exchange.order(
                self.coin_name,
                side == "buy",
                safe_size,
                safe_price,
                order_type,
                reduce_only=reduce_only,
                cloid=cloid_obj,
            ),
            label="post-only order",
        )
        if result is None:
            self.loggers["errors"].warning("post-only order %s sz=%.6f px=%.6f failed", side, safe_size, safe_price)
            return False
        self.loggers["main"].info("Order response: %s", result)

        # Check if API call succeeded
        status = result.get("status") if isinstance(result, dict) else None
        if status != "ok":
            self._check_rate_limit(result)
            self.loggers["errors"].warning("Exchange API error: %s", result)
            return False

        # Check actual order status (Hyperliquid returns status:"ok" even for rejected orders!)
        response_data = result.get("response", {}).get("data", {})
        statuses = response_data.get("statuses", [])
        if not statuses:
            self.loggers["errors"].warning("No order status in response: %s", result)
            return False

        order_status = statuses[0]
        # Order is successful only if it has 'resting' (placed) or 'filled' status
        # 'error' means rejected (e.g., would cross spread, insufficient margin, etc.)
        if "error" in order_status:
            error_msg = str(order_status.get("error", ""))
            self.loggers["errors"].warning("Order REJECTED: %s", error_msg)
            if "insufficient margin" in error_msg.lower():
                self._margin_rejected = True
            return False
        elif "resting" in order_status or "filled" in order_status:
            oid = self._extract_oid(order_status)
            self._record_cloid_oid(cloid, oid)
            return True
        else:
            self.loggers["errors"].warning("Unknown order status: %s", order_status)
            return False

    async def cancel_by_cloids(self, clo_ids: List[str]) -> bool:
        if not clo_ids:
            return True
        assert self.exchange is not None
        if self._is_rate_limited():
            return False
        cancels = [{"coin": self.coin_name, "cloid": Cloid.from_str(cid)} for cid in clo_ids]
        self.loggers["main"].info("Cancelling %d orders by cloid", len(cancels))
        result = await self._run(lambda: self.exchange.bulk_cancel_by_cloid(cancels), label="cancel by cloid")
        if result is None:
            self.loggers["errors"].warning("Cancel failed for %s", clo_ids)
            return False
        self.loggers["main"].info("Cancel response: %s", result)
        if not isinstance(result, dict) or result.get("status") != "ok":
            self._check_rate_limit(result)
            self.loggers["errors"].warning("Cancel status not ok: %s", result)
            return False

        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            self.loggers["errors"].warning("No cancel statuses in response: %s", result)
            return False
        if len(statuses) != len(clo_ids):
            self.loggers["errors"].warning(
                "Cancel status count mismatch: expected %d got %d", len(clo_ids), len(statuses)
            )
            return False

        all_success = True
        for cloid, status in zip(clo_ids, statuses):
            should_clear = False
            if "error" in status:
                error_msg = str(status.get("error", ""))
                if "not found" in error_msg.lower():
                    self.loggers["main"].debug("Order already gone for %s: %s", cloid, error_msg)
                    should_clear = True
                else:
                    self.loggers["errors"].warning("Failed to cancel %s: %s", cloid, error_msg)
                    all_success = False
            elif "success" in status or "resting" in status:
                self.loggers["main"].debug("Successfully cancelled %s", cloid)
                should_clear = True
            else:
                self.loggers["errors"].warning("Unknown cancel status for %s: %s", cloid, status)
                all_success = False

            if should_clear:
                oid = self._cloid_to_oid.pop(cloid, None)
                if oid is not None:
                    self._oid_to_cloid.pop(oid, None)
        return all_success

    async def cancel_all_open_orders(self) -> bool:
        """Cancel ALL open orders for this coin on the exchange (not just tracked ones)."""
        assert self.info is not None
        assert self.exchange is not None

        try:
            # Fetch all open orders from the exchange
            # CRITICAL: For builder DEXs, the API seems to return orders differently
            # CRITICAL: The API has caching/delay - retry with backoff to catch orphaned orders
            # Use account_address (main wallet) if specified, otherwise agent address
            account = self.config.account_address or self.wallet.address
            dex_param = self.builder_dex or ""

            def _fetch_open_orders():
                # Try builder dex first
                orders = self.info.open_orders(account, dex=dex_param)
                if orders:
                    return orders
                # If empty and we have builder_dex, also try without dex param
                if dex_param:
                    orders = self.info.open_orders(account, dex="")
                return orders

            # Fetch open orders using correct account (vault_address if available)
            open_orders = await self.loop.run_in_executor(None, _fetch_open_orders)
            self.loggers["main"].info("Fetched %d open orders for account %s",
                                     len(open_orders) if open_orders else 0, account)

            if not open_orders:
                self.loggers["main"].info("No open orders to cancel")
                return True

            # DEBUG: Log what we got from the exchange
            self.loggers["main"].info("DEBUG: Fetched %d total open orders", len(open_orders))
            if open_orders:
                sample_coins = list(set(o.get("coin") for o in open_orders[:5]))
                self.loggers["main"].info("DEBUG: Sample coin names from orders: %s", sample_coins)

            # Filter for this coin only
            # IMPORTANT: For builder DEX, exchange may return coin name without prefix
            coin_name_without_prefix = self.coin_name.split(":")[-1] if ":" in self.coin_name else self.coin_name
            self.loggers["main"].info("DEBUG: Looking for coin='%s' OR coin='%s'", self.coin_name, coin_name_without_prefix)
            coin_orders = [
                o for o in open_orders
                if o.get("coin") == self.coin_name or o.get("coin") == coin_name_without_prefix
            ]
            if not coin_orders:
                self.loggers["main"].info("No open orders for %s (checked both '%s' and '%s')",
                                         self.coin_name, self.coin_name, coin_name_without_prefix)
                return True

            self.loggers["main"].info("Found %d open orders for %s, cancelling all", len(coin_orders), self.coin_name)

            # Cancel by OID (more reliable than cloid for orphaned orders)
            # Use the coin name as returned by the exchange (may be without prefix)
            cancels = [{"coin": o.get("coin"), "oid": o["oid"]} for o in coin_orders]
            result = await self._run(
                lambda: self.exchange.bulk_cancel(cancels),
                label="bulk cancel all"
            )

            if result is None:
                self.loggers["errors"].warning("Failed to cancel all open orders")
                return False

            self.loggers["main"].info("Cancel all response: %s", result)
            return True

        except Exception as exc:
            self.loggers["errors"].warning("Failed to fetch/cancel open orders: %s", exc)
            # Fall back to just clearing tracked orders
            return False

    async def place_ioc(
            self,
            side: str,
            price: float,
            size: float,
            *,
            cloid: str,
            reduce_only: bool = False,
            bypass_rate_limit: bool = False,
    ) -> bool:
        """Place an Immediate-Or-Cancel order - fills immediately or cancels unfilled portion."""
        assert self.exchange is not None
        if not bypass_rate_limit and self._is_rate_limited():
            return False
        cloid_obj = Cloid.from_str(cloid)
        order_type = {"limit": {"tif": "Ioc"}}  # IOC instead of Alo
        safe_price = self._sanitize_price(price)
        safe_size = self._sanitize_size(size)
        self.loggers["main"].info(
            "Submitting IOC %s sz=%.6f px=%.6f reduce_only=%s", side, safe_size, safe_price, reduce_only
        )
        result = await self._run(
            lambda: self.exchange.order(
                self.coin_name,
                side == "buy",
                safe_size,
                safe_price,
                order_type,
                reduce_only=reduce_only,
                cloid=cloid_obj,
            ),
            label="IOC order",
        )
        if result is None:
            self.loggers["errors"].warning("IOC order %s sz=%.6f px=%.6f failed", side, safe_size, safe_price)
            return False
        self.loggers["main"].info("IOC Order response: %s", result)

        # Check if API call succeeded
        status = result.get("status") if isinstance(result, dict) else None
        if status != "ok":
            self._check_rate_limit(result)
            self.loggers["errors"].warning("Exchange API error: %s", result)
            return False

        # Check actual order status
        response_data = result.get("response", {}).get("data", {})
        statuses = response_data.get("statuses", [])
        if not statuses:
            self.loggers["errors"].warning("No order status in response: %s", result)
            return False

        order_status = statuses[0]
        if "error" in order_status:
            self.loggers["errors"].warning("IOC order REJECTED: %s", order_status.get("error"))
            return False
        elif "filled" in order_status or "resting" in order_status:
            # IOC orders typically fill immediately or get cancelled
            oid = self._extract_oid(order_status)
            self._record_cloid_oid(cloid, oid)
            return True
        else:
            # IOC might return other statuses, log but don't fail
            self.loggers["main"].info("IOC order status: %s", order_status)
            return True

    async def fetch_l2_snapshot(self) -> Optional[OrderBook]:
        if not self.info:
            return None

        def _fetch():
            return self.info.l2_snapshot(self.coin_name)

        snapshot = await self.loop.run_in_executor(None, _fetch)
        if not snapshot or "levels" not in snapshot:
            return None

        bids_raw, asks_raw = snapshot["levels"]
        bids = [OrderBookLevel(price=float(level["px"]), size=float(level["sz"])) for level in bids_raw]
        asks = [OrderBookLevel(price=float(level["px"]), size=float(level["sz"])) for level in asks_raw]
        if not bids or not asks:
            return None
        return OrderBook(bids=bids, asks=asks)

    def _handle_orderbook_msg(self, message: Dict) -> None:
        if not self._orderbook_handler:
            return
        data = message.get("data", {})
        levels = data.get("levels")
        if not levels or len(levels) != 2:
            return
        try:
            bids = [OrderBookLevel(price=float(level["px"]), size=float(level["sz"])) for level in levels[0]]
            asks = [OrderBookLevel(price=float(level["px"]), size=float(level["sz"])) for level in levels[1]]
        except Exception:
            return
        book = OrderBook(bids=bids, asks=asks)
        self.loop.call_soon_threadsafe(self._orderbook_handler, book)

    def _handle_user_fills(self, message: Dict) -> None:
        if not self._fill_handler:
            return
        fills = message.get("data", {}).get("fills", [])
        if not fills:
            return
        self.loop.call_soon_threadsafe(self._fill_handler, fills)

    def _handle_asset_ctx(self, message: Dict) -> None:
        data = message.get("data", {})
        ctx = data.get("ctx", {})

        # Extract oracle price
        oracle_px_str = ctx.get("oraclePx")
        if oracle_px_str and self._oracle_handler:
            try:
                oracle_px = float(oracle_px_str)
                self.loop.call_soon_threadsafe(self._oracle_handler, oracle_px)
            except Exception:
                pass

        # Extract funding rate (in bps per hour)
        # Hyperliquid returns funding as a decimal rate per 8 hours
        funding_str = ctx.get("funding")
        if funding_str and self._funding_handler:
            try:
                # Convert from 8hr rate to bps/hr: rate * 10000 / 8
                funding_8hr = float(funding_str)
                funding_bps_hr = funding_8hr * 10000 / 8
                self.loop.call_soon_threadsafe(self._funding_handler, funding_bps_hr)
            except Exception:
                pass

    def close(self) -> None:
        if self.info and self.info.ws_manager:
            try:
                self.info.disconnect_websocket()
            except Exception as exc:  # noqa: BLE001
                self.loggers["errors"].warning("Failed to close websocket: %s", exc)

    def reconnect_streams(self) -> bool:
        """Reconnect websocket streams and re-subscribe."""
        try:
            if self.info and self.info.ws_manager:
                self.loggers["main"].info("Reconnecting websocket streams")
                self.info.disconnect_websocket()
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to close websocket before reconnect: %s", exc)

        try:
            self.info = info.Info(base_url=self.config.base_url, skip_ws=False, spot_meta=self._empty_spot_meta())
            self._ensure_builder_metadata_loaded()
            self._derive_increments()
            self._alias_symbol_if_needed()
            self._start_streams()
            return True
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to reconnect websocket streams: %s", exc)
            return False


class MarketMaker:
    def __init__(self, config: MarketMakerConfig) -> None:
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self.orderbook = OrderBook()
        self.inventory: float = 0.0
        self.pnl: float = 0.0
        self.last_oracle_price: Optional[float] = None
        self.mark_price: Optional[float] = None
        self._tasks: List[asyncio.Task] = []
        self._paused_for_funding = False
        self._paused_for_emergency = False
        self._flush_in_progress = False
        self._last_oracle_update: Optional[float] = None
        self._start_time: Optional[float] = None
        self._oracle_stale_seconds = 120.0
        self._oracle_startup_grace = 60.0
        self._orderbook_stale_seconds = 2.0
        self._orderbook_poll_interval = 1.0
        self._orderbook_stale = False
        self._last_orderbook_stale_log_ts: float = 0.0
        self._emergency_flatten_in_progress = False
        self._last_funding_epoch: Optional[datetime] = None
        self._shutdown = asyncio.Event()
        # Track when bot started to filter out historical fills
        self._startup_timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # De-duplicate fills across websocket snapshots/reconnects
        self._seen_fill_ids: set[str] = set()
        self._seen_fill_queue: Deque[str] = deque()
        self._seen_fill_max = 5000
        # Track multiple orders per side: side -> [(cloid, price, size), ...]
        self._active_orders: Dict[str, List[Tuple[str, float, float]]] = {"buy": [], "sell": []}
        # Performance tracking
        self._total_fills = 0
        self._total_volume = 0.0  # Total notional traded
        self._estimated_rebates = 0.0  # Estimated maker rebates earned (~0.02% per fill)
        # Daily stats (timezone-aware, for Telegram)
        self._stats_day_start_ms = self._compute_day_start_ms()
        self._daily_realized_pnl: float = 0.0
        self._daily_total_fills: int = 0
        self._daily_volume: float = 0.0
        self._daily_estimated_rebates: float = 0.0
        self._daily_fill_ids: set[str] = set()
        self._daily_fill_buffer: List[Tuple[Dict, float, float, int]] = []
        self._daily_backfill_in_flight: bool = False
        self._daily_backfill_complete: bool = False
        self._daily_needs_backfill: bool = True
        self._daily_backfill_end_ms: Optional[int] = None
        # Taker volume tracking (rate limit workaround — excluded from stats)
        self._taker_volume_cloids: set[str] = set()
        self._taker_volume_fills: int = 0
        self._taker_volume_cost: float = 0.0

        # Fill rate tracking for adaptive spread
        # Store (timestamp, event_type) where event_type = "placed" or "filled"
        self._order_events: Deque[Tuple[float, str]] = deque()
        self._fill_rate_spread_mult = 1.0  # Current fill rate multiplier
        self._last_orderbook_update: Optional[float] = None
        self._last_quote_refresh: Dict[str, float] = {"buy": 0.0, "sell": 0.0}  # Cooldown tracking
        # Track total open order exposure for safety checks
        self._total_buy_exposure: float = 0.0  # Sum of all open buy order sizes
        self._total_sell_exposure: float = 0.0  # Sum of all open sell order sizes

        # === NEW: Adaptive strategy state ===
        # Volatility tracking (rolling window of mid prices)
        self._price_history: Deque[float] = deque(maxlen=config.volatility_lookback)
        self._current_volatility_bps: float = config.baseline_volatility_bps

        # Auto-baseline volatility: 1-hour rolling window with timestamps
        # Stores (timestamp, price) tuples for baseline calculation
        self._hourly_price_history: Deque[Tuple[float, float]] = deque()
        self._auto_baseline_volatility_bps: float = config.baseline_volatility_bps  # Start with config value

        # Funding rate tracking
        self._current_funding_rate: Optional[float] = None  # In bps per hour
        self._funding_bias_boost: float = 0.0  # Dynamic bias adjustment from funding

        # Adverse selection tracking (recent fills)
        self._recent_fills: Deque[FillRecord] = deque(maxlen=config.adverse_selection_lookback)
        self._adverse_selection_multiplier: float = 1.0  # 1.0 = normal, >1.0 = widen spreads

        # Dynamic inventory limit
        self._effective_max_inventory: float = config.max_inventory

        # === Risk Management State ===
        # Track average entry price for unrealized P&L calculation
        self._avg_entry_price: float = 0.0
        self._total_entry_cost: float = 0.0  # Sum of (price * size) for open position
        self._unrealized_pnl: float = 0.0
        self._auto_margin_in_flight: bool = False
        self._last_auto_margin_ts: float = 0.0
        self._last_oracle_gate_log_ts: float = 0.0
        self._margin_exhausted_until_by_side: Dict[str, float] = {"buy": 0.0, "sell": 0.0}
        self._margin_exhausted_cooldown: float = 60.0  # Seconds to wait before retrying

        self.loggers = self._configure_logging()
        self.client: Optional[HyperliquidClient] = None

    def _configure_logging(self) -> Dict[str, logging.Logger]:
        os.makedirs("logs", exist_ok=True)

        log_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        )

        fills_logger = logging.getLogger("fills")
        fills_logger.propagate = False  # Prevent duplicate logs
        if not fills_logger.handlers:
            fills_handler = logging.FileHandler("logs/fills.log")
            fills_handler.setFormatter(log_formatter)
            fills_logger.addHandler(fills_handler)
        fills_logger.setLevel(logging.INFO)

        errors_logger = logging.getLogger("errors")
        errors_logger.propagate = False  # Prevent duplicate logs
        if not errors_logger.handlers:
            errors_handler = logging.FileHandler("logs/errors.log")
            errors_handler.setFormatter(log_formatter)
            errors_logger.addHandler(errors_handler)
            # Also log errors to stderr so we see them
            errors_stream = logging.StreamHandler()
            errors_stream.setFormatter(log_formatter)
            errors_logger.addHandler(errors_stream)
        errors_logger.setLevel(logging.WARNING)

        main_logger = logging.getLogger("market_maker")
        main_logger.propagate = False  # Prevent duplicate logs
        if not main_logger.handlers:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(log_formatter)
            main_logger.addHandler(stream_handler)
        main_logger.setLevel(logging.INFO)

        return {
            "fills": fills_logger,
            "errors": errors_logger,
            "main": main_logger,
        }

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        self.session = aiohttp.ClientSession()
        self.client = HyperliquidClient(self.config, self.loggers, loop)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()
        if self.client:
            self.client.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _sync_position_from_exchange(self, *, log_startup: bool = True) -> None:
        """
        CRITICAL: Fetch current position from exchange and sync local inventory.
        Without this, bot restarts would accumulate position beyond max_inventory.
        """
        assert self.client is not None
        assert self.client.info is not None

        try:
            # Fetch user state from exchange
            def _fetch_state():
                # Use account_address (main wallet) if specified, otherwise agent address
                account = self.config.account_address or self.client.wallet.address
                return self.client.info.user_state(
                    account,
                    dex=self.client.builder_dex or ""
                )

            user_state = await asyncio.get_running_loop().run_in_executor(None, _fetch_state)

            if not user_state or "assetPositions" not in user_state:
                self.loggers["main"].warning("Failed to fetch user state on startup, starting with inventory=0")
                return

            # Find position for our coin
            # IMPORTANT: For builder DEX coins, exchange returns positions with just the coin name (e.g. "XYZ100")
            # but our coin_name may have the prefix (e.g. "xyz:XYZ100"). Check both formats.
            coin_name = self.client.coin_name
            coin_name_without_prefix = coin_name.split(":")[-1] if ":" in coin_name else coin_name
            positions = user_state.get("assetPositions", [])

            old_inventory = self.inventory
            for asset_pos in positions:
                position = asset_pos.get("position", {})
                position_coin = position.get("coin", "")

                # Match either full name or without prefix
                if position_coin == coin_name or position_coin == coin_name_without_prefix:
                    # Found our position! szi = signed size (+ for long, - for short)
                    szi = float(position.get("szi", 0))
                    entry_px = position.get("entryPx")
                    unrealized_pnl = float(position.get("unrealizedPnl", 0))

                    self.inventory = szi
                    if entry_px:
                        self._avg_entry_price = float(entry_px)
                        self._total_entry_cost = abs(szi) * self._avg_entry_price

                    if log_startup:
                        self.loggers["main"].warning(
                            "STARTUP POSITION SYNC: Found existing position for %s: size=%.4f entry=%.4f unrealizedPnl=%.2f",
                            coin_name, szi, self._avg_entry_price, unrealized_pnl
                        )
                    elif abs(szi - old_inventory) > 0.0001:
                        self.loggers["main"].info(
                            "Position resync: %s size=%.4f (was %.4f) entry=%.4f unrealizedPnl=%.2f",
                            coin_name, szi, old_inventory, self._avg_entry_price, unrealized_pnl
                        )

                    # Check if we're already beyond max inventory
                    if abs(self.inventory) > self.config.max_inventory:
                        self.loggers["errors"].error(
                            "CRITICAL: Existing position %.4f EXCEEDS max_inventory %.4f! Bot will try to reduce position.",
                            abs(self.inventory), self.config.max_inventory
                        )
                    return

            # No position found for this coin
            if abs(self.inventory) > 0.0001:
                self.loggers["main"].info(
                    "Position resync: no position for %s (was %.4f), resetting to 0",
                    coin_name, self.inventory
                )
            self.inventory = 0.0
            self._avg_entry_price = 0.0
            self._total_entry_cost = 0.0
            if log_startup:
                self.loggers["main"].info("No existing position found for %s, starting with inventory=0", coin_name)

        except Exception as exc:
            self.loggers["errors"].error(
                "Failed to sync position from exchange: %s. Starting with inventory=0 (DANGEROUS!)", exc
            )

    async def position_sync_loop(self) -> None:
        """Periodically resync position to prevent local inventory drift."""
        while not self._shutdown.is_set():
            try:
                await self._sync_position_from_exchange(log_startup=False)
            except Exception as exc:  # noqa: BLE001
                self.loggers["errors"].warning("Position resync failed: %s", exc)
            await asyncio.sleep(30)

    async def _get_available_balance(self) -> Optional[float]:
        """
        Fetch available balance (withdrawable) from the account.
        Returns the balance in USD that can be used for margin.
        """
        assert self.client is not None
        assert self.client.info is not None

        try:
            def _extract_withdrawable(state: Dict) -> Optional[float]:
                value = state.get("withdrawable")
                if value is None:
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            def _fetch_state(dex_param: str) -> Optional[Dict]:
                account = self.config.account_address or self.client.wallet.address
                return self.client.info.user_state(
                    account,
                    dex=dex_param,
                )

            dex_param = self.client.builder_dex or ""
            loop = asyncio.get_running_loop()
            user_state = await loop.run_in_executor(None, _fetch_state, dex_param)
            withdrawable = _extract_withdrawable(user_state) if user_state else None

            if dex_param:
                base_state = await loop.run_in_executor(None, _fetch_state, "")
                base_withdrawable = _extract_withdrawable(base_state) if base_state else None
                candidates = [value for value in (withdrawable, base_withdrawable) if value is not None]
                if candidates:
                    withdrawable = max(candidates)

            if withdrawable is None:
                return None

            return withdrawable

        except Exception as exc:
            self.loggers["errors"].warning("Failed to fetch available balance: %s", exc)
            return None

    async def _reconcile_active_orders(self) -> None:
        """Rebuild tracked orders from exchange state when local tracking is uncertain."""
        if self.client is None or self.client.info is None:
            return

        account = self.config.account_address or self.client.wallet.address
        dex_param = self.client.builder_dex or ""

        def _fetch_open_orders():
            orders = self.client.info.open_orders(account, dex=dex_param)
            if orders:
                return orders
            if dex_param:
                return self.client.info.open_orders(account, dex="")
            return orders

        try:
            open_orders = await asyncio.get_running_loop().run_in_executor(None, _fetch_open_orders)
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].warning("Failed to reconcile open orders: %s", exc)
            return

        coin_name = self.client.coin_name
        coin_name_without_prefix = coin_name.split(":")[-1] if ":" in coin_name else coin_name
        rebuilt = {"buy": [], "sell": []}

        for order in open_orders or []:
            coin = order.get("coin")
            if coin != coin_name and coin != coin_name_without_prefix:
                continue
            cloid = order.get("cloid")
            if not cloid:
                continue
            side = order.get("side")
            side_key = "buy" if side in ("B", "buy") else "sell"
            try:
                price = float(order.get("limitPx") or order.get("px") or 0)
                size = float(order.get("sz") or order.get("size") or 0)
            except (TypeError, ValueError):
                continue
            rebuilt[side_key].append((cloid, price, size))
            oid = order.get("oid")
            if oid is not None:
                try:
                    self.client._record_cloid_oid(cloid, int(oid))
                except (TypeError, ValueError):
                    pass

        rebuilt["buy"].sort(key=lambda x: x[1], reverse=True)
        rebuilt["sell"].sort(key=lambda x: x[1])
        self._active_orders = rebuilt
        self._update_exposure_tracking()
        self.loggers["main"].info(
            "Reconciled open orders: buys=%d sells=%d",
            len(self._active_orders["buy"]),
            len(self._active_orders["sell"]),
        )

    async def _cancel_tracked_orders(self, cloids: List[str]) -> bool:
        if not cloids or self.client is None:
            return True
        ok = await self.client.cancel_by_cloids(cloids)
        if not ok:
            await self._reconcile_active_orders()
        return ok

    async def _auto_add_margin_to_position(self) -> bool:
        """
        Automatically add margin to the isolated position.
        Uses config.auto_margin_fraction of available balance.

        Returns True if margin was successfully added.
        """
        if not self.config.auto_add_margin:
            return False
        if self._auto_margin_in_flight:
            return False

        # Only add margin if we have a position
        if abs(self.inventory) < 0.0001:
            return False

        assert self.client is not None
        assert self.client.exchange is not None

        now = asyncio.get_running_loop().time()
        if now - self._last_auto_margin_ts < 10.0:
            return False

        self._auto_margin_in_flight = True
        self._last_auto_margin_ts = now
        try:
            # Get available balance
            available = await self._get_available_balance()
            if available is None or available < 1.0:
                # Less than $1 available, skip
                return False

            # Calculate how much margin to add (cap at available to avoid rejection)
            fraction = max(self.config.auto_margin_fraction, 0.0)
            margin_to_add = available * fraction
            if margin_to_add > available:
                margin_to_add = available

            # Add a minimum threshold ($10) to avoid tiny transfers
            if margin_to_add < 10.0:
                self.loggers["main"].debug(
                    "Skipping auto-margin: amount %.2f is below $10 minimum", margin_to_add
                )
                return False

            self.loggers["main"].info(
                "Auto-adding margin: $%.2f (%.1f%% of available $%.2f) to %s position",
                margin_to_add,
                self.config.auto_margin_fraction * 100,
                available,
                self.client.coin_name
            )

            # Use the exchange API to update isolated margin.
            # Hyperliquid SDK signature: update_isolated_margin(amount, name)
            coin_name = self.client.coin_name

            result = await self.client._run(
                lambda: self.client.exchange.update_isolated_margin(
                    margin_to_add,
                    coin_name,
                ),
                label="add_margin"
            )

            if result and result.get("status") == "ok":
                self.loggers["main"].info(
                    "✓ Successfully added $%.2f margin to %s position",
                    margin_to_add,
                    coin_name
                )
                return True
            else:
                self.loggers["errors"].warning(
                    "Failed to add margin: %s", result
                )
                return False

        except Exception as exc:
            self.loggers["errors"].warning("Error adding margin: %s", exc)
            return False
        finally:
            self._auto_margin_in_flight = False

    async def send_telegram_message(self, message: str) -> bool:
        """Send a message via Telegram bot."""
        if not self.config.telegram_enabled or not self.config.telegram_chat_id:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }

            async with self.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                else:
                    error_text = await resp.text()
                    self.loggers["errors"].warning("Telegram send failed: %s - %s", resp.status, error_text)
                    return False
        except Exception as exc:
            self.loggers["errors"].warning("Telegram send error: %s", exc)
            return False

    def _format_fill_notification(
        self,
        *,
        fill: Dict,
        size: float,
        price: float,
        side: str,
        notional: float,
        pnl_delta: float,
    ) -> str:
        side_label = "BUY" if side == "B" else "SELL" if side == "A" else str(side)
        raw_symbol = fill.get("coin") or (self.client.coin_name if self.client else "")
        symbol = html.escape(str(raw_symbol))
        oid = fill.get("oid")
        cloid = fill.get("cloid")
        fill_hash = fill.get("hash")
        direction = fill.get("dir")
        fee = fill.get("fee")
        fee_token = fill.get("feeToken")
        fill_time_ms = fill.get("time")
        if isinstance(fill_time_ms, (int, float)):
            timestamp = datetime.fromtimestamp(fill_time_ms / 1000, tz=timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)
        time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

        return (
            f"✅ <b>Fill</b> {side_label} {symbol}\n"
            f"Time: {time_str}\n"
            f"Size: {size:.4f}\n"
            f"Price: {price:.4f}\n"
            f"Notional: ${notional:.2f}\n"
            f"Inventory: {self.inventory:.4f}\n"
            f"PnL: {pnl_delta:+.4f}\n"
            f"OID: {html.escape(str(oid)) if oid is not None else 'N/A'}\n"
            f"CLOID: {html.escape(str(cloid)) if cloid else 'N/A'}\n"
            f"Hash: {html.escape(str(fill_hash)) if fill_hash else 'N/A'}\n"
            f"Dir: {html.escape(str(direction)) if direction else 'N/A'}\n"
            f"Fee: {fee} {fee_token}" if fee is not None else "Fee: N/A"
        )

    async def telegram_update_loop(self) -> None:
        """Periodically send position and order updates via Telegram."""
        if not self.config.telegram_enabled:
            self.loggers["main"].info("Telegram notifications disabled")
            return

        if not self.config.telegram_chat_id:
            self.loggers["errors"].warning(
                "Telegram enabled but chat_id not configured! Send a message to your bot and check logs."
            )
            return

        self.loggers["main"].info("Telegram update loop started (interval: %ds)",
                                 self.config.telegram_update_interval_seconds)

        while not self._shutdown.is_set():
            try:
                # Fetch current position from exchange
                if self.client and self.client.info:
                    def _fetch_state():
                        # Use account_address (main wallet) if specified, otherwise agent address
                        account = self.config.account_address or self.client.wallet.address
                        return self.client.info.user_state(
                            account,
                            dex=self.client.builder_dex or ""
                        )
                    user_state = await asyncio.get_running_loop().run_in_executor(None, _fetch_state)

                    # Use TRACKED orders instead of fetching (API has delay issues)
                    # Convert tracked orders to same format as API would return
                    tracked_orders = []
                    for side in ["buy", "sell"]:
                        for cloid, price, size in self._active_orders[side]:
                            tracked_orders.append({
                                "coin": self.client.coin_name.split(":")[-1],  # Without prefix for display
                                "side": "B" if side == "buy" else "A",
                                "limitPx": str(price),
                                "sz": str(size),
                                "cloid": cloid
                            })

                    # Format message
                    now_ms = int(time.time() * 1000)
                    self._ensure_daily_window(now_ms)
                    if self._daily_needs_backfill and not self._daily_backfill_in_flight:
                        await self._refresh_daily_stats_from_exchange()
                    message = self._format_telegram_update(user_state, tracked_orders)
                    await self.send_telegram_message(message)

            except Exception as exc:
                self.loggers["errors"].warning("Telegram update error: %s", exc)

            await asyncio.sleep(self.config.telegram_update_interval_seconds)

    def _format_telegram_update(self, user_state: Optional[Dict], open_orders: Optional[List]) -> str:
        """Format position and orders into a Telegram message."""
        coin_name = self.client.coin_name if self.client else self.config.symbol
        coin_name_without_prefix = coin_name.split(":")[-1] if ":" in coin_name else coin_name

        lines = [
            f"<b>🤖 Market Maker Update - {coin_name}</b>",
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            ""
        ]

        # Position info
        if user_state and "assetPositions" in user_state:
            positions = user_state.get("assetPositions", [])
            position_found = False

            for asset_pos in positions:
                pos = asset_pos.get("position", {})
                pos_coin = pos.get("coin", "")

                if pos_coin == coin_name or pos_coin == coin_name_without_prefix:
                    position_found = True
                    szi = float(pos.get("szi", 0))
                    entry_px = float(pos.get("entryPx", 0)) if pos.get("entryPx") else 0
                    unrealized_pnl = float(pos.get("unrealizedPnl", 0))
                    position_value = float(pos.get("positionValue", 0))
                    leverage_val = pos.get("leverage", {}).get("value", 0)

                    direction = "🟢 LONG" if szi > 0 else "🔴 SHORT" if szi < 0 else "⚪️ FLAT"

                    lines.extend([
                        f"<b>📊 Position</b>",
                        f"  Direction: {direction}",
                        f"  Size: {abs(szi):.4f}",
                        f"  Entry: ${entry_px:.2f}" if entry_px else "  Entry: N/A",
                        f"  Value: ${position_value:.2f}",
                        f"  Leverage: {leverage_val}x",
                        f"  Unrealized P&L: ${unrealized_pnl:.2f}",
                        f"  Local Inventory: {self.inventory:.4f}",
                        ""
                    ])
                    break

            if not position_found:
                lines.extend([
                    "<b>📊 Position</b>",
                    "  ⚪️ FLAT (No position)",
                    f"  Local Inventory: {self.inventory:.4f}",
                    ""
                ])
        else:
            lines.extend([
                "<b>📊 Position</b>",
                "  ❌ Failed to fetch position",
                f"  Local Inventory: {self.inventory:.4f}",
                ""
            ])

        # Open orders
        if open_orders is not None:
            coin_orders = [
                o for o in open_orders
                if o.get("coin") == coin_name or o.get("coin") == coin_name_without_prefix
            ]

            if coin_orders:
                buy_orders = [o for o in coin_orders if o.get("side") == "B"]
                sell_orders = [o for o in coin_orders if o.get("side") == "A"]

                lines.append(f"<b>📝 Open Orders ({len(coin_orders)})</b>")

                if buy_orders:
                    lines.append(f"  🟢 BUY: {len(buy_orders)} order(s)")
                    for order in buy_orders[:3]:  # Show first 3
                        sz = float(order.get("sz", 0))
                        px = float(order.get("limitPx", 0))
                        lines.append(f"    • {sz:.4f} @ ${px:.2f}")
                    if len(buy_orders) > 3:
                        lines.append(f"    ... and {len(buy_orders) - 3} more")

                if sell_orders:
                    lines.append(f"  🔴 SELL: {len(sell_orders)} order(s)")
                    for order in sell_orders[:3]:  # Show first 3
                        sz = float(order.get("sz", 0))
                        px = float(order.get("limitPx", 0))
                        lines.append(f"    • {sz:.4f} @ ${px:.2f}")
                    if len(sell_orders) > 3:
                        lines.append(f"    ... and {len(sell_orders) - 3} more")

                lines.append("")
            else:
                lines.extend([
                    "<b>📝 Open Orders</b>",
                    "  No open orders",
                    ""
                ])
        else:
            lines.extend([
                "<b>📝 Open Orders</b>",
                "  ❌ Failed to fetch orders",
                ""
            ])

        # Stats
        tz_offset = self.config.stats_day_tz_offset_hours
        tz_sign = "+" if tz_offset >= 0 else "-"
        tz_label = f"UTC{tz_sign}{abs(tz_offset):g}"
        lines.extend([
            f"<b>💰 Stats (Today, {tz_label})</b>",
            f"  Realized P&L: ${self._daily_realized_pnl:.2f}",
            f"  Mark Price: ${self.mark_price:.2f}" if self.mark_price else "  Mark Price: N/A",
            f"  Max Inventory: {self.config.max_inventory:.2f}",
            "",
            f"<b>📈 Performance (Today, {tz_label})</b>",
            f"  Total Fills: {self._daily_total_fills}",
            f"  Volume Traded: ${self._daily_volume:,.2f}",
            f"  Est. Rebates: ${self._daily_estimated_rebates:.2f}",
        ])

        return "\n".join(lines)

    async def start(self) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession()
        if not self.client:
            loop = asyncio.get_running_loop()
            self.client = HyperliquidClient(self.config, self.loggers, loop)
        self._start_time = asyncio.get_running_loop().time()

        try:
            await self.client.initialize(
                on_orderbook=self._apply_orderbook,
                on_fill=self._apply_fills,
                on_oracle=self._apply_oracle,
                on_funding=self._apply_funding,
            )
        except Exception as exc:  # noqa: BLE001
            self.loggers["errors"].error("Failed to initialize Hyperliquid client: %s", exc)
            await self.shutdown()
            return

        # CRITICAL: Sync inventory from exchange before starting
        # This prevents accumulating position across restarts
        await self._sync_position_from_exchange()

        # Refresh daily stats (timezone-aware) before starting loops
        await self._refresh_daily_stats_from_exchange()

        if self.config.auto_add_margin and abs(self.inventory) >= 0.0001:
            self.loggers["main"].info(
                "Startup: existing position detected (inv=%.4f), triggering auto-margin addition",
                self.inventory
            )
            asyncio.create_task(self._auto_add_margin_to_position())

        # Cancel any orphaned orders from previous runs before starting
        self.loggers["main"].info("Startup: cancelling all existing orders to ensure clean state")
        await self.cancel_all()
        # Don't call .clear() here - cancel_all() already resets the dict properly

        self._tasks = [
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._adaptive_update_loop(), name="adaptive"),
            asyncio.create_task(self.orderbook_listener(), name="orderbook_ws"),
            asyncio.create_task(self.fills_listener(), name="fills_ws"),
            asyncio.create_task(self.oracle_listener(), name="oracle_ws"),
            asyncio.create_task(self.oracle_watchdog_loop(), name="oracle_watchdog"),
            asyncio.create_task(self.position_sync_loop(), name="position_sync"),
            asyncio.create_task(self.quoting_loop(), name="quoting"),
            asyncio.create_task(self.funding_flush_loop(), name="funding_flush"),
            asyncio.create_task(self.risk_monitor(), name="risk_monitor"),
            asyncio.create_task(self.telegram_update_loop(), name="telegram"),
        ]

        await self._shutdown.wait()

    async def shutdown(self) -> None:
        self._shutdown.set()

    async def _reconnect_oracle_streams(self) -> bool:
        if not self.client:
            return False
        loop = asyncio.get_running_loop()
        self.loggers["errors"].warning("Oracle watchdog: attempting websocket reconnect")
        ok = await loop.run_in_executor(None, self.client.reconnect_streams)
        if ok:
            self._last_oracle_update = loop.time()
            self.loggers["errors"].warning("Oracle watchdog: websocket reconnect succeeded; awaiting oracle updates")
        return ok

    async def heartbeat_loop(self) -> None:
        assert self.session is not None
        if not self.config.heartbeat_url:
            self.loggers["main"].info("Heartbeat disabled; no URL configured")
            return
        while not self._shutdown.is_set():
            try:
                async with self.session.get(self.config.heartbeat_url) as resp:
                    self.loggers["main"].info("Heartbeat status: %s", resp.status)
            except Exception as exc:  # noqa: BLE001
                self.loggers["errors"].warning("Heartbeat failed: %s", exc)
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    def _apply_orderbook(self, snapshot: OrderBook) -> None:
        self.orderbook = snapshot
        self.mark_price = snapshot.mid_price
        self._last_orderbook_update = asyncio.get_running_loop().time()
        if self._orderbook_stale:
            self._orderbook_stale = False
            self.loggers["main"].info("Orderbook fresh; resuming quoting")

        # Track price history for volatility calculation
        if snapshot.mid_price is not None:
            self._price_history.append(snapshot.mid_price)
            # Also track for 1-hour baseline calculation
            self._hourly_price_history.append((time.time(), snapshot.mid_price))

    def _apply_oracle(self, oracle_px: float) -> None:
        self.last_oracle_price = oracle_px
        self._last_oracle_update = asyncio.get_running_loop().time()
        if self.mark_price:
            diff = (self.mark_price - oracle_px) / oracle_px * 10000
            if abs(diff) > 50:  # log only significant moves > 50 bps
                self.loggers["main"].info(
                    "Oracle update: %.4f (market=%.4f diff=%.2fbps)", oracle_px, self.mark_price, diff
                )

    def _apply_funding(self, funding_bps_hr: float) -> None:
        """Handle funding rate update (in bps per hour)."""
        self._current_funding_rate = funding_bps_hr

        # Funding Alpha: If funding is significantly positive (shorts pay longs),
        # increase long bias to get paid. If negative, reduce bias.
        threshold = self.config.funding_alpha_threshold_bps
        boost = self.config.funding_alpha_boost_bps

        if funding_bps_hr > threshold:
            # Positive funding = shorts pay longs = be more long-biased
            self._funding_bias_boost = boost * (funding_bps_hr / threshold)
        elif funding_bps_hr < -threshold:
            # Negative funding = longs pay shorts = reduce long bias
            self._funding_bias_boost = -boost * (abs(funding_bps_hr) / threshold)
        else:
            self._funding_bias_boost = 0.0

        # Cap the boost to avoid extreme swings
        self._funding_bias_boost = max(-boost * 2, min(boost * 2, self._funding_bias_boost))

    def _apply_fills(self, fills: List[Dict]) -> None:
        for fill in fills:
            try:
                self.loggers["fills"].info("RAW fill: %s", fill)
                # CRITICAL: Filter fills by coin to avoid tracking other coins/bots
                # Handle both formats: "xyz:XYZ100" and "XYZ100"
                fill_coin = fill.get("coin")
                coin_name = self.client.coin_name
                coin_name_without_prefix = coin_name.split(":")[-1] if ":" in coin_name else coin_name

                if fill_coin != coin_name and fill_coin != coin_name_without_prefix:
                    continue  # Skip fills for other coins

                # CRITICAL: Filter out historical fills from before bot started
                # WebSocket replays old fills, but we already synced position from API
                # IMPORTANT: Only filter if timestamp exists, otherwise assume current fill
                fill_timestamp_ms = fill.get("time")
                if fill_timestamp_ms is not None and fill_timestamp_ms < self._startup_timestamp_ms:
                    # This is a historical fill from before bot started - ignore it
                    # We already synced the current position from the API on startup
                    self.loggers["fills"].debug(
                        "Skipping historical fill: time=%s startup=%s", fill_timestamp_ms, self._startup_timestamp_ms
                    )
                    continue

                # Deduplicate fills to avoid replaying snapshots on reconnect
                fill_id = self._fill_dedupe_id(fill)
                if fill_id in self._seen_fill_ids:
                    continue
                self._seen_fill_ids.add(fill_id)
                self._seen_fill_queue.append(fill_id)
                while len(self._seen_fill_queue) > self._seen_fill_max:
                    old_id = self._seen_fill_queue.popleft()
                    self._seen_fill_ids.discard(old_id)

                size = float(fill.get("sz", 0))
                if size <= 0:
                    continue
                price = self._resolve_fill_price(fill)
                if price is None:
                    self.loggers["fills"].warning("Fill missing price: %s", fill)
                    continue
                side = fill.get("side")
                if side not in ("B", "A"):
                    self.loggers["errors"].warning("Fill has invalid side '%s': %s", side, fill)
                    continue
                notional = size * price
                # Detect taker volume fills (rate limit workaround) — exclude from stats
                fill_cloid_raw = fill.get("cloid")
                is_taker_volume = (
                    (isinstance(fill_cloid_raw, str) and fill_cloid_raw.startswith("0xrl"))
                    or fill_cloid_raw in self._taker_volume_cloids
                )

                # Track entry price for unrealized P&L calculation
                old_inventory = self.inventory

                # Inventory tracks position SIZE (not notional) so skew/risk checks work correctly
                if side == "B":
                    self.inventory += size
                elif side == "A":
                    self.inventory -= size

                # Update average entry price
                self._update_entry_price(old_inventory, side, price, size)

                pnl_delta = float(fill.get("closedPnl", 0))

                if is_taker_volume:
                    # Track taker volume separately, exclude from all stats
                    self._taker_volume_fills += 1
                    self._taker_volume_cost += abs(pnl_delta)
                    self._taker_volume_cloids.discard(fill_cloid_raw)
                else:
                    self.pnl += pnl_delta

                    # Daily stats (timezone-aware) used in Telegram updates
                    daily_time_ms: int
                    if isinstance(fill_timestamp_ms, (int, float)):
                        daily_time_ms = int(fill_timestamp_ms)
                    else:
                        daily_time_ms = int(time.time() * 1000)
                    self._ensure_daily_window(daily_time_ms)
                    if self._daily_backfill_in_flight:
                        if daily_time_ms >= self._stats_day_start_ms:
                            self._daily_fill_buffer.append((fill, notional, pnl_delta, daily_time_ms))
                    else:
                        self._record_daily_stats_from_fill(fill, notional, pnl_delta, daily_time_ms)

                    # Track performance metrics
                    self._total_fills += 1
                    self._total_volume += notional
                    # Hyperliquid maker rebate is ~0.02% (2 bps)
                    self._estimated_rebates += notional * 0.0002

                if not is_taker_volume:
                    # Track filled event for fill rate calculation
                    now = asyncio.get_running_loop().time()
                    self._order_events.append((now, "filled"))

                # Remove or shrink filled order from tracking when possible (prevents exposure drift)
                fill_cloid = fill.get("cloid")
                if not fill_cloid and self.client:
                    fill_oid = fill.get("oid")
                    if fill_oid is not None:
                        try:
                            fill_cloid = self.client.cloid_for_oid(int(fill_oid))
                        except (TypeError, ValueError):
                            fill_cloid = None
                if fill_cloid:
                    fill_size = size
                    for side_key in ("buy", "sell"):
                        before = self._active_orders.get(side_key, [])
                        if not before:
                            continue
                        updated = []
                        changed = False
                        for cloid, open_price, open_size in before:
                            if cloid != fill_cloid:
                                updated.append((cloid, open_price, open_size))
                                continue
                            remaining = max(0.0, open_size - fill_size)
                            if remaining > 0.0001:
                                updated.append((cloid, price, remaining))
                            changed = True
                        if changed:
                            self._active_orders[side_key] = updated
                            self._update_exposure_tracking()
                            break

                if not is_taker_volume:
                    # Track fill for adverse selection analysis
                    if self.mark_price:
                        fill_record = FillRecord(
                            timestamp=datetime.now(timezone.utc),
                            side=side,
                            price=price,
                            size=size,
                            mark_price_at_fill=self.mark_price,
                        )
                        self._recent_fills.append(fill_record)

                    self.loggers["fills"].info(
                        "Fill %s sz=%.4f px=%.4f notional=%.2f inv=%.4f pnl=%.4f entry=%.4f | "
                        "Session: %d fills, $%.2f volume, $%.2f est. rebates",
                        side,
                        size,
                        price,
                        notional,
                        self.inventory,
                        pnl_delta,
                        self._avg_entry_price,
                        self._total_fills,
                        self._total_volume,
                        self._estimated_rebates,
                    )

                    if self.config.telegram_enabled and self.config.telegram_chat_id:
                        message = self._format_fill_notification(
                            fill=fill,
                            size=size,
                            price=price,
                            side=side,
                            notional=notional,
                            pnl_delta=pnl_delta,
                        )
                        asyncio.create_task(self.send_telegram_message(message))

                # Auto-add margin if position was opened or increased
                position_increased = abs(self.inventory) > abs(old_inventory) + 0.0001
                if position_increased and self.config.auto_add_margin:
                    verb = "opened" if abs(old_inventory) < 0.0001 else "increased"
                    self.loggers["main"].info(
                        "Position %s (%.4f -> %.4f), triggering auto-margin addition",
                        verb, old_inventory, self.inventory
                    )
                    asyncio.create_task(self._auto_add_margin_to_position())

                # IMMEDIATE inventory limit check - cancel resting orders that would exceed limit
                # This prevents race conditions between fills and the quoting loop
                self._check_and_cancel_excess_orders(side)

            except Exception as exc:  # noqa: BLE001
                self.loggers["errors"].warning("Failed to parse fill: %s", exc)

    def _resolve_fill_price(self, fill: Dict) -> Optional[float]:
        """Pick the best available execution price from a fill payload."""
        for key in ("px", "fillPx", "avgPx", "price", "limitPx"):
            value = fill.get(key)
            if value is None:
                continue
            try:
                f = float(value)
                if math.isfinite(f):
                    return f
            except (TypeError, ValueError):
                continue
        return None

    def _fill_dedupe_id(self, fill: Dict) -> str:
        fill_hash = fill.get("hash")
        if fill_hash:
            return str(fill_hash)
        oid = fill.get("oid")
        cloid = fill.get("cloid")
        timestamp = fill.get("time")
        if timestamp is not None:
            timestamp = int(timestamp)
        return f"{oid}:{cloid}:{timestamp}:{fill.get('side')}:{fill.get('sz')}:{fill.get('px')}"

    def _check_and_cancel_excess_orders(self, fill_side: str) -> None:
        """
        Immediately cancel resting orders that would exceed inventory limits.
        Called right after a fill to prevent race conditions.
        """
        if self.client is None:
            return

        max_inv = self._effective_max_inventory

        async def _cancel_and_clear(side_key: str, cloids: List[str]) -> None:
            if not cloids:
                return
            ok = await self._cancel_tracked_orders(cloids)
            if ok:
                self._active_orders[side_key] = []
                self._update_exposure_tracking()
            else:
                self.loggers["errors"].warning("Cancel failed for %s orders: %s", side_key, cloids)

        # After a BUY fill, check if we should cancel the resting buy order
        if fill_side == "B":
            # If next buy would reach/exceed limit, cancel resting buy immediately
            if self.inventory + self._total_buy_exposure >= max_inv:
                buy_orders = self._active_orders.get("buy", [])
                if buy_orders:
                    cloids = [cloid for cloid, _, _ in buy_orders]
                    self.loggers["main"].info(
                        "Immediate cancel buy orders (inv=%.4f + %.4f > max=%.4f)",
                        self.inventory, self._total_buy_exposure, max_inv
                    )
                    # Schedule cancel (can't await in sync callback)
                    asyncio.create_task(_cancel_and_clear("buy", cloids))

        # After a SELL fill, check if we should cancel the resting sell order
        elif fill_side == "A":
            # If next sell would reach/exceed short limit, cancel resting sell immediately
            if self.inventory - self._total_sell_exposure <= -max_inv:
                sell_orders = self._active_orders.get("sell", [])
                if sell_orders:
                    cloids = [cloid for cloid, _, _ in sell_orders]
                    self.loggers["main"].info(
                        "Immediate cancel sell orders (inv=%.4f - %.4f < -max=%.4f)",
                        self.inventory, self._total_sell_exposure, max_inv
                    )
                    # Schedule cancel (can't await in sync callback)
                    asyncio.create_task(_cancel_and_clear("sell", cloids))

    def _calculate_fill_rate(self) -> Optional[float]:
        """
        Calculate fill rate over the lookback window.
        Fill rate = fills / (fills + placed orders still open)

        Returns None if insufficient data.
        """
        now = asyncio.get_running_loop().time()
        lookback_seconds = self.config.fill_rate_lookback_minutes * 60

        # Clean up old events outside the lookback window
        cutoff_time = now - lookback_seconds
        while self._order_events and self._order_events[0][0] < cutoff_time:
            self._order_events.popleft()

        if not self._order_events:
            return None  # No data yet

        # Count events in the lookback window
        placed_count = sum(1 for _, event_type in self._order_events if event_type == "placed")
        filled_count = sum(1 for _, event_type in self._order_events if event_type == "filled")

        if placed_count == 0:
            return None  # No orders placed yet

        # Fill rate = filled / placed
        # Note: This is approximate since some placed orders may still be open
        fill_rate = filled_count / placed_count
        return min(1.0, fill_rate)  # Cap at 100%

    def _update_fill_rate_spread_mult(self) -> None:
        """
        Update spread multiplier based on fill rate.
        - High fill rate (>80%) → widen spread (being picked off)
        - Low fill rate (<20%) → tighten spread (not competitive)
        """
        fill_rate = self._calculate_fill_rate()

        if fill_rate is None:
            # Not enough data yet, use neutral multiplier
            self._fill_rate_spread_mult = 1.0
            return

        high_threshold = self.config.fill_rate_high_threshold
        low_threshold = self.config.fill_rate_low_threshold

        if fill_rate > high_threshold:
            # Being picked off - widen spread
            # Linear interpolation from 1.0 to max_mult
            excess = (fill_rate - high_threshold) / (1.0 - high_threshold)
            self._fill_rate_spread_mult = 1.0 + excess * (self.config.fill_rate_spread_mult_max - 1.0)

        elif fill_rate < low_threshold:
            # Not competitive - tighten spread
            # Linear interpolation from min_mult to 1.0
            deficit = (low_threshold - fill_rate) / low_threshold
            self._fill_rate_spread_mult = self.config.fill_rate_spread_mult_min + \
                                         (1.0 - deficit) * (1.0 - self.config.fill_rate_spread_mult_min)
        else:
            # In the sweet spot - neutral multiplier
            self._fill_rate_spread_mult = 1.0

    def _update_entry_price(self, old_inventory: float, side: str, price: float, size: float) -> None:
        """Update average entry price based on fill."""
        new_inventory = self.inventory

        # Position flipped or went flat - reset entry tracking
        if (old_inventory >= 0 and new_inventory <= 0) or (old_inventory <= 0 and new_inventory >= 0):
            if abs(new_inventory) < 0.0001:  # Flat
                self._avg_entry_price = 0.0
                self._total_entry_cost = 0.0
            else:
                # Flipped sides - new entry price is fill price
                self._avg_entry_price = price
                self._total_entry_cost = abs(new_inventory) * price
            return

        # Adding to existing position
        if (old_inventory > 0 and side == "B") or (old_inventory < 0 and side == "A"):
            # Weighted average entry
            old_cost = abs(old_inventory) * self._avg_entry_price
            new_cost = size * price
            self._total_entry_cost = old_cost + new_cost
            self._avg_entry_price = self._total_entry_cost / abs(new_inventory) if new_inventory != 0 else 0
        # Reducing position - keep same avg entry (realized P&L handled separately)

    def _calculate_unrealized_pnl(self) -> float:
        """Calculate unrealized P&L based on current mark price vs entry."""
        if self.mark_price is None or abs(self.inventory) < 0.0001:
            return 0.0

        # Can't calculate if entry price is unknown (e.g., after restart)
        if self._avg_entry_price <= 0:
            return 0.0

        if self.inventory > 0:  # Long position
            return (self.mark_price - self._avg_entry_price) * self.inventory
        else:  # Short position
            return (self._avg_entry_price - self.mark_price) * abs(self.inventory)

    async def _emergency_flatten(self, reason: str) -> None:
        """Emergency flatten position using aggressive IOC orders with retry."""
        if abs(self.inventory) < 0.0001 or self.mark_price is None:
            return
        if self._emergency_flatten_in_progress:
            return

        self._emergency_flatten_in_progress = True
        self._paused_for_emergency = True

        try:
            self.loggers["errors"].warning("EMERGENCY FLATTEN: %s (inv=%.4f, unrealized=%.2f)",
                                           reason, self.inventory, self._unrealized_pnl)

            # Cancel all existing orders first
            await self.cancel_all()

            assert self.client is not None

            # Retry up to 3 times with increasingly aggressive pricing
            for attempt in range(3):
                if abs(self.inventory) < 0.0001:
                    self.loggers["main"].info("Emergency flatten complete - position closed")
                    return

                side = "sell" if self.inventory > 0 else "buy"
                # Increasingly aggressive pricing: 0.5%, 1%, 2% through the market
                slippage_pct = 0.005 * (attempt + 1)
                aggressive_offset = self.mark_price * slippage_pct

                if side == "sell":
                    price = self.mark_price - aggressive_offset  # Sell below market
                else:
                    price = self.mark_price + aggressive_offset  # Buy above market

                size_to_flatten = abs(self.inventory)

                # Use IOC (Immediate-Or-Cancel) instead of post-only for guaranteed execution
                ok = await self.client.place_ioc(
                    side,
                    price,
                    size_to_flatten,
                    cloid=self.client.new_cloid(),
                    reduce_only=True,
                )

                self.loggers["main"].info(
                    "Emergency flatten attempt %d: %s %.4f @ %.4f (slippage=%.1f%%) - %s",
                    attempt + 1, side, size_to_flatten, price, slippage_pct * 100,
                    "sent" if ok else "FAILED"
                )

                # Wait briefly for fill to process, then resync position
                await asyncio.sleep(0.5)
                await self._sync_position_from_exchange(log_startup=False)

            if abs(self.inventory) >= 0.0001:
                self.loggers["errors"].error(
                    "EMERGENCY FLATTEN FAILED after 3 attempts! Remaining inventory: %.4f",
                    self.inventory
                )
        finally:
            self._paused_for_emergency = False
            self._emergency_flatten_in_progress = False

    async def orderbook_listener(self) -> None:
        assert self.client is not None
        while not self._shutdown.is_set():
            stale = self._last_orderbook_update is None or (
                    asyncio.get_running_loop().time() - self._last_orderbook_update > 2
            )
            if stale:
                snapshot = await self.client.fetch_l2_snapshot()
                if snapshot:
                    self._apply_orderbook(snapshot)
                    self.loggers["main"].info(
                        "Applied fresh snapshot: bid=%.4f ask=%.4f", snapshot.bids[0].price, snapshot.asks[0].price
                    )
                elif not self.orderbook.bids or not self.orderbook.asks:
                    now = asyncio.get_running_loop().time()
                    if now - self._last_orderbook_stale_log_ts >= 5.0:
                        self.loggers["errors"].warning(
                            "Orderbook missing; quoting will remain paused until fresh data arrives"
                        )
                        self._last_orderbook_stale_log_ts = now
            await asyncio.sleep(self._orderbook_poll_interval)

    async def fills_listener(self) -> None:
        # Websocket subscriptions push fills; this loop simply keeps the task alive.
        while not self._shutdown.is_set():
            await asyncio.sleep(5)

    async def oracle_listener(self) -> None:
        # Websocket pushes oracle updates; this loop keeps task alive
        while not self._shutdown.is_set():
            await asyncio.sleep(5)

    async def oracle_watchdog_loop(self) -> None:
        """Restart bot if oracle stream stalls to avoid stale gates."""
        while not self._shutdown.is_set():
            now = asyncio.get_running_loop().time()
            if self._start_time and now - self._start_time < self._oracle_startup_grace:
                await asyncio.sleep(5)
                continue
            if self._last_oracle_update is None or now - self._last_oracle_update > self._oracle_stale_seconds:
                self.loggers["errors"].warning(
                    "Oracle watchdog: no oracle updates for %.0fs; attempting websocket reconnect",
                    self._oracle_stale_seconds,
                )
                reconnected = await self._reconnect_oracle_streams()
                if reconnected:
                    await asyncio.sleep(5)
                    continue
                self.loggers["errors"].warning("Oracle watchdog: websocket reconnect failed; restarting bot")
                await self.cancel_all()
                await self.shutdown()
                return
            await asyncio.sleep(5)

    async def _adaptive_update_loop(self) -> None:
        """Periodically update adaptive parameters: volatility, adverse selection, inventory limits."""
        self.loggers["main"].info("Adaptive update loop started")
        while not self._shutdown.is_set():
            try:
                self._update_volatility()
                self._update_baseline_volatility()
                self._update_adverse_selection()
                self._update_dynamic_inventory_limit()
                self._update_fill_rate_spread_mult()

                # Log adaptive state every minute
                fill_rate = self._calculate_fill_rate()
                fill_rate_str = f"{fill_rate*100:.1f}%" if fill_rate is not None else "N/A"
                self.loggers["main"].info(
                    "Adaptive state: vol=%.2fbps baseline=%.2fbps adv_sel=%.2fx max_inv=%.2f funding_boost=%.2fbps | "
                    "fill_rate=%s spread_mult=%.2fx",
                    self._current_volatility_bps,
                    self._auto_baseline_volatility_bps,
                    self._adverse_selection_multiplier,
                    self._effective_max_inventory,
                    self._funding_bias_boost,
                    fill_rate_str,
                    self._fill_rate_spread_mult,
                )
            except Exception as exc:
                self.loggers["errors"].warning("Adaptive update error: %s", exc)

            await asyncio.sleep(5)  # Update every 5 seconds

    def _update_volatility(self) -> None:
        """Calculate rolling volatility from price history."""
        if len(self._price_history) < 10:
            return  # Need minimum samples

        prices = list(self._price_history)
        # Calculate returns in bps
        returns_bps = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                ret = (prices[i] - prices[i - 1]) / prices[i - 1] * 10000
                returns_bps.append(ret)

        if not returns_bps:
            return

        # Calculate standard deviation of returns
        mean_ret = sum(returns_bps) / len(returns_bps)
        variance = sum((r - mean_ret) ** 2 for r in returns_bps) / len(returns_bps)
        std_dev = math.sqrt(variance) if variance > 0 else 0

        # Smooth the volatility update (exponential moving average)
        alpha = 0.3  # Smoothing factor
        self._current_volatility_bps = (
                alpha * std_dev + (1 - alpha) * self._current_volatility_bps
        )

    def _update_baseline_volatility(self) -> None:
        """Calculate baseline volatility from past 1 hour of price data."""
        now = time.time()
        one_hour_ago = now - 3600  # 1 hour in seconds

        # Prune old data (older than 1 hour)
        while self._hourly_price_history and self._hourly_price_history[0][0] < one_hour_ago:
            self._hourly_price_history.popleft()

        # Need at least 30 samples for initial baseline calculation
        if len(self._hourly_price_history) < 30:
            return  # Keep using config value until we have enough data

        prices = [p for _, p in self._hourly_price_history]

        # Calculate returns in bps
        returns_bps = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                ret = (prices[i] - prices[i - 1]) / prices[i - 1] * 10000
                returns_bps.append(ret)

        if len(returns_bps) < 20:
            return

        # Calculate standard deviation of returns
        mean_ret = sum(returns_bps) / len(returns_bps)
        variance = sum((r - mean_ret) ** 2 for r in returns_bps) / len(returns_bps)
        std_dev = math.sqrt(variance) if variance > 0 else 0

        # Floor at minimum to avoid division issues
        calculated_baseline = max(1.0, std_dev)

        # Adaptive alpha: fast adaptation when far from calculated, slow when close
        # This allows quick startup adaptation but stable operation
        diff_ratio = abs(calculated_baseline - self._auto_baseline_volatility_bps) / max(
            self._auto_baseline_volatility_bps, 1)
        if diff_ratio > 0.5:
            # Large difference (>50%): adapt quickly
            alpha = 0.5
        elif diff_ratio > 0.2:
            # Moderate difference: medium speed
            alpha = 0.3
        else:
            # Close to target: slow smoothing
            alpha = 0.1

        self._auto_baseline_volatility_bps = (
                alpha * calculated_baseline + (1 - alpha) * self._auto_baseline_volatility_bps
        )

    def _update_adverse_selection(self) -> None:
        """Analyze recent fills to detect adverse selection (being picked off)."""
        if not self._recent_fills or self.mark_price is None:
            self._adverse_selection_multiplier = 1.0
            return

        # Calculate average unrealized P&L of recent fills
        total_pnl = 0.0
        total_notional = 0.0

        for fill in self._recent_fills:
            pnl = fill.unrealized_pnl(self.mark_price)
            notional = fill.price * fill.size
            total_pnl += pnl
            total_notional += notional

        if total_notional == 0:
            self._adverse_selection_multiplier = 1.0
            return

        # P&L as percentage of notional
        pnl_pct = total_pnl / total_notional

        # If we're losing on recent fills, widen spreads
        threshold = self.config.adverse_selection_threshold  # e.g., -0.002 = -0.2%
        if pnl_pct < threshold:
            # Scale multiplier based on how bad the adverse selection is
            severity = abs(pnl_pct / threshold)
            self._adverse_selection_multiplier = 1.0 + min(severity * 0.5, 2.0)  # Cap at 3x
            self.loggers["main"].info(
                "Adverse selection detected: recent_pnl=%.4f%% multiplier=%.2fx",
                pnl_pct * 100, self._adverse_selection_multiplier
            )
        else:
            # Gradually reduce multiplier back to 1.0
            self._adverse_selection_multiplier = max(
                1.0, self._adverse_selection_multiplier * 0.95
            )

    def _update_dynamic_inventory_limit(self) -> None:
        """Adjust max inventory based on current volatility.

        - High volatility → reduce limit (safety)
        - Low volatility → keep at config limit (never exceed user's hard cap)
        """
        # Use auto-calculated baseline from 1-hour history
        baseline_vol = self._auto_baseline_volatility_bps
        current_vol = self._current_volatility_bps

        if baseline_vol <= 0:
            self._effective_max_inventory = self.config.max_inventory
            return

        # Higher volatility = lower inventory limit
        # vol_ratio > 1 means higher than baseline volatility
        vol_ratio = current_vol / baseline_vol

        if vol_ratio > 1.0:
            # High volatility: reduce inventory limit
            self._effective_max_inventory = self.config.max_inventory / vol_ratio
            # Floor at minimum of 2 orders worth
            self._effective_max_inventory = max(
                self.config.order_size * 2,
                self._effective_max_inventory
            )
        else:
            # Low/normal volatility: use config limit (NEVER exceed it)
            self._effective_max_inventory = self.config.max_inventory

    def _is_us_market_hours(self) -> bool:
        """Check if current time is during US market hours (9:30-16:00 ET)."""
        # Convert to US Eastern time
        now_utc = datetime.now(timezone.utc)
        # Eastern time is UTC-5 (EST) or UTC-4 (EDT)
        # For simplicity, use UTC-5 (close enough for trading purposes)
        eastern_hour = (now_utc.hour - 5) % 24
        eastern_minute = now_utc.minute

        # Market hours: 9:30 AM to 4:00 PM ET
        time_decimal = eastern_hour + eastern_minute / 60
        return 9.5 <= time_decimal <= 16.0

    def _get_time_of_day_multiplier(self) -> float:
        """Get spread multiplier based on time of day."""
        if self._is_us_market_hours():
            return self.config.us_market_hours_spread_mult
        return 1.0

    def _is_orderbook_stale(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = asyncio.get_running_loop().time()
        if self._last_orderbook_update is None:
            return True
        return (now - self._last_orderbook_update) > self._orderbook_stale_seconds

    def _compute_day_start_ms(self, now_ms: Optional[int] = None) -> int:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        offset = timedelta(hours=self.config.stats_day_tz_offset_hours)
        now_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        local = now_utc + offset
        local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_start - offset
        return int(start_utc.timestamp() * 1000)

    def _ensure_daily_window(self, now_ms: Optional[int] = None) -> None:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        day_start = self._compute_day_start_ms(now_ms)
        if day_start != self._stats_day_start_ms:
            self._stats_day_start_ms = day_start
            self._daily_realized_pnl = 0.0
            self._daily_total_fills = 0
            self._daily_volume = 0.0
            self._daily_estimated_rebates = 0.0
            self._daily_fill_ids.clear()
            self._daily_fill_buffer.clear()
            self._daily_backfill_complete = False
            self._daily_needs_backfill = True

    def _daily_fill_id(self, fill: Dict, fill_time_ms: int) -> str:
        fill_hash = fill.get("hash")
        if fill_hash:
            return str(fill_hash)
        oid = fill.get("oid")
        cloid = fill.get("cloid")
        return f"{oid}:{cloid}:{fill_time_ms}:{fill.get('side')}:{fill.get('sz')}:{fill.get('px')}"

    def _record_daily_stats_from_fill(
        self, fill: Dict, notional: float, pnl_delta: float, fill_time_ms: int
    ) -> None:
        if fill_time_ms < self._stats_day_start_ms:
            return
        fill_id = self._daily_fill_id(fill, fill_time_ms)
        if fill_id in self._daily_fill_ids:
            return
        self._daily_fill_ids.add(fill_id)
        self._daily_total_fills += 1
        self._daily_volume += notional
        self._daily_estimated_rebates += notional * 0.0002
        self._daily_realized_pnl += pnl_delta

    async def _refresh_daily_stats_from_exchange(self) -> None:
        if self._daily_backfill_in_flight:
            return
        if self.client is None or self.client.info is None:
            return

        self._daily_backfill_in_flight = True
        try:
            end_ms = int(time.time() * 1000)
            self._ensure_daily_window(end_ms)
            start_ms = self._stats_day_start_ms
            account = self.config.account_address or self.client.wallet.address

            def _fetch():
                return self.client.info.user_fills_by_time(
                    account, start_ms, end_ms, aggregate_by_time=False
                )

            fills = await asyncio.get_running_loop().run_in_executor(None, _fetch)
            fills = fills or []

            coin_name = self.client.coin_name
            coin_name_without_prefix = coin_name.split(":")[-1] if ":" in coin_name else coin_name

            daily_ids: set[str] = set()
            realized = 0.0
            total_fills = 0
            volume = 0.0
            rebates = 0.0

            for fill in fills:
                try:
                    fill_coin = fill.get("coin")
                    if fill_coin != coin_name and fill_coin != coin_name_without_prefix:
                        continue
                    fill_time_ms = fill.get("time")
                    if fill_time_ms is None:
                        continue
                    fill_time_ms = int(fill_time_ms)
                    if fill_time_ms < start_ms or fill_time_ms > end_ms:
                        continue
                    price = self._resolve_fill_price(fill) or float(fill.get("px") or 0)
                    size = float(fill.get("sz", 0))
                    if price <= 0 or size <= 0:
                        continue
                    notional = price * size
                    pnl_delta = float(fill.get("closedPnl", 0))
                    fill_id = self._daily_fill_id(fill, fill_time_ms)
                    if fill_id in daily_ids:
                        continue
                    daily_ids.add(fill_id)
                    total_fills += 1
                    volume += notional
                    rebates += notional * 0.0002
                    realized += pnl_delta
                except Exception:
                    continue

            self._daily_realized_pnl = realized
            self._daily_total_fills = total_fills
            self._daily_volume = volume
            self._daily_estimated_rebates = rebates
            self._daily_fill_ids = daily_ids
            self._daily_backfill_end_ms = end_ms
            self._daily_backfill_complete = True
            self._daily_needs_backfill = False

            if self._daily_fill_buffer:
                buffered = self._daily_fill_buffer
                self._daily_fill_buffer = []
                for fill, notional, pnl_delta, fill_time_ms in buffered:
                    if self._daily_backfill_end_ms is not None and fill_time_ms <= self._daily_backfill_end_ms:
                        continue
                    self._record_daily_stats_from_fill(fill, notional, pnl_delta, fill_time_ms)
        finally:
            self._daily_backfill_in_flight = False

    async def _place_taker_volume_orders(self, mid_price: float) -> None:
        """Place IOC buy + sell at market to generate taker volume and unlock more API requests.
        Buys then immediately sells order_size at market, generating ~2 * order_size * price in volume.
        Cost: taker fee (~0.035%) on both legs."""
        order_size = self.config.order_size
        if order_size <= 0 or mid_price <= 0:
            return

        if not self.orderbook.bids or not self.orderbook.asks:
            self.loggers["main"].warning("Cannot place taker volume orders — no orderbook")
            return
        best_bid = self.orderbook.bids[0].price
        best_ask = self.orderbook.asks[0].price

        # Calculate how many round trips we need
        volume_per_trip = 2 * order_size * mid_price
        trips_needed = 1
        if self.client._rl_calibrated:
            deficit = self.client._rl_requests_sent + self.client._rl_session_api_calls - self.client._rl_requests_allowed
            # Add 20% buffer over deficit
            volume_needed = max(deficit * 1.2, volume_per_trip)
            trips_needed = max(1, math.ceil(volume_needed / volume_per_trip))
            trips_needed = min(trips_needed, 5)  # Cap at 5 round trips

        self.loggers["main"].info(
            "Placing %d taker volume round-trip(s): size=%.4f, mid=%.2f, est_volume=$%.2f",
            trips_needed, order_size, mid_price, trips_needed * volume_per_trip,
        )

        for i in range(trips_needed):
            # Buy at best_ask + 0.5% slippage to ensure fill
            buy_price = best_ask * 1.005
            buy_cloid = f"0xrl{secrets.token_hex(14)}"
            self._taker_volume_cloids.add(buy_cloid)
            buy_ok = await self.client.place_ioc(
                "buy", buy_price, order_size, cloid=buy_cloid, bypass_rate_limit=True,
            )
            if not buy_ok:
                self.loggers["errors"].warning("Taker volume buy leg %d failed — aborting", i + 1)
                break

            # Sell at best_bid - 0.5% slippage to ensure fill
            sell_price = best_bid * 0.995
            sell_cloid = f"0xrl{secrets.token_hex(14)}"
            self._taker_volume_cloids.add(sell_cloid)
            sell_ok = await self.client.place_ioc(
                "sell", sell_price, order_size, cloid=sell_cloid, bypass_rate_limit=True,
            )
            if not sell_ok:
                self.loggers["errors"].warning("Taker volume sell leg %d failed — aborting", i + 1)
                break

            self.loggers["main"].info("Taker volume round-trip %d/%d complete", i + 1, trips_needed)

        # After taker volume, update calibration: our allowed requests should have increased
        if self.client._rl_calibrated:
            added_volume = trips_needed * volume_per_trip
            self.client._rl_requests_allowed += int(added_volume)
            self.client._rl_session_api_calls = 0  # Reset session counter
            self.loggers["main"].info(
                "Rate limit updated: ~%d new allowed requests from $%.0f taker volume",
                int(added_volume), added_volume,
            )

    async def quoting_loop(self) -> None:
        self.loggers["main"].info("Quoting loop started")
        loop_count = 0
        while not self._shutdown.is_set():
            try:
                loop_count += 1
                # Rate limit: check if taker volume orders are needed (reactive or preemptive)
                if self.client is not None and (
                    self.client._rl_taker_volume_pending or self.client._needs_preemptive_taker_volume()
                ):
                    mid = self.orderbook.mid_price
                    if mid is not None:
                        await self._place_taker_volume_orders(mid)
                    self.client._rl_taker_volume_pending = False

                if self.client is not None and self.client._is_rate_limited():
                    remaining = self.client._rate_limit_backoff_until - time.monotonic()
                    if loop_count % 40 == 1:  # Log every ~10s
                        self.loggers["main"].warning(
                            "Quoting paused — rate limited (%.0fs remaining)", remaining
                        )
                    await asyncio.sleep(1)
                    continue

                if self._paused_for_funding or self._paused_for_emergency:
                    if self._paused_for_funding:
                        self.loggers["main"].info("Quoting paused for funding")
                    if self._paused_for_emergency:
                        self.loggers["main"].info("Quoting paused for emergency flatten")
                    await asyncio.sleep(1)
                    continue

                now = asyncio.get_running_loop().time()
                if self._is_orderbook_stale(now):
                    if not self._orderbook_stale:
                        self._orderbook_stale = True
                        age = (
                            None if self._last_orderbook_update is None else now - self._last_orderbook_update
                        )
                        age_str = "unknown" if age is None else f"{age:.1f}s"
                        self.loggers["errors"].warning(
                            "Orderbook stale (%s); pausing quoting and cancelling resting orders", age_str
                        )
                        self._last_orderbook_stale_log_ts = now
                        await self.cancel_all()
                    elif now - self._last_orderbook_stale_log_ts >= 5.0:
                        self.loggers["errors"].warning("Orderbook still stale; quoting paused")
                        self._last_orderbook_stale_log_ts = now
                    await asyncio.sleep(0.25)
                    continue

                mid = self.orderbook.mid_price
                if mid is None:
                    if loop_count % 100 == 1:  # Log every ~5 seconds when waiting
                        self.loggers["main"].info("Waiting for orderbook data (mid=None, bids=%d, asks=%d)",
                                                  len(self.orderbook.bids), len(self.orderbook.asks))
                    await asyncio.sleep(0.05)
                    continue

                bid, ask = self.compute_quotes(mid)
                oracle_mode = self.oracle_mode(mid)
                oracle_bid_enabled, oracle_ask_enabled = oracle_mode

                # Inventory-aware side gating: disable side that would ADD to current position
                # If we have long inventory, disable buys (which add to longs)
                # If we have short inventory, disable sells (which add to shorts)
                inv_bid_enabled, inv_ask_enabled = self._inventory_side_gate()

                bid_enabled = oracle_bid_enabled and inv_bid_enabled
                ask_enabled = oracle_ask_enabled and inv_ask_enabled

                if loop_count == 1 or loop_count % 20 == 0:  # Log first iteration and every ~5s
                    self.loggers["main"].info(
                        "Quoting mid=%.4f bid=%.4f ask=%.4f bid_en=%s ask_en=%s inv=%.4f",
                        mid, bid, ask, bid_enabled, ask_enabled, self.inventory
                    )

                await self.maintain_quotes_layered("buy", bid, enabled=bid_enabled)
                await self.maintain_quotes_layered("sell", ask, enabled=ask_enabled)

                await asyncio.sleep(0.25)
            except Exception as exc:
                self.loggers["errors"].error("Quoting loop error: %s", exc, exc_info=True)
                self.loggers["main"].error("Quoting loop error: %s", exc)
                await asyncio.sleep(1)  # Brief pause before retrying

    async def maintain_quotes_layered(self, side: str, base_price: float, *, enabled: bool) -> None:
        """
        Maintain multiple layered orders for volume generation and rebates.

        Args:
            side: "buy" or "sell"
            base_price: The closest price level (from compute_quotes)
            enabled: Whether this side should have active orders
        """
        assert self.client is not None
        num_layers = self.config.num_layers
        layer_spacing_bps = self.config.layer_spacing_bps
        order_size = self.config.order_size

        # Calculate all target price levels
        target_prices = []
        for i in range(num_layers):
            if side == "buy":
                # Buys: spread downward (lower prices)
                offset_bps = -i * layer_spacing_bps
            else:
                # Sells: spread upward (higher prices)
                offset_bps = i * layer_spacing_bps

            offset = offset_bps * 0.0001 * base_price
            layer_price = base_price + offset
            safe_price = self.client._sanitize_price(layer_price)

            # Post-only clamping for closest layer only (to avoid crossing spread)
            # Use 2-tick buffer to reduce rejections from spread movement
            if i == 0 and enabled and self.orderbook.bids and self.orderbook.asks:
                best_bid = self.orderbook.bids[0].price
                best_ask = self.orderbook.asks[0].price
                tick = float(self.client.price_increment)
                safety_ticks = 2  # Stay 2 ticks away from spread to avoid rejections

                if side == "buy" and safe_price >= best_ask - (safety_ticks * tick):
                    limit = best_ask - (safety_ticks * tick)
                    if safe_price > limit:
                        safe_price = self.client._sanitize_price(limit)
                elif side == "sell" and safe_price <= best_bid + (safety_ticks * tick):
                    limit = best_bid + (safety_ticks * tick)
                    if safe_price < limit:
                        safe_price = self.client._sanitize_price(limit)

            target_prices.append(safe_price)

        # Get existing orders for this side
        existing_orders = self._active_orders[side]
        if existing_orders:
            # Keep closest layer at index 0 for refresh checks.
            existing_orders.sort(key=lambda x: x[1], reverse=(side == "buy"))

        # If disabled, cancel all orders on this side
        if not enabled:
            if existing_orders:
                cloids_to_cancel = [cloid for cloid, _, _ in existing_orders]
                self.loggers["main"].info("%s side disabled, cancelling %d orders", side, len(cloids_to_cancel))
                cancel_ok = await self._cancel_tracked_orders(cloids_to_cancel)
                if cancel_ok:
                    self._active_orders[side] = []
                    self._update_exposure_tracking()
                else:
                    self.loggers["errors"].warning("Failed to cancel some %s orders on disable", side)
            return

        # Determine which orders need to be refreshed
        # Check if we need to refresh (price has moved significantly)
        needs_refresh = False
        if existing_orders:
            # Check if closest layer price has moved enough
            if existing_orders:
                _, closest_existing_price, _ = existing_orders[0]  # First order is closest
                price_change_bps = abs(target_prices[0] - closest_existing_price) / max(closest_existing_price, 1) * 10_000

                if price_change_bps >= self.config.quote_refresh_threshold_bps:
                    # Check cooldown
                    now = asyncio.get_running_loop().time()
                    time_since_refresh = now - self._last_quote_refresh.get(side, 0)
                    min_refresh_interval = 2.0

                    if time_since_refresh >= min_refresh_interval:
                        needs_refresh = True

        # If we have no orders or need refresh, cancel all and place new layers
        if not existing_orders or needs_refresh:
            if existing_orders:
                # Cancel existing orders
                cloids_to_cancel = [cloid for cloid, _, _ in existing_orders]
                self.loggers["main"].info(
                    "%s refreshing %d layers: old_px=%.6f new_px=%.6f",
                    side, len(cloids_to_cancel), existing_orders[0][1] if existing_orders else 0, target_prices[0]
                )
                cancel_ok = await self._cancel_tracked_orders(cloids_to_cancel)
                if not cancel_ok:
                    self.loggers["errors"].warning("Cancel failed for %s layers, skipping refresh", side)
                    return
                self._active_orders[side] = []
                self._update_exposure_tracking()

            # Skip placing if margin is exhausted (avoid spamming rejected orders)
            margin_exhausted_until = self._margin_exhausted_until_by_side.get(side, 0.0)
            if time.monotonic() < margin_exhausted_until:
                self.loggers["main"].debug(
                    "Skipping %s orders — margin exhausted (%.0fs remaining)",
                    side, margin_exhausted_until - time.monotonic(),
                )
                return

            # Place new layered orders
            new_orders = []
            for i, target_price in enumerate(target_prices):
                new_cloid = self.client.new_cloid()
                ok = await self.client.place_post_only(side, target_price, order_size, cloid=new_cloid)
                if ok:
                    new_orders.append((new_cloid, target_price, order_size))
                    self.client._margin_rejected = False  # Reset on success
                    # Track order placed event for fill rate calculation
                    now = asyncio.get_running_loop().time()
                    self._order_events.append((now, "placed"))
                    self.loggers["main"].info(
                        "Resting %s layer %d/%d cloid=%s px=%.6f sz=%.4f",
                        side, i+1, num_layers, new_cloid, target_price, order_size
                    )
                else:
                    # If margin rejected, set cooldown and stop placing more layers
                    if self.client._margin_rejected:
                        self._margin_exhausted_until_by_side[side] = (
                            time.monotonic() + self._margin_exhausted_cooldown
                        )
                        self.loggers["errors"].warning(
                            "Margin exhausted — pausing %s orders for %.0fs",
                            side, self._margin_exhausted_cooldown,
                        )
                        self.client._margin_rejected = False
                        break

            self._active_orders[side] = new_orders
            self._update_exposure_tracking()
            self._last_quote_refresh[side] = asyncio.get_running_loop().time()
            return

        # If we are missing layers, try to add them without canceling everything.
        if len(existing_orders) < num_layers:
            tick = float(self.client.price_increment) if self.client.price_increment else 0.0

            def _price_matches(left: float, right: float) -> bool:
                if tick <= 0:
                    return abs(left - right) <= 1e-9
                return abs(left - right) <= (tick / 2)

            existing_prices = [price for _, price, _ in existing_orders]
            missing_targets = [
                (i, target_price)
                for i, target_price in enumerate(target_prices)
                if not any(_price_matches(target_price, existing_price) for existing_price in existing_prices)
            ]

            if missing_targets:
                now = asyncio.get_running_loop().time()
                time_since_refresh = now - self._last_quote_refresh.get(side, 0)
                min_refresh_interval = 2.0

                if time_since_refresh >= min_refresh_interval:
                    if time.monotonic() < self._margin_exhausted_until_by_side.get(side, 0.0):
                        return
                    for i, target_price in missing_targets:
                        new_cloid = self.client.new_cloid()
                        ok = await self.client.place_post_only(side, target_price, order_size, cloid=new_cloid)
                        if ok:
                            existing_orders.append((new_cloid, target_price, order_size))
                            self.client._margin_rejected = False
                            self._order_events.append((now, "placed"))
                            self.loggers["main"].info(
                                "Resting %s layer %d/%d cloid=%s px=%.6f sz=%.4f",
                                side, i + 1, num_layers, new_cloid, target_price, order_size
                            )
                        elif self.client._margin_rejected:
                            self._margin_exhausted_until_by_side[side] = (
                                time.monotonic() + self._margin_exhausted_cooldown
                            )
                            self.client._margin_rejected = False
                            break
                    existing_orders.sort(key=lambda x: x[1], reverse=(side == "buy"))
                    self._active_orders[side] = existing_orders
                    self._update_exposure_tracking()
                    self._last_quote_refresh[side] = now

    def _update_exposure_tracking(self) -> None:
        """Update total buy/sell exposure based on active orders."""
        self._total_buy_exposure = sum(size for _, _, size in self._active_orders["buy"])
        self._total_sell_exposure = sum(size for _, _, size in self._active_orders["sell"])

    async def maintain_quote(self, side: str, price: float, *, enabled: bool) -> None:
        assert self.client is not None
        safe_price = self.client._sanitize_price(price)

        # Post-only clamping: ensure we don't cross the spread if skew is aggressive
        # If we are bidding, price must be < best ask. If selling, price must be > best bid.
        # We rely on the orderbook being reasonably fresh.
        if enabled and self.orderbook.bids and self.orderbook.asks:
            best_bid = self.orderbook.bids[0].price
            best_ask = self.orderbook.asks[0].price
            tick = float(self.client.price_increment)  # Convert Decimal to float for arithmetic
            if side == "buy" and safe_price >= best_ask:
                # Our skewed bid is crossing the spread. Clamp to best_ask - 1 tick.
                limit = best_ask - tick
                if safe_price > limit:
                    safe_price = self.client._sanitize_price(limit)
            elif side == "sell" and safe_price <= best_bid:
                # Our skewed ask is crossing. Clamp to best_bid + 1 tick.
                limit = best_bid + tick
                if safe_price < limit:
                    safe_price = self.client._sanitize_price(limit)

        existing = self._active_orders.get(side)
        if not enabled:
            if existing:
                self.loggers["main"].info("%s side disabled, cancelling cloid=%s", side, existing[0])
                cancel_ok = await self._cancel_tracked_orders([existing[0]])
                if cancel_ok:
                    self._active_orders.pop(side, None)
                else:
                    self.loggers["errors"].warning("Failed to cancel %s order on disable", side)
            return

        if existing:
            _, current_price = existing
            price_change = abs(safe_price - current_price) / max(current_price, 1) * 10_000

            # Check cooldown - minimum 2 seconds between refreshes to prevent spam
            now = asyncio.get_running_loop().time()
            time_since_refresh = now - self._last_quote_refresh.get(side, 0)
            min_refresh_interval = 2.0  # seconds

            if price_change < self.config.quote_refresh_threshold_bps:
                # Price hasn't moved enough, keep existing order
                return

            if time_since_refresh < min_refresh_interval:
                # Cooldown not elapsed, don't spam refreshes
                return

            self.loggers["main"].info(
                "%s quote refresh: old_px=%.6f new_px=%.6f (%.4fbps) cloid=%s",
                side,
                current_price,
                safe_price,
                price_change,
                existing[0],
            )
            cancel_ok = await self._cancel_tracked_orders([existing[0]])
            if not cancel_ok:
                # Cancel failed - don't place new order to avoid duplicates
                self.loggers["errors"].warning("Cancel failed for %s, skipping new order to avoid duplicates", side)
                return
            self._active_orders.pop(side, None)  # Clear tracking after successful cancel
            self._last_quote_refresh[side] = now

        new_cloid = self.client.new_cloid()
        ok = await self.client.place_post_only(side, safe_price, self.config.order_size, cloid=new_cloid)
        if ok:
            self._active_orders[side] = (new_cloid, safe_price)
            self._last_quote_refresh[side] = asyncio.get_running_loop().time()  # Track placement time
            self.loggers["main"].info("Resting %s quote cloid=%s px=%.6f", side, new_cloid, safe_price)

    def compute_quotes(self, mid_price: float) -> Tuple[float, float]:
        # === BASE SPREAD ===
        spread = self.config.spread_bps * 0.0001 * mid_price

        # === SPREAD MULTIPLIERS (NEW: Adaptive) ===
        # 1. Volatility-based spread adjustment (use auto-calculated baseline)
        vol_ratio = self._current_volatility_bps / max(self._auto_baseline_volatility_bps, 1)
        volatility_multiplier = 1.0 + max(0, vol_ratio - 1) * 0.5  # Widen 50% for each 1x above baseline

        # 2. Adverse selection multiplier (widen when getting picked off)
        adverse_sel_multiplier = self._adverse_selection_multiplier

        # 3. Time-of-day multiplier (wider during US market hours)
        time_multiplier = self._get_time_of_day_multiplier()

        # 4. Legacy volatility guard (orderbook spread-based)
        legacy_vol_guard = 1.0
        if self.orderbook.spread_bps and self.orderbook.spread_bps > self.config.volatility_guard_threshold_bps:
            legacy_vol_guard = self.config.volatility_guard_multiplier

        # 5. Fill rate multiplier (adaptive based on how often we get filled)
        fill_rate_multiplier = self._fill_rate_spread_mult

        # Apply all multipliers
        total_spread_mult = (volatility_multiplier * adverse_sel_multiplier * time_multiplier *
                           legacy_vol_guard * fill_rate_multiplier)
        spread *= total_spread_mult

        # === SKEWS ===
        ob_imbalance = self.orderbook.imbalance() or 0.0
        orderbook_skew = 0.0
        if ob_imbalance > 0.3:
            orderbook_skew = self.config.orderbook_skew_bps * 0.0001 * mid_price
        elif ob_imbalance < -0.3:
            orderbook_skew = -self.config.orderbook_skew_bps * 0.0001 * mid_price

        # Use DYNAMIC max inventory for inventory ratio
        effective_max = max(self._effective_max_inventory, 0.001)
        inventory_ratio = max(-1.0, min(1.0, self.inventory / effective_max))
        inventory_skew = -self.config.inventory_skew_bps * inventory_ratio * mid_price * 0.0001

        # Basis skew: fade the premium/discount vs oracle
        basis_skew = 0.0
        if self.last_oracle_price:
            oracle_diff_bps = (mid_price - self.last_oracle_price) / self.last_oracle_price * 10000
            clamped_diff = max(-500.0, min(500.0, oracle_diff_bps))
            basis_skew = -clamped_diff * 0.5 * mid_price * 0.0001

        # === LONG BIAS (Inventory-Aware + Funding Alpha) ===
        # Base long bias from config + funding boost
        base_long_bias = self.config.long_bias_bps + self._funding_bias_boost

        # KEY FIX: Reduce long bias as we accumulate long inventory
        # - When flat/short: Apply FULL bias (want to accumulate)
        # - When already long: REDUCE bias (already have position, let sells catch up)
        # This prevents aggressive buying when we're already long
        if self.inventory > 0:
            # Scale down bias based on how much long we already have
            # At 50% of max inventory, bias is reduced to 0
            # At 100% of max inventory, bias is actually negative (favor selling)
            long_ratio = self.inventory / max(self._effective_max_inventory * 0.5, 0.001)
            bias_scale = max(0, 1 - long_ratio)  # 1.0 when flat, 0 when at 50% limit
            effective_long_bias = base_long_bias * bias_scale
        elif self.inventory < 0:
            # When short, apply EXTRA bias to close shorts quickly
            effective_long_bias = base_long_bias * 1.5  # 50% more aggressive buying
        else:
            # Flat: apply full bias
            effective_long_bias = base_long_bias

        long_bias = effective_long_bias * 0.0001 * mid_price

        total_skew = orderbook_skew + inventory_skew + basis_skew + long_bias

        bid_price = mid_price - (spread / 2) + total_skew
        ask_price = mid_price + (spread / 2) + total_skew
        return bid_price, ask_price

    def oracle_mode(self, mark_price: float) -> Tuple[bool, bool]:
        if self.last_oracle_price is None:
            return True, True
        delta = (mark_price - self.last_oracle_price) / self.last_oracle_price
        threshold = self.config.oracle_deviation_threshold_bps / 10_000
        if abs(delta) <= threshold:
            return True, True
        now = time.time()
        if now - self._last_oracle_gate_log_ts >= 5.0:
            delta_bps = delta * 10_000
            self.loggers["main"].info(
                "Oracle gate: mark=%.4f oracle=%.4f diff=%.2fbps threshold=%.2fbps",
                mark_price,
                self.last_oracle_price,
                delta_bps,
                self.config.oracle_deviation_threshold_bps,
            )
            self._last_oracle_gate_log_ts = now
        if delta > 0:
            # Mark above oracle: avoid buying high, allow sells.
            return False, True
        # Mark below oracle: avoid selling low, allow buys.
        return True, False

    def _inventory_side_gate(self) -> Tuple[bool, bool]:
        """
        Returns (bid_enabled, ask_enabled) based on current inventory and total open order exposure.

        CRITICAL FOR LAYERED ORDERS: Accounts for ALL open orders that could fill simultaneously.
        Total exposure = inventory + total_buy_exposure (for buys) or inventory - total_sell_exposure (for sells)

        ASYMMETRIC logic for bullish assets (controlled by long_bias_bps + funding boost):
        - If effective_long_bias > 0: We WANT to accumulate longs, so:
          * Allow buying until we reach a "long accumulation threshold" (DYNAMIC max)
          * Immediately stop selling once we have ANY short (force close shorts quickly)
        - If effective_long_bias == 0: Symmetric mean-reversion (original behavior)
        - If effective_long_bias < 0: Inverse (favor shorts)

        Uses DYNAMIC max inventory (adjusted for volatility) instead of static config.
        """
        position_threshold = self.config.order_size * 0.1
        # Total exposure if all pending buy/sell orders fill
        total_buy_size = self.config.order_size * self.config.num_layers
        total_sell_size = self.config.order_size * self.config.num_layers

        # Effective bias includes funding rate alpha
        effective_bias = self.config.long_bias_bps + self._funding_bias_boost

        # Use DYNAMIC max inventory (adjusted for current volatility)
        max_inv = self._effective_max_inventory

        has_long_bias = effective_bias > 0
        has_short_bias = effective_bias < 0

        if has_long_bias:
            # BULLISH MODE: favor accumulating longs
            # Allow buys until we hit max inventory (use FULL limit, not 50%)
            long_limit = max_inv

            if self.inventory < -position_threshold:
                # Short inventory: ONLY buy to close (never add to shorts in bullish mode)
                return True, False
            elif self.inventory + total_buy_size <= long_limit:
                # Room for all buy layers without exceeding limit
                # Use <= to ensure we stay within bounds even if all orders fill
                return True, True
            else:
                # Would reach/exceed max inventory if all buy layers fill: only allow sells to reduce
                return False, True

        elif has_short_bias:
            # BEARISH MODE: favor accumulating shorts (inverse of bullish)
            short_limit = -max_inv

            if self.inventory > position_threshold:
                # Long inventory: ONLY sell to close
                return False, True
            elif self.inventory - total_sell_size >= short_limit:
                # Room for all sell layers without exceeding limit
                return True, True
            else:
                # Would reach/exceed max short inventory if all sell layers fill: only allow buys
                return True, False

        else:
            # NEUTRAL MODE: symmetric, respect max_inventory on both sides
            # Check if ALL layers would reach/exceed limits
            if self.inventory + total_buy_size > max_inv:
                # All buy layers would exceed max long: only sells
                return False, True
            elif self.inventory - total_sell_size < -max_inv:
                # All sell layers would exceed max short: only buys
                return True, False
            else:
                # Within limits: quote both sides
                return True, True

    async def cancel_all(self) -> None:
        """Cancel all open orders for this coin on the exchange."""
        assert self.client is not None
        self.loggers["main"].info("Cancelling ALL open orders for this coin")

        # For layered orders, we track what we placed, so cancel those directly
        all_cloids = []
        for side in ["buy", "sell"]:
            cloids = [cloid for cloid, _, _ in self._active_orders[side]]
            all_cloids.extend(cloids)

        if all_cloids:
            self.loggers["main"].info("Cancelling %d tracked orders by cloid", len(all_cloids))
            await self.client.cancel_by_cloids(all_cloids)

        # Also try the exchange-level cancel (for any orphaned orders)
        await self.client.cancel_all_open_orders()

        self._active_orders = {"buy": [], "sell": []}
        self._update_exposure_tracking()

    async def funding_flush_loop(self) -> None:
        while not self._shutdown.is_set():
            seconds_to_funding, next_epoch = self.seconds_until_next_funding()
            should_flush = (
                    seconds_to_funding <= self.config.funding_flush_lead_seconds
                    and (self._last_funding_epoch is None or self._last_funding_epoch != next_epoch)
            )
            if should_flush:
                self.loggers["main"].info(
                    "Entering funding flush window: %ss to %s", seconds_to_funding, next_epoch.isoformat()
                )
                await self.funding_flush(seconds_to_funding, next_epoch)
                await asyncio.sleep(self.config.funding_resume_delay_seconds)
                self._paused_for_funding = False
                # cancel_all() already resets _active_orders properly
            await asyncio.sleep(1)

    async def funding_flush(self, seconds_to_funding: int, next_epoch: datetime) -> None:
        if self._flush_in_progress:
            return
        self._flush_in_progress = True
        self._paused_for_funding = True
        await self.cancel_all()

        if abs(self.inventory) < 0.0001 or self.mark_price is None:
            self._flush_in_progress = False
            # Bug fix: don't leave paused if we exit early
            # We will rely on funding_flush_loop to unpause after delay
            return

        side = "sell" if self.inventory > 0 else "buy"
        lead = max(self.config.funding_flush_lead_seconds, 1)
        progress = (lead - seconds_to_funding) / lead  # 0 at start, 1 near funding

        # As funding approaches, be MORE aggressive (willing to cross spread)
        # progress 0 = 0.1% offset (passive), progress 1 = 0.5% offset (aggressive)
        slippage_pct = 0.001 + (progress * 0.004)  # 0.1% to 0.5%
        offset = self.mark_price * slippage_pct

        # Aggressive pricing: sell BELOW market, buy ABOVE market
        if side == "sell":
            price = self.mark_price - offset
        else:
            price = self.mark_price + offset

        self.loggers["main"].info(
            "Funding flush reduce-only %s @ %.4f to flatten inventory %.2f (slippage=%.2f%%)",
            side, price, self.inventory, slippage_pct * 100,
        )

        assert self.client is not None

        # Use IOC when close to funding (<60 seconds) to guarantee fill
        # Otherwise use post-only to try for maker rebates
        if seconds_to_funding < 60:
            self.loggers["main"].info("Using IOC for urgent funding flush (%ds to funding)", seconds_to_funding)
            await self.client.place_ioc(
                side,
                price,
                abs(self.inventory),
                cloid=self.client.new_cloid(),
                reduce_only=True,
            )
        else:
            await self.client.place_post_only(
                side,
                price,
                abs(self.inventory),
                cloid=self.client.new_cloid(),
                reduce_only=True,
            )

        self._last_funding_epoch = next_epoch
        self._flush_in_progress = False

    def seconds_until_next_funding(self) -> Tuple[int, datetime]:
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return int((next_hour - now).total_seconds()), next_hour

    async def risk_monitor(self) -> None:
        while not self._shutdown.is_set():
            # Update unrealized P&L
            self._unrealized_pnl = self._calculate_unrealized_pnl()

            # Calculate position notional for % loss calculation
            position_notional = abs(self.inventory) * (self.mark_price or 0)

            # === CHECK 1: Max inventory ===
            if abs(self.inventory) >= self.config.max_inventory * 1.5:  # 50% buffer before shutdown
                self.loggers["errors"].warning(
                    "Max inventory EXCEEDED (%.4f >= %.4f), halting",
                    abs(self.inventory),
                    self.config.max_inventory * 1.5,
                )
                await self.cancel_all()
                await self.shutdown()
                break

            # === CHECK 2: Session drawdown (realized P&L) ===
            if self.pnl <= self.config.session_drawdown_limit:
                self.loggers["errors"].warning("Drawdown limit breached (pnl=%.4f), halting", self.pnl)
                await self.cancel_all()
                await self.shutdown()
                break

            # === CHECK 3: Unrealized loss (liquidation prevention) ===
            if (self.config.emergency_flatten_enabled and
                    position_notional > 0 and
                    self._avg_entry_price > 0):

                loss_pct = -self._unrealized_pnl / position_notional if position_notional > 0 else 0

                if loss_pct >= self.config.max_unrealized_loss_pct:
                    self.loggers["errors"].warning(
                        "UNREALIZED LOSS LIMIT: %.2f%% loss (threshold: %.2f%%), emergency flatten!",
                        loss_pct * 100,
                        self.config.max_unrealized_loss_pct * 100,
                    )
                    await self._emergency_flatten(f"Unrealized loss {loss_pct * 100:.2f}%")
                    # Don't shutdown, just flatten and continue
                elif loss_pct >= self.config.max_unrealized_loss_pct * 0.7:
                    # Warning at 70% of threshold
                    self.loggers["main"].warning(
                        "Risk warning: unrealized loss %.2f%% approaching limit %.2f%%",
                        loss_pct * 100,
                        self.config.max_unrealized_loss_pct * 100,
                    )

            # Log risk status periodically
            if position_notional > 0:
                self.loggers["main"].info(
                    "Risk: inv=%.4f notional=$%.0f entry=%.2f mark=%.2f unrealized=$%.2f (%.2f%%)",
                    self.inventory,
                    position_notional,
                    self._avg_entry_price,
                    self.mark_price or 0,
                    self._unrealized_pnl,
                    (self._unrealized_pnl / position_notional * 100) if position_notional > 0 else 0,
                )

            await asyncio.sleep(0.5)


def load_config(path: str) -> MarketMakerConfig:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return MarketMakerConfig.from_dict(data)


def main() -> None:
    config_path = os.environ.get("MARKET_MAKER_CONFIG", "config.json")
    config = load_config(config_path)

    async def runner():
        async with MarketMaker(config) as maker:
            await maker.start()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
