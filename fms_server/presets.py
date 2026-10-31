"""현장별 작업 프리셋. 게시/직접 실행 시 작업에 명령과 좌표의 스냅샷을 복사한다."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from uuid import uuid4

from fms_server.task import JobError, JobNotFound, _now_iso, _parse_command, resolve_steps


class PresetStore:
    def __init__(self, data_root):
        self.root = Path(data_root) / 'sites'
        self._lock = threading.RLock()

    def _path(self, site):
        from fms_server.task import _check_seg
        _check_seg(site, 'site_id')
        return self.root / site / 'presets.json'

    def list(self, site):
        with self._lock:
            path = self._path(site)
            return json.loads(path.read_text()) if path.is_file() else []

    def get(self, site, preset_id):
        for p in self.list(site):
            if p['preset_id'] == preset_id:
                return p
        raise JobNotFound('프리셋을 찾을 수 없습니다')

    def save(self, site, name, commands, registry, preset_id=None):
        if not name.strip() or len(name) > 128:
            raise JobError('프리셋 이름은 1~128자여야 합니다')
        if not commands:
            raise JobError('프리셋에 단계를 하나 이상 추가하세요')
        parsed = [_parse_command(c) for c in commands]
        resolve_steps(parsed, site, registry)
        with self._lock:
            rows = self.list(site)
            previous = self.get(site,preset_id) if preset_id else None
            now = _now_iso()
            result = dict(preset_id=preset_id or uuid4().hex[:16], name=name.strip(),
                          commands=[c.to_dict() for c in parsed],
                          created_at=previous['created_at'] if previous else now, updated_at=now)
            rows = [p for p in rows if p['preset_id'] != result['preset_id']] + [result]
            self._write(site,rows)
            return copy.deepcopy(result)

    def delete(self, site, preset_id):
        with self._lock:
            self.get(site,preset_id)
            self._write(site,[p for p in self.list(site) if p['preset_id'] != preset_id])

    def _write(self, site, rows):
        path = self._path(site)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n')
        temp.replace(path)
