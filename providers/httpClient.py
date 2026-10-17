"""Shared sequential HTTP transport: sessions, pacing, retries and JSON errors."""
from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from curl_cffi import requests


class ProviderError(Exception):
    """Base exception shared by the provider clients."""


class ProviderRequestError(ProviderError):
    """A failed request, including its endpoint and HTTP status when available."""

    def __init__(self, message, *, endpoint=None, status_code=None, attempts=0):
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.attempts = attempts


class RequestGate:
    """Coordinate request pacing and a 403 circuit across client sessions."""

    def __init__(self, min_delay: float, max_delay: float):
        if (not all(math.isfinite(x) for x in (min_delay, max_delay))
                or not 0 <= min_delay <= max_delay):
            raise ValueError("delays must be finite and 0 <= min_delay <= max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._lock = threading.Lock()
        self._next_request_time = None
        self._blocked = None

    def wait(self, endpoint: str):
        """Reserve one globally paced request slot, then recheck the circuit."""
        with self._lock:
            self._raise_if_blocked(endpoint)
            now = time.monotonic()
            start = now if self._next_request_time is None else max(now, self._next_request_time)
            self._next_request_time = start + random.uniform(self.min_delay, self.max_delay)
        if start > now:
            time.sleep(start - now)
        with self._lock:
            self._raise_if_blocked(endpoint)

    def block(self, error: ProviderRequestError):
        """Open the circuit once; later calls fail without touching the network."""
        with self._lock:
            if self._blocked is None:
                self._blocked = (error.status_code, error.endpoint, str(error))

    @property
    def is_blocked(self):
        with self._lock:
            return self._blocked is not None

    def _raise_if_blocked(self, endpoint):
        if self._blocked is None:
            return
        status, _, message = self._blocked
        raise ProviderRequestError(
            f"Request circuit is open after {message}; refusing {endpoint}",
            endpoint=endpoint, status_code=status, attempts=0,
        )


class HttpClient:
    BASE_URL = ""
    REFERER = ""

    def __init__(
        self,
        timeout: float = 20,
        min_delay: float = 8,
        max_delay: float = 15,
        max_retries: int = 1,
        *,
        session=None,
        request_gate=None,
    ):
        """Create a sequential client.

        max_retries retains the original meaning: total attempts, including the
        first. An injected session is owned and closed by the caller.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if not all(math.isfinite(x) for x in (min_delay, max_delay)) or not 0 <= min_delay <= max_delay:
            raise ValueError("delays must be finite and 0 <= min_delay <= max_delay")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        self.timeout = timeout
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.request_gate = request_gate or RequestGate(min_delay, max_delay)
        self._owns_session = session is None
        self.session = session if session is not None else requests.Session(
            impersonate="chrome",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self.REFERER,
            },
        )
        self._closed = False

    def close(self):
        if not self._closed:
            if self._owns_session:
                self.session.close()
            self._closed = True

    def __enter__(self):
        if self._closed:
            raise ProviderError("Client is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _rate_limit(self, endpoint):
        self.request_gate.wait(endpoint)

    @staticmethod
    def _retry_delay(response, attempt):
        delay = 2 ** attempt
        value = response.headers.get("Retry-After")
        if value is not None:
            try:
                retry_after = float(value)
            except (TypeError, ValueError):
                try:
                    deadline = parsedate_to_datetime(value)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    retry_after = (deadline - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    retry_after = 0
            if math.isfinite(retry_after):
                delay = max(delay, retry_after)
        return delay

    @staticmethod
    def _safe_error(exc):
        from .networkSettings import redact
        return redact(exc)

    def _request(self, endpoint: str, params: dict | None = None):
        if self._closed:
            raise ProviderError("Client is closed")
        endpoint = endpoint.lstrip("/")
        if not endpoint or "://" in endpoint:
            raise ValueError("Use an API-relative endpoint")
        url = f"{self.BASE_URL}/{endpoint}"
        for attempt in range(self.max_retries):
            self._rate_limit(endpoint)
            response = None
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                if attempt + 1 == self.max_retries:
                    raise ProviderRequestError(
                        f"Request failed after {attempt + 1} attempts: {endpoint}: {self._safe_error(exc)}",
                        endpoint=endpoint, attempts=attempt + 1,
                    ) from exc
            if response is None:
                time.sleep(2 ** attempt)
                continue

            status = response.status_code
            if not 200 <= status < 300:
                retryable = status == 408 or 500 <= status < 600
                if not retryable or attempt + 1 == self.max_retries:
                    reason = None
                    if status == 403:
                        try:
                            payload = response.json()
                            reason = payload.get("error", {}).get("reason") if isinstance(payload, dict) else None
                        except (TypeError, ValueError):
                            pass
                    detail = f" ({reason})" if reason else ""
                    error = ProviderRequestError(
                        f"HTTP {status}{detail} for {endpoint} after {attempt + 1} attempts",
                        endpoint=endpoint, status_code=status, attempts=attempt + 1,
                    )
                    # Authentication, challenge and rate-limit responses open
                    # the provider circuit. A 5xx is endpoint/server transient:
                    # record that match as partial but allow the next paced
                    # request instead of disabling the rest of the season.
                    if status in (401, 403, 429):
                        error.retry_after = self._retry_delay(response, attempt)
                        self.request_gate.block(error)
                    raise error
                time.sleep(self._retry_delay(response, attempt))
                continue
            self._last_attempts = attempt + 1
            return response

    def _get(self, endpoint, params=None):
        response = self._request(endpoint, params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(f"Invalid JSON for {endpoint}", endpoint=endpoint,
                status_code=response.status_code, attempts=self._last_attempts) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError(f"Expected a JSON object for {endpoint}", endpoint=endpoint,
                status_code=response.status_code, attempts=self._last_attempts)
        return payload

    def get(self, endpoint, params=None):
        return self._get(endpoint, params)

    def get_text(self, endpoint, params=None):
        return self._request(endpoint, params).text
