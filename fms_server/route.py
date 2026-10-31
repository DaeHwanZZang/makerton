"""
다층 경로 계획 — job 의 목적지들이 여러 층(맵)에 걸치면 승강기 이동 단계를 끼워 넣는다
=====================================================================================

`build_plan` 은 순수 함수에 가깝다 (MQTT/로봇을 모른다). 결과는 JSON 직렬화 가능한 stage 목록이고
`task.JobCoordinator` 가 하나씩 실행한다.

stage 종류 (`t`):
    order     한 맵 안의 노드 묶음을 로봇에 Order 로 보낸다. nodes[i] = {x,y,theta,cmd,action}
              cmd = 이 노드가 완료시키는 job 명령 인덱스 (진행률용, 승강기 이동용 노드는 None)
    call      (로봇이 승강장 앞에 선 뒤) 승강기를 이 층으로 불러 문을 연 채 붙잡는다 (hold).
              승강기 임대(lease)를 잡는다. 다른 job 이 쓰는 중이면 승강장 앞에서 기다린다
    ride      (로봇이 칸 안) 목표 층으로 GO + 출발 층 hold 풀기, 도착해 문이 열릴 때까지 대기
    switch    로봇 배정 맵을 목표 층으로 바꿔 SET_MAP, 로봇이 새 맵을 보고할 때까지 대기
    localize  (시뮬) 로봇을 새 층 승강장 앞으로 옮기고(teleport) 위치 힌트를 줘서 LOCALIZED 가 될 때까지 대기
    release   승강기 놓아주기 (임대 반납)

한 번 갈아타기:
    order(승강장 앞) call order(전진 탑승) ride order(후진 하차, 아직 출발 층 맵) release switch localize ...
하차는 맵을 바꾸기 전에 한다 — 칸 안에서는 새 층 맵으로 위치를 잡을 수 없으므로 직선 후진(경로계획 없음)으로
나온 뒤 맵을 바꾸고 로컬라이징한다. 그래서 승강기 문은 층마다 같은 쪽이어야 한다 (전진으로 타서 후진으로 나온다).
여러 승강기가 같은 층 쌍을 이으면 로봇 위치에서 승강장까지 **주행 거리**(맵 격자 최단거리)가 가장 짧은 것을 탄다.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

from fms_server.elevators import ElevatorError, ElevatorStore
from fms_server.locations import LocationError

# 승강기 탑승(전진)·하차(후진) 직선 이동 속도 (m/s). 지정하지 않으면 로봇 기본값(0.2 m/s, 도킹·주차와 같음)을 쓴다.
ELEVATOR_STRAIGHT_SPEED = 0.6


class RouteError(Exception):
    pass


def _find_hops(elevators, src: str, dst: str) -> list[tuple[str, str]]:
    """src 맵 -> dst 맵 최소 환승 경로 [(from_map, to_map), ...] (어느 승강기를 탈지는 build_plan 이 정한다)."""
    prev: dict[str, Optional[str]] = {src: None}
    queue = deque([src])
    while queue:
        cur = queue.popleft()
        if cur == dst:
            break
        for e in elevators:
            if any(s.map_id == cur for s in e.stops):
                for s in e.stops:
                    if s.map_id not in prev:
                        prev[s.map_id] = cur
                        queue.append(s.map_id)
    if dst not in prev:
        raise RouteError(f"맵 {src!r} 에서 {dst!r} 로 가는 승강기 연결이 없다")
    hops = []
    cur = dst
    while prev[cur] is not None:
        hops.append((prev[cur], cur))
        cur = prev[cur]
    return hops[::-1]


def _node(pose, cmd=None, action=None) -> dict:
    x, y, theta = pose
    return {"x": x, "y": y, "theta": theta, "cmd": cmd, "action": action}


_COARSE = 0.25  # 주행 거리 계산 격자 (m)


def path_distances(model, start_xy, goals) -> list[float]:
    """start_xy 에서 goals[i] 까지 맵 FREE 칸 위 최단 주행 거리 (8방향, 0.25 m 격자). 못 가면 inf.

    정확한 planner 거리는 아니다 (로봇 폭 팽창 없음) — 승강기 고르기용 비교값.
    """
    import heapq

    import numpy as np

    m = model.metadata
    k = max(1, round(_COARSE / m.resolution))
    free = model.classify_grid() == 2
    h, w = (free.shape[0] // k) * k, (free.shape[1] // k) * k
    # 1 px 벽선도 막아야 하므로 "대부분 FREE" 가 아니라 막힌 픽셀 수로 본다 (고립 노이즈 1~2 px 는 봐준다)
    blocked_px = (~free[:h, :w]).reshape(h // k, k, w // k, k).sum(axis=(1, 3))
    blocks = blocked_px < max(2, k // 2)
    rows, cols = blocks.shape
    cell = k * m.resolution

    def to_cell(xy):
        c = int((xy[0] - m.origin_x) / cell)
        r = int((xy[1] - m.origin_y) / cell)
        best = None
        for dr in range(-3, 4):  # 막힌 칸이면 가까운 열린 칸으로
            for dc in range(-3, 4):
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols and blocks[rr, cc]:
                    d = dr * dr + dc * dc
                    if best is None or d < best[0]:
                        best = (d, rr, cc)
        return None if best is None else (best[1], best[2])

    src = to_cell(start_xy)
    targets = [to_cell(g) for g in goals]
    out = [math.inf] * len(goals)
    if src is None:
        return out
    want = {t: [] for t in targets if t is not None}
    for i, t in enumerate(targets):
        if t is not None:
            want[t].append(i)
    dist = np.full(blocks.shape, np.inf)
    dist[src] = 0.0
    heap = [(0.0, src)]
    left = len(want)
    steps = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
             (-1, -1, 1.4142), (-1, 1, 1.4142), (1, -1, 1.4142), (1, 1, 1.4142)]
    while heap and left:
        d, (r, c) = heapq.heappop(heap)
        if d > dist[r, c]:
            continue
        if (r, c) in want:
            for i in want.pop((r, c)):
                out[i] = d * cell
            left -= 1
        for dr, dc, cost in steps:
            rr, cc = r + dr, c + dc
            if 0 <= rr < rows and 0 <= cc < cols and blocks[rr, cc]:
                nd = d + cost
                if nd < dist[rr, cc]:
                    dist[rr, cc] = nd
                    heapq.heappush(heap, (nd, (rr, cc)))
    return out


def build_plan(commands, resolved: list[dict], start_map: str, site_id: str, elevators: ElevatorStore,
               *, registry=None, start_xy: Optional[tuple[float, float]] = None,
               avoid: frozenset = frozenset()) -> list[dict]:
    """commands[i] 는 resolved[i] = {map_id, pose} 를 가진다. start_map 은 로봇이 지금 있는 맵.

    같은 층 쌍을 잇는 승강기가 여럿이면 지금 위치(start_xy, 없으면 직전 목적지)에서 승강장까지 주행 거리가
    가장 짧은 것을 탄다. avoid 에 든 승강기 id 는 다른 후보가 있을 때만 피한다 (다른 job 이 계획에 쓰는 중).
    """
    from fms_server.task import ActionCommand  # 순환 import 회피

    all_elevators = elevators.list(site_id)
    store = (registry or elevators._registry).map_store(site_id)
    plan: list[dict] = []
    group: list[dict] = []
    cur_map = start_map
    cur_xy = start_xy

    def flush():
        nonlocal group
        if group:
            plan.append({"t": "order", "map_id": cur_map, "nodes": group, "label": "이동"})
            group = []

    def stop_of(elevator, map_id):
        return next(s for s in elevator.stops if s.map_id == map_id)

    for i, (command, step) in enumerate(zip(commands, resolved, strict=True)):
        target_map = step["map_id"]
        if target_map != cur_map:
            for a, b in _find_hops(all_elevators, cur_map, target_map):
                options, errors = [], []
                for elevator in all_elevators:
                    if not (any(st.map_id == a for st in elevator.stops) and any(st.map_id == b for st in elevator.stops)):
                        continue
                    try:
                        ga = elevators.geometry(site_id, stop_of(elevator, a))
                        gb = elevators.geometry(site_id, stop_of(elevator, b))
                    except ElevatorError as exc:
                        errors.append(f"승강기 {elevator.elevator_id}: {exc}")
                        continue
                    turn = abs(math.atan2(math.sin(ga.inside[2] - gb.inside[2]), math.cos(ga.inside[2] - gb.inside[2])))
                    if turn > 0.35:
                        errors.append(f"승강기 {elevator.elevator_id}: {a} 와 {b} 의 문 방향이 다르다 "
                                      f"(전진 탑승/후진 하차라 같은 쪽이어야 한다)")
                        continue
                    options.append([elevator.elevator_id in avoid, 0.0, elevator.elevator_id, elevator, ga, gb])
                if not options:
                    raise RouteError("; ".join(errors) or f"맵 {a!r} -> {b!r} 승강기가 없다")
                if cur_xy is not None and len(options) > 1:
                    dists = path_distances(store.load_map_model(a), cur_xy, [o[4].outside[:2] for o in options])
                    if all(math.isinf(d) for d in dists):  # 격자에서 못 찾으면 직선거리
                        dists = [math.hypot(o[4].outside[0] - cur_xy[0], o[4].outside[1] - cur_xy[1]) for o in options]
                    for o, d in zip(options, dists):
                        o[1] = d
                _, _, _, elevator, ga, gb = min(options, key=lambda o: o[:3])
                eid = elevator.elevator_id
                group.append(_node(ga.outside))
                flush()
                plan[-1]["label"] = f"승강기 {eid} 앞으로 이동"
                plan.append({"t": "call", "elevator_id": eid, "map_id": a})
                plan.append({"t": "order", "map_id": a, "label": f"승강기 {eid} 탑승 (전진)", "nodes": [
                    _node(ga.outside, action={"type": "straight",
                                              "params": {"distance": ga.distance, "speed": ELEVATOR_STRAIGHT_SPEED}})]})
                plan.append({"t": "ride", "elevator_id": eid, "from_map": a, "to_map": b})
                # 하차: 아직 출발 층 맵이다. 칸 안 자세(출발 층 좌표)에서 도착 층 승강장 깊이만큼 후진
                plan.append({"t": "order", "map_id": a, "label": f"승강기 {eid} 하차 (후진)", "nodes": [
                    _node(ga.inside, action={"type": "straight",
                                             "params": {"distance": -gb.distance, "speed": ELEVATOR_STRAIGHT_SPEED}})]})
                plan.append({"t": "release", "elevator_id": eid})
                plan.append({"t": "switch", "from_map": a, "to_map": b})
                # 하차를 마친 자세 = 도착 층 승강장 앞. 출발 층 기준(ref_from) 오차를 도착 층(ref_to)에 옮겨 시뮬 teleport 한다
                plan.append({"t": "localize", "map_id": b, "x": gb.outside[0], "y": gb.outside[1],
                             "theta": gb.outside[2], "ref_from": list(ga.outside), "ref_to": list(gb.outside)})
                cur_map = b
                cur_xy = (gb.outside[0], gb.outside[1])
        pose = step["pose"]
        action = None
        if isinstance(command, ActionCommand):
            if command.kind in ("wait", "pickup", "drop"):
                action = {"type": command.kind, "duration": command.duration, "params": dict(command.params)}
            else:
                action = {"type": command.kind, "params": dict(command.params)}
                if command.kind == "docking" and registry is not None:
                    from common.schemas import Pose
                    from fms_server.mapkit.chargers import require_charger
                    require_charger(registry.map_store(site_id), target_map, Pose(**pose), command.params["entry"])
        group.append(_node((pose["x"], pose["y"], pose["theta"]), cmd=i, action=action))
        cur_xy = (pose["x"], pose["y"])
    flush()
    if plan and plan[-1]["t"] == "order" and plan[-1]["label"] == "이동":
        plan[-1]["label"] = "목적지로 이동"
    return plan


def transfer_pose(pose, ref_from, ref_to) -> tuple[float, float, float]:
    """ref_from 기준 상대 자세를 ref_to 기준으로 옮긴다 (출발 층 승강장 앞 오차 -> 도착 층)."""
    x, y, th = pose
    fx, fy, ft = ref_from
    tx, ty, tt = ref_to
    dx, dy = x - fx, y - fy
    c, s = math.cos(-ft), math.sin(-ft)
    lx, ly = c * dx - s * dy, s * dx + c * dy
    c, s = math.cos(tt), math.sin(tt)
    th2 = tt + (th - ft)
    return tx + c * lx - s * ly, ty + s * lx + c * ly, math.atan2(math.sin(th2), math.cos(th2))


def stage_label(stage: dict) -> str:
    t = stage["t"]
    if t == "order":
        return f"{stage.get('label', '이동')} ({stage['map_id']})"
    if t == "call":
        return f"승강기 {stage['elevator_id']} 호출·대기 ({stage['map_id']})"
    if t == "switch":
        return f"맵 변경 ({stage['from_map']} -> {stage['to_map']})"
    if t == "ride":
        return f"승강기 {stage['elevator_id']} 이동 ({stage['from_map']} -> {stage['to_map']})"
    if t == "localize":
        return f"새 맵 {stage['map_id']} 위치 잡기"
    return f"승강기 {stage['elevator_id']} 놓기"
