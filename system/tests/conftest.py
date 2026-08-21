"""pytest conftest — 경로 부트스트랩 + 편의 픽스처.

핵심 규율:
  * 테스트 함수는 pytest 픽스처 인자에 의존하지 않고 _support 헬퍼(fresh_db 등)를
    직접 호출한다 → pytest 없이도(run_dict) 동일 코드가 동작한다.
  * 아래 픽스처는 pytest 사용자를 위한 편의 제공(선택). 미사용이어도 무해.
"""
import sys
from pathlib import Path

# _support 가 경로 부트스트랩을 수행하지만, conftest 가 먼저 로드될 수 있으므로 중복 보장.
_TESTS_DIR = Path(__file__).resolve().parent
_SYSTEM_ROOT = _TESTS_DIR.parent
for _p in (str(_SYSTEM_ROOT), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _support  # noqa: E402

try:
    import pytest

    @pytest.fixture()
    def db():
        """샘플 시드된 인메모리 연결."""
        conn = _support.fresh_db(seed=True)
        yield conn
        conn.close()

    @pytest.fixture()
    def raw_db():
        """시드 없는 초기화 연결."""
        conn = _support.fresh_db(seed=False)
        yield conn
        conn.close()

    @pytest.fixture()
    def fixtures_dir():
        return _support.FIX_DIR

    def pytest_report_header(config):
        return "policy_maps 테스트: 코어=stdlib, 병렬 미구현 모듈은 자동 skip"

except ImportError:  # pragma: no cover - pytest 미설치 환경
    pass
