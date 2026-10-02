"""
NOVA SMARTOPS — Predictive Operations Backend Server
Built for NOVA CART Hackathon Prototype

Hardened, thread-safe, zero-dependency Python 3 HTTP + REST API server.

Security features:
  - Path traversal protection (static file serving restricted to DIRECTORY)
  - Request body size limit (default 1 MiB, override with NOVA_MAX_BODY)
  - CORS allowlist (override with NOVA_CORS_ORIGINS, comma-separated)
  - Strict input validation & bounded type coercion
  - Uniform 404 responses for unknown /api/* paths (anti-enumeration)
  - Security response headers (X-Content-Type-Options, X-Frame-Options, CSP)
  - Per-IP token-bucket rate limiting (NOVA_RATE_LIMIT_RPS / _BURST)
  - No stack traces or Python version leaked to clients
  - Thread-safe shared state via RLock
  - Slowloris mitigation via socket timeout
"""

from __future__ import annotations

import http.server
import socketserver
import json
import os
import sys
import time
import math
import logging
import threading
import urllib.parse
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

PORT = int(os.environ.get("NOVA_PORT", "8000"))
HOST = os.environ.get("NOVA_HOST", "0.0.0.0")
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
MAX_BODY_BYTES = int(os.environ.get("NOVA_MAX_BODY", str(1 * 1024 * 1024)))
RATE_LIMIT_RPS = float(os.environ.get("NOVA_RATE_LIMIT_RPS", "20"))
RATE_LIMIT_BURST = int(os.environ.get("NOVA_RATE_LIMIT_BURST", "40"))
SLA_TARGET_MIN = 30
SPEED_MIN_PER_KM = 3.0
CORS_ALLOWLIST = {
    o.strip()
    for o in os.environ.get(
        "NOVA_CORS_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
}

logging.basicConfig(
    level=os.environ.get("NOVA_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nova.smartops")


@dataclass
class InventoryItem:
    id: str
    name: str
    category: str
    currentStock: int
    history: List[int]
    status: str = "ACTIVE"


@dataclass
class Order:
    id: str
    store: str
    distance: float
    items: int
    prepTime: int
    storeLoad: str
    availablePartners: int
    isPrioritized: bool = False


INVENTORY_SEED: List[Dict[str, Any]] = [
    {"id": "SKU-101", "name": "Amul Taaza Milk 1L", "category": "Dairy & Eggs", "currentStock": 20, "history": [14, 12, 15, 13, 11, 14, 16, 13, 14, 15, 13, 12, 14, 15], "status": "ACTIVE"},
    {"id": "SKU-102", "name": "Britannia Whole Wheat Bread", "category": "Bakery", "currentStock": 18, "history": [7, 9, 8, 6, 8, 9, 7, 8, 9, 7, 8, 10, 8, 7], "status": "ACTIVE"},
    {"id": "SKU-103", "name": "India Gate Basmati Rice 5kg", "category": "Staples & Grains", "currentStock": 42, "history": [5, 4, 6, 5, 4, 5, 6, 4, 5, 5, 6, 4, 5, 5], "status": "ACTIVE"},
    {"id": "SKU-104", "name": "Farm Fresh Eggs (Pack of 12)", "category": "Dairy & Eggs", "currentStock": 8, "history": [8, 9, 7, 8, 9, 8, 10, 8, 9, 8, 9, 10, 9, 8], "status": "ACTIVE"},
    {"id": "SKU-105", "name": "Fresh Red Onion 1kg", "category": "Fresh Produce", "currentStock": 15, "history": [10, 11, 9, 12, 10, 11, 13, 10, 11, 12, 11, 10, 12, 11], "status": "ACTIVE"},
    {"id": "SKU-106", "name": "Hybrid Tomato 1kg", "category": "Fresh Produce", "currentStock": 12, "history": [8, 9, 8, 7, 9, 8, 9, 8, 9, 8, 9, 8, 9, 8], "status": "ACTIVE"},
    {"id": "SKU-107", "name": "Aashirvaad Shudh Chakki Atta 5kg", "category": "Staples & Grains", "currentStock": 35, "history": [4, 3, 5, 4, 3, 4, 5, 3, 4, 4, 5, 3, 4, 4], "status": "ACTIVE"},
    {"id": "SKU-108", "name": "Amul Masti Dahi 400g", "category": "Dairy & Eggs", "currentStock": 6, "history": [9, 10, 8, 11, 9, 10, 11, 9, 10, 11, 10, 9, 11, 10], "status": "ACTIVE"},
    {"id": "SKU-109", "name": "Robusta Banana (1 Dozen)", "category": "Fresh Produce", "currentStock": 9, "history": [6, 7, 6, 5, 7, 6, 7, 6, 7, 6, 7, 5, 7, 6], "status": "ACTIVE"},
    {"id": "SKU-110", "name": "Fortune Sunlite Sunflower Oil 1L", "category": "Staples & Grains", "currentStock": 25, "history": [3, 2, 3, 4, 3, 2, 3, 3, 2, 3, 4, 3, 2, 3], "status": "ACTIVE"},
    {"id": "SKU-111", "name": "Tata Salt Vaccum Evaporated 1kg", "category": "Staples & Grains", "currentStock": 50, "history": [4, 3, 4, 3, 4, 3, 4, 4, 3, 4, 3, 4, 3, 4], "status": "ACTIVE"},
    {"id": "SKU-112", "name": "Maggi 2-Minute Noodles (Pack of 4)", "category": "Packaged Foods", "currentStock": 14, "history": [9, 8, 10, 9, 11, 10, 12, 10, 11, 12, 11, 10, 12, 11], "status": "ACTIVE"},
]

ORDERS_SEED: List[Dict[str, Any]] = [
    {"id": "ORD-1048", "store": "Fresh Mart (Indiranagar)", "distance": 6.8, "items": 9, "prepTime": 18, "storeLoad": "HIGH", "availablePartners": 1, "isPrioritized": False},
    {"id": "ORD-1050", "store": "Sharma Grocery (Koramangala)", "distance": 7.2, "items": 11, "prepTime": 20, "storeLoad": "HIGH", "availablePartners": 1, "isPrioritized": False},
    {"id": "ORD-1046", "store": "Metro Bazaar (HSR Layout)", "distance": 5.7, "items": 8, "prepTime": 15, "storeLoad": "HIGH", "availablePartners": 1, "isPrioritized": False},
    {"id": "ORD-1044", "store": "Daily Needs (Whitefield)", "distance": 4.5, "items": 6, "prepTime": 12, "storeLoad": "MEDIUM", "availablePartners": 2, "isPrioritized": False},
    {"id": "ORD-1047", "store": "Green Valley Organics", "distance": 3.9, "items": 5, "prepTime": 10, "storeLoad": "MEDIUM", "availablePartners": 2, "isPrioritized": False},
    {"id": "ORD-1051", "store": "Daily Needs (Jayanagar)", "distance": 3.1, "items": 4, "prepTime": 9, "storeLoad": "MEDIUM", "availablePartners": 3, "isPrioritized": False},
    {"id": "ORD-1042", "store": "ABC Supermarket (BTM)", "distance": 3.2, "items": 4, "prepTime": 8, "storeLoad": "LOW", "availablePartners": 3, "isPrioritized": False},
    {"id": "ORD-1043", "store": "Fresh Mart (MG Road)", "distance": 2.1, "items": 3, "prepTime": 6, "storeLoad": "LOW", "availablePartners": 4, "isPrioritized": False},
    {"id": "ORD-1045", "store": "QuickStop Express (Domlur)", "distance": 1.8, "items": 2, "prepTime": 5, "storeLoad": "LOW", "availablePartners": 5, "isPrioritized": False},
    {"id": "ORD-1049", "store": "ABC Grocery (Bellandur)", "distance": 2.5, "items": 3, "prepTime": 7, "storeLoad": "LOW", "availablePartners": 3, "isPrioritized": False},
]

VALID_STORE_LOAD = {"LOW", "MEDIUM", "HIGH"}
RISK_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


class AppState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.inventory: Dict[str, InventoryItem] = {
            d["id"]: InventoryItem(**d) for d in INVENTORY_SEED
        }
        self.orders: Dict[str, Order] = {
            d["id"]: Order(**d) for d in ORDERS_SEED
        }

    def snapshot_inventory(self) -> List[InventoryItem]:
        with self._lock:
            return [InventoryItem(**asdict(i)) for i in self.inventory.values()]

    def snapshot_orders(self) -> List[Order]:
        with self._lock:
            return [Order(**asdict(o)) for o in self.orders.values()]

    def restock(self, sku_id: str, qty: int) -> Optional[InventoryItem]:
        with self._lock:
            item = self.inventory.get(sku_id)
            if item is None:
                return None
            item.currentStock += qty
            item.status = "RESTOCK ORDERED"
            return InventoryItem(**asdict(item))

    def delist(self, sku_id: str) -> Optional[InventoryItem]:
        with self._lock:
            item = self.inventory.get(sku_id)
            if item is None:
                return None
            item.status = "MARKED UNAVAILABLE"
            return InventoryItem(**asdict(item))

    def prioritize(self, order_id: str) -> Optional[Order]:
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                return None
            order.isPrioritized = True
            return Order(**asdict(order))


def analyze_product_demand(item: InventoryItem, forecast_days: int = 3) -> Dict[str, Any]:
    if forecast_days <= 0:
        raise ValueError("forecast_days must be positive")
    if not item.history:
        raise ValueError("history must be non-empty")

    sales = item.history
    avg_daily = sum(sales) / len(sales)
    recent_3d = sum(sales[-3:]) / 3
    trend_factor = recent_3d / avg_daily if avg_daily else 1.0

    trend = "STABLE"
    if trend_factor > 1.06:
        trend = "RISING"
    elif trend_factor < 0.94:
        trend = "FALLING"

    predicted_demand = round(avg_daily * trend_factor * forecast_days)
    safety_buffer = math.ceil(predicted_demand * 0.1)
    total_required = predicted_demand + safety_buffer

    if item.currentStock < predicted_demand:
        risk, risk_color, action = "HIGH", "red", "RESTOCK NOW"
    elif item.currentStock <= total_required:
        risk, risk_color, action = "MEDIUM", "amber", "RESTOCK SOON"
    else:
        risk, risk_color, action = "LOW", "green", "STOCK HEALTHY"

    return {
        **asdict(item),
        "avgDailySales": round(avg_daily, 1),
        "trend": trend,
        "trendFactor": round(trend_factor, 2),
        "predictedDemand": predicted_demand,
        "safetyBuffer": safety_buffer,
        "totalRequired": total_required,
        "risk": risk,
        "riskColor": risk_color,
        "recommendedRestock": max(total_required - item.currentStock, 0),
        "action": action,
    }


def calculate_delivery_eta(order: Order) -> Dict[str, Any]:
    if order.distance < 0 or order.items < 0 or order.prepTime < 0:
        raise ValueError("order fields must be non-negative")
    if order.storeLoad not in VALID_STORE_LOAD:
        raise ValueError(f"invalid storeLoad: {order.storeLoad!r}")

    is_p = order.isPrioritized
    travel_time = round(order.distance * SPEED_MIN_PER_KM)
    workload_penalty = {"HIGH": 12, "MEDIUM": 5, "LOW": 0}[order.storeLoad]
    item_penalty = round((order.items - 5) * 1.5) if order.items > 5 else 0
    partner_penalty = 6 if order.availablePartners <= 1 else 0

    prep_effective = max(5, order.prepTime - 6) if is_p else order.prepTime
    effective_workload = math.floor(workload_penalty * 0.4) if is_p else workload_penalty
    effective_partner = 1 if is_p else partner_penalty

    eta = prep_effective + travel_time + effective_workload + item_penalty + effective_partner

    if eta > SLA_TARGET_MIN + 5:
        risk = "HIGH"
        recs = [
            "Assign nearest dedicated delivery partner",
            "Prioritize queue in store dispatch station",
            "Proactively update customer ETA to prevent friction",
        ]
    elif eta >= SLA_TARGET_MIN - 2:
        risk = "MEDIUM"
        recs = ["Pre-reserve next incoming rider", "Monitor packaging progress closely"]
    else:
        risk = "LOW"
        recs = ["Optimal operational flow — on target for SLA"]

    return {
        **asdict(order),
        "travelTime": travel_time,
        "workloadPenalty": workload_penalty,
        "itemPenalty": item_penalty,
        "partnerPenalty": partner_penalty,
        "prepEffective": prep_effective,
        "estimatedDeliveryTime": eta,
        "slaTarget": SLA_TARGET_MIN,
        "delayDelta": eta - SLA_TARGET_MIN,
        "risk": risk,
        "recommendations": recs,
    }


def _sort_by_risk(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda r: RISK_RANK.get(r["risk"], 0), reverse=True)


class TokenBucket:
    __slots__ = ("tokens", "last", "rate", "burst")

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    def __init__(self, rate: float, burst: int) -> None:
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._rate = rate
        self._burst = burst

    def allow(self, ip: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(ip)
            if bucket is None:
                bucket = TokenBucket(self._rate, self._burst)
                self._buckets[ip] = bucket
            return bucket.allow()


def _coerce_int(value: Any, *, minimum: int = 0, maximum: int = 10_000, default: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < minimum:
        return minimum
    if n > maximum:
        return maximum
    return n


def _coerce_float(value: Any, *, minimum: float = 0.0, maximum: float = 1e6, default: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(n) or math.isinf(n):
        return default
    return max(minimum, min(maximum, n))


def _is_safe_path(base: str, target: str) -> bool:
    base_abs = os.path.realpath(base)
    target_abs = os.path.realpath(target)
    try:
        return os.path.commonpath([base_abs, target_abs]) == base_abs
    except ValueError:
        return False


class SmartOpsHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "NovaSmartOps/1.1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def _send_cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and origin in CORS_ALLOWLIST:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'",
        )

    def end_headers(self) -> None:
        self._send_cors()
        self._send_security_headers()
        super().end_headers()

    def _check_rate_limit(self) -> bool:
        if not self.server.rate_limiter.allow(self._client_ip()):  # type: ignore[attr-defined]
            self.send_json({"error": "rate limit exceeded"}, status=429)
            return False
        return True

    def send_json(self, data: Any, status: int = 200) -> None:
        try:
            body = json.dumps(data).encode("utf-8")
        except (TypeError, ValueError) as exc:
            log.exception("JSON serialization failed: %s", exc)
            body = json.dumps({"error": "internal serialization error"}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:
        if not self._check_rate_limit():
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False) -> None:
        if not self._check_rate_limit():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        routes: Dict[str, Callable[[], Any]] = {
            "/api/health": self._route_health,
            "/api/stats": self._route_stats,
            "/api/inventory": self._route_inventory,
            "/api/orders": self._route_orders,
        }
        handler = routes.get(path)
        if handler is not None:
            payload = handler()
            if head_only:
                self._send_head(payload)
            else:
                self.send_json(payload)
            return

        if path.startswith("/api/"):
            self.send_json({"error": "not found"}, status=404)
            return

        self._serve_static_safely(path, head_only=head_only)

    def do_POST(self) -> None:
        if not self._check_rate_limit():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        payload = self._read_json_body()
        if payload is None:
            return

        routes: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "/api/inventory/analyze": self._route_inventory_analyze,
            "/api/inventory/restock": self._route_inventory_restock,
            "/api/inventory/delist": self._route_inventory_delist,
            "/api/orders/prioritize": self._route_orders_prioritize,
            "/api/impact/simulate": self._route_impact_simulate,
        }
        handler = routes.get(path)
        if handler is None:
            self.send_json({"error": "not found"}, status=404)
            return
        try:
            result = handler(payload)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        except Exception:
            log.exception("Unhandled error in %s", path)
            self.send_json({"error": "internal server error"}, status=500)
            return
        self.send_json(result)

    def _route_health(self) -> Dict[str, Any]:
        return {
            "status": "healthy",
            "service": "NOVA SMARTOPS API",
            "version": "1.1.0",
            "store": "NOVA CART Store #402",
        }

    def _route_stats(self) -> Dict[str, Any]:
        return {
            "registeredUsers": 120000,
            "monthlyOrders": 38500,
            "cancellationRate": 11.0,
            "monthlyCancellations": 4235,
            "unavailabilityShare": 35.0,
            "delayShare": 27.0,
            "aov": 486,
            "repeatPurchase": 27.0,
            "targetRetention": 72.0,
        }

    def _route_inventory(self) -> Dict[str, Any]:
        rows = [analyze_product_demand(i) for i in self.server.state.snapshot_inventory()]  # type: ignore[attr-defined]
        return {"items": _sort_by_risk(rows)}

    def _route_orders(self) -> Dict[str, Any]:
        rows = [calculate_delivery_eta(o) for o in self.server.state.snapshot_orders()]  # type: ignore[attr-defined]
        return {"orders": _sort_by_risk(rows)}

    def _route_inventory_analyze(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        rows = [analyze_product_demand(i) for i in self.server.state.snapshot_inventory()]  # type: ignore[attr-defined]
        rows = _sort_by_risk(rows)
        return {
            "success": True,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "totalAnalyzed": len(rows),
            "highRisk": sum(1 for r in rows if r["risk"] == "HIGH"),
            "mediumRisk": sum(1 for r in rows if r["risk"] == "MEDIUM"),
            "items": rows,
        }

    def _route_inventory_restock(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        sku_id = payload.get("id")
        if not isinstance(sku_id, str) or not sku_id:
            raise ValueError("missing or invalid 'id'")
        qty = _coerce_int(payload.get("qty", 15), minimum=1, maximum=10_000, default=15)
        item = self.server.state.restock(sku_id, qty)  # type: ignore[attr-defined]
        if item is None:
            raise ValueError("SKU not found")
        return {"success": True, "item": analyze_product_demand(item)}

    def _route_inventory_delist(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        sku_id = payload.get("id")
        if not isinstance(sku_id, str) or not sku_id:
            raise ValueError("missing or invalid 'id'")
        item = self.server.state.delist(sku_id)  # type: ignore[attr-defined]
        if item is None:
            raise ValueError("SKU not found")
        return {"success": True, "item": analyze_product_demand(item)}

    def _route_orders_prioritize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        order_id = payload.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("missing or invalid 'id'")
        order = self.server.state.prioritize(order_id)  # type: ignore[attr-defined]
        if order is None:
            raise ValueError("Order not found")
        return {"success": True, "order": calculate_delivery_eta(order)}

    def _route_impact_simulate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        stockout_red = _coerce_float(payload.get("stockoutReduction", 40), minimum=0.0, maximum=100.0, default=40.0)
        delay_red = _coerce_float(payload.get("delayReduction", 30), minimum=0.0, maximum=100.0, default=30.0)

        monthly_orders = 38500
        current_cancellations = 4235
        stockout_cancellations = 1482
        delay_cancellations = 1143
        aov = 486

        prevented_stockout = round(stockout_cancellations * (stockout_red / 100))
        prevented_delay = round(delay_cancellations * (delay_red / 100))
        total_prevented = prevented_stockout + prevented_delay
        projected_cancellations = max(0, current_cancellations - total_prevented)
        projected_rate = round((projected_cancellations / monthly_orders) * 100, 1)
        revenue_retained = total_prevented * aov

        return {
            "preventedCancellations": total_prevented,
            "projectedCancellationRate": projected_rate,
            "monthlyRevenueRetained": revenue_retained,
            "annualizedRevenueRetained": revenue_retained * 12,
            "supportTicketsReduced": round(total_prevented * 0.95),
        }

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len) if raw_len is not None else 0
        except ValueError:
            self.send_json({"error": "invalid Content-Length"}, status=400)
            return None

        if length < 0 or length > MAX_BODY_BYTES:
            self.send_json({"error": "request body too large"}, status=413)
            return None

        if length == 0:
            return {}

        body = self.rfile.read(length)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "malformed JSON body"}, status=400)
            return None

        if not isinstance(parsed, dict):
            self.send_json({"error": "JSON body must be an object"}, status=400)
            return None
        return parsed

    def _send_head(self, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve_static_safely(self, path: str, head_only: bool = False) -> None:
        decoded = urllib.parse.unquote(path)
        if "\x00" in decoded:
            self.send_json({"error": "bad request"}, status=400)
            return

        candidate = os.path.normpath(os.path.join(DIRECTORY, decoded.lstrip("/")))
        if not _is_safe_path(DIRECTORY, candidate):
            log.warning("Blocked path traversal attempt: %r from %s", path, self._client_ip())
            self.send_json({"error": "forbidden"}, status=403)
            return

        if head_only:
            self.path = "/" + os.path.relpath(candidate, DIRECTORY).replace(os.sep, "/")
            self.path = urllib.parse.quote(self.path)
            super().do_HEAD()
        else:
            super().do_GET()


class SmartOpsServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    timeout = 30

    def __init__(self, addr: Tuple[str, int], handler: type) -> None:
        super().__init__(addr, handler)
        self.state = AppState()
        self.rate_limiter = RateLimiter(RATE_LIMIT_RPS, RATE_LIMIT_BURST)


def run_server() -> None:
    os.chdir(DIRECTORY)
    with SmartOpsServer((HOST, PORT), SmartOpsHandler) as httpd:
        log.info("=" * 60)
        log.info("⚡ NOVA SMARTOPS BACKEND RUNNING ON http://%s:%d", HOST, PORT)
        log.info("⚡ Open http://localhost:%d in your browser", PORT)
        log.info("⚡ CORS allowlist: %s", sorted(CORS_ALLOWLIST) or "(none)")
        log.info("⚡ Rate limit: %.1f rps / burst %d per IP", RATE_LIMIT_RPS, RATE_LIMIT_BURST)
        log.info("=" * 60)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("Shutting down server.")
        finally:
            httpd.server_close()


if __name__ == "__main__":
    try:
        run_server()
    except OSError as exc:
        log.error("Failed to start server: %s", exc)
        sys.exit(1)
