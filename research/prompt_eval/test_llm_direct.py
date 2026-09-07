"""直接调用 v9.1 LLM 看真实响应/错误,绕过 v9.1 的 try/except.

用 002671 / 603268 这两个总报 RateLimit 的股票模拟现场.
"""
from __future__ import annotations
import sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

import os
from openai import OpenAI

API_KEY = os.getenv('DEEPSEEK_API_KEY')
BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash')

print(f'API_KEY: {API_KEY[:10]}...')
print(f'BASE_URL: {BASE_URL}')
print(f'MODEL: {MODEL}')
print()

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# Simulate the exact prompt size we use in backtest (large)
big_prompt = "下面是某股票的VPA分析数据 (5000 字符模拟):\n" + ("数据数据数据数据数据数据数据数据" * 200)

system = "你是 Anna Coulling VPA 专家. 请分析以下股票数据,给出 verdict (看多/看空/中性) + 信心 (0-1)."

print(f'Prompt size: ~{len(system) + len(big_prompt)} chars')
print()

# Test 1: single call
print('=== Test 1: 单次调用 ===')
try:
    t0 = time.time()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{'role': 'system', 'content': system},
                   {'role': 'user', 'content': big_prompt + '\n请简短回答 verdict.'}],
        max_tokens=12000,
        timeout=180,
        extra_body={'enable_thinking': False},
    )
    print(f'  ✓ Success in {time.time()-t0:.1f}s')
    print(f'  finish_reason: {resp.choices[0].finish_reason}')
    print(f'  content[:300]: {resp.choices[0].message.content[:300]}')
    print(f'  usage: {resp.usage}')
except Exception as e:
    print(f'  ✗ Error: {type(e).__name__}')
    print(f'    msg: {str(e)[:500]}')
    if hasattr(e, 'response') and e.response:
        print(f'    http_status: {e.response.status_code}')
        print(f'    body: {e.response.text[:500]}')

# Test 2: 10 parallel calls (simulating burst)
print('\n=== Test 2: 10 并发模拟 ===')
from concurrent.futures import ThreadPoolExecutor, as_completed

def one_call(i):
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{'role': 'system', 'content': system},
                       {'role': 'user', 'content': big_prompt + f'\n第{i}次. verdict?'}],
            max_tokens=12000,
            timeout=180,
            extra_body={'enable_thinking': False},
        )
        return f'#{i}: ✓ {time.time()-t0:.1f}s usage={resp.usage.total_tokens}t'
    except Exception as e:
        msg = str(e)[:200]
        body = ''
        if hasattr(e, 'response') and e.response:
            body = f' http={e.response.status_code} body={e.response.text[:200]}'
        return f'#{i}: ✗ {type(e).__name__}: {msg}{body}'

with ThreadPoolExecutor(max_workers=10) as ex:
    futures = [ex.submit(one_call, i) for i in range(10)]
    for fut in as_completed(futures):
        print(' ', fut.result())
