"""로봇이 읽는 chargers.json 검증과 도킹 목적지 검사."""
import math

from pydantic import BaseModel, ConfigDict, Field


class Charger(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(min_length=1)
    x: float
    y: float
    theta: float = Field(ge=-math.pi, le=math.pi)
    entry: str = ''
    tol_xy: float = Field(default=.06, gt=0)
    tol_theta: float = Field(default=.1, gt=0)


def read_chargers(data):
    if not isinstance(data, dict) or not isinstance(data.get('chargers'), list):
        raise ValueError('chargers.json에는 chargers 배열이 필요하다')
    chargers = [Charger.model_validate(c) for c in data['chargers']]
    if len({c.id for c in chargers}) != len(chargers):
        raise ValueError('충전기 ID가 중복되었다')
    if any(c.entry not in ('', 'FORWARD', 'BACKWARD') for c in chargers):
        raise ValueError('충전기 entry는 FORWARD 또는 BACKWARD여야 한다')
    return chargers


def require_charger(store, map_id, pose, entry):
    import json
    from fms_server.task import JobError
    from fms_server.mapkit.store import MapStoreError
    try:
        chargers = read_chargers(json.loads(store.file_path(map_id, 'chargers.json').read_text()))
    except (MapStoreError, ValueError, OSError) as exc:
        raise JobError('맵에 충전 도킹 스테이션을 먼저 등록하세요') from exc
    for c in chargers:
        delta = math.atan2(math.sin(c.theta-pose.theta), math.cos(c.theta-pose.theta))
        if math.hypot(c.x-pose.x,c.y-pose.y) <= .3 and abs(delta) <= .35 and c.entry in ('',entry):
            return
    raise JobError('도킹 목적지의 위치·방향·진입 방향과 일치하는 충전기가 없습니다')
