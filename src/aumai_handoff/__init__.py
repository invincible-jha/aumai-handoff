"""AumAI Handoff — framework-independent agent-to-agent task handoff protocol."""

__version__ = "0.1.0"

# Core models
from .models import (
    HandoffRecord,
    HandoffRequest,
    HandoffResponse,
    HandoffStatus,
)

# Synchronous core
from .core import (
    AgentCapabilityRegistry,
    HandoffManager,
    HandoffRouter,
)

# Async API (requires aumai-async-core)
from .async_core import (
    AsyncHandoffManager,
    AsyncHandoffManagerConfig,
)

# Persistence (requires aumai-store)
from .store import (
    HandoffMetrics,
    HandoffStore,
    HandoffStoreConfig,
)

# LLM-powered smart routing (requires aumai-llm-core)
from .smart_routing import (
    RoutingDecision,
    SmartRouter,
    SmartRouterConfig,
    make_mock_smart_router,
)

# AumOS integration (requires aumai-integration)
from .integration import (
    HandoffIntegration,
    HandoffIntegrationConfig,
)

__all__ = [
    # version
    "__version__",
    # models
    "HandoffRecord",
    "HandoffRequest",
    "HandoffResponse",
    "HandoffStatus",
    # sync core
    "AgentCapabilityRegistry",
    "HandoffManager",
    "HandoffRouter",
    # async API
    "AsyncHandoffManager",
    "AsyncHandoffManagerConfig",
    # persistence
    "HandoffMetrics",
    "HandoffStore",
    "HandoffStoreConfig",
    # smart routing
    "RoutingDecision",
    "SmartRouter",
    "SmartRouterConfig",
    "make_mock_smart_router",
    # integration
    "HandoffIntegration",
    "HandoffIntegrationConfig",
]
