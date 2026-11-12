"""Explicit credential loading and HTTPS transport with credential-free artifacts."""
from copy import deepcopy
import http.client
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit

from .common import digest, encoded, now, write_private


def dotenv(path):
    """Read literal assignments; never evaluate shell commands or expand variables."""
    values = {}
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise ValueError("invalid environment-file assignment")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


class Credentials:
    def __init__(self, path=None, *, environment=None):
        self.values = dict(os.environ if environment is None else environment)
        if path:
            self.values.update(dotenv(path))
        self.used = set()

    def get(self, reference, aliases=()):
        for name in (reference, *aliases):
            value = self.values.get(name)
            if value:
                self.used.add(value)
                return value
        raise ValueError(f"missing credential/config reference {reference}")

    def redact(self, raw):
        for value in sorted(self.used, key=len, reverse=True):
            raw = raw.replace(value.encode(), b"[REDACTED_CREDENTIAL]")
        return raw


def https(request, auth, credentials, folder, *, timeout_seconds):
    """No redirect, implicit retry, source fetch or subprocess credential arguments."""
    if "date_dispatch" in request:
        raise ValueError("resolve and retain the declared date range before transport")
    parsed = urlsplit(request["endpoint"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("transport endpoint must be an explicit credential-free HTTPS URL")
    headers = {"Accept": "application/json", **request.get("headers", {})}
    if auth:
        headers[auth["header"]] = auth.get("prefix", "") + credentials.get(auth["secret_ref"], auth.get("aliases", ()))
    body = deepcopy(request.get("body", {}))
    for field, reference in request.get("body_secret_refs", {}).items():
        body[field] = credentials.get(reference)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    method = request["method"]
    raw_body = None
    if method == "GET":
        path += ("&" if parsed.query else "?") + urlencode(body, doseq=True)
    elif method == "POST":
        raw_body = encoded(body)
        headers["Content-Type"] = "application/json"
    elif method == "DELETE" and not body:
        pass
    else:
        raise ValueError("unsupported HTTP method")
    started = time.monotonic()
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout_seconds)
    raw, status, complete, error_type = b"", None, False, None
    http_started = False
    response_headers = []
    try:
        # Establish DNS/TCP/TLS before any HTTP bytes or credentials are sent.
        # An interrupted request/read after this point remains uncertain.
        connection.connect()
        http_started = True
        connection.request(method, path, body=raw_body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = response.getheaders()
        try:
            raw = response.read()
            complete = True
        except http.client.IncompleteRead as error:
            raw = error.partial
            error_type = type(error).__name__
    except (OSError, http.client.HTTPException) as error:
        error_type = type(error).__name__
    finally:
        connection.close()
    safe = credentials.redact(raw)
    write_private(folder / "response.bin", safe)
    # Authentication/session headers are not permitted retained evidence.
    retained_headers = [(key, value) for key, value in response_headers
                        if key.lower() not in {"set-cookie", "authorization", "proxy-authenticate", "www-authenticate"}]
    metadata = dict(http_status=status, complete=complete, captured_at=now(),
                    dispatch_state="completed" if complete else "uncertain" if http_started else "not_sent",
                    transport_stage="http_request_or_response" if http_started else "pre_http_connect",
                    elapsed_seconds=time.monotonic() - started, raw_sha256=digest(raw),
                    retained_sha256=digest(safe), credential_echo_redacted=safe != raw,
                    response_headers=json.loads(credentials.redact(encoded(retained_headers))),
                    error_type=error_type,
                    transport_success=bool(complete and status is not None and 200 <= status < 300))
    write_private(folder / "transport.json", metadata)
    return dict(metadata, raw_text=safe.decode("utf-8", errors="replace"))
