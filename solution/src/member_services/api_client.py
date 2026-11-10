"""HTTP client for the member-services mock and the provider directory.

Tenant id and credentials are fixed when the client is created. A call whose
path names a different tenant is rejected before it is sent. Grievance writes
retry a 503 with the same body and the same idempotency key.
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from member_services.phi import minimize_claim, minimize_member


class IsolationError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"HTTP {status} {code}")
        self.status = status
        self.code = code


class MemberClient:
    def __init__(self, base_url: str, tenant: str, token: str, directory_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self._token = token
        self._directory_key = directory_key
        self.timeout = timeout
        self._cache: dict[str, dict] = {}

    def health(self) -> dict:
        return self._send("GET", "/health", auth=None)

    def get_member(self, member_id: str) -> dict | None:
        status, body = self._send_status("GET", f"/tenants/{self.tenant}/members/{member_id}")
        if status == 404:
            return None
        if status != 200:
            raise ApiError(status, _code(body))
        return minimize_member(body)

    def verify_ssn_last4(self, member_id: str, last4: str) -> dict:
        """Compare last-four inside the client. The SSN never leaves this method."""
        status, body = self._send_status("GET", f"/tenants/{self.tenant}/members/{member_id}")
        if status == 404:
            return {"found": False, "match": False}
        if status != 200:
            raise ApiError(status, _code(body))
        digits = "".join(ch for ch in str(body.get("ssn", "")) if ch.isdigit())
        supplied = "".join(ch for ch in last4 if ch.isdigit())
        return {"found": True, "match": len(supplied) == 4 and digits.endswith(supplied)}

    def get_benefits(self, member_id: str) -> dict | None:
        status, body = self._send_status("GET", f"/tenants/{self.tenant}/members/{member_id}/benefits")
        if status == 404:
            return None
        if status != 200:
            raise ApiError(status, _code(body))
        return body

    def get_claim(self, claim_id: str) -> dict | None:
        path = f"/tenants/{self.tenant}/claims/{claim_id}"
        status, body = self._send_status("GET", path, cache=True)
        if status == 404:
            return None
        if status != 200:
            raise ApiError(status, _code(body))
        return minimize_claim(body)

    def list_claims(self, member_id: str) -> list[dict]:
        path = f"/tenants/{self.tenant}/members/{member_id}/claims?page_size=5"
        items: list[dict] = []
        seen: set[str] = set()
        while path:
            if path in seen:
                break
            seen.add(path)
            body = self._send("GET", path, cache=True)
            items.extend(minimize_claim(item) for item in body.get("data", []))
            path = body.get("next")
        return items

    def list_grievances(self, member_id: str) -> list[dict]:
        body = self._send("GET", f"/tenants/{self.tenant}/members/{member_id}/grievances")
        return list(body.get("data", []))

    def file_grievance(self, member_id: str, body_bytes: bytes, idempotency_key: str) -> dict:
        path = f"/tenants/{self.tenant}/members/{member_id}/grievances"
        headers = {"Idempotency-Key": idempotency_key}
        attempts = 0
        replayed = False
        last_status = 0
        last_body: dict = {}
        while attempts < 3:
            attempts += 1
            status, response, response_headers = self._send_raw("POST", path, body_bytes, headers)
            last_status = status
            last_body = response
            if status in (200, 201):
                replayed = response_headers.get("Idempotent-Replayed", "").lower() == "true"
                record = dict(response)
                record["attempts"] = attempts
                record["idempotent_replay"] = replayed or status == 200
                return record
            if status == 503:
                continue
            if status == 429:
                time.sleep(_retry_after(response_headers))
                continue
            raise ApiError(status, _code(response))
        raise ApiError(last_status or 503, _code(last_body) or "service_unavailable")

    def search_providers(self, params: dict) -> dict:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        return self._send("GET", f"/directory/v3/providers?{query}", auth="directory")

    def _send(self, method: str, path: str, body: bytes | None = None, auth: str | None = "bearer", cache: bool = False) -> dict:
        status, payload = self._send_status(method, path, body, auth=auth, cache=cache)
        if status >= 400:
            raise ApiError(status, _code(payload))
        return payload

    def _send_status(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        auth: str | None = "bearer",
        cache: bool = False,
    ) -> tuple[int, dict]:
        self._guard(path)
        cached = cache and method == "GET"
        if cached and path in self._cache:
            return 200, self._cache[path]
        delays = 0
        while True:
            status, payload, headers = self._send_raw(method, path, body, auth=auth)
            if status == 429 and delays < 5:
                delays += 1
                time.sleep(_retry_after(headers))
                continue
            if cached and status == 200:
                self._cache[path] = payload
            return status, payload

    def _send_raw(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        extra_headers: dict | None = None,
        auth: str | None = "bearer",
    ) -> tuple[int, dict, dict]:
        self._guard(path)
        headers = {"Accept": "application/json"}
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {self._token}"
        elif auth == "directory":
            headers["X-Api-Key"] = self._directory_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return response.status, parsed, dict(response.headers)
        except HTTPError as exc:
            raw = exc.read()
            headers = dict(exc.headers)
            code = exc.code
            exc.close()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                parsed = {"error": "bad_response"}
            return code, parsed, headers
        except URLError as exc:
            raise ApiError(0, "unreachable") from exc

    def _guard(self, path: str) -> None:
        parts = [part for part in path.split("?", 1)[0].split("/") if part]
        if parts and parts[0] == "tenants":
            if len(parts) < 2 or parts[1] != self.tenant:
                raise IsolationError(f"Refusing to call tenant path outside {self.tenant}.")


def _code(body: dict) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or "error")
    return "error"


def _retry_after(headers: dict) -> float:
    try:
        return max(float(headers.get("Retry-After", 1)), 0.0)
    except (TypeError, ValueError):
        return 1.0
