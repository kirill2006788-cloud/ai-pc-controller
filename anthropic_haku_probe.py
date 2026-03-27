import requests
from config import API_KEY, BASE_URL

versions = ["2024-10-01", "2024-06-01", "2023-11-08", "2023-10-11", "2023-06-01"]
models = [
    "haku-3.5",
    "haku-3.5-deepseek",
    "haku-3.5-deep-seek",
    "haku-3.5-deepseek-1",
    "haku-haka-3.5",
    "haku-3.5-haka",
    "claude-haku-3.5",
    "cladue-haku-3.5",
    "haku_3_5",
]

prompt = "Human: Кратко представься на русском.\nAssistant:"

for v in versions:
    for m in models:
        headers = {"x-api-key": API_KEY, "Content-Type": "application/json", "anthropic-version": v}
        payload = {"model": m, "prompt": prompt, "max_tokens_to_sample": 60}
        url = f"{BASE_URL.rstrip('/')}/v1/complete"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            status = resp.status_code
            body = resp.text
        except Exception as e:
            status = None
            body = str(e)
        print(f"version={v} model={m} status={status}")
        if status and (200 <= status < 300):
            print('SUCCESS body:', body)
            raise SystemExit(0)
        else:
            print('BODY:', body)

print('Probe finished — no successful combos found.')
