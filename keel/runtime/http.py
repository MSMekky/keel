"""A small HTTP client, built on the standard library.

No ``requests``, no ``httpx``, no vendor SDK. Three reasons, in order:

1. **The transport is injectable.** Every network call in this platform goes
   through ``Transport.send``. Tests substitute a fake and exercise the real
   request-construction and response-parsing code without a socket, which is
   how the provider and the GitHub tools are covered in CI.
2. **No SDK version drift.** The Anthropic and GitHub HTTP APIs are stable and
   documented; an SDK is one more thing to pin, upgrade and be surprised by.
3. **Deploys anywhere.** ``pip install`` is not a step.

What is given up: streaming, connection pooling, HTTP/2. None matter for an
agent runtime whose unit of work is a single completion with a 12-turn cap.

Retries are on 429 and 5xx only, with exponential backoff and jitter, honouring
``Retry-After`` when the server sends it. A 4xx that is not 429 is a bug in our
request and retrying it just makes the same mistake more expensive.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:400]}")
        self.status, self.body, self.url = status, body, url

    @property
    def retryable(self) -> bool:
        return self.status == 429 or 500 <= self.status < 600


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode() or "null")


class Transport(Protocol):
    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None, timeout: float) -> Response: ...


class UrllibTransport:
    """The real one. The only place in the codebase that opens a socket."""

    def send(self, method, url, headers, body, timeout) -> Response:
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            # urllib raises on 4xx/5xx; we want them as values, so the retry
            # policy lives in one place rather than in every call site.
            return Response(exc.code, dict(exc.headers or {}), exc.read())


@dataclass
class RecordingTransport:
    """Wraps a transport and writes every exchange to a cassette.

    Used by the live evaluation runner: record once against the real API, then
    replay in CI forever. See ``keel/evals/live.py``.
    """

    inner: Transport
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def send(self, method, url, headers, body, timeout) -> Response:
        resp = self.inner.send(method, url, headers, body, timeout)
        self.exchanges.append({
            "method": method,
            "url": url,
            "request": json.loads(body.decode()) if body else None,
            "status": resp.status,
            "response": json.loads(resp.body.decode() or "null"),
        })
        return resp


@dataclass
class ReplayTransport:
    """Replays a cassette in order. Raises if the script runs out.

    Deliberately strict about ordering: a change that reorders calls should
    fail loudly rather than silently replay the wrong response.
    """

    exchanges: list[dict[str, Any]]
    _i: int = 0

    def send(self, method, url, headers, body, timeout) -> Response:
        if self._i >= len(self.exchanges):
            raise AssertionError(
                f"cassette exhausted: unexpected {method} {url}. Re-record with "
                f"KEEL_RECORD=1 if the agent legitimately makes more calls now."
            )
        ex = self.exchanges[self._i]
        self._i += 1
        return Response(ex["status"], {},
                        json.dumps(ex["response"]).encode())


class Client:
    def __init__(self, transport: Transport | None = None, *, timeout: float = 60.0,
                 max_retries: int = 4, sleep=time.sleep):
        self.transport = transport or UrllibTransport()
        self.timeout, self.max_retries, self._sleep = timeout, max_retries, sleep
        #: Every request made, for the audit log and for cost attribution.
        self.calls: list[tuple[str, str, int]] = []

    def request(self, method: str, url: str, *, headers: dict[str, str],
                json_body: Any = None) -> Any:
        body = json.dumps(json_body).encode() if json_body is not None else None
        if body is not None:
            headers = {**headers, "content-type": "application/json"}

        last: HTTPError | None = None
        for attempt in range(self.max_retries + 1):
            resp = self.transport.send(method, url, headers, body, self.timeout)
            self.calls.append((method, url, resp.status))

            if 200 <= resp.status < 300:
                return resp.json()

            err = HTTPError(resp.status, resp.body.decode(errors="replace"), url)
            if not err.retryable or attempt == self.max_retries:
                raise err
            last = err

            retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 2.0 ** attempt
            else:
                delay = 2.0 ** attempt
            # Jitter, so a fleet of workers hitting a rate limit does not
            # synchronise and hammer the endpoint in lockstep.
            self._sleep(delay + random.uniform(0, 0.5))

        raise last  # unreachable; kept so the type is honest
