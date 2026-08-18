from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional, Protocol
import hashlib
import math
import json
import os
import re
from urllib import request, error


class MemoryType(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass
class MemoryEntry:
    memory_id: str
    memory_type: MemoryType
    issue_id: str
    content: str
    tags: List[str] = field(default_factory=list)
    confidence: float = 0.5
    success_rate: float = 0.5
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    source: str = "system"

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.utcnow()
        return bool(self.expires_at and self.expires_at <= now)


@dataclass
class Issue:
    issue_id: str
    title: str
    description: str
    priority: int = 3
    labels: List[str] = field(default_factory=list)

    @property
    def context(self) -> str:
        return " ".join([self.title, self.description, " ".join(self.labels)]).strip().lower()


@dataclass
class IssueCycleResult:
    issue_id: str
    identified_priority: int
    proposed_actions: List[str]
    executed_actions: List[str]
    accepted: bool
    reopened: bool
    started_at: datetime
    ended_at: datetime


@dataclass
class SuccessMetrics:
    resolution_durations_minutes: List[float] = field(default_factory=list)
    recurrence_count: int = 0
    accepted_fixes: int = 0
    total_fixes: int = 0
    reopened_count: int = 0

    def update(self, cycle: IssueCycleResult) -> None:
        minutes = max((cycle.ended_at - cycle.started_at).total_seconds() / 60.0, 0.0)
        self.resolution_durations_minutes.append(minutes)
        self.total_fixes += 1
        if cycle.accepted:
            self.accepted_fixes += 1
        if cycle.reopened:
            self.reopened_count += 1

    def as_dict(self) -> Dict[str, float]:
        avg_resolution = (
            sum(self.resolution_durations_minutes) / len(self.resolution_durations_minutes)
            if self.resolution_durations_minutes
            else 0.0
        )
        acceptance_rate = self.accepted_fixes / self.total_fixes if self.total_fixes else 0.0
        reopen_rate = self.reopened_count / self.total_fixes if self.total_fixes else 0.0
        return {
            "avg_resolution_minutes": avg_resolution,
            "recurrence_rate": float(self.recurrence_count),
            "fix_acceptance_rate": acceptance_rate,
            "reopen_rate": reopen_rate,
        }


class GovernancePolicy:
    SECRET_PATTERNS = [
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"(?i)api[_-]?key\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{12,}"),
        re.compile(r"(?i)token\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{12,}"),
    ]

    @classmethod
    def validate_memory(cls, entry: MemoryEntry) -> bool:
        if not entry.content.strip():
            return False
        if not (0 <= entry.confidence <= 1):
            return False
        if not (0 <= entry.success_rate <= 1):
            return False
        if cls.contains_secret(entry.content):
            return False
        return True

    @classmethod
    def contains_secret(cls, text: str) -> bool:
        return any(pattern.search(text) for pattern in cls.SECRET_PATTERNS)


class MemoryStore:
    def __init__(self) -> None:
        self._entries: Dict[str, MemoryEntry] = {}
        self._issue_history: Dict[str, int] = {}

    def upsert(self, entry: MemoryEntry) -> None:
        if not GovernancePolicy.validate_memory(entry):
            raise ValueError("Memory failed governance validation")
        existing = self._entries.get(entry.memory_id)
        if existing:
            # Conflict handling: keep higher-confidence content as canonical.
            if entry.confidence >= existing.confidence:
                entry.updated_at = datetime.utcnow()
                self._entries[entry.memory_id] = entry
            return
        self._entries[entry.memory_id] = entry

    def all_entries(self) -> List[MemoryEntry]:
        return [entry for entry in self._entries.values() if not entry.is_expired()]

    def mark_issue_seen(self, issue_id: str) -> None:
        self._issue_history[issue_id] = self._issue_history.get(issue_id, 0) + 1

    def seen_count(self, issue_id: str) -> int:
        return self._issue_history.get(issue_id, 0)

    def purge_expired(self) -> int:
        expired_ids = [k for k, v in self._entries.items() if v.is_expired()]
        for k in expired_ids:
            self._entries.pop(k, None)
        return len(expired_ids)


class MemoryRetriever:
    @staticmethod
    def _token_set(text: str) -> set:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    @classmethod
    def _similarity(cls, issue_context: str, memory_content: str) -> float:
        a = cls._token_set(issue_context)
        b = cls._token_set(memory_content)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    @classmethod
    def rank(
        cls,
        issue: Issue,
        entries: Iterable[MemoryEntry],
        now: Optional[datetime] = None,
        top_k: int = 5,
    ) -> List[MemoryEntry]:
        now = now or datetime.utcnow()

        def score(entry: MemoryEntry) -> float:
            similarity = cls._similarity(issue.context, entry.content)
            age_hours = max((now - entry.updated_at).total_seconds() / 3600.0, 0.0)
            recency = math.exp(-age_hours / 72.0)
            return (0.45 * similarity) + (0.25 * recency) + (0.2 * entry.confidence) + (0.1 * entry.success_rate)

        ranked = sorted((e for e in entries if not e.is_expired(now)), key=score, reverse=True)
        return ranked[:top_k]


class ExternalMemoryProvider(Protocol):
    name: str

    def upsert(self, entry: MemoryEntry) -> None:
        ...

    def search(self, issue: Issue, top_k: int = 5) -> List[MemoryEntry]:
        ...


class HTTPExtMemoryProvider:
    def __init__(self, name: str, base_url: str, api_key: Optional[str] = None) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def upsert(self, entry: MemoryEntry) -> None:
        payload = {
            "id": entry.memory_id,
            "type": entry.memory_type,
            "issue_id": entry.issue_id,
            "content": entry.content,
            "tags": entry.tags,
            "confidence": entry.confidence,
            "success_rate": entry.success_rate,
            "source": entry.source,
        }
        self._post_json("/memories/upsert", payload)

    def search(self, issue: Issue, top_k: int = 5) -> List[MemoryEntry]:
        payload = {"issue_id": issue.issue_id, "context": issue.context, "top_k": top_k}
        data = self._post_json("/memories/search", payload)
        if not isinstance(data, list):
            return []
        memories: List[MemoryEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                memories.append(
                    MemoryEntry(
                        memory_id=str(item.get("id", "")),
                        memory_type=MemoryType(str(item.get("type", MemoryType.EPISODIC))),
                        issue_id=str(item.get("issue_id", issue.issue_id)),
                        content=str(item.get("content", "")),
                        tags=[str(t) for t in item.get("tags", []) if isinstance(t, str)],
                        confidence=float(item.get("confidence", 0.5)),
                        success_rate=float(item.get("success_rate", 0.5)),
                        source=str(item.get("source", self.name)),
                    )
                )
            except (ValueError, TypeError):
                continue
        return memories

    def _post_json(self, path: str, payload: Dict) -> object:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=2.5) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except (error.URLError, error.HTTPError, json.JSONDecodeError):
            return {}


def build_configured_providers() -> List[ExternalMemoryProvider]:
    providers: List[ExternalMemoryProvider] = []
    cognee_url = os.getenv("COGNEE_BASE_URL")
    cognee_key = os.getenv("COGNEE_API_KEY")
    memoryai_url = os.getenv("MEMORYAI_BASE_URL")
    memoryai_key = os.getenv("MEMORYAI_API_KEY")
    if cognee_url:
        providers.append(HTTPExtMemoryProvider("cognee", cognee_url, cognee_key))
    if memoryai_url:
        providers.append(HTTPExtMemoryProvider("memoryai", memoryai_url, memoryai_key))
    return providers


class AgenticMemoryFramework:
    """Issue-improvement workflow powered by agentic memory."""

    ROLLOUT_PHASES = [
        "memory_schema_and_storage",
        "retrieval_integration",
        "issue_assist_behavior",
        "feedback_reflection_loop",
        "metrics_dashboard_and_tuning",
    ]

    REVIEW_CADENCE = {
        "memory_quality_review": "weekly",
        "retrieval_tuning": "monthly",
        "framework_audit": "quarterly",
    }

    def __init__(self, providers: Optional[List[ExternalMemoryProvider]] = None) -> None:
        self.store = MemoryStore()
        self.metrics = SuccessMetrics()
        self.providers = providers or build_configured_providers()

    def ingest_issue(self, issue: Issue) -> None:
        self.store.mark_issue_seen(issue.issue_id)
        if self.store.seen_count(issue.issue_id) > 1:
            self.metrics.recurrence_count += 1

    def retrieve_memories(self, issue: Issue, top_k: int = 5) -> List[MemoryEntry]:
        local_entries = self.store.all_entries()
        external_entries: List[MemoryEntry] = []
        for provider in self.providers:
            external_entries.extend(provider.search(issue, top_k=top_k))
        return MemoryRetriever.rank(issue, [*local_entries, *external_entries], top_k=top_k)

    def propose_actions(self, issue: Issue, memories: List[MemoryEntry]) -> List[str]:
        actions = []
        if issue.priority <= 2:
            actions.append("Escalate to fast-track issue lane")
        actions.append("Triaging with retrieved episodic/procedural insights")
        for memory in memories:
            if memory.memory_type == MemoryType.PROCEDURAL and memory.success_rate >= 0.6:
                actions.append(f"Apply known resolution pattern from memory {memory.memory_id}")
                break
        if not memories:
            actions.append("Create a new diagnostic playbook and capture learnings")
        return actions

    def execute_issue_cycle(self, issue: Issue, accepted: bool, reopened: bool) -> IssueCycleResult:
        started = datetime.utcnow()
        self.ingest_issue(issue)
        memories = self.retrieve_memories(issue)
        proposed = self.propose_actions(issue, memories)

        # Placeholder for execution orchestration: in real usage, connect to issue trackers/agents.
        executed = list(proposed)

        ended = datetime.utcnow()
        cycle = IssueCycleResult(
            issue_id=issue.issue_id,
            identified_priority=issue.priority,
            proposed_actions=proposed,
            executed_actions=executed,
            accepted=accepted,
            reopened=reopened,
            started_at=started,
            ended_at=ended,
        )
        self.metrics.update(cycle)
        self.reflect_and_learn(issue, cycle, memories)
        self.apply_lifecycle_policy()
        return cycle

    def reflect_and_learn(
        self,
        issue: Issue,
        cycle: IssueCycleResult,
        used_memories: List[MemoryEntry],
    ) -> None:
        outcome = "accepted" if cycle.accepted and not cycle.reopened else "needs_refinement"
        confidence = 0.8 if outcome == "accepted" else 0.45
        success_rate = 0.9 if outcome == "accepted" else 0.4

        procedural_content = (
            f"Issue {issue.issue_id} outcome={outcome}. "
            f"Actions: {'; '.join(cycle.executed_actions)}"
        )
        semantic_content = (
            f"Domain lesson from issue {issue.issue_id}: priority={issue.priority}, "
            f"labels={','.join(issue.labels) or 'none'}, outcome={outcome}"
        )

        self.store.upsert(
            MemoryEntry(
                memory_id=_stable_id(f"procedural:{issue.issue_id}:{procedural_content}"),
                memory_type=MemoryType.PROCEDURAL,
                issue_id=issue.issue_id,
                content=procedural_content,
                tags=["reflection", "procedural", outcome],
                confidence=confidence,
                success_rate=success_rate,
                source="reflection_loop",
            )
        )
        self.store.upsert(
            MemoryEntry(
                memory_id=_stable_id(f"semantic:{issue.issue_id}:{semantic_content}"),
                memory_type=MemoryType.SEMANTIC,
                issue_id=issue.issue_id,
                content=semantic_content,
                tags=["reflection", "semantic", outcome],
                confidence=min(confidence + 0.1, 1.0),
                success_rate=success_rate,
                source="reflection_loop",
            )
        )

        for memory in used_memories:
            memory.success_rate = min(memory.success_rate + 0.05, 1.0) if cycle.accepted else max(memory.success_rate - 0.1, 0.0)
            memory.updated_at = datetime.utcnow()

    def apply_lifecycle_policy(self) -> None:
        # Update and retention policy (default):
        # - Working memory: expires after 24h.
        # - Episodic memory: expires after 180 days.
        # - Semantic/procedural memory: no auto-expiry.
        now = datetime.utcnow()
        for entry in self.store.all_entries():
            if entry.memory_type == MemoryType.WORKING:
                entry.expires_at = entry.expires_at or now + timedelta(hours=24)
            elif entry.memory_type == MemoryType.EPISODIC:
                entry.expires_at = entry.expires_at or now + timedelta(days=180)
            entry.updated_at = now
        self.store.purge_expired()

    def add_memory(
        self,
        memory_type: MemoryType,
        issue_id: str,
        content: str,
        tags: Optional[List[str]] = None,
        confidence: float = 0.5,
        success_rate: float = 0.5,
        source: str = "manual",
    ) -> str:
        memory_id = _stable_id(f"{memory_type}:{issue_id}:{content}")
        entry = MemoryEntry(
            memory_id=memory_id,
            memory_type=memory_type,
            issue_id=issue_id,
            content=content,
            tags=tags or [],
            confidence=confidence,
            success_rate=success_rate,
            source=source,
        )
        self.store.upsert(entry)
        for provider in self.providers:
            provider.upsert(entry)
        return memory_id


def _stable_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
