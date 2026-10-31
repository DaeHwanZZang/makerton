"""루트 `common/` (원본) 과 `viewer/common/` (사본) 이 같은지 검사.

뷰어는 `viewer/` 폴더만 떼어도 돌아가야 해서 사본을 갖는다. 자동 동기화가 없으므로
원본을 고치고 사본 복사를 잊으면 여기서 걸린다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FILES = ["schemas.py", "map_model.py", "map_annotations.py"]


@pytest.mark.parametrize("name", FILES)
def test_viewer_copy_matches(name):
    src = (REPO / "common" / name).read_bytes()
    dst = (REPO / "viewer" / "common" / name).read_bytes()
    assert src == dst, f"viewer/common/{name} 가 common/{name} 와 다르다 — cp common/{name} viewer/common/"
