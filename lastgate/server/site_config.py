"""현장 설정(건물·호실·보관 공간) 검증과 계산.

관리자 초기 설정으로 저장되는 단일 원본이다. 키오스크·세대 웹·관리자 웹은 서버가
발행하는 공개 설정(locker/config/site)의 units·guide 를 그대로 쓰고 직접 계산하지 않는다.

호실 번호는 층×100+호(3~4자리)다. 범위 '101 ~ 614' 는 1~6층의 01~14호를 뜻한다.
없는 호실 '404, 501~514' 는 범위 안의 호실 번호를 숫자 구간으로 뺀다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import config


class SiteError(ValueError):
    pass


_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _unit_ok(unit: int) -> bool:
    return 101 <= unit <= 9999 and unit % 100 >= 1


def parse_unit_list(text: str, label: str) -> list[tuple[int, int]]:
    """'404, 501~514' → [(404, 404), (501, 514)]. 빈 문자열은 빈 목록."""
    ranges = []
    for part in re.split(r"[,\s]+", (text or "").replace("～", "~").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{3,4})(?:~(\d{3,4}))?", part)
        if not m:
            raise SiteError(f"{label}: '{part}' 를 이해할 수 없습니다. 예: 404, 501~514")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            raise SiteError(f"{label}: '{part}' 는 앞 번호가 뒤 번호보다 큽니다.")
        ranges.append((a, b))
    return ranges


@dataclass
class Building:
    code: int                 # 건물 번호. 계정 아이디 앞부분이며 바꿀 수 없다
    name: str                 # 화면 표시 이름 (예: 101동, 공학관)
    unit_from: int            # 호실 범위 시작 (예: 101)
    unit_to: int              # 호실 범위 끝 (예: 614)
    missing: str = ""         # 없는 호실 (예: "404, 501~514")
    extra: str = ""           # 범위 밖 추가 호실 (예: "1501")

    def base_units(self) -> list[int]:
        f1, r1 = divmod(self.unit_from, 100)
        f2, r2 = divmod(self.unit_to, 100)
        return [f * 100 + r for f in range(f1, f2 + 1) for r in range(r1, r2 + 1)]

    def units(self) -> list[int]:
        base = self.base_units()
        gone = parse_unit_list(self.missing, "없는 호실")
        kept = [u for u in base if not any(a <= u <= b for a, b in gone)]
        extra = []
        for a, b in parse_unit_list(self.extra, "추가 호실"):
            extra.extend(u for u in range(a, b + 1) if _unit_ok(u))
        return sorted(set(kept) | set(extra))

    def guide(self, unit_term: str) -> str:
        """입력 전 안내 문구. 예: '101~614호 · 없는 호실: 404호, 5층 전체'"""
        base = self.base_units()
        units = set(self.units())
        text = f"{self.unit_from}~{self.unit_to}{unit_term}"
        gone = [u for u in base if u not in units]
        if gone:
            floors: dict[int, list[int]] = {}
            for u in base:
                floors.setdefault(u // 100, []).append(u)
            whole = [f for f, us in floors.items() if all(u not in units for u in us)]
            parts = [f"{f}층 전체" for f in whole]
            singles = [u for u in gone if u // 100 not in whole]
            parts = [*_compress(singles, unit_term), *parts]
            text += f" · 없는 호실: {', '.join(parts)}"
        added = sorted(u for u in units if u not in set(base))
        if added:
            text += f" · 추가 호실: {', '.join(_compress(added, unit_term))}"
        return text


def _compress(units: list[int], term: str) -> list[str]:
    """연속된 호실은 '501~503호' 로 묶는다 (같은 층 안에서만)."""
    out, run = [], []
    for u in sorted(units):
        if run and u == run[-1] + 1 and u // 100 == run[0] // 100:
            run.append(u)
            continue
        if run:
            out.append(_run_text(run, term))
        run = [u]
    if run:
        out.append(_run_text(run, term))
    return out


def _run_text(run: list[int], term: str) -> str:
    return f"{run[0]}{term}" if len(run) == 1 else f"{run[0]}~{run[-1]}{term}"


@dataclass
class Site:
    name: str
    building_term: str
    unit_term: str
    buildings: list[Building]
    grid_bins: int = config.SITE_DEFAULTS["grid_bins"]
    oversize_doors: int = config.SITE_DEFAULTS["oversize_doors"]
    robots: int = config.SITE_DEFAULTS["robots"]
    service_start: str = config.SITE_DEFAULTS["service_start"]
    service_end: str = config.SITE_DEFAULTS["service_end"]
    second_seat_same_building: bool = config.SITE_DEFAULTS["second_seat_same_building"]
    version: int = 0
    updated_at: str | None = None
    _units: dict[int, list[int]] = field(default_factory=dict, repr=False, compare=False)

    # --- 생성·검증 -------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "Site":
        """관리자 입력을 검증해 Site 를 만든다. 문제가 있으면 SiteError(사용자용 문구)."""
        if not isinstance(data, dict):
            raise SiteError("설정 형식이 올바르지 않습니다.")
        name = _text(data.get("name"), "사이트 이름", 40)
        building_term = _text(data.get("building_term"), "건물 용어", 6)
        unit_term = _text(data.get("unit_term"), "호실 용어", 6)
        raw = data.get("buildings")
        if not isinstance(raw, list) or not raw:
            raise SiteError("건물을 하나 이상 추가하세요.")
        buildings, codes, names = [], set(), set()
        for i, b in enumerate(raw, 1):
            if not isinstance(b, dict):
                raise SiteError(f"{i}번째 건물 형식이 올바르지 않습니다.")
            label = f"{i}번째 건물"
            bname = _text(b.get("name"), f"{label} 이름", 20)
            code = _int(b.get("code"), f"{label}({bname}) 번호")
            if not 1 <= code <= 999:
                raise SiteError(f"{bname}: 건물 번호는 1~999 사이 숫자입니다.")
            if code in codes:
                raise SiteError(f"건물 번호 {code} 가 중복되었습니다.")
            if bname in names:
                raise SiteError(f"건물 이름 '{bname}' 이 중복되었습니다.")
            codes.add(code)
            names.add(bname)
            unit_from = _int(b.get("unit_from"), f"{bname} 호실 범위 시작")
            unit_to = _int(b.get("unit_to"), f"{bname} 호실 범위 끝")
            for u in (unit_from, unit_to):
                if not _unit_ok(u):
                    raise SiteError(f"{bname}: 호실 번호 {u} 는 층×100+호 형식(101~9999, 끝 두 자리 01 이상)이어야 합니다.")
            if unit_from // 100 > unit_to // 100 or unit_from % 100 > unit_to % 100:
                raise SiteError(f"{bname}: 호실 범위는 낮은 층·앞 호부터 적습니다. 예: 101 ~ 614")
            building = Building(code=code, name=bname, unit_from=unit_from, unit_to=unit_to,
                                missing=str(b.get("missing") or "").strip(),
                                extra=str(b.get("extra") or "").strip())
            try:
                units = building.units()
            except SiteError as e:
                raise SiteError(f"{bname} {e}") from None
            for a, bb in parse_unit_list(building.extra, "추가 호실"):
                for u in range(a, bb + 1):
                    if not _unit_ok(u):
                        raise SiteError(f"{bname}: 추가 호실 {u} 는 올바른 호실 번호가 아닙니다.")
            if not units:
                raise SiteError(f"{bname}: 남는 호실이 없습니다.")
            buildings.append(building)
        site = cls(
            name=name, building_term=building_term, unit_term=unit_term, buildings=buildings,
            grid_bins=_bounded(data, "grid_bins", "그리드 보관함 수", 1, 5000),
            oversize_doors=_bounded(data, "oversize_doors", "배송 불가 보관함 수", 0, 500),
            robots=_bounded(data, "robots", "로봇 대수", 1, 20),
            service_start=_time(data, "service_start", "배송 시작 시각"),
            service_end=_time(data, "service_end", "배송 종료 시각"),
            second_seat_same_building=bool(data.get("second_seat_same_building",
                                                    config.SITE_DEFAULTS["second_seat_same_building"])),
        )
        if site.service_start >= site.service_end:
            raise SiteError("배송 종료 시각은 시작 시각보다 늦어야 합니다.")
        if site.account_count() > config.MAX_UNITS:
            raise SiteError(f"호실이 너무 많습니다 ({site.account_count()}개, 최대 {config.MAX_UNITS}개).")
        return site

    @classmethod
    def load(cls, data: dict) -> "Site":
        """DB 에 저장된 설정을 그대로 읽는다 (이미 검증된 값)."""
        buildings = [Building(**b) for b in data["buildings"]]
        rest = {k: v for k, v in data.items() if k != "buildings" and not k.startswith("_")}
        return cls(buildings=buildings, **rest)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_units", None)
        return data

    # --- 조회 ------------------------------------------------------------
    def building(self, code: int | None) -> Building | None:
        return next((b for b in self.buildings if b.code == code), None)

    def units_of(self, code: int) -> list[int]:
        if code not in self._units:
            b = self.building(code)
            self._units[code] = b.units() if b else []
        return self._units[code]

    def has_unit(self, code: int | None, unit: int | None) -> bool:
        return code is not None and unit is not None and unit in set(self.units_of(code))

    def households(self) -> list[tuple[int, int]]:
        return [(b.code, u) for b in self.buildings for u in self.units_of(b.code)]

    def account_count(self) -> int:
        return sum(len(self.units_of(b.code)) for b in self.buildings)

    def order(self) -> dict[int, int]:
        """건물 번호 → 표시 순서. 가까운 건물 판단에 쓴다."""
        return {b.code: i for i, b in enumerate(self.buildings)}

    def label(self, code: int, unit: int) -> str:
        b = self.building(code)
        return f"{b.name if b else code} {unit}{self.unit_term}"

    def building_name(self, code: int) -> str:
        b = self.building(code)
        return b.name if b else str(code)

    def unit_error(self, code: int | None, unit: int | None) -> str | None:
        """없는 건물·호실이면 화면에 보여줄 문구, 정상이면 None."""
        b = self.building(code)
        if not b:
            return f"{self.building_term}을(를) 선택하세요."
        if unit is None:
            return f"{self.unit_term} 번호를 입력하세요."
        if unit not in set(self.units_of(code)):
            return f"{b.name}에 {unit}{self.unit_term}는 없습니다. ({b.guide(self.unit_term)})"
        return None

    def public(self) -> dict:
        """키오스크·세대 웹에 공개하는 설정. 비밀 정보는 없다."""
        return {
            "name": self.name, "building_term": self.building_term, "unit_term": self.unit_term,
            "buildings": [{"code": b.code, "name": b.name, "unit_from": b.unit_from,
                           "unit_to": b.unit_to, "missing": b.missing, "extra": b.extra,
                           "guide": b.guide(self.unit_term), "units": self.units_of(b.code)}
                          for b in self.buildings],
            "grid_bins": self.grid_bins, "oversize_doors": self.oversize_doors,
            "robots": self.robots, "service_start": self.service_start,
            "service_end": self.service_end,
            "second_seat_same_building": self.second_seat_same_building,
            "grid_limit_mm": list(config.GRID_LIMIT_MM),
            "version": self.version, "updated_at": self.updated_at,
        }


def _text(value, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise SiteError(f"{label}을(를) 입력하세요.")
    if len(text) > limit:
        raise SiteError(f"{label}은(는) {limit}자 이하로 입력하세요.")
    return text


def _int(value, label: str) -> int:
    text = str(value if value is not None else "").strip()
    if not re.fullmatch(r"\d{1,5}", text):
        raise SiteError(f"{label}은(는) 숫자로 입력하세요.")
    return int(text)


def _bounded(data: dict, key: str, label: str, lo: int, hi: int) -> int:
    value = _int(data.get(key, config.SITE_DEFAULTS[key]), label)
    if not lo <= value <= hi:
        raise SiteError(f"{label}은(는) {lo}~{hi} 사이로 입력하세요.")
    return value


def _time(data: dict, key: str, label: str) -> str:
    value = str(data.get(key) or config.SITE_DEFAULTS[key]).strip()
    if not _TIME_RE.fullmatch(value):
        raise SiteError(f"{label}은(는) HH:MM 형식입니다.")
    return value
