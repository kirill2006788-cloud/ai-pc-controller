# Case Study — AI PC Controller

## 1) Goal

Design a living desktop assistant system that feels like a real operator layer on top of the OS:

- understand user intent,
- route to proper subsystem,
- execute and respond quickly,
- remain extensible for future modules.

## 2) Product Intent

Unlike basic chatbot wrappers, this project targets a richer desktop experience:

- scene/state driven UI,
- dedicated scenes like **Neon City** and **System Monitor**,
- **HUD mode** for overlay controls and contextual widgets,
- **Agent mode** for task execution flow and progress feedback,
- command routing,
- local-first response path when possible,
- integration points for external providers.

## 3) Architecture Approach

Core ideas:

- **Engine as state orchestrator** (signals/events)
- **Command router** for intent → action mapping
- **Service layer** for AI, TTS, Telegram, OS control, metrics
- **Provider abstraction** for AI backends

This enables faster feature iteration without tight coupling.

## 4) Engineering Trade-offs

- prioritized modularity and maintainability over quick monolith shortcuts
- built fallback paths (local mode when some integrations unavailable)
- designed integration boundaries before adding more providers

## 5) What’s next (roadmap)

- stronger telemetry and observability
- stricter sandboxing for OS-level actions
- richer plugin ecosystem for tools and automations
- packaged desktop distribution improvements

## 6) Portfolio outcome

Shows capability in:

- architecture thinking,
- desktop app systems design,
- AI integration layering,
- practical product engineering.
