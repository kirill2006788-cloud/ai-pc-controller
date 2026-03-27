import os
from dotenv import load_dotenv
import requests

load_dotenv()
DEEP_KEY = os.getenv('DEEPSEEK_API_KEY')
BASE = os.getenv('DEEPSEEK_BASE_URL', 'https://api.openai.com').rstrip('/')

models = [
    os.getenv('DEEPSEEK_MODEL'),
    'deepseek-coder',
    'deepseek-coder-v1',
    'deepseek-coder-1',
    'deepseek_coder',
    'deepseek-coder-beta',
    'haku-3.5-deepseek',
    'code-davinci',
]
endpoints = ['/v1/chat/completions', '/v1/completions']

prompt = 'You are an assistant. Provide a 1-2 sentence intro in Russian.'

if not DEEP_KEY:
    print('No DEEPSEEK_API_KEY in environment; aborting probe.')
    raise SystemExit(1)

headers = {'Authorization': f'Bearer {DEEP_KEY}', 'Content-Type': 'application/json'}

for ep in endpoints:
    for m in models:
        if not m:
            continue
        if ep.endswith('/chat/completions'):
            payload = {
                'model': m,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 120,
            }
        else:
            payload = {
                'model': m,
                'prompt': prompt,
                'max_tokens': 120,
            }
        url = f"{BASE}{ep}"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            status = resp.status_code
            text = resp.text
        except Exception as e:
            status = None
            text = str(e)
        print(f'EP={ep} model={m} status={status}')
        print(('BODY_SNIPPET: ' + (text or '')[:1000]).replace(DEEP_KEY[:8], '***'))
        if status and 200 <= status < 300:
            print('SUCCESS for', ep, m)
            print('Full body:', text)
            raise SystemExit(0)

print('Probe finished — no successful combos found.')
