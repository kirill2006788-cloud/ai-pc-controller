import os
from dotenv import load_dotenv
import requests

# load .env
load_dotenv()

DEEP_KEY = os.getenv('DEEPSEEK_API_KEY')
BASE = os.getenv('DEEPSEEK_BASE_URL', 'https://api.openai.com').rstrip('/')
CAND = os.getenv('DEEPSEEK_MODEL')

models = [
    CAND,
    'haku-3.5-deepseek',
    'haku-3.5-deep-seek',
    'haku-3.5',
    'haku-3.5-100k',
    'gpt-4',
    'gpt-3.5-turbo'
]
endpoints = [
    '/v1/chat/completions',
    '/v1/completions'
]

prompt = 'Ты ассистент. Кратко представься на русском.'

if not DEEP_KEY:
    print('No DEEPSEEK_API_KEY in environment; aborting probe.')
    raise SystemExit(1)

headers = {'Authorization': f'Bearer {DEEP_KEY}', 'Content-Type': 'application/json'}

for ep in endpoints:
    for m in models:
        if m is None:
            continue
        payload = {}
        if ep.endswith('/chat/completions'):
            # chat format
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
        # print only start of body to avoid huge dumps and never print keys
        body_snippet = (text or '')[:1000]
        print('BODY_SNIPPET:', body_snippet)
        if status and 200 <= status < 300:
            print('SUCCESS for', ep, m)
            raise SystemExit(0)

print('Probe finished — no successful combos found.')
