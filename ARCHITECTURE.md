# Architecture Overview (Public)

```text
[Voice/Input UI]
      |
      v
[Living Engine / Scene State]
      |
      +--> [Command Router] --> [OS Control Service]
      |
      +--> [AI Router] -------> [Local AI Engine]
      |                         [Cloud Providers]
      |
      +--> [TTS Service]
      +--> [Telegram Service]
      +--> [System Metrics]
```

## Key Principles

- Event-driven desktop architecture
- Service abstraction boundaries
- Provider-agnostic AI layer
- Fallback-friendly behavior

## Security posture (public summary)

- Credentials are read from environment, never hardcoded in public docs
- API keys are excluded from this public portfolio package
- Full operational configuration remains private
