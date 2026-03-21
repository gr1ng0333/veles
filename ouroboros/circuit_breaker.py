"""Generic three-state circuit breaker for unreliable external services.

States
------
CLOSED    – normal operation, requests flow through.
OPEN      – all requests are rejected; waiting for recovery timeout.
HALF_OPEN – one probe request is allowed; success → CLOSED, failure → OPEN.

Recovery timeout uses exponential backoff with jitter so that multiple
backends don't retry in lock-step.
"""

from __future__ import annotations

import logging
import random
import threading
import time

log = logging.getLogger(__name__)


class CircuitBreaker:
    """Thread-safe circuit breaker with exponential backoff and jitter."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
        max_recovery_timeout: float = 600.0,
    ) -> None:
        self.name = name
        self.failure_threshold = max(failure_threshold, 1)
        self._base_recovery_timeout = float(recovery_timeout)
        self._current_recovery_timeout = float(recovery_timeout)
        self._max_recovery_timeout = float(max_recovery_timeout)
        self.half_open_max_calls = max(half_open_max_calls, 1)

        self._state: str = self.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: float = 0.0
        self._half_open_calls: int = 0
        self._trip_count: int = 0
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow_request(self) -> bool:
        """Return ``True`` if a request should be attempted."""
        with self._lock:
            if self._state == self.CLOSED:
                return True

            if self._state == self.OPEN:
                elapsed = time.monotonic() - self._opened_at
                jittered = self._current_recovery_timeout * (1.0 + random.random() * 0.2)
                if elapsed >= jittered:
                    self._state = self.HALF_OPEN
                    self._half_open_calls = 0
                    log.info(
                        "Circuit breaker [%s] OPEN → HALF_OPEN after %.1fs",
                        self.name,
                        elapsed,
                    )
                    return True
                return False

            # HALF_OPEN — allow up to half_open_max_calls probes
            if self._half_open_calls < self.half_open_max_calls:
                return True
            return False

    def record_success(self) -> None:
        """Record a successful call.  HALF_OPEN → CLOSED, resets backoff."""
        with self._lock:
            self._consecutive_failures = 0
            if self._state != self.CLOSED:
                log.info(
                    "Circuit breaker [%s] %s → CLOSED (success)",
                    self.name,
                    self._state.upper(),
                )
                self._state = self.CLOSED
                self._current_recovery_timeout = self._base_recovery_timeout

    def record_failure(self) -> None:
        """Record a failed call.  May transition CLOSED → OPEN or HALF_OPEN → OPEN."""
        with self._lock:
            self._consecutive_failures += 1

            if self._state == self.HALF_OPEN:
                self._half_open_calls += 1
                # Probe failed — back to OPEN with doubled backoff
                self._current_recovery_timeout = min(
                    self._current_recovery_timeout * 2,
                    self._max_recovery_timeout,
                )
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                self._trip_count += 1
                log.warning(
                    "Circuit breaker [%s] HALF_OPEN → OPEN (probe failed). "
                    "Next cooldown: %.0fs (trip #%d)",
                    self.name,
                    self._current_recovery_timeout,
                    self._trip_count,
                )
                return

            if self._consecutive_failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                self._trip_count += 1
                log.warning(
                    "Circuit breaker [%s] CLOSED → OPEN after %d failures. "
                    "Cooldown: %.0fs (trip #%d)",
                    self.name,
                    self._consecutive_failures,
                    self._current_recovery_timeout,
                    self._trip_count,
                )
