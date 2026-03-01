"""LLM-powered smart agent selection for aumai-handoff."""

from __future__ import annotations

import json
from typing import Any

from aumai_llm_core import (
    CompletionRequest,
    LLMClient,
    Message,
    MockProvider,
    ModelConfig,
    ProviderRegistry,
)
from pydantic import BaseModel, Field

from .models import HandoffRequest

__all__ = [
    "RoutingDecision",
    "SmartRouter",
    "SmartRouterConfig",
    "make_mock_smart_router",
]


class RoutingDecision(BaseModel):
    """Structured output produced by :class:`SmartRouter`.

    Attributes:
        target_agent: ID of the recommended target agent.
        confidence: Confidence score in the range [0.0, 1.0].
        reasoning: Human-readable explanation for the recommendation.
        fallback_agents: Ordered list of alternative agents if the primary
            recommendation is unavailable.
    """

    target_agent: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    fallback_agents: list[str] = Field(default_factory=list)


class SmartRouterConfig(BaseModel):
    """Configuration for :class:`SmartRouter`.

    Args:
        provider: LLM provider name registered with
            :class:`~aumai_llm_core.ProviderRegistry`.
        model_id: Model identifier to use for routing decisions.
        temperature: Sampling temperature (lower = more deterministic).
        max_tokens: Max tokens for the routing response.
        system_prompt: System prompt prepended to every routing request.
            Defaults to a built-in instruction.
    """

    provider: str = "mock"
    model_id: str = "mock-router"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1)
    system_prompt: str = (
        "You are an expert agent orchestrator. Given a task description and a "
        "list of available agents with their capabilities, select the single "
        "best agent to handle the task. Respond ONLY with a valid JSON object "
        "matching this schema: "
        '{"target_agent": "<agent_id>", "confidence": <0.0-1.0>, '
        '"reasoning": "<explanation>", "fallback_agents": ["<id>", ...]}. '
        "Do not include any prose outside the JSON object."
    )

    model_config = {"frozen": False}


# ---------------------------------------------------------------------------
# Internal provider key used for mock routing in tests
# ---------------------------------------------------------------------------

_MOCK_PROVIDER_KEY = "handoff-mock-router"


def _register_mock_provider(responses: list[str]) -> None:
    """Dynamically create and register a MockProvider subclass.

    Because :class:`~aumai_llm_core.LLMClient` always instantiates the
    provider via ``ProviderRegistry.get(name)()`` (no arguments), we create
    a named subclass whose ``__init__`` bakes in the desired responses.
    """
    mock_responses = responses

    class _BoundMockProvider(MockProvider):
        def __init__(self) -> None:
            super().__init__(responses=mock_responses)

    ProviderRegistry.register(_MOCK_PROVIDER_KEY, _BoundMockProvider)


def make_mock_smart_router(
    agent_ids: list[str],
    target_agent: str | None = None,
    confidence: float = 0.95,
    reasoning: str = "Best capability match for the requested task.",
) -> "SmartRouter":
    """Build a :class:`SmartRouter` backed by :class:`~aumai_llm_core.MockProvider`.

    Useful in unit tests — no real LLM call is made.

    Args:
        agent_ids: Available agent IDs for the registry.
        target_agent: Which agent the mock LLM recommends.  Defaults to
            the first element of *agent_ids*.
        confidence: Confidence score returned by the mock LLM.
        reasoning: Reasoning string returned by the mock LLM.

    Returns:
        A :class:`SmartRouter` configured with the mock provider.
    """
    chosen = target_agent or (agent_ids[0] if agent_ids else "unknown-agent")
    fallbacks = [a for a in agent_ids if a != chosen]

    mock_json = json.dumps(
        {
            "target_agent": chosen,
            "confidence": confidence,
            "reasoning": reasoning,
            "fallback_agents": fallbacks,
        }
    )
    _register_mock_provider([mock_json])

    config = SmartRouterConfig(
        provider=_MOCK_PROVIDER_KEY,
        model_id="mock-router-v1",
        temperature=0.0,
    )
    registry: dict[str, list[str]] = {a: [] for a in agent_ids}
    return SmartRouter(config=config, agent_registry=registry)


class SmartRouter:
    """LLM-powered router that selects the best agent for a handoff.

    Given a :class:`~aumai_handoff.models.HandoffRequest` and a registry
    of available agents with their capabilities, the router sends a
    structured prompt to the configured LLM and parses a
    :class:`RoutingDecision` from the response.

    Example::

        router = make_mock_smart_router(
            agent_ids=["agent-alpha", "agent-beta"],
            target_agent="agent-beta",
            reasoning="Agent beta has data-processing capabilities.",
        )
        request = HandoffRequest(
            from_agent="agent-alpha",
            to_agent="",
            task_description="Process the sales dataset.",
        )
        decision = await router.route(request)
        assert decision.target_agent == "agent-beta"

    For production use, supply a real provider::

        from aumai_llm_core import AnthropicProvider, ProviderRegistry
        ProviderRegistry.register("anthropic", AnthropicProvider)

        config = SmartRouterConfig(
            provider="anthropic",
            model_id="claude-3-haiku-20240307",
            temperature=0.1,
        )
        registry = {
            "agent-alpha": ["python", "data-analysis"],
            "agent-beta": ["java", "reporting"],
        }
        router = SmartRouter(config=config, agent_registry=registry)
        decision = await router.route(request)
    """

    def __init__(
        self,
        config: SmartRouterConfig,
        agent_registry: dict[str, list[str]],
    ) -> None:
        """Initialize the SmartRouter.

        Args:
            config: LLM and prompt configuration.
            agent_registry: Mapping of agent_id to list of capability strings.
        """
        self._config = config
        self._agent_registry: dict[str, list[str]] = dict(agent_registry)
        model_config = ModelConfig(
            provider=config.provider,
            model_id=config.model_id,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        self._client = LLMClient(config=model_config)

    # ------------------------------------------------------------------
    # Registry management
    # ------------------------------------------------------------------

    def register_agent(self, agent_id: str, capabilities: list[str]) -> None:
        """Add or replace an agent in the local registry.

        Args:
            agent_id: Unique identifier of the agent.
            capabilities: List of capability strings the agent supports.
        """
        self._agent_registry[agent_id] = list(capabilities)

    def unregister_agent(self, agent_id: str) -> None:
        """Remove an agent from the local registry.

        Args:
            agent_id: Agent to remove.  Silently ignored if not present.
        """
        self._agent_registry.pop(agent_id, None)

    def list_agents(self) -> dict[str, list[str]]:
        """Return a copy of the current agent registry."""
        return dict(self._agent_registry)

    # ------------------------------------------------------------------
    # Core routing
    # ------------------------------------------------------------------

    async def route(
        self,
        request: HandoffRequest,
        extra_context: dict[str, Any] | None = None,
    ) -> RoutingDecision:
        """Select the best agent for *request* using the LLM.

        Args:
            request: The handoff request describing the task.
            extra_context: Optional additional context key-value pairs to
                include in the prompt.

        Returns:
            A :class:`RoutingDecision` with the target agent, confidence,
            and reasoning.

        Raises:
            :class:`~aumai_llm_core.ExtractionError`: If the LLM response
                cannot be parsed into a :class:`RoutingDecision`.
        """
        prompt = self._build_prompt(request, extra_context or {})
        completion_request = CompletionRequest(
            messages=[
                Message(role="system", content=self._config.system_prompt),
                Message(role="user", content=prompt),
            ],
        )
        decision: RoutingDecision = await self._client.complete_structured(
            completion_request, RoutingDecision
        )
        return decision

    async def route_with_fallback(
        self,
        request: HandoffRequest,
        extra_context: dict[str, Any] | None = None,
    ) -> RoutingDecision:
        """Like :meth:`route`, but falls back to capability-based scoring on error.

        If the LLM call fails for any reason (network error, parse error,
        etc.), the router applies a simple keyword-intersection scoring
        against agent capabilities and returns a synthetic
        :class:`RoutingDecision`.

        Args:
            request: The handoff request.
            extra_context: Optional additional context.

        Returns:
            A :class:`RoutingDecision` (from LLM or from fallback heuristic).
        """
        try:
            return await self.route(request, extra_context)
        except Exception:  # noqa: BLE001
            return self._heuristic_route(request)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        request: HandoffRequest,
        extra_context: dict[str, Any],
    ) -> str:
        """Compose the user prompt for the routing LLM call."""
        available_agents = [
            {"agent_id": agent_id, "capabilities": caps}
            for agent_id, caps in self._agent_registry.items()
            if agent_id != request.from_agent
        ]
        context_block = ""
        if request.context or extra_context:
            merged = {**request.context, **extra_context}
            context_block = (
                f"\n\nAdditional context:\n{json.dumps(merged, indent=2)}"
            )

        return (
            f"Task description: {request.task_description}\n"
            f"Priority: {request.priority}/10\n"
            f"Sent from agent: {request.from_agent}\n"
            f"Available agents:\n{json.dumps(available_agents, indent=2)}"
            f"{context_block}"
        )

    def _heuristic_route(self, request: HandoffRequest) -> RoutingDecision:
        """Capability-keyword fallback when LLM is unavailable."""
        task_words = set(request.task_description.lower().split())
        best_agent: str | None = None
        best_score = -1

        for agent_id, caps in self._agent_registry.items():
            if agent_id == request.from_agent:
                continue
            score = sum(
                1 for cap in caps if cap.lower() in task_words
            )
            if score > best_score:
                best_score = score
                best_agent = agent_id

        if best_agent is None:
            candidates = [
                a for a in self._agent_registry if a != request.from_agent
            ]
            best_agent = candidates[0] if candidates else request.from_agent

        return RoutingDecision(
            target_agent=best_agent,
            confidence=0.4,
            reasoning=(
                "Heuristic fallback: selected by keyword-capability intersection. "
                "LLM routing was unavailable."
            ),
            fallback_agents=[
                a
                for a in self._agent_registry
                if a not in (request.from_agent, best_agent)
            ],
        )
