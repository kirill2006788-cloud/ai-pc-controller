import requests
from config import API_KEY, BASE_URL, NEURAL_MODEL

versions = ["2024-10-01", "2024-06-01", "2023-11-08", "2023-10-11", "2023-06-01"]
models = [NEURAL_MODEL or "claude-3.5", "claude-3", "claude-2", "claude-instant-v1", "claude-2.1", "claude-instant-1"]

prompt = "Human: Привет\nAssistant:"

results = []
for v in versions:
    for m in models:
        headers = {"x-api-key": API_KEY, "Content-Type": "application/json", "anthropic-version": v}
        payload = {"model": m, "prompt": prompt, "max_tokens_to_sample": 40}
        url = f"{BASE_URL.rstrip('/')}/v1/complete"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            status = resp.status_code
            text = resp.text
        except Exception as e:
            status = None
            text = str(e)
        print(f"version={v} model={m} status={status}")
        results.append((v, m, status, text))

# Print summary of successful attempts
success = [r for r in results if r[2] and r[2] >= 200 and r[2] < 300]
print('\nSummary:')
if success:
    for s in success:
        print('OK', s[0], s[1])
else:
    print('No successful combinations found. See above output for error messages.')
