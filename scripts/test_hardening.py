#!/usr/bin/env python3
"""
Hardening test script for the Roadent API.

Hits a running local instance (default http://localhost:8000) with:
  1. Malformed JSON body               -> expects a clean 400, never a crash
  2. An empty chat message              -> expects 400
  3. A 5000-character chat message      -> expects 400
  4. A chat request against a
     temporarily-invalid Mistral key    -> spins up a throwaway server on
                                           another port with a bad key,
                                           expects 200 + graceful fallback
                                           reply, never a 500
  5. 50 rapid requests                  -> expects 429s once the 30/min
                                           per-IP cap is exceeded, with a
                                           friendly JSON body (run LAST since
                                           it burns the main server's rate
                                           budget for the next minute)

Run: python3 scripts/test_hardening.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("ROADENT_TEST_URL", "http://localhost:8000")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def raw_post(url, body_bytes, headers=None):
    headers = headers or {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return None, str(e)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return None, str(e)


def section(title):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def test_malformed_json():
    section("1. MALFORMED JSON BODY -> clean 400, no crash")
    body = b'{"message": "hello", "lat": 28.6, "lng": '  # truncated / invalid JSON
    status, resp_body = raw_post(f"{BASE}/api/chat", body)
    print(f"status={status}\nbody={resp_body[:300]}")
    assert status == 400, f"expected 400, got {status}"
    data = json.loads(resp_body)
    assert "message" in data
    print("PASS")


def test_empty_message():
    section("2. EMPTY MESSAGE -> 400")
    body = json.dumps({"message": "", "lat": 28.6, "lng": 77.2}).encode()
    status, resp_body = raw_post(f"{BASE}/api/chat", body)
    print(f"status={status}\nbody={resp_body[:300]}")
    assert status == 400, f"expected 400, got {status}"
    print("PASS")


def test_oversized_message():
    section("3. 5000-CHAR MESSAGE -> 400 (cap is 1000)")
    body = json.dumps({"message": "a" * 5000, "lat": 28.6, "lng": 77.2}).encode()
    status, resp_body = raw_post(f"{BASE}/api/chat", body)
    print(f"status={status}\nbody={resp_body[:300]}")
    assert status == 400, f"expected 400, got {status}"
    print("PASS")


def test_invalid_mistral_key():
    section("4. TEMPORARILY-INVALID MISTRAL KEY -> graceful fallback, never 500")
    env = os.environ.copy()
    env["MISTRAL_API_KEY"] = "sk-invalid-test-key-00000000"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--port", "8901"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            status, _ = get("http://localhost:8901/health")
            if status == 200:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("throwaway server on :8901 never came up")

        body = json.dumps({
            "message": "I was in an accident, what do I do?",
            "lat": 28.6139, "lng": 77.2090,
        }).encode()
        status, resp_body = raw_post("http://localhost:8901/api/chat", body)
        print(f"status={status}\nbody={resp_body[:400]}")
        assert status == 200, f"expected 200 even with a bad Mistral key, got {status}"
        data = json.loads(resp_body)
        assert data.get("reply"), "expected a fallback reply, got none"
        print("PASS — invalid Mistral key degraded gracefully, no crash")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_rate_limit():
    section("5. RATE LIMITING — 50 rapid requests to /api/stats (30/min cap) [run last]")
    # Rate limiting only applies to /api/* paths (per spec) — /health is
    # intentionally exempt so uptime checks aren't affected.
    codes = []
    for _ in range(50):
        status, _ = get(f"{BASE}/api/stats")
        codes.append(status)
    ok = codes.count(200)
    limited = codes.count(429)
    print(f"200 OK: {ok}   429 Too Many Requests: {limited}")
    assert limited > 0, "expected at least one 429 after exceeding the rate limit"

    status, body = get(f"{BASE}/api/stats")
    print(f"a further request -> status={status} body={body}")
    if status == 429:
        data = json.loads(body)
        assert "message" in data, "429 body should carry a friendly message"
    print("PASS")


if __name__ == "__main__":
    print(f"Testing Roadent API hardening against {BASE}")
    failures = []
    for test in (test_malformed_json, test_empty_message, test_oversized_message,
                 test_invalid_mistral_key, test_rate_limit):
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
            print(f"FAIL: {e}")

    section("SUMMARY")
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)
    print("All hardening checks passed — every response was graceful, no crashes.")
