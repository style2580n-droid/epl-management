# -*- coding: utf-8 -*-
"""
범용 지능형 수집기 (Incremental Fetcher v2)
- ETag / Last-Modified 조건부 요청으로 쿼터 80%+ 절감 (새 틀 3.1)
- API마다 인증 방식이 달라(X-Auth-Token, Authorization: Token, X-RapidAPI-Key,
  무인증 등) 헤더를 주입식으로 받도록 일반화
"""
import os
import json
import time
import requests


class IncrementalFetcher:
    def __init__(self, base_url, headers=None, state_file='data/fetch_state.json',
                 state_namespace=''):
        self.base_url = base_url.rstrip('/')
        self.headers = headers or {}
        self.state_file = state_file
        self.ns = state_namespace  # API별 상태 충돌 방지
        self.state = self._load_state()

    def _key(self, endpoint):
        return f'{self.ns}:{endpoint}' if self.ns else endpoint

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_file) or '.', exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=4, ensure_ascii=False)

    def _respect_rate_limit(self, response):
        # football-data.org 전용 헤더 (있을 때만 동작)
        available = response.headers.get('X-Requests-Available-Minute')
        if available is not None:
            try:
                if int(available) <= 1:
                    wait = min(int(response.headers.get('X-RequestCounter-Reset', 60)), 60)
                    print(f'[rate-limit] 잔여 {available}회 → {wait}s 대기')
                    time.sleep(wait)
            except ValueError:
                pass

    def fetch_with_cache(self, endpoint, params=None, max_retries=3):
        """Returns (data, is_updated). 304/실패 시 (None, False)."""
        url = f'{self.base_url}/{endpoint.lstrip("/")}'
        key = self._key(endpoint)
        last_state = self.state.get(key, {})
        req_headers = self.headers.copy()
        if 'etag' in last_state:
            req_headers['If-None-Match'] = last_state['etag']
        if 'last_modified' in last_state:
            req_headers['If-Modified-Since'] = last_state['last_modified']

        for attempt in range(1, max_retries + 1):
            try:
                r = requests.get(url, headers=req_headers, params=params, timeout=30)
            except requests.RequestException as e:
                print(f'[warn] {endpoint} 실패 ({attempt}/{max_retries}): {e}')
                time.sleep(2 ** attempt)
                continue

            self._respect_rate_limit(r)

            if r.status_code == 304:
                print(f'[skip] {endpoint} → 304 (쿼터 절감)')
                return None, False
            if r.status_code == 200:
                new_state = {}
                if r.headers.get('ETag'):
                    new_state['etag'] = r.headers['ETag']
                if r.headers.get('Last-Modified'):
                    new_state['last_modified'] = r.headers['Last-Modified']
                if new_state:
                    self.state[key] = new_state
                    self._save_state()
                try:
                    return r.json(), True
                except ValueError:
                    return None, False
            if r.status_code == 429:
                wait = int(r.headers.get('Retry-After', 60))
                print(f'[rate-limit] 429 → {wait}s 대기')
                time.sleep(wait)
                continue
            print(f'[error] {endpoint} → HTTP {r.status_code}')
            return None, False
        return None, False
