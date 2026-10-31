"""테스트 격리. `fms_server.config` 가 import 될 때 환경변수를 읽으므로 여기서 먼저 정한다.

- MQTT: 개발 중 로컬 브로커(1883)와 로봇이 떠 있어도 테스트가 그 state 를 받지 않게
  아무도 안 듣는 포트로 돌린다 (connect_async 라 연결 실패해도 서버는 뜬다).
- 데이터 루트: lifespan 이 만드는 저장소가 리포의 fms_server/maps 에 쌓이지 않게 임시 폴더로.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("FMS_MQTT_PORT", "18859")
os.environ.setdefault("FMS_DATA_ROOT", tempfile.mkdtemp(prefix="fms_test_"))
