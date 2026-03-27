import requests
from config import API_KEY, BASE_URL, NEURAL_MODEL

# Expanded probe: tries multiple headers, model names and endpoints
versions = [None, "2023-06-01", "2023-10-11", "2023-11-08", "2024-01-01", "2024-06-01"]
models = [
    NEURAL_MODEL,
    "claude-2",
    "claude-2.1",
    "claude-3",
    "claude-3.1",
    "claude-3.5",
    "claude-3.5-100k",
    "claude-instant-1",
    "claude-instant-v1",
    "haku-3.5",
    "haku-3.5-deepseek",
    "haka-3.5",
    "haku",
    None,
]
endpoints = ["/v1/complete", "/v1/answers", "/v1/models"]

prompt = "Human: Кратко представься на русском.\nAssistant:"

results = []
for ep in endpoints:
    for v in versions:
        for m in models:
            # build headers
            headers = {"Content-Type": "application/json"}
            if API_KEY:
                headers["x-api-key"] = API_KEY
            if v:
                headers["anthropic-version"] = v

            payload = None
            method = "POST"
            url = f"{BASE_URL.rstrip('/')}{ep}"

            if ep == "/v1/models":
                method = "GET"
            else:
                payload = {}
                if m:
                    payload["model"] = m
                payload["prompt"] = prompt
                # different params names in various docs
                payload["max_tokens_to_sample"] = 60
                payload["temperature"] = 0.2

            try:
                if method == "GET":
                    resp = requests.get(url, headers=headers, timeout=12)
                else:
                    resp = requests.post(url, json=payload, headers=headers, timeout=12)
                status = resp.status_code
                body = resp.text
            except Exception as e:
                status = None
                body = str(e)

            print(f"EP={ep} version={v} model={m} status={status}")
            print(body)
            results.append((ep, v, m, status, body))

# summary of successes
success = [r for r in results if r[3] and 200 <= r[3] < 300]
print('\nSummary:')
if success:
    for s in success:
        print('OK', s[0], s[1], s[2])
else:
    print('No successful combinations found.')
