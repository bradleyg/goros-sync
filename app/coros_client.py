"""Minimal, unofficial COROS Training Hub client.

COROS has no public upload API. This mirrors what the Training Hub web app does
when you import a FIT file:

1. ``POST {teamapi}/account/login`` with an MD5-hashed password.
2. ``GET faq.coros.com/openapi/oss/sts`` to obtain temporary S3 credentials for
   the account region's bucket.
3. ``PUT`` a zip (``{md5}/{name}.fit``, stored uncompressed) to
   ``fit_zip/{userId}/{md5}.zip`` in that bucket.
4. ``POST {teamapi}/activity/fit/import`` to register the object for import.

Reverse-engineered constants are based on the open-source projects
``XiaoSiHwang/garmin-sync-coros`` and ``Likenttt/coros-additional-mcp``.
If COROS rotates them, uploads will fail until they are updated here.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import time
import zipfile
from dataclasses import dataclass
from urllib.parse import quote

import httpx

SUCCESS = "0000"

BASE_URL_BY_REGION = {
    "en": "https://teamapi.coros.com",
    "eu": "https://teameuapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
    "sg": "https://teamsgapi.coros.com",
}
TRAINING_HUB_BY_REGION = {
    "en": "https://training.coros.com",
    "eu": "https://trainingeu.coros.com",
    "cn": "https://trainingcn.coros.com",
    "sg": "https://trainingsg.coros.com",
}
REGION_BY_ID = {1: "en", 2: "cn", 3: "eu", 4: "sg"}
REGION_ID = {v: k for k, v in REGION_BY_ID.items()}

# The CN host acts as a discovery endpoint: it accepts logins for every region
# and the response's regionId tells us where the account actually lives.
DISCOVERY_LOGIN_URL = BASE_URL_BY_REGION["cn"]

STS_SALT = "9y78gpoERW4lBNYL"
# Legacy, unauthenticated STS endpoint (retired by COROS in 2026; kept as a fallback).
FAQ_API_URL = "https://faq.coros.com"
STS_APP_ID = "1660188068672619112"
STS_CONFIG = {
    "en": {"bucket": "coros-s3", "service": "aws", "sign": "E34EF0E34A498A54A9C3EAEFC12B7CAF", "region": "us-west-1"},
    "eu": {"bucket": "eu-coros", "service": "aws", "sign": "877571111A1EE5316E4B590103D4B5B3", "region": "eu-central-1"},
    "cn": {"bucket": "coros-oss", "service": "aliyun", "sign": "9AD4AA35AAFEE6BB1E847A76848D58DF", "region": None},
    "sg": {"bucket": "coros-sg-prod", "service": "aliyun", "sign": None, "region": None},
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class CorosError(Exception):
    """Raised for any COROS API failure."""


class CorosAuthError(CorosError):
    """Raised when the COROS login is rejected or the token has expired."""


@dataclass
class CorosSession:
    access_token: str
    user_id: str
    region: str
    nickname: str | None = None

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "user_id": self.user_id,
            "region": self.region,
            "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CorosSession":
        return cls(
            access_token=data["access_token"],
            user_id=str(data["user_id"]),
            region=data["region"],
            nickname=data.get("nickname"),
        )


@dataclass
class ImportResult:
    import_id: str | None
    status: int | None
    success: bool
    pending: bool
    message: str
    raw: dict


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _local_timezone_quarter_hours() -> int:
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
    return int(offset.total_seconds() // 900)


def build_coros_zip(fit_bytes: bytes, original_filename: str) -> tuple[bytes, str]:
    """Wrap a FIT file the way Training Hub does: ``{md5}/{name}``, stored."""
    md5 = _md5_hex(fit_bytes)
    buf = io.BytesIO()
    now = time.gmtime()[:6]
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        folder = zipfile.ZipInfo(f"{md5}/", date_time=now)
        folder.external_attr = 0x10  # MS-DOS directory flag
        zf.writestr(folder, b"")
        entry = zipfile.ZipInfo(f"{md5}/{original_filename}", date_time=now)
        entry.compress_type = zipfile.ZIP_STORED
        zf.writestr(entry, fit_bytes)
    return buf.getvalue(), md5


# --------------------------------------------------------------------------- #
# AWS SigV4 (single PUT). Small enough that pulling in boto3 isn't worth it.
# --------------------------------------------------------------------------- #

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _s3_put(creds: dict, region: str, bucket: str, key: str, body: bytes, timeout: float) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    amz_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    content_type = "application/zip"

    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{creds['token']}\n"
    )
    canonical_request = "\n".join(
        ["PUT", f"/{encoded_key}", "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    k_date = _hmac(f"AWS4{creds['secret']}".encode(), date_stamp)
    k_signing = _hmac(_hmac(_hmac(k_date, region), "s3"), "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers = {
        "Content-Type": content_type,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "x-amz-security-token": creds["token"],
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={creds['key_id']}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    res = httpx.put(f"https://{host}/{encoded_key}", content=body, headers=headers, timeout=timeout)
    if res.status_code >= 300:
        raise CorosError(f"S3 upload failed (HTTP {res.status_code}): {res.text[:300]}")


class CorosClient:
    def __init__(self, email: str, password: str, session: CorosSession | None = None, timeout: float = 30.0):
        self.email = email
        self.password = password
        self.session = session
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})

    # ------------------------------------------------------------------ auth
    def login(self) -> CorosSession:
        if not self.email or not self.password:
            raise CorosAuthError("COROS email and password are required.")
        body = {
            "account": self.email,
            "accountType": 2,
            "pwd": _md5_hex(self.password.encode()),
        }
        data = self._request("POST", f"{DISCOVERY_LOGIN_URL}/account/login", json_body=body, auth=False)
        region = REGION_BY_ID.get(int(data.get("regionId", 1)), "en")
        self.session = CorosSession(
            access_token=data["accessToken"],
            user_id=str(data["userId"]),
            region=region,
            nickname=data.get("nickname"),
        )
        return self.session

    def ensure_session(self) -> CorosSession:
        if self.session is None:
            return self.login()
        try:
            self._request("GET", f"{self.base_url}/account/query")
        except CorosAuthError:
            return self.login()
        return self.session

    @property
    def base_url(self) -> str:
        region = self.session.region if self.session else "en"
        return BASE_URL_BY_REGION[region]

    # ---------------------------------------------------------------- upload
    def upload_fit(self, fit_bytes: bytes, original_filename: str, wait_seconds: float = 45) -> ImportResult:
        session = self.ensure_session()
        storage = STS_CONFIG[session.region]
        if storage["service"] != "aws":
            raise CorosError(
                f"COROS {session.region.upper()}-region accounts upload via Aliyun OSS, which this app doesn't support yet."
            )

        zip_bytes, md5 = build_coros_zip(fit_bytes, original_filename)
        object_key = f"fit_zip/{session.user_id}/{md5}.zip"
        creds = self._sts_credentials(session.region)
        _s3_put(creds, creds["region"], creds["bucket"], object_key, zip_bytes, self.timeout)

        payload = {
            "source": 1,
            "timezone": _local_timezone_quarter_hours(),
            "bucket": creds["bucket"],
            "md5": md5,
            "size": len(fit_bytes),  # the web app reports the original file's size
            "object": object_key,
            "serviceName": storage["service"],
            "oriFileName": original_filename,
        }
        data = self._request(
            "POST",
            f"{self.base_url}/activity/fit/import",
            form={"jsonParameter": json.dumps(payload)},
            token_header="AccessToken",
            timeout=60,
        ) or {}
        result = self._interpret_import(data)
        if result.pending and result.import_id and wait_seconds > 0:
            result = self._wait_for_import(result, wait_seconds)
        return result

    def _interpret_import(self, data: dict) -> ImportResult:
        status = data.get("status")
        import_id = data.get("idString") or data.get("id")
        error_size = data.get("errorSize") or 0
        finish_size = data.get("finishSize") or 0
        if status == 2 and not error_size:
            return ImportResult(import_id, status, True, False, "Imported", data)
        if error_size and not finish_size:
            return ImportResult(import_id, status, False, False, "COROS rejected the file", data)
        if status == 2:
            return ImportResult(import_id, status, True, False, "Imported (with warnings)", data)
        return ImportResult(import_id, status, False, True, "Processing on COROS", data)

    def _wait_for_import(self, result: ImportResult, wait_seconds: float) -> ImportResult:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                items = self._request(
                    "POST", f"{self.base_url}/activity/fit/getImportSportList", json_body={"size": 20}
                ) or []
            except CorosError:
                break
            match = next(
                (i for i in items if str(i.get("idString") or i.get("id")) == str(result.import_id)),
                None,
            )
            if match is None:
                continue
            latest = self._interpret_import(match)
            if not latest.pending:
                return latest
        return result

    def _sts_credentials(self, region: str) -> dict:
        """Temporary object-storage credentials for the account's upload bucket.

        The Training Hub web app fetches these from its own authenticated
        ``/api/proxy/oss/sts`` endpoint (session cookie = COROS access token).
        The older unauthenticated ``faq.coros.com`` endpoint is tried as a fallback.
        """
        cfg = STS_CONFIG[region]
        errors: list[str] = []

        attempts = []
        if self.session:
            hub = TRAINING_HUB_BY_REGION[region]
            token = self.session.access_token
            attempts.append((
                f"{hub}/api/proxy/oss/sts",
                {"bucket": cfg["bucket"], "service": cfg["service"], "v": "2"},
                {
                    "Cookie": f"CPL-coros-token={token}; CPL-coros-region={REGION_ID[region]}",
                    "accesstoken": token,
                    "Referer": f"{hub}/",
                    "Accept": "application/json, text/plain, */*",
                },
            ))
        if cfg.get("sign"):
            attempts.append((
                f"{FAQ_API_URL}/openapi/oss/sts",
                {"bucket": cfg["bucket"], "service": cfg["service"], "v": "2", "app_id": STS_APP_ID, "sign": cfg["sign"]},
                {},
            ))

        for url, params, headers in attempts:
            try:
                res = self._http.get(url, params=params, headers=headers)
                body = res.json()
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{url.split('/')[2]}: {exc.__class__.__name__}")
                continue
            if body.get("code") == 200 and (body.get("data") or {}).get("credentials"):
                raw = json.loads(base64.b64decode(body["data"]["credentials"].replace(STS_SALT, "")))
                key_id = raw.get("AccessKeyId")
                secret = raw.get("SecretAccessKey") or raw.get("AccessKeySecret")
                token = raw.get("SessionToken") or raw.get("SecurityToken")
                if key_id and secret and token:
                    return {
                        "key_id": key_id,
                        "secret": secret,
                        "token": token,
                        "bucket": raw.get("Bucket") or cfg["bucket"],
                        "region": raw.get("Region") or cfg["region"],
                    }
            errors.append(f"{url.split('/')[2]}: {body.get('msg') or body.get('message') or res.status_code}")

        raise CorosError("Could not get COROS upload credentials (" + "; ".join(errors) + ")")

    # ------------------------------------------------------------------ http
    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        form: dict | None = None,
        auth: bool = True,
        token_header: str = "accessToken",
        timeout: float | None = None,
    ):
        headers = {"Accept": "application/json, text/plain, */*"}
        if auth:
            if not self.session:
                raise CorosAuthError("Not logged in to COROS.")
            headers[token_header] = self.session.access_token
        try:
            res = self._http.request(
                method,
                url,
                json=json_body,
                # (None, value) tuples make httpx send multipart/form-data fields,
                # which is what the Training Hub web app uses for activity/fit/import.
                files={k: (None, v) for k, v in form.items()} if form is not None else None,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise CorosError(f"Network error talking to COROS: {exc}") from exc
        try:
            body = res.json()
        except ValueError as exc:
            raise CorosError(f"COROS returned a non-JSON response (HTTP {res.status_code})") from exc

        result = body.get("result")
        if result != SUCCESS:
            message = body.get("message") or "Request failed"
            # 1019 = token invalid / expired; 1030 = login elsewhere
            if result in {"1019", "1030"} or "token" in message.lower() or "login" in message.lower():
                raise CorosAuthError(f"COROS: {message}")
            raise CorosError(f"COROS: {message} (code {result})")
        return body.get("data")

    def close(self) -> None:
        self._http.close()
