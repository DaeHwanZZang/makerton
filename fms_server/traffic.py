"""맵별 이동 허가와 점유 존. 모든 호출은 JobCoordinator.lock 안에서 수행한다.

예약은 발행 전에 디스크에 기록한다. 취소/장애만으로 풀지 않고 새 State의
정지·위치·노드 통과 증거를 확인한다. TTL에 의한 자동 해제는 없다.
"""
from __future__ import annotations

import heapq
import json
import math
import time
from pathlib import Path

import numpy as np

from common.map_annotations import ZoneType, rectangle_frame
from common.schemas import LocalizationState, OrderNode, RobotState
from fms_server.fleet import RobotLiveness


class TrafficError(ValueError):
    pass


def bounds(zone):
    return rectangle_frame(zone.polygon)


def intersects(box, pose, radius):
    if len(box) == 4:  # 회전 도입 전 저장한 예약도 같은 실제 경계로 복원한다.
        x0,y0,x1,y1 = box
        box = ((x0+x1)/2,(y0+y1)/2,1,0,(x1-x0)/2,(y1-y0)/2)
    cx,cy,ux,uy,half_width,half_height = box
    dx,dy = pose.x-cx,pose.y-cy
    return abs(ux*dx+uy*dy) <= half_width+radius and abs(-uy*dx+ux*dy) <= half_height+radius


def radius(state):
    # 회전 중에도 차체 전체가 존 밖에 있도록 외접원 + 추정 위치 여유를 사용한다.
    return math.hypot(state.width, state.length) / 2 + 0.1


class ZonePlanner:
    """정적 grid와 방향별 gate로 통과 순서를 고른다. 실제 경로는 로봇이 계획한다."""

    def __init__(self, model, zones, state):
        self.model, self.zones = model, zones
        self.margin = radius(state)
        grid = model.classify_grid() == 2
        self.free = grid.copy()
        h, w = grid.shape
        r = math.ceil(state.width / 2 / model.metadata.resolution)
        # planner.cpp와 같은 원판 팽창. UNKNOWN과 맵 밖은 주행 불가다.
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy > r * r:
                    continue
                y0, y1 = max(0, -dy), min(h, h - dy)
                x0, x1 = max(0, -dx), min(w, w - dx)
                self.free[y0:y1, x0:x1] &= grid[y0+dy:y1+dy, x0+dx:x1+dx]
        self.cache = {}
        self.grids = {}

    def mask(self, allowed):
        if allowed not in self.grids:
            free = self.free.copy()
            m = self.model.metadata
            yy, xx = np.ogrid[:free.shape[0], :free.shape[1]]
            wx = m.origin_x + (xx + 0.5) * m.resolution
            wy = m.origin_y + (yy + 0.5) * m.resolution
            for z in self.zones:
                if z.zone_id == allowed:
                    continue
                cx,cy,ux,uy,half_width,half_height = bounds(z)
                dx,dy = wx-cx,wy-cy
                free[(np.abs(ux*dx+uy*dy) <= half_width+self.margin)
                     & (np.abs(-uy*dx+ux*dy) <= half_height+self.margin)] = False
            self.grids[allowed] = free
        return self.grids[allowed]

    def distance(self, a, b, allowed=None):
        start, goal = self.model.world_to_pixel(a.x, a.y), self.model.world_to_pixel(b.x, b.y)
        key = (start, goal, allowed)
        if key in self.cache:
            return self.cache[key]
        free = self.mask(allowed)
        h, w = free.shape
        def ok(p):
            return 0 <= p[0] < w and 0 <= p[1] < h and free[p[1], p[0]]
        if not ok(start) or not ok(goal):
            return math.inf
        q, costs = [(0, 0, start)], {start: 0}
        while q:
            _, cost, p = heapq.heappop(q)
            if cost != costs[p]:
                continue
            if p == goal:
                self.cache[key] = cost * self.model.metadata.resolution
                return self.cache[key]
            for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                n = p[0]+dx, p[1]+dy
                if not ok(n) or (dx and dy and (not ok((p[0]+dx,p[1])) or not ok((p[0],p[1]+dy)))):
                    continue
                nc = cost + math.hypot(dx, dy)
                if nc < costs.get(n, math.inf):
                    costs[n] = nc
                    heapq.heappush(q, (nc+math.hypot(n[0]-goal[0], n[1]-goal[1]), nc, n))
        self.cache[key] = math.inf
        return math.inf

    def route(self, start, goal):
        # 존 내부 목적지는 장기 점유·재출발 규칙이 필요하므로 첫 버전에서는 거절한다.
        if any(intersects(bounds(z), p, self.margin) for z in self.zones for p in (start, goal)):
            raise TrafficError("점유 존 내부·경계에는 작업 시작점이나 목적지를 둘 수 없습니다")
        gates = [(z, g) for z in self.zones for g in z.gates]
        points = [start, goal] + [p for _, g in gates for p in (g.wait, g.exit)]
        # 출발/출구 -> 목적지/대기 node는 모든 존을 막고, gate 통과만 해당 존을 연다.
        graph = {i: [] for i in range(len(points))}
        for i in [0] + list(range(3, len(points), 2)):
            for j in [1] + list(range(2, len(points), 2)):
                d = self.distance(points[i], points[j])
                if math.isfinite(d):
                    graph[i].append((j, d, None))
        for k, (z, g) in enumerate(gates):
            if any(intersects(bounds(other), p, self.margin) for other in self.zones for p in (g.wait, g.exit)):
                continue
            d = self.distance(g.wait, g.exit, z.zone_id)
            if math.isfinite(d):
                graph[2+2*k].append((3+2*k, d, k))
        q, dist, prev = [(0, 0)], {0: 0}, {}
        while q:
            cost, i = heapq.heappop(q)
            if cost != dist[i]:
                continue
            if i == 1:
                crossings = []
                while i:
                    source, gate = prev[i]
                    if gate is not None:
                        crossings.append(gates[gate])
                    i = source
                return list(reversed(crossings))
            for j, d, gate in graph[i]:
                if cost+d < dist.get(j, math.inf):
                    dist[j], prev[j] = cost+d, (i, gate)
                    heapq.heappush(q, (cost+d, j))
        raise TrafficError("점유 존 대기·출구 node를 통한 안전한 경로가 없습니다")


class TrafficController:
    def __init__(self, registry, fleet, jobs):
        self.registry, self.fleet, self.jobs = registry, fleet, jobs
        self.path = Path(registry._sites_root).parent / "traffic.json"
        self.data = (json.loads(self.path.read_text()) if self.path.exists()
                     else {"limits": {}, "leases": {}})
        self.planners = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2)+"\n")
        tmp.replace(self.path)

    @staticmethod
    def key(site, map_id):
        return f"{site}/{map_id}"

    def limit(self, site, map_id):
        return self.data["limits"].get(self.key(site, map_id))

    def set_limit(self, site, map_id, limit):
        self.data["limits"][self.key(site, map_id)] = limit
        self.save()

    def snapshot(self, site, map_id):
        key = self.key(site, map_id)
        store = self.registry.map_store(site)
        zones = []
        uid = store.map_uid(map_id)
        records = self.fleet.all()
        for z in (store.load_annotations(map_id).zones if store.active_version(map_id) else []):
            if z.type != ZoneType.OCCUPANCY:
                continue
            lease = self.data['leases'].get(f'zone:{key}:{z.zone_id}')
            nearby = [r for r in records if r.state and r.state.map_id == uid]
            occupants = [r.robot_id for r in nearby if intersects(bounds(z),r.state.pose,radius(r.state))]
            unknown = any(not self.fresh(r) for r in nearby)
            unknown |= any(a.map_id == map_id and not any(r.robot_id==a.robot_id and r.state and r.state.map_id for r in records)
                           for a in self.registry.get_site(site).robots.values())
            if lease:
                unknown |= not self.fresh(self.fleet.get(lease['robot']))
            unauthorized = any(rid != (lease or {}).get('robot') for rid in occupants)
            status = 'blocked' if unknown or unauthorized else 'occupied' if occupants else 'reserved' if lease else 'free'
            zones.append({'zone_id':z.zone_id,'name':z.name,'status':status,
                          'owner':(lease or {}).get('robot'),'occupants':occupants})
        return {"max_moving_robots": self.limit(site, map_id),
                "zones": zones,
                "leases": [dict(v, resource=k) for k, v in self.data["leases"].items() if v["map"] == key],
                "waiting": [{"job_id": j.job_id, "robot_id": j.robot_id,
                             "reason": j.traffic.get("waiting")}
                            for j in self.jobs.list(site) if j.traffic.get("map") == key
                            and not j.status.terminal and j.traffic.get("waiting")]}

    def prepare(self, job, order, map_id, state):
        store = self.registry.map_store(job.site_id)
        version = store.active_version(map_id)
        zones = [z for z in store.load_annotations(map_id, version).zones if z.type == ZoneType.OCCUPANCY]
        if not zones and self.limit(job.site_id, map_id) is None:
            job.traffic = {}
            return order
        if zones and not state.traffic_control:
            raise TrafficError("이 로봇은 점유 존 제어를 지원하지 않습니다. 클라이언트를 갱신하세요")
        planner = None
        if zones:
            cache_key = (job.site_id, map_id, version, state.width, state.length)
            if cache_key not in self.planners:
                self.planners = {cache_key: ZonePlanner(store.load_map_model(map_id, version), zones, state)}
            planner = self.planners[cache_key]
        nodes, crossings, sources = [], [], {}
        previous = state.pose
        for original in order.nodes:
            # 직선/도킹/언도킹은 우회할 수 없다. 존을 걸치는 설비는 등록 시 분리한다.
            route = planner.route(previous, original.position) if planner else []
            for z, gate in route:
                seq = len(nodes)
                nodes.append(OrderNode(node_id=f"{order.order_id}-traffic-{seq}", sequence_id=seq,
                                       released=False, position=gate.wait))
                nodes.append(OrderNode(node_id=f"{order.order_id}-traffic-{seq+1}", sequence_id=seq+1,
                                       released=False, position=gate.exit, allowed_zone_ids=[z.zone_id]))
                crossings.append({"zone": z.zone_id, "gate": gate.gate_id, "wait": seq,
                                  "exit": seq+1, "bounds": bounds(z), "done": False})
            n = original.model_copy(deep=True)
            n.sequence_id, n.released = len(nodes), False
            sources[n.node_id] = original.sequence_id
            nodes.append(n)
            previous = original.position
        job.traffic = {"map": self.key(job.site_id, map_id), "map_id": map_id,
                       "version": version, "uid": store.map_uid(map_id), "crossings": crossings,
                       "sources": sources, "waiting": "층별 이동 허가 대기"}
        return order.model_copy(update={"nodes": nodes})

    def fresh(self, record):
        return (record is not None and record.state is not None
                and record.liveness(self.fleet.stale_after) == RobotLiveness.ONLINE
                and record.state.localization == LocalizationState.LOCALIZED)

    def acquire_floor(self, job, map_id, *, future=False):
        key = self.key(job.site_id, map_id)
        leases = self.data["leases"]
        resource = f"floor:{key}:{job.robot_id}"
        if resource in leases:
            # 도착 층 예약을 실제 이동 허가로 전환한다.
            if not future and (leases[resource].get("future") or leases[resource].get("order") != job.order_id):
                leases[resource].update(future=False, order=job.order_id, job=job.job_id, ack=False, at=time.time())
                self.save()
            return True
        limit = self.limit(job.site_id, map_id)
        if future and limit is None:
            return True
        holders = {v["robot"] for k,v in leases.items() if k.startswith(f"floor:{key}:")}
        uid = self.registry.map_store(job.site_id).map_uid(map_id)
        # 수동·외부 오더도 실제 이동 중이면 정원을 차지한다.
        managed = {j.order_id for j in self.jobs.list() if not j.status.terminal}
        holders.update(r.robot_id for r in self.fleet.all() if r.state and r.state.map_id == uid
                       and (abs(r.state.velocity) > 0.01 or (r.state.order_id and r.state.order_id not in managed)))
        holders.discard(job.robot_id)
        if limit is not None and len(holders) >= limit:
            return False
        leases[resource] = {"map": key, "robot": job.robot_id, "job": job.job_id,
                            "at": time.time(), "future": future, "kind": "floor",
                            "stage": job.stage,
                            "order": job.order_id, "ack": False}
        self.save()
        return True

    def _peers(self, job):
        records = {r.robot_id:r for r in self.fleet.all()}
        for r in records.values():
            if r.robot_id != job.robot_id and r.state and r.state.map_id == job.traffic['uid']:
                yield r
        # 재시작 직후 아직 한 번도 상태를 받지 못한 등록 로봇도 확인 대상으로 남긴다.
        for a in self.registry.get_site(job.site_id).robots.values():
            if a.robot_id == job.robot_id or a.map_id != job.traffic['map_id']:
                continue
            r = records.get(a.robot_id)
            if r is None or r.state is None or r.state.map_id is None:
                yield None

    def _clear_space(self, job, pose, margin):
        for r in self._peers(job):
            if not self.fresh(r) or math.hypot(r.state.pose.x-pose.x, r.state.pose.y-pose.y) <= margin+radius(r.state):
                return False
        for lease in self.data['leases'].values():
            if lease['map'] != job.traffic['map'] or lease['robot'] == job.robot_id or 'pose' not in lease:
                continue
            p = lease['pose']
            if math.hypot(p['x']-pose.x,p['y']-pose.y) <= margin+lease['radius']:
                return False
        return True

    def _zone_clear(self, job, crossing):
        for r in self._peers(job):
            if not self.fresh(r) or intersects(crossing["bounds"], r.state.pose, radius(r.state)):
                return False
        return True

    def reconcile(self):
        """새 상태로만 예약을 해제한다. 종료 작업의 남은 점유도 확인한다."""
        changed = False
        for key, lease in list(self.data["leases"].items()):
            r = self.fleet.get(lease["robot"])
            if not self.fresh(r) or r.state.timestamp <= lease["at"]:
                continue
            s = r.state
            try:
                j = self.jobs.get(lease["job"])
            except Exception:
                continue
            uid = self.registry.map_store(j.site_id).map_uid(lease["map"].split("/",1)[1])
            if (lease["kind"] == "floor" and lease.get("future") and j.status.terminal
                    and j.stage == lease.get("stage") and not s.order_id and abs(s.velocity) < .01
                    and s.state in (RobotState.IDLE, RobotState.CHARGING)):
                # 호출 단계를 벗어나지 않았으므로 탑승 전 도착 층 예약이다.
                del self.data["leases"][key]
                changed = True
                continue
            if s.map_id != uid:
                continue  # 맵 변경만으로 안전한 퇴장을 증명할 수 없다
            stopped = abs(s.velocity) < 0.01 and s.order_id is None and s.state in (RobotState.IDLE, RobotState.CHARGING)
            passed = (s.order_id == lease.get("order") and s.order_update_id >= lease.get("update", 0)
                      and (s.last_node_id == lease.get("exit_id")
                           or s.last_node_sequence_id > lease.get("exit", math.inf)))
            if lease["kind"] == "zone":
                clear = (passed or (stopped and j.status.terminal)) and not intersects(lease["bounds"], s.pose, radius(s))
            elif lease["kind"] == "wait":
                clear = (passed or (stopped and j.status.terminal))
            else:
                if s.order_id == lease.get("order") and not lease.get("ack"):
                    lease["ack"] = True
                    changed = True
                clear = stopped and (j.status.terminal or (not lease.get("future") and lease.get("ack")))
            if clear:
                del self.data["leases"][key]
                changed = True
        if changed:
            self.save()

    def update(self, job):
        """기존 허가를 유지하고 대기 node/출구까지 연속해서 허가한다."""
        t = job.traffic
        if not t or not job.order_snapshot:
            return None
        r = self.fleet.get(job.robot_id)
        if not self.fresh(r) or r.state.paused or r.state.state in (RobotState.ERROR, RobotState.EMERGENCY):
            return None
        s = r.state
        if s.map_id != t["uid"] or s.map_version != t["version"]:
            t["waiting"] = "맵·위치 확인 대기"
            return None
        if not self.acquire_floor(job, t["map_id"]):
            t["waiting"] = "층별 동시 이동 대수 제한"
            return None
        from common.schemas import Order
        order = Order.model_validate(job.order_snapshot)
        through = len(order.nodes)-1
        leases = self.data["leases"]
        for c in t["crossings"]:
            if c["done"]:
                continue
            zk = f'zone:{t["map"]}:{c["zone"]}'
            wk = f'wait:{t["map"]}:{c["zone"]}:{c["gate"]}'
            own = leases.get(zk, {}).get("order") == order.order_id and leases.get(zk, {}).get("robot") == job.robot_id
            passed = (s.order_id == order.order_id and s.order_update_id >= order.order_update_id
                      and (s.last_node_id == order.nodes[c["exit"]].node_id or s.last_node_sequence_id > c["exit"]))
            if passed and not intersects(c["bounds"], s.pose, radius(s)):
                c["done"] = True
                continue
            if own:
                through = c["exit"]
                t["waiting"] = None
                break
            wait = order.nodes[c["wait"]]
            held = leases.get(wk)
            if held and (held["robot"] != job.robot_id or held.get("order") != order.order_id):
                through = c["wait"]-1
                t["waiting"] = "대기 node 사용 중"
                break
            if not held:
                if not self._clear_space(job, wait.position, radius(s)):
                    through = c["wait"]-1
                    t["waiting"] = "대기 node 주변 확인 대기"
                    break
                leases[wk] = {"map": t["map"], "robot": job.robot_id, "job": job.job_id,
                              "kind": "wait", "at": time.time(), "order": order.order_id,
                              "pose": wait.position.model_dump(), "radius": radius(s),
                              "exit_id": order.nodes[c["exit"]].node_id, "exit": c["exit"], "update": order.order_update_id}
                self.save()
            at_wait = (s.order_id == order.order_id and s.last_node_id == wait.node_id
                       and abs(s.velocity) < 0.01 and math.hypot(s.pose.x-wait.position.x,s.pose.y-wait.position.y) < 0.08)
            end = order.nodes[c["exit"]]
            if (at_wait and zk not in leases and self._zone_clear(job, c)
                    and self._clear_space(job, end.position, radius(s))):
                leases[zk] = {"map": t["map"], "robot": job.robot_id, "job": job.job_id,
                              "kind": "zone", "at": time.time(), "order": order.order_id,
                              "pose": end.position.model_dump(), "radius": radius(s),
                              "exit_id": end.node_id, "exit": c["exit"], "bounds": c["bounds"],
                              "update": order.order_update_id+1}
                self.save()
                through = c["exit"]
                t["waiting"] = None
            else:
                through = c["wait"]
                t["waiting"] = "점유 존 진입 허가 대기"
            break
        else:
            t["waiting"] = None
        changed = False
        for n in order.nodes:
            if n.sequence_id <= through and not n.released:
                n.released, changed = True, True
        if not changed:
            if s.order_id == order.order_id and s.order_update_id < order.order_update_id:
                # 기존 오더의 미수신 갱신만 재전송한다. 신규 오더·다른 로봇 재배정은 하지 않는다.
                return order
            return None
        order.order_update_id += 1
        order.timestamp = time.time()
        return order
