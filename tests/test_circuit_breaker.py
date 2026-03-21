"""Tests for CircuitBreaker."""

from __future__ import annotations

import threading
import time

from ouroboros.circuit_breaker import CircuitBreaker


def test_circuit_breaker_starts_closed():
    cb = CircuitBreaker("test", failure_threshold=3)
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.allow_request() is True


def test_circuit_breaker_opens_after_failures():
    cb = CircuitBreaker("test", failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is True  # still CLOSED (2 < 3)
    cb.record_failure()
    assert cb.allow_request() is False  # OPEN


def test_circuit_breaker_half_open_after_timeout():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is False  # OPEN
    time.sleep(0.25)
    assert cb.allow_request() is True  # → HALF_OPEN
    assert cb.state == CircuitBreaker.HALF_OPEN


def test_circuit_breaker_closes_on_success():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.25)
    assert cb.allow_request() is True  # → HALF_OPEN
    cb.record_success()  # → CLOSED
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.allow_request() is True


def test_circuit_breaker_half_open_probe_failure_reopens():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.25)
    assert cb.allow_request() is True  # → HALF_OPEN
    cb.record_failure()  # probe failed → OPEN with doubled backoff
    assert cb.state == CircuitBreaker.OPEN
    assert cb.allow_request() is False


def test_circuit_breaker_exponential_backoff():
    cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1, max_recovery_timeout=10.0)
    # Trip 1
    cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN
    time.sleep(0.25)
    cb.allow_request()  # → HALF_OPEN
    cb.record_failure()  # probe fail → OPEN, backoff doubles to 0.2
    assert cb._current_recovery_timeout >= 0.19  # ~0.2


def test_circuit_breaker_success_resets_failures():
    cb = CircuitBreaker("test", failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()  # resets counter
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is True  # still CLOSED (only 2 since reset)


def test_circuit_breaker_thread_safe():
    cb = CircuitBreaker("test", failure_threshold=100)
    errors: list[Exception] = []

    def hammer():
        try:
            for _ in range(1000):
                cb.allow_request()
                cb.record_failure()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 0
