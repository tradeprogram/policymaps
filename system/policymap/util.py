"""policymap.util — HTTP 클라이언트·재시도·레이트리밋·해시·로깅.

설계 원칙(명세[realtime] §4 규율 승계):
  * httpx 있으면 사용, 없으면 urllib.request 폴백 (graceful).
  * 재시도: 일시적 오류(timeout/5xx/네트워크/OVER_REQUEST_LIMIT)만 지수백오프
            800ms*(attempt+1), 최대 N회. NOT_FOUND/빈결과는 "정상적 없음"→재시도 제외.
  * 레이트리밋: 토큰버킷 근사(초당 qps).
  * 해시: 콘텐츠 정규화(compact) 후 sha256 — CDC 2단 본문해시용.

공개 API:
    get_logger(name) -> logging.Logger
    HttpClient(cfg=None)                 # .get_json(url, params) / .get_text(...)
    class NotFound(Exception)            # 재시도 안 함(정상적 없음)
    class TransientError(Exception)      # 재시도 대상
    compact(text) -> str                 # 공백·낫표·ㆍ 제거 정규화(korea100 규율)
    sha256_hex(text|bytes) -> str
    content_hash(text) -> str            # 'sha256:' + sha256_hex(compact(text))
    stable_id(*parts) -> str             # 결정적 짧은 해시 ID(중복방지 delegation_id 등)
    retry(fn, retries, base, logger)     # 데코레이터 겸 함수
    now_kst_iso() -> str                 # ISO8601 Asia/Seoul
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

try:  # 선택적 고속 클라이언트
    import httpx  # type: ignore
    _HAS_HTTPX = True
except Exception:  # pragma: no cover - 폴백 경로
    httpx = None  # type: ignore
    _HAS_HTTPX = False

KST = timezone(timedelta(hours=9))
_UA = "policy-maps/0.1 (+https://github.com/policy-maps)"


# --------------------------------------------------------------------------- #
# 로깅
# --------------------------------------------------------------------------- #
def get_logger(name: str = "policymap", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(h)
        logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        logger.propagate = False
    return logger


# --------------------------------------------------------------------------- #
# 예외
# --------------------------------------------------------------------------- #
class NotFound(Exception):
    """정상적 없음(빈 결과/미매치). 재시도·오류 예산에서 제외."""


class TransientError(Exception):
    """일시적 오류(재시도 대상): timeout/5xx/네트워크/rate limit."""


class PermanentError(Exception):
    """영구 오류(재시도 무의미): 4xx(429 제외)/인증실패/파라미터 오류."""


# --------------------------------------------------------------------------- #
# 텍스트 정규화 / 해시
# --------------------------------------------------------------------------- #
_COMPACT_RE = re.compile(r"[\s　「」『』ㆍ·]+")


def compact(text: Optional[str]) -> str:
    """공백·전각공백·낫표(「」『』)·가운뎃점(ㆍ·) 제거 정규화.

    korea100 sync-verification.mjs compact() 규율. 본문 해시 안정화용.
    """
    if not text:
        return ""
    return _COMPACT_RE.sub("", str(text))


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(text: Optional[str]) -> str:
    """정규화 후 sha256. 'sha256:' 접두. CDC 2단 본문해시."""
    return "sha256:" + sha256_hex(compact(text))


def stable_id(*parts: Any, length: int = 16) -> str:
    """결정적 짧은 해시 ID. delegation_id/rel_id 등 중복방지 자연키 대용."""
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return sha256_hex(joined)[:length]


# --------------------------------------------------------------------------- #
# 시간
# --------------------------------------------------------------------------- #
def now_kst_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 재시도
# --------------------------------------------------------------------------- #
def retry(
    fn: Optional[Callable] = None,
    *,
    retries: int = 3,
    base: float = 0.8,
    logger: Optional[logging.Logger] = None,
):
    """지수백오프 재시도 데코레이터.

    TransientError 만 재시도(base*(attempt+1) 초 대기). NotFound/PermanentError 즉시 전파.
    데코레이터(@retry) 및 인자형(@retry(retries=5)) 둘 다 지원.
    """
    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last: Optional[Exception] = None
            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except (NotFound, PermanentError):
                    raise
                except TransientError as exc:
                    last = exc
                    if attempt >= retries:
                        break
                    delay = base * (attempt + 1)
                    if logger:
                        logger.warning("재시도 %d/%d (%.1fs): %s",
                                       attempt + 1, retries, delay, exc)
                    time.sleep(delay)
            raise last if last else RuntimeError("retry: 알 수 없는 실패")
        return wrapper

    if fn is not None and callable(fn):
        return decorate(fn)
    return decorate


# --------------------------------------------------------------------------- #
# 레이트리밋 (토큰버킷 근사)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, qps: float = 5.0):
        self.min_interval = 1.0 / qps if qps > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


# --------------------------------------------------------------------------- #
# HTTP 클라이언트
# --------------------------------------------------------------------------- #
# 응답 본문에 이 토큰이 있으면 rate limit → TransientError (V-World/국회)
_RATE_TOKENS = ("OVER_REQUEST_LIMIT", "ERROR-336", "LIMITED")


class HttpClient:
    """httpx/urllib 폴백 HTTP 클라이언트. GET 전용(수집 파이프라인용)."""

    def __init__(self, cfg=None, logger: Optional[logging.Logger] = None):
        # cfg는 policymap.config.Config; 순환 import 피하려고 duck typing.
        self.timeout = getattr(cfg, "http_timeout", 40.0) if cfg else 40.0
        self.retries = getattr(cfg, "http_retries", 3) if cfg else 3
        self.base = getattr(cfg, "http_backoff_base", 0.8) if cfg else 0.8
        qps = getattr(cfg, "rate_limit_qps", 5.0) if cfg else 5.0
        self.limiter = RateLimiter(qps)
        self.log = logger or get_logger("policymap.http",
                                        getattr(cfg, "log_level", "INFO") if cfg else "INFO")
        self._client = httpx.Client(timeout=self.timeout, headers={"User-Agent": _UA}) \
            if _HAS_HTTPX else None

    # --- 저수준 1회 요청 ---
    def _raw_get(self, url: str, params: Optional[dict] = None) -> tuple[int, bytes]:
        self.limiter.wait()
        if self._client is not None:
            try:
                r = self._client.get(url, params=params)
                return r.status_code, r.content
            except httpx.TimeoutException as e:  # type: ignore
                raise TransientError(f"timeout: {e}") from e
            except httpx.HTTPError as e:  # type: ignore
                raise TransientError(f"httpx error: {e}") from e
        # urllib 폴백
        full = url
        if params:
            full = url + "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(full, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.getcode(), resp.read()
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if e.code >= 500 or e.code == 429:
                raise TransientError(f"HTTP {e.code}") from e
            # 4xx는 본문 반환(호출부가 에러 신호 해석). PermanentError로 감싸지 않음.
            return e.code, body
        except (urllib.error.URLError, TimeoutError) as e:
            raise TransientError(f"network: {e}") from e

    def _get(self, url: str, params: Optional[dict]) -> bytes:
        """재시도 래핑 GET. 상태·본문 검사 후 bytes 반환."""
        def once() -> bytes:
            status, body = self._raw_get(url, params)
            if status >= 500 or status == 429:
                raise TransientError(f"HTTP {status}")
            text_head = body[:512].decode("utf-8", "ignore")
            if any(tok in text_head for tok in _RATE_TOKENS):
                raise TransientError("rate limit token in body")
            return body
        wrapped = retry(once, retries=self.retries, base=self.base, logger=self.log)
        return wrapped()

    # --- 공개 API ---
    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        body = self._get(url, params)
        text = body.decode("utf-8", "ignore").strip()
        if not text:
            raise NotFound("빈 응답 본문")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # 일부 API는 잘못된 target에서 비-JSON/빈 바디 → 정상적 없음으로 취급
            raise NotFound(f"JSON 파싱 실패: {e} / head={text[:120]!r}") from e

    def get_text(self, url: str, params: Optional[dict] = None) -> str:
        return self._get(url, params).decode("utf-8", "ignore")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def as_list(value: Any) -> list:
    """법령 API가 1건일 때 dict, 다수일 때 list 로 오는 필드 방어 정규화.

    None -> []; dict/scalar -> [value]; list -> 그대로.
    (명세[law_api]: law/조문단위/위임정보 배열/스칼라 혼재 대응)
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


__all__ = [
    "get_logger", "HttpClient", "RateLimiter", "NotFound", "TransientError",
    "PermanentError", "compact", "sha256_hex", "content_hash", "stable_id",
    "retry", "now_kst_iso", "today_kst", "as_list", "KST",
]
