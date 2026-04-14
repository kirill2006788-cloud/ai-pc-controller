# Publish Checklist (No Source / No Secrets)

## Never publish

- full source code directories
- `.env` with real values
- API keys / tokens / secrets
- personal sessions, credentials, local cache

## Publish allowed

- portfolio README
- case study
- architecture summary
- safe pseudo snippets
- screenshots / mock visuals

## Final pre-push checks

- search for `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`
- verify only public kit files are tracked
- open README and confirm images render correctly
