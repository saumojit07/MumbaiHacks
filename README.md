# Agentic Memory Framework for Issue Improvement

This project implements an agentic-memory-powered framework to improve issue identification, prioritization, and resolution quality over time.

## What is implemented

- Memory types:
  - **Working memory** for active context
  - **Episodic memory** for past issue cycles
  - **Semantic memory** for domain knowledge and reusable lessons
  - **Procedural memory** for successful resolution patterns
- End-to-end issue improvement workflow:
  - Ingest issue
  - Retrieve ranked memories
  - Propose and execute actions
  - Reflect on outcomes
  - Store learnings for reuse
- Memory lifecycle:
  - Validation with governance checks
  - Confidence + success scoring
  - Retention and expiry policy
  - Conflict handling through confidence-based upserts
- Retrieval and ranking:
  - Context similarity
  - Recency
  - Confidence
  - Historical success rate
- Reflection loop:
  - Updates semantic/procedural memories after each issue cycle
  - Reinforces or downgrades used memories by outcome
- Success metrics:
  - Average resolution time
  - Recurrence count/rate indicator
  - Fix acceptance rate
  - Reopen rate
- Governance and safety:
  - Secret-pattern filtering
  - Validation guardrails before memory storage
- Rollout and operations metadata:
  - Five rollout phases
  - Weekly/monthly/quarterly improvement cadence

## External memory systems support

The framework includes pluggable HTTP provider support and can integrate with systems like **Cognee** and **MemoryAI** through environment variables:

- `COGNEE_BASE_URL`
- `COGNEE_API_KEY` (optional)
- `MEMORYAI_BASE_URL`
- `MEMORYAI_API_KEY` (optional)

If configured, new memories are synced to providers and provider memories are included during retrieval/ranking.

## Source

- Core implementation: `/home/runner/work/MumbaiHacks/MumbaiHacks/src/agentic_memory_framework.py`
