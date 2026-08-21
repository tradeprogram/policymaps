"""policymap.config — 환경설정 로드.

우선순위: os.environ > .env 파일 > 기본값.
.env 파서는 순수 표준라이브러리(외부 python-dotenv 불필요).

공개 API:
    load_config(env_path=None) -> Config
    get_config() -> Config          # 프로세스 캐시(싱글턴)
    Config (dataclass)              # 모든 키 속성 접근

모든 API 키는 .env.example 에 정리. 키 부재 시 즉시 예외를 던지지 않고
Config에 None으로 담기며, 실제 사용 시점(collectors)에서 require()로 검증한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

# 프로젝트 루트 = 이 파일 기준 상위 2단계 (F:/policy_maps/system)
SYSTEM_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = SYSTEM_ROOT / ".env"
DEFAULT_DB_PATH = SYSTEM_ROOT / "data" / "policymap.db"
DEFAULT_OUT_DIR = SYSTEM_ROOT / "data"


def _parse_env_file(path: Path) -> dict[str, str]:
    """단순 .env 파서: KEY=VALUE, # 주석, 따옴표 제거. 실패해도 빈 dict."""
    out: dict[str, str] = {}
    if not path or not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    except OSError:
        pass
    return out


@dataclass
class Config:
    # --- 국가법령정보 Open API ---
    law_oc: Optional[str] = None            # LAW_OC: 신청 이메일 ID부분(예 g4c)
    law_base: str = "https://www.law.go.kr/DRF"
    # --- 열린국회정보 ---
    assembly_key: Optional[str] = None      # ASSEMBLY_KEY
    assembly_base: str = "https://open.assembly.go.kr/portal/openapi"
    # --- V-World ---
    vworld_key: Optional[str] = None        # VWORLD_KEY (도메인 종속)
    vworld_domain: Optional[str] = None     # VWORLD_DOMAIN (서버호출 필수)
    vworld_base: str = "https://api.vworld.kr/req/data"
    # --- 지방재정365 ---
    lofin_key: Optional[str] = None         # LOFIN_KEY
    lofin_base: str = "https://www.lofin365.go.kr/lf/hub"
    # --- 행정표준코드 (StanReginCd) ---
    stanregin_key: Optional[str] = None     # STANREGIN_KEY (data.go.kr)
    stanregin_base: str = "https://apis.data.go.kr/1741000/StanReginCd"
    # --- 저장/실행 ---
    db_path: str = str(DEFAULT_DB_PATH)
    out_dir: str = str(DEFAULT_OUT_DIR)
    schema_path: str = str(SYSTEM_ROOT / "db" / "schema.sql")
    # --- 네트워크 튜닝 ---
    http_timeout: float = 40.0
    http_retries: int = 3
    http_backoff_base: float = 0.8          # korea100 규율: 800ms*(attempt+1)
    concurrency: int = 6                    # korea100 CONCURRENCY
    error_budget: float = 0.05              # max(10, ceil(N*0.05))
    rate_limit_qps: float = 5.0             # 초당 요청 상한(예의상 스로틀)
    # --- 로깅 ---
    log_level: str = "INFO"

    def require(self, *keys: str) -> None:
        """지정 키가 채워졌는지 검증. 비면 RuntimeError. collectors 진입부에서 호출."""
        missing = [k for k in keys if not getattr(self, k, None)]
        if missing:
            raise RuntimeError(
                f"필수 설정 누락: {', '.join(missing)} — .env 또는 환경변수를 확인하라 "
                f"(.env.example 참조)."
            )


# 키 이름 매핑: 환경변수명 -> Config 속성명
_ENV_MAP = {
    "LAW_OC": "law_oc",
    "LAW_BASE": "law_base",
    "ASSEMBLY_KEY": "assembly_key",
    "ASSEMBLY_BASE": "assembly_base",
    "VWORLD_KEY": "vworld_key",
    "VWORLD_DOMAIN": "vworld_domain",
    "VWORLD_BASE": "vworld_base",
    "LOFIN_KEY": "lofin_key",
    "LOFIN_BASE": "lofin_base",
    "STANREGIN_KEY": "stanregin_key",
    "STANREGIN_BASE": "stanregin_base",
    "POLICYMAP_DB": "db_path",
    "POLICYMAP_OUT": "out_dir",
    "POLICYMAP_SCHEMA": "schema_path",
    "HTTP_TIMEOUT": "http_timeout",
    "HTTP_RETRIES": "http_retries",
    "HTTP_BACKOFF_BASE": "http_backoff_base",
    "POLICYMAP_CONCURRENCY": "concurrency",
    "POLICYMAP_ERROR_BUDGET": "error_budget",
    "POLICYMAP_RATE_QPS": "rate_limit_qps",
    "POLICYMAP_LOG_LEVEL": "log_level",
}
_FLOAT_ATTRS = {"http_timeout", "http_backoff_base", "error_budget", "rate_limit_qps"}
_INT_ATTRS = {"http_retries", "concurrency"}


def load_config(env_path: Optional[str | Path] = None) -> Config:
    """.env(있으면) + os.environ 병합해 Config 생성. os.environ 우선."""
    path = Path(env_path) if env_path else DEFAULT_ENV_PATH
    merged: dict[str, str] = {}
    merged.update(_parse_env_file(path))
    merged.update({k: v for k, v in os.environ.items() if k in _ENV_MAP})

    cfg = Config()
    for env_name, attr in _ENV_MAP.items():
        if env_name in merged:
            raw = merged[env_name]
            if attr in _FLOAT_ATTRS:
                try:
                    setattr(cfg, attr, float(raw))
                except ValueError:
                    pass
            elif attr in _INT_ATTRS:
                try:
                    setattr(cfg, attr, int(raw))
                except ValueError:
                    pass
            else:
                setattr(cfg, attr, raw)
    return cfg


_CACHED: Optional[Config] = None


def get_config() -> Config:
    """프로세스 단위 캐시된 Config. 최초 호출 시 load_config()."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load_config()
    return _CACHED


def reset_config() -> None:
    """테스트용 캐시 초기화."""
    global _CACHED
    _CACHED = None


__all__ = ["Config", "load_config", "get_config", "reset_config",
           "SYSTEM_ROOT", "DEFAULT_DB_PATH", "DEFAULT_ENV_PATH"]
