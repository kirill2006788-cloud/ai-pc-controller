# Safe Snippets (Public)

> Reduced examples for architecture discussion only.

## 1) Provider fallback concept

```python
def ask_ai(prompt: str) -> str:
    for provider in preferred_providers:
        if provider.is_available():
            answer = provider.try_answer(prompt)
            if answer:
                return answer
    return "Fallback response"
```

## 2) Command routing concept

```python
def handle_command(text: str):
    intent = router.detect_intent(text)
    handler = handlers.get(intent)
    if not handler:
        return "Unknown command"
    return handler(text)
```

## 3) Event-driven UI update

```python
engine.messages_changed.emit(messages)
engine.state_changed.emit(new_state)
```

## 4) Env-driven secrets pattern

```python
api_key = os.getenv("OPENAI_API_KEY", "")
if not api_key:
    use_local_mode()
```
