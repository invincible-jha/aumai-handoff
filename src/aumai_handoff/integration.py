"""AumOS integration for aumai-handoff."""

from __future__ import annotations

from typing import Any

from aumai_integration import AumOS, Event, EventBus, ServiceInfo

from .models import HandoffRecord, HandoffRequest, HandoffStatus

__all__ = [
    "HandoffIntegration",
    "HandoffIntegrationConfig",
]

_SERVICE_NAME = "handoff"
_SERVICE_VERSION = "0.1.0"
_SERVICE_DESCRIPTION = (
    "Framework-independent agent-to-agent task handoff protocol. "
    "Provides lifecycle management, smart routing, and event-driven "
    "coordination for multi-agent workflows."
)
_SERVICE_CAPABILITIES = ["agent-handoff", "smart-routing"]

# Event type constants
EVENT_INITIATED = "handoff.initiated"
EVENT_ACCEPTED = "handoff.accepted"
EVENT_COMPLETED = "handoff.completed"
EVENT_FAILED = "handoff.failed"
EVENT_REJECTED = "handoff.rejected"


class HandoffIntegrationConfig:
    """Configuration for :class:`HandoffIntegration`.

    Args:
        service_name: Override the default service name (``"handoff"``).
        service_version: Override the default version string.
        additional_capabilities: Extra capability strings to register
            alongside the defaults.
        subscribe_to_capability_events: When ``True``, the integration
            subscribes to AumOS ``agent.capability.*`` events and maintains
            an internal capability cache.
    """

    def __init__(
        self,
        service_name: str = _SERVICE_NAME,
        service_version: str = _SERVICE_VERSION,
        additional_capabilities: list[str] | None = None,
        subscribe_to_capability_events: bool = True,
    ) -> None:
        self.service_name = service_name
        self.service_version = service_version
        self.capabilities = list(_SERVICE_CAPABILITIES) + (
            additional_capabilities or []
        )
        self.subscribe_to_capability_events = subscribe_to_capability_events


class HandoffIntegration:
    """Bridges aumai-handoff with the AumOS service mesh.

    Responsibilities:
    - Registers the handoff service with :class:`~aumai_integration.AumOS`.
    - Publishes ``handoff.initiated``, ``handoff.completed``, and
      ``handoff.failed`` events to the shared :class:`~aumai_integration.EventBus`.
    - Subscribes to ``agent.capability.*`` events to maintain an up-to-date
      map of available agent capabilities.

    Example::

        aumos = AumOS()
        bus = EventBus()
        integration = HandoffIntegration(aumos=aumos, bus=bus)
        integration.register()

        request = HandoffRequest(
            from_agent="planner",
            to_agent="executor",
            task_description="Run the data pipeline.",
        )
        record = HandoffRecord(record_id="r1", request=request)

        integration.publish_initiated(record)
        # ... do work ...
        integration.publish_completed(record, result={"rows": 100})

    Subscribe to events on the bus::

        @bus.on("handoff.*")
        def handle_handoff_event(event: Event) -> None:
            print(event.event_type, event.data)
    """

    def __init__(
        self,
        aumos: AumOS,
        bus: EventBus,
        config: HandoffIntegrationConfig | None = None,
    ) -> None:
        self._aumos = aumos
        self._bus = bus
        self._config = config or HandoffIntegrationConfig()
        self._registered = False
        # Local cache populated by agent capability event subscriptions.
        self._agent_capabilities: dict[str, list[str]] = {}
        self._subscription_ids: list[str] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self) -> None:
        """Register the handoff service with AumOS and subscribe to events.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._registered:
            return

        service = ServiceInfo(
            name=self._config.service_name,
            version=self._config.service_version,
            description=_SERVICE_DESCRIPTION,
            capabilities=self._config.capabilities,
            metadata={
                "protocol": "aumai-handoff",
                "events": [
                    EVENT_INITIATED,
                    EVENT_ACCEPTED,
                    EVENT_COMPLETED,
                    EVENT_FAILED,
                    EVENT_REJECTED,
                ],
            },
        )
        self._aumos.register(service)

        if self._config.subscribe_to_capability_events:
            self._subscribe_to_capability_events()

        self._registered = True

    def unregister(self) -> None:
        """Unregister from AumOS and remove event subscriptions."""
        if not self._registered:
            return

        for sub_id in self._subscription_ids:
            self._bus.unsubscribe(sub_id)
        self._subscription_ids.clear()

        self._aumos.unregister(self._config.service_name)
        self._registered = False

    # ------------------------------------------------------------------
    # Event publication
    # ------------------------------------------------------------------

    async def publish_initiated(self, record: HandoffRecord) -> None:
        """Publish a ``handoff.initiated`` event.

        Args:
            record: The newly created handoff record.
        """
        await self._publish(
            event_type=EVENT_INITIATED,
            data={
                "record_id": record.record_id,
                "from_agent": record.request.from_agent,
                "to_agent": record.request.to_agent,
                "task_description": record.request.task_description,
                "priority": record.request.priority,
                "status": record.status.value,
                "created_at": record.created_at.isoformat(),
            },
        )

    async def publish_accepted(self, record: HandoffRecord) -> None:
        """Publish a ``handoff.accepted`` event.

        Args:
            record: The accepted handoff record.
        """
        await self._publish(
            event_type=EVENT_ACCEPTED,
            data={
                "record_id": record.record_id,
                "from_agent": record.request.from_agent,
                "to_agent": record.request.to_agent,
                "status": record.status.value,
                "updated_at": record.updated_at.isoformat(),
            },
        )

    async def publish_completed(
        self,
        record: HandoffRecord,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Publish a ``handoff.completed`` event.

        Args:
            record: The completed handoff record.
            result: Optional result payload to include in the event data.
        """
        data: dict[str, Any] = {
            "record_id": record.record_id,
            "from_agent": record.request.from_agent,
            "to_agent": record.request.to_agent,
            "status": record.status.value,
            "updated_at": record.updated_at.isoformat(),
        }
        if result is not None:
            data["result"] = result
        elif record.result:
            data["result"] = record.result

        await self._publish(event_type=EVENT_COMPLETED, data=data)

    async def publish_failed(
        self,
        record: HandoffRecord,
        reason: str = "",
    ) -> None:
        """Publish a ``handoff.failed`` event.

        Covers both FAILED and REJECTED terminal states.

        Args:
            record: The failed or rejected handoff record.
            reason: Human-readable failure reason.
        """
        await self._publish(
            event_type=EVENT_FAILED,
            data={
                "record_id": record.record_id,
                "from_agent": record.request.from_agent,
                "to_agent": record.request.to_agent,
                "status": record.status.value,
                "reason": reason or (
                    record.response.reason if record.response else ""
                ),
                "updated_at": record.updated_at.isoformat(),
            },
        )

    async def publish_rejected(self, record: HandoffRecord, reason: str = "") -> None:
        """Publish a ``handoff.rejected`` event.

        Args:
            record: The rejected handoff record.
            reason: Rejection reason.
        """
        await self._publish(
            event_type=EVENT_REJECTED,
            data={
                "record_id": record.record_id,
                "from_agent": record.request.from_agent,
                "to_agent": record.request.to_agent,
                "status": HandoffStatus.rejected.value,
                "reason": reason or (
                    record.response.reason if record.response else ""
                ),
                "updated_at": record.updated_at.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # Capability cache
    # ------------------------------------------------------------------

    def get_known_agent_capabilities(self) -> dict[str, list[str]]:
        """Return the locally cached agent-to-capabilities map.

        Populated by ``agent.capability.*`` events received on the bus.
        """
        return dict(self._agent_capabilities)

    def find_agents_with_capability(self, capability: str) -> list[str]:
        """Return agent IDs known to have *capability*.

        Args:
            capability: The capability string to match.

        Returns:
            Sorted list of matching agent IDs.
        """
        matching = [
            agent_id
            for agent_id, caps in self._agent_capabilities.items()
            if capability in caps
        ]
        return sorted(matching)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event to the shared bus."""
        event = Event(
            event_type=event_type,
            source=self._config.service_name,
            data=data,
        )
        await self._bus.publish(event)

    def _subscribe_to_capability_events(self) -> None:
        """Subscribe to AumOS agent capability announcement events."""

        async def _handle_capability_registered(event: Event) -> None:
            agent_id = event.data.get("agent_id", "")
            capabilities = event.data.get("capabilities", [])
            if agent_id and isinstance(capabilities, list):
                self._agent_capabilities[agent_id] = list(capabilities)

        async def _handle_capability_unregistered(event: Event) -> None:
            agent_id = event.data.get("agent_id", "")
            if agent_id:
                self._agent_capabilities.pop(agent_id, None)

        sub_id_reg = self._bus.subscribe(
            "agent.capability.registered",
            _handle_capability_registered,
            subscriber=self._config.service_name,
        )
        sub_id_unreg = self._bus.subscribe(
            "agent.capability.unregistered",
            _handle_capability_unregistered,
            subscriber=self._config.service_name,
        )
        self._subscription_ids.extend([sub_id_reg, sub_id_unreg])
