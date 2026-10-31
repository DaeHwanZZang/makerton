"""물리(MuJoCo) 통합 테스트. empty_hall1 에서 robot_client 를 실제로 띄워 MQTT 로 시험한다.

리포 루트에서: .venv/bin/python tools/physics_test.py [시나리오 이름...]
시나리오: move straight dock collision pause ghost lowfric slip loop longrun obstacle dockint gui topic estimate localization unlocalized charger battery traffic traffic-rotated (생략하면 전부)
mosquitto 를 자체 포트(18851)로 띄우므로 이미 떠 있는 브로커와 부딪히지 않는다. 로봇 로그와 기록 CSV 는 임시 폴더에 남는다.
"""
import csv, glob, json, math, os, shutil, signal, subprocess, sys, tempfile, threading, time

import paho.mqtt.client as mqtt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCR = tempfile.mkdtemp(prefix="physics_test_")
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/sbin:/usr/local/sbin"  # mosquitto 위치 (macOS brew)
PORT = 18851
MAP = f"{REPO}/maps/empty_hall1/fms_map.json"
REC = f"{SCR}/rec_test"

results = []
procs = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


class Robot:
    def __init__(self, rid, x, y, theta=0.0, extra=(), record=True, mapfile=MAP, localize=True):
        self.rid = rid
        self.state = None
        self.states = []
        self.lock = threading.Lock()
        cmd = [f"{REPO}/robot_client", "--id", rid, "--port", str(PORT), "--x", str(x), "--y", str(y),
               "--theta", str(theta), "--seed", "1", "--speed-noise", "0"]
        if mapfile:
            cmd += ["--map", mapfile]
        if record:
            cmd += ["--record", REC]
        cmd += list(extra)
        self.log = open(f"{SCR}/{rid}.log", "w")
        self.p = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT, cwd=REPO)
        procs.append(self.p)
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.c.on_message = self._on
        self.c.connect("localhost", PORT)
        self.c.subscribe(f"fms/v1/{rid}/state")
        self.c.loop_start()
        self.wait(lambda s: True, 10, "first state")
        if localize and mapfile:
            # 켜자마자는 UNLOCALIZED. 시작 자세를 힌트로 줘서 스캔 매칭으로 위치를 잡는다
            self.hint(x, y, theta)
            if self.wait(lambda s: s["localization"] == "LOCALIZED", 10) is None:
                print(f"  ({rid}: localization failed: {self.state and self.state.get('errors')})")

    def _on(self, c, u, msg):
        s = json.loads(msg.payload)
        with self.lock:
            self.state = s
            self.states.append(s)

    def wait(self, cond, timeout, what=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                s = self.state
            if s is not None and cond(s):
                return s
            time.sleep(0.05)
        return None

    def order(self, oid, path, action=None, params=(), upd=0):
        a = [f"{REPO}/tools/send_order", self.rid, "--port", str(PORT), "--order-id", oid, "--update", str(upd), "--path"] + path
        if action:
            a += ["--action", action]
            for p in params:
                a += ["--param", p]
        r = subprocess.run(a, capture_output=True, text=True)
        if r.returncode != 0:
            print("send_order failed", r.stdout, r.stderr)
        return r.returncode == 0

    def instant(self, kind):
        subprocess.run([f"{REPO}/tools/send_order", self.rid, "--port", str(PORT), "--instant", kind], capture_output=True)

    def hint(self, x, y, theta=0.0):
        subprocess.run([f"{REPO}/tools/send_order", self.rid, "--port", str(PORT), "--instant", "INIT_POSITION",
                        "--param", f"x={x}", "--param", f"y={y}", "--param", f"theta={theta}"], capture_output=True)

    def pose(self):
        return self.state["pose"]

    def idle_done(self, oid, timeout):
        """오더 oid 가 끝나(IDLE 이고 order_id 없음) 기다린다"""
        def done(s):
            return s["state"] in ("IDLE", "ERROR", "CHARGING") and s["order_id"] is None
        time.sleep(0.5)
        return self.wait(done, timeout)

    def stop(self):
        self.c.loop_stop()
        self.p.send_signal(signal.SIGTERM)
        try:
            self.p.wait(3)
        except Exception:
            self.p.kill()
        self.log.close()

    def csv_rows(self):
        files = sorted(glob.glob(f"{REC}/{self.rid}_*.csv"))
        if not files:
            return []
        # 로봇이 쓰는 중에 읽으면 마지막 줄이 잘려 있다 (빈 칸 = None). 그런 줄은 버린다
        return [r for r in csv.DictReader(open(files[-1])) if None not in r.values()]


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def adiff(a, b):
    d = a - b
    while d > math.pi: d -= 2 * math.pi
    while d < -math.pi: d += 2 * math.pi
    return d


def test_move():
    r = Robot("M1", 3, 3)
    r.order("o1", ["12,3", "12,9,0"])
    s = r.idle_done("o1", 40)
    ok = s is not None
    p = r.pose()
    check("move: reaches goal and IDLE", ok and s["state"] == "IDLE" and not s["errors"], f"state={s and s['state']} pose={p}")
    check("move: final position error < 3 cm", dist((p["x"], p["y"]), (12, 9)) < 0.03, f"err={dist((p['x'], p['y']), (12, 9)) * 1000:.1f} mm")
    check("move: final heading error < 0.03 rad", abs(adiff(p["theta"], 0.0)) < 0.03, f"theta={p['theta']}")
    rows = [x for x in r.csv_rows() if x["order_id"] == "o1"]
    vmax = max(abs(float(x["v_fwd"])) for x in rows)
    wmax = max(abs(float(x["yaw_rate"])) for x in rows)
    check("move: speed within limits (v<1.35, w<2.5)", vmax < 1.35 and wmax < 2.5, f"vmax={vmax:.2f} wmax={wmax:.2f}")
    check("move: no wall contact", max(float(x["wall_force"]) for x in rows) < 1.0)
    # 하나 더: 같은 로봇으로 연속 오더
    r.order("o2", ["12,3,0"])
    s = r.idle_done("o2", 30)
    p = r.pose()
    check("move: second order works", s is not None and s["state"] == "IDLE" and dist((p["x"], p["y"]), (12, 3)) < 0.03, f"pose={p}")
    r.stop()


def test_straight():
    r = Robot("S1", 20, 6)
    r.order("s1", ["22,6,0"], "STRAIGHT", ["distance=1.0"])
    s = r.idle_done("s1", 40)
    p = r.pose()
    check("straight +1.0: ends 1.0 m ahead", s is not None and abs(p["x"] - 23.0) < 0.03 and abs(p["y"] - 6.0) < 0.03 and abs(p["theta"]) < 0.03,
          f"pose={p}")
    r.order("s2", ["23,6,0"], "STRAIGHT", ["distance=-1.5"])
    s = r.idle_done("s2", 40)
    p = r.pose()
    check("straight -1.5: reverses, heading kept", s is not None and abs(p["x"] - 21.5) < 0.03 and abs(p["y"] - 6.0) < 0.03 and abs(p["theta"]) < 0.03,
          f"pose={p}")
    r.order("s3", ["21.5,6,1.5708"], "STRAIGHT", ["distance=1.0", "speed=0.5"])
    s = r.idle_done("s3", 40)
    p = r.pose()
    check("straight at 90deg heading", s is not None and abs(p["x"] - 21.5) < 0.04 and abs(p["y"] - 7.0) < 0.04, f"pose={p}")
    r.stop()


def test_dock_park():
    r = Robot("D1", 30, 6, extra=["--battery", "50"])
    # DOCK 후진 진입: 승인점 (31,6) 에서 후진해 (30,6) 로 들어간다
    r.order("d1", ["30,6,0"], "DOCK", ["entry=BACKWARD", "undock_distance=1.0", "min_charge=56"])
    s = r.idle_done("d1", 60)
    p = r.pose()
    # 보고 방위 = 추정 (스캔 매칭 + 추적 노이즈 σθ 0.01 rad 가 섞인다). 충전 접점 허용 0.1 rad 보다 충분히 작으면 된다
    check("dock BACKWARD: docked at (30,6) heading 0", s is not None and dist((p["x"], p["y"]), (30, 6)) < 0.03 and abs(p["theta"]) < 0.05,
          f"pose={p} state={s and s['state']}")
    check("dock: charging below min_charge -> CHARGING", s is not None and s["state"] == "CHARGING", f"state={s and s['state']} batt={s and s['battery']}")
    r.order("d2", ["35,6,0"])
    time.sleep(1.0)
    s = r.state
    check("dock: job rejected while below min_charge", s["order_id"] is None, f"order_id={s['order_id']}")
    s = r.wait(lambda s: s["state"] == "IDLE", 30)
    check("dock: becomes IDLE after reaching min_charge", s is not None, f"batt={r.state['battery']}")
    r.order("d3", ["35,6,0"])
    s = r.idle_done("d3", 40)
    p = r.pose()
    check("dock: undocks forward then drives on", s is not None and dist((p["x"], p["y"]), (35, 6)) < 0.03, f"pose={p}")
    # PARK 전진 진입 -> 후진 언도킹
    r.order("p1", ["40,6,0"], "PARK", ["entry=FORWARD", "undock_distance=0.8"])
    s = r.idle_done("p1", 60)
    p = r.pose()
    check("park FORWARD: parked at (40,6)", s is not None and dist((p["x"], p["y"]), (40, 6)) < 0.03 and abs(p["theta"]) < 0.03, f"pose={p}")
    r.order("p2", ["45,6,0"])
    # 언도킹 중간에 위치를 본다: 후진이므로 x 가 먼저 39.2 쪽으로 간다
    mn = 99
    t0 = time.time()
    while time.time() - t0 < 6:
        mn = min(mn, r.state["pose"]["x"])
        time.sleep(0.05)
    check("park: undock reverses first", mn < 39.4, f"min x={mn:.2f}")
    s = r.idle_done("p2", 60)
    p = r.pose()
    check("park: then drives to next goal", s is not None and dist((p["x"], p["y"]), (45, 6)) < 0.03, f"pose={p}")
    r.stop()


def has_err(s, kind):
    return s is not None and any(e["error_type"] == kind for e in s["errors"])


def test_charger():
    """충전기는 맵의 물리 설비 (maps/empty_hall1/chargers.json: CH-1 (30,6,0) 후진 진입)."""
    r = Robot("H1", 20, 6, extra=["--battery", "50"])
    r.order("h1", ["25,6,0"], "DOCK", ["entry=BACKWARD", "undock_distance=1.0", "min_charge=0"])
    s = r.wait(lambda s: has_err(s, "DOCK_FAILED"), 40)
    check("charger: DOCK where no charger -> DOCK_FAILED", s is not None and s["state"] == "ERROR" and not s["battery"]["charging"],
          f"state={s and (s['state'], s['errors'])}")
    r.instant("RESET_ERROR")
    r.order("h2", ["20,6,0"], "CHARGE")
    s = r.wait(lambda s: has_err(s, "CHARGE_FAILED"), 30)
    check("charger: CHARGE off the charger -> CHARGE_FAILED", s is not None, f"errors={s and s['errors']}")
    r.instant("RESET_ERROR")
    r.order("h3", ["30,6,0"], "DOCK", ["entry=FORWARD", "undock_distance=1.0", "min_charge=0"])
    s = r.wait(lambda s: has_err(s, "DOCK_FAILED"), 60)
    check("charger: wrong entry side -> DOCK_FAILED, not charging", s is not None and not s["battery"]["charging"],
          f"errors={s and [e['description'] for e in s['errors']]}")
    r.instant("RESET_ERROR")
    r.order("h4", ["30,6,0"], "DOCK", ["entry=BACKWARD", "undock_distance=1.0", "min_charge=0"])
    s = r.wait(lambda s: s["order_id"] is None and s["battery"]["charging"], 90)
    p = r.pose()
    check("charger: undocks, then docks BACKWARD and charges", s is not None and dist((p["x"], p["y"]), (30, 6)) < 0.04,
          f"pose={p} batt={r.state['battery']} errors={r.state['errors']}")
    b0 = r.state["battery"]["charge"]
    time.sleep(2.0)
    check("charger: battery rises while docked", r.state["battery"]["charge"] > b0, f"{b0} -> {r.state['battery']['charge']}")
    r.stop()


def test_battery():
    r = Robot("B1", 3, 6, extra=["--battery", "20.3"])
    r.order("b1", ["40,6"])
    s = r.wait(lambda s: has_err(s, "LOW_BATTERY"), 30)
    check("battery: LOW_BATTERY warning below 20%, order continues", s is not None and s["order_id"] == "b1",
          f"state={s and (s['state'], s['battery'])}")
    r.stop()

    r = Robot("B2", 3, 6, extra=["--battery", "5.3"])
    r.order("b2", ["40,6"])
    s = r.wait(lambda s: has_err(s, "BATTERY_CRITICAL"), 30)
    check("battery: crossing 5% -> FATAL BATTERY_CRITICAL, order dropped", s is not None and s["state"] == "ERROR" and s["order_id"] is None,
          f"state={s and (s['state'], s['battery'])}")
    r.instant("RESET_ERROR")
    r.wait(lambda s: s["state"] == "IDLE", 5)
    r.order("b3", ["10,6"])
    time.sleep(1.0)
    check("battery: below critical, normal order rejected", r.state["order_id"] is None and r.state["state"] == "IDLE",
          f"state={r.state['state']} order={r.state['order_id']}")
    r.order("b4", ["30,6,0"], "DOCK", ["entry=BACKWARD", "undock_distance=1.0", "min_charge=0"])
    s = r.wait(lambda s: s["order_id"] is None and s["battery"]["charging"], 90)
    check("battery: below critical, DOCK order accepted and charges", s is not None, f"state={r.state['state']} batt={r.state['battery']}")
    r.stop()

    r = Robot("B3", 3, 6, extra=["--battery", "0.15"])
    r.order("b5", ["40,6"])
    s = r.wait(lambda s: has_err(s, "BATTERY_EMPTY"), 30)
    check("battery: empty -> FATAL BATTERY_EMPTY", s is not None and s["state"] == "ERROR", f"state={s and (s['state'], s['battery'])}")
    time.sleep(1.0)
    p0 = r.pose()
    r.instant("RESET_ERROR")
    time.sleep(1.0)
    r.order("b6", ["10,6,0"], "DOCK", ["entry=BACKWARD", "undock_distance=1.0", "min_charge=0"])
    time.sleep(2.0)
    p1 = r.pose()
    check("battery: empty robot cannot drive (even after reset / DOCK order)",
          dist((p0["x"], p0["y"]), (p1["x"], p1["y"])) < 0.02 and has_err(r.state, "BATTERY_EMPTY"),
          f"moved={dist((p0['x'], p0['y']), (p1['x'], p1['y'])):.3f} state={r.state['state']}")
    r.stop()


def test_collision():
    r = Robot("C1", 50, 6)
    r.order("c1", ["50,6,0"], "STRAIGHT", ["distance=40", "speed=0.8"])
    s = r.wait(lambda s: any(e["error_type"] == "COLLISION" for e in s["errors"]), 60)
    p = r.pose()
    check("collision: COLLISION FATAL raised on wall", s is not None, f"errors={s and s['errors']}")
    check("collision: robot stays inside, no ejection", 52 < p["x"] < 54.6 and abs(p["y"] - 6) < 0.3, f"pose={p}")
    time.sleep(1.0)
    p2 = r.pose()
    check("collision: robot stays put after error", dist((p["x"], p["y"]), (p2["x"], p2["y"])) < 0.02, f"{p} -> {p2}")
    rows = r.csv_rows()
    check("collision: wall force recorded", max(float(x["wall_force"]) for x in rows) > 20, f"max={max(float(x['wall_force']) for x in rows):.0f} N")
    r.stop()


def test_pause_cancel():
    r = Robot("P1", 3, 3)
    r.order("q1", ["20,3,0"])
    r.wait(lambda s: s["velocity"] > 0.8, 15)
    r.instant("PAUSE")
    r.wait(lambda s: s["paused"], 5)
    time.sleep(0.5)
    p = r.pose()
    v = r.state["velocity"]
    check("pause: stops immediately", v < 0.02, f"v={v}")
    time.sleep(1.0)
    p2 = r.pose()
    check("pause: stays still", dist((p["x"], p["y"]), (p2["x"], p2["y"])) < 0.01)
    r.instant("RESUME")
    s = r.wait(lambda s: s["velocity"] > 0.5, 10)
    check("resume: moves again", s is not None)
    r.instant("CANCEL_ORDER")
    time.sleep(0.2)
    s = r.wait(lambda s: s["velocity"] < 0.05, 5)
    check("cancel: coasts to a stop", s is not None, f"v={r.state['velocity']}")
    p = r.pose()
    time.sleep(1.0)
    p2 = r.pose()
    check("cancel: stays stopped (no stale velocity)", dist((p["x"], p["y"]), (p2["x"], p2["y"])) < 0.01, f"{p} -> {p2}")
    r.order("q2", [f"{p['x'] + 3:.2f},{p['y']:.2f},0"])
    s = r.idle_done("q2", 30)
    p3 = r.pose()
    check("cancel: next order runs fine", s is not None and abs(p3["x"] - (p["x"] + 3)) < 0.04, f"pose={p3}")
    r.stop()


def test_ghost_start():
    r = Robot("G1", 0.0, 0.0, localize=False)  # 맵 여백(UNKNOWN) 위
    time.sleep(2.0)
    p = r.pose()
    check("ghost start: no ejection", abs(p["x"]) < 0.01 and abs(p["y"]) < 0.01, f"pose={p}")
    # 벽 속에서는 라이다가 아무것도 못 본다 -> 힌트를 줘도 로컬라이징 실패, 오더는 거절
    r.hint(0.0, 0.0)
    s = r.wait(lambda s: any(e["error_type"] == "LOCALIZATION_FAILED" for e in s["errors"]), 10)
    check("ghost start: localization fails inside the wall", s is not None and s["localization"] == "UNLOCALIZED",
          f"state={s and (s['localization'], s['errors'])}")
    r.order("g1", ["5,5,0"])
    time.sleep(1.0)
    check("ghost start: order rejected while UNLOCALIZED", r.state["order_id"] is None)
    rows = r.csv_rows()
    check("ghost start: ghost flag recorded", any(x["ghost"] == "1" for x in rows))
    p = r.pose()
    check("ghost start: still at start", abs(p["x"]) < 0.01 and abs(p["y"]) < 0.01, f"pose={p}")
    r.stop()


def test_unlocalized():
    """켜자마자 UNLOCALIZED: 오더 거절 -> 먼 힌트는 실패 -> 가까운 힌트(0.9 m, 11°)는 스캔 매칭으로 참 자세에 수렴."""
    r = Robot("U2", 3, 3, localize=False)
    check("unlocalized: starts UNLOCALIZED", r.state["localization"] == "UNLOCALIZED")
    r.order("u1", ["5,3"])
    time.sleep(1.0)
    check("unlocalized: order rejected", r.state["order_id"] is None and r.state["state"] == "IDLE")
    r.hint(8.0, 3.0, 0.3)  # 5 m 틀림 (창 ±1.5 m 밖)
    s = r.wait(lambda s: any(e["error_type"] == "LOCALIZATION_FAILED" for e in s["errors"]), 10)
    check("unlocalized: far hint fails", s is not None and s["localization"] == "UNLOCALIZED",
          f"score={s and s['localization_score']}")
    r.hint(3.8, 2.5, 0.2)
    s = r.wait(lambda s: s["localization"] == "LOCALIZED", 10)
    p = s and s["pose"]
    check("unlocalized: near hint localizes", s is not None and not s["errors"], f"state={s and (s['localization'], s['errors'])}")
    check("unlocalized: estimate snaps to true pose (< 3 cm, < 0.02 rad)",
          p is not None and dist((p["x"], p["y"]), (3, 3)) < 0.03 and abs(adiff(p["theta"], 0.0)) < 0.02, f"pose={p}")
    r.order("u2", ["5,3"])
    s = r.idle_done("u2", 30)
    check("unlocalized: drives after localization", s is not None and dist((s["pose"]["x"], s["pose"]["y"]), (5, 3)) < 0.03)
    r.stop()


def test_low_friction():
    # 위치보정을 켜면 로봇이 참 위치를 따라 속도를 조절해 슬립이 줄어든다. 슬립/오도메트리 오차 자체를 보려고 끈다
    r = Robot("F1", 3, 3, extra=["--friction", "0.15", "--max-acceleration", "2.0", "--loc-rate", "0"])
    r.order("f1", ["15,3,0"])
    s = r.idle_done("f1", 90)
    p = r.pose()
    rows = [x for x in r.csv_rows() if x["order_id"] == "f1"]
    slip = max(max(abs(float(x["slip_l"])), abs(float(x["slip_r"]))) for x in rows)
    last = rows[-1]
    # 가속 때 앞으로 미끄러진 만큼 오도메트리가 앞서 가고 감속 때 다시 줄어든다. 끝에선 상쇄되니 주행 중 최대 오차를 본다
    odom_err = max(math.hypot(float(x["odom_x"]) - float(x["x"]), float(x["odom_y"]) - float(x["y"])) for x in rows)
    check("low friction: still arrives", s is not None and dist((p["x"], p["y"]), (15, 3)) < 0.05, f"pose={p}")
    check("low friction: wheel slip occurs (>0.3)", slip > 0.3, f"max slip={slip:.2f}")
    check("low friction: odometry drifts from truth during the run (>2 cm)", odom_err > 0.02, f"odom err={odom_err * 100:.1f} cm")
    r.stop()


def test_normal_slip_small():
    r = Robot("N1", 3, 3)
    r.order("n1", ["15,3,0"])
    r.idle_done("n1", 60)
    rows = [x for x in r.csv_rows() if x["order_id"] == "n1" and abs(float(x["v_fwd"])) > 0.3]
    slip = sorted(max(abs(float(x["slip_l"])), abs(float(x["slip_r"]))) for x in rows)
    med = slip[len(slip) // 2]
    last = r.csv_rows()[-1]
    odom_err = math.hypot(float(last["odom_x"]) - float(last["x"]), float(last["odom_y"]) - float(last["y"]))
    check("normal friction: median slip small (<0.08)", med < 0.08, f"median slip={med:.3f}")
    check("normal friction: odometry close to truth (<35 cm / 12 m, ~2% heading drift)", odom_err < 0.35, f"odom err={odom_err * 100:.1f} cm")
    r.stop()


def test_loop_map():
    # 통로가 좁은 생성 맵: 물리 충돌 판정이 planner 와 어긋나는지 본다 (정보용 + 완주 확인)
    m = f"{REPO}/maps/gen_loop/fms_map.json"
    r = Robot("L1", 1, 1, mapfile=m)
    r.order("l1", ["1,9,0", "10,9,0", "10,1,0", "1,1,0"])
    s = r.idle_done("l1", 120)
    errs = r.state["errors"]
    p = r.pose()
    check("loop map: completes loop without COLLISION", s is not None and not errs, f"state={s and s['state']} errors={errs} pose={p}")
    r.stop()


def test_longrun():
    # 55 m 가로지르기: 벽 창(window)이 여러 번 다시 잡힌다. 자세가 끊기지 않아야 한다.
    r = Robot("R1", 2, 3)
    r.order("r1", ["52,3,0", "52,12,0", "2,12,0"])
    s = r.idle_done("r1", 200)
    p = r.pose()
    rows = [x for x in r.csv_rows() if x["order_id"] == "r1"]
    jump = 0.0   # 참 자세의 tick 간 이동 속도 (m/s). tick 시간(dt)이 늘어난 것과 위치 점프를 구분한다
    for a, b in zip(rows, rows[1:]):
        dt = max(1e-3, float(b["t"]) - float(a["t"]))
        jump = max(jump, math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"])) / dt)
    check("longrun: completes 110 m route", s is not None and s["state"] == "IDLE" and not s["errors"] and dist((p["x"], p["y"]), (2, 12)) < 0.03, f"pose={p} errors={s and s['errors']}")
    check("longrun: no position jump at window rebuilds (true speed < 1.5 m/s)", jump < 1.5, f"max step speed={jump:.2f} m/s")
    check("longrun: no wall contact", max(float(x["wall_force"]) for x in rows) < 1.0)
    r.stop()


def test_obstacle():
    r = Robot("O1", 3, 6)
    r.order("b1", ["30,6,0"])
    r.wait(lambda s: s["pose"]["x"] > 8 and s["velocity"] > 0.8, 30)
    subprocess.run([f"{REPO}/tools/place_obstacle", "O1", "--port", str(PORT), "--x", "20", "--y", "6", "--width", "1.0", "--height", "3.0"], capture_output=True)
    s = r.wait(lambda s: s["sub_state"] == "WAITING" if s.get("sub_state") else False, 20)
    check("obstacle: robot waits when path blocked", s is not None)
    time.sleep(4)
    rows = r.csv_rows()
    amax = 0.0
    for a, b in zip(rows, rows[1:]):
        if abs(float(a["v_fwd"])) > 0.6:
            amax = max(amax, abs(float(b["v_fwd"]) - float(a["v_fwd"])) / max(1e-3, float(b["t"]) - float(a["t"])))
    check("obstacle: no abrupt stops from cruise speed (|a| < 2 m/s^2 above 0.6 m/s)", amax < 2.0, f"max |a|={amax:.2f}")
    subprocess.run([f"{REPO}/tools/place_obstacle", "O1", "--port", str(PORT), "--clear"], capture_output=True)
    s = r.idle_done("b1", 60)
    p = r.pose()
    check("obstacle: resumes and arrives after clearing", s is not None and dist((p["x"], p["y"]), (30, 6)) < 0.04, f"pose={p}")
    r.stop()


def test_dock_interrupt():
    r = Robot("K1", 30, 6, extra=["--battery", "90"])
    r.order("k1", ["30,6,0"], "DOCK", ["entry=BACKWARD", "undock_distance=1.5", "min_charge=0"])
    r.wait(lambda s: s.get("sub_state") == "ACTING", 30)
    time.sleep(2.0)
    r.instant("PAUSE")
    r.wait(lambda s: s["paused"], 5)
    time.sleep(0.3)
    p = r.pose()
    check("dock pause: stops mid-insert", r.state["velocity"] < 0.02 and 30.0 < p["x"] < 31.5, f"pose={p}")
    r.instant("RESUME")
    s = r.idle_done("k1", 60)
    p = r.pose()
    check("dock resume: finishes insert at (30,6)", s is not None and dist((p["x"], p["y"]), (30, 6)) < 0.03 and abs(p["theta"]) < 0.03, f"pose={p}")
    # 진입 중 취소 -> 다음 오더가 먼저 언도킹
    r.order("k2", ["40,6,0"])
    time.sleep(0.3)
    r.instant("CANCEL_ORDER")
    time.sleep(0.5)
    r.order("k3", ["40,6,0"])
    s = r.idle_done("k3", 60)
    p = r.pose()
    check("dock: order after cancel undocks first and arrives", s is not None and dist((p["x"], p["y"]), (40, 6)) < 0.03, f"pose={p}")
    r.stop()


def test_physics_topic():
    r = Robot("T1", 20, 6, extra=["--friction", "0.5"])
    got = []
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.on_message = lambda cl, u, m: got.append((time.time(), json.loads(m.payload)))
    c.connect("localhost", PORT)
    c.subscribe("fms/v1/T1/physics")
    c.loop_start()
    r.order("p1", ["23,6,0"], "STRAIGHT", ["distance=-1.5"])
    r.idle_done("p1", 40)
    c.loop_stop()
    check("physics topic: published", len(got) > 20, f"n={len(got)}")
    if got:
        d = got[-1][1]
        need = {"x", "y", "theta", "odom_x", "v_fwd", "v_lat", "yaw_rate", "wheel_w", "wheel_cmd", "slip", "cmd_v", "cmd_w",
                "wall_force", "ghost", "collided", "at_rest", "friction", "mass", "path_points", "path_remaining", "goal", "sim_time"}
        check("physics topic: has all fields", need <= set(d), f"missing={need - set(d)}")
        check("physics topic: friction/mass reported", abs(d["friction"] - 0.5) < 1e-6 and abs(d["mass"] - 60) < 1e-6)
        span = got[-1][0] - got[0][0]
        rate = len(got) / span if span > 0 else 0
        check("physics topic: rate ~20 Hz (every tick)", 15 <= rate <= 25, f"rate={rate:.1f}")
        check("physics topic: reverse straight has negative v_fwd", min(g[1]["v_fwd"] for g in got) < -0.1,
              f"min v_fwd={min(g[1]['v_fwd'] for g in got):.3f}")
    r.stop()


def test_estimated_pose():
    """보정 없음(--loc-rate 0): 제어와 State.pose 는 순수 오도메트리로 돈다. 저마찰이면 참 자세가 추정에서 벌어진다."""
    r = Robot("E1", 3, 3, extra=["--friction", "0.3", "--loc-rate", "0"])
    r.order("e1", ["12,3", "12,9,0"])
    s = r.idle_done("e1", 60)
    p = r.pose()
    rows = [x for x in r.csv_rows() if x["order_id"] == "e1"]
    last = rows[-1]
    tx, ty = float(last["x"]), float(last["y"])
    ox, oy = float(last["odom_x"]), float(last["odom_y"])
    # 처음 위치는 스캔 매칭이 정한다 (추정 = T∘오도메트리, T 는 매칭 결과). 보정이 없으니 그 뒤로 T 가 그대로여야 한다
    first = rows[0]
    th = float(first["est_theta"]) - float(first["odom_theta"])
    c, sn = math.cos(th), math.sin(th)
    tx0 = float(first["est_x"]) - (c * float(first["odom_x"]) - sn * float(first["odom_y"]))
    ty0 = float(first["est_y"]) - (sn * float(first["odom_x"]) + c * float(first["odom_y"]))
    px, py = c * ox - sn * oy + tx0, sn * ox + c * oy + ty0
    check("no localization: State.pose follows raw odometry (fixed offset)", dist((p["x"], p["y"]), (px, py)) < 0.02,
          f"state=({p['x']:.3f},{p['y']:.3f}) odom∘T=({px:.3f},{py:.3f})")
    check("no localization: arrives at goal by estimate", s is not None and dist((p["x"], p["y"]), (12, 9)) < 0.03)
    off = dist((tx, ty), (p["x"], p["y"]))
    check("no localization: true pose differs from estimate at low friction (> 3 cm)", off > 0.03, f"off={off * 100:.1f} cm")
    r.stop()


def test_localization():
    """기본 켜짐(라이다 위치보정 시뮬레이션): 저마찰에서도 추정이 참 자세를 따라가고, 보정 전 오도메트리는 벌어진다."""
    r = Robot("L1", 3, 3, extra=["--friction", "0.3"])
    r.order("l1", ["12,3", "12,9", "6,9", "6,12,0"])
    s = r.idle_done("l1", 90)
    p = r.pose()
    rows = [x for x in r.csv_rows() if x["order_id"] == "l1"]
    est_err = [dist((float(x["est_x"]), float(x["est_y"])), (float(x["x"]), float(x["y"]))) for x in rows]
    odom_err = [dist((float(x["odom_x"]), float(x["odom_y"])), (float(x["x"]), float(x["y"]))) for x in rows]
    last = rows[-1]
    check("localization: order completes at low friction", s is not None and s["state"] == "IDLE", f"state={s and s['state']}")
    check("localization: estimate error stays small (max < 8 cm)", max(est_err) < 0.08, f"max={max(est_err) * 100:.1f} cm")
    check("localization: raw odometry drifts far more than estimate", max(odom_err) > 5 * max(est_err),
          f"odom max={max(odom_err) * 100:.0f} cm est max={max(est_err) * 100:.1f} cm")
    check("localization: true final position within 5 cm of goal",
          dist((float(last["x"]), float(last["y"])), (6, 12)) < 0.05, f"true=({last['x']},{last['y']}) state=({p['x']:.2f},{p['y']:.2f})")
    # 위치 재지정(없으니 생략): 보정 통계가 토픽에 올라오는지만 본다
    r.stop()


def test_gui_offscreen():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = subprocess.Popen([f"{REPO}/robot_client", "--id", "U1", "--port", str(PORT), "--x", "3", "--y", "3", "--map", MAP, "--gui"],
                         env=dict(env, AMR_GUI_SNAPSHOT=f"{SCR}/gui_U1.png"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=REPO)
    time.sleep(3.0)
    alive = p.poll() is None
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(5)
    except Exception:
        p.kill()
    out = p.stdout.read().decode(errors="ignore")
    check("gui(offscreen): starts and runs", alive and "physics: wall boxes built" in out, out[-200:] if not alive else "")
    check("gui(offscreen): snapshot saved (physics panel renders)", os.path.exists(f"{SCR}/gui_U1.png"))


def test_traffic(rotation=0):
    """실제 두 C++ 프로세스 + FMS + MQTT + 맵 번들로 점유 존을 검증한다."""
    import functools
    import http.server
    import io
    from pathlib import Path
    from PIL import Image
    sys.path.insert(0, REPO)
    from common.schemas import InstantAction, InstantActionType
    from common.map_annotations import rectangle_polygon
    from fms_server.config import Settings
    from fms_server.fleet import Fleet
    from fms_server.mqtt_client import FmsMqttClient
    from fms_server.sites import SiteRegistry
    from fms_server.task import JobCoordinator, JobStore

    root = Path(SCR)/f'traffic-{rotation}'
    registry = SiteRegistry(root)
    registry.create_site('hq','교통 시험')
    angle = math.radians(rotation)
    co,si = math.cos(angle),math.sin(angle)
    target_cx,target_cy = (10,10) if rotation else (8,5)
    def transform(x,y,theta=0):
        dx,dy = x-8,y-5
        return dict(x=target_cx+co*dx-si*dy,y=target_cy+si*dx+co*dy,theta=theta+angle)
    def original_x(p):
        return 8+co*(p['x']-target_cx)+si*(p['y']-target_cy)
    shape = (200,200) if rotation else (160,100)
    image = Image.new('L',shape,255)
    from PIL import ImageDraw
    ImageDraw.Draw(image).rectangle((0,0,shape[0]-1,shape[1]-1),outline=0,width=2)
    buf = io.BytesIO(); image.save(buf,format='PNG')
    p = transform
    # 45도 존도 맵 양 끝에 닿게 해 미허가 우회 경로가 없는 장면을 만든다.
    polygon = (rectangle_polygon(9,10-(20*math.sqrt(2)-2)/2,2,20*math.sqrt(2)-2,rotation)
               if rotation else rectangle_polygon(7,0,2,10))
    files = {'fms_map.json':json.dumps(dict(name='hall',image='grid.png',resolution=.1,
              origin_x=0,origin_y=0,width_px=shape[0],height_px=shape[1])).encode(), 'grid.png':buf.getvalue(),
             'zone_meta.json':json.dumps({'zones':[dict(id='crossing',name='교차로',type='occupancy',
                 polygon=[[v.x,v.y] for v in polygon],gates=[
                     dict(gate_id='west',wait=p(5,3),exit=p(11,3)),
                     dict(gate_id='east',wait=p(11,7),exit=p(5,7))])]}).encode()}
    maps = registry.map_store('hq')
    result = maps.ingest_upload('f1',files,activate=True)
    root.mkdir(parents=True,exist_ok=True)
    (root/'bundle.zip').write_bytes(maps.bundle_zip('f1'))
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self,*args): pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(root)))
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    fleet, jobs = Fleet(), JobStore(root)
    transport = FmsMqttClient(fleet,Settings(mqtt_port=PORT,mqtt_client_id='traffic-test'))
    coord = JobCoordinator(fleet,registry,jobs,transport)
    coord.start();transport.start()
    robots = []
    try:
        for rid,x,y,th in [('T1',3,3,0),('T2',13,7,math.pi)]:
            start = transform(x,y,th)
            x,y,th = start['x'],start['y'],start['theta']
            registry.register_robot('hq',rid,'f1')
            robot = Robot(rid,x,y,th,mapfile=str(maps.file_path('f1','fms_map.json')),
                          extra=('--map-cache',str(root/rid)),record=False,localize=False)
            robots.append(robot)
            transport.publish_instant(InstantAction(header_id=0,timestamp=time.time(),robot_id=rid,
                action_id='set-'+rid,action_type=InstantActionType.SET_MAP,
                params=dict(map_id=maps.map_uid('f1'),map_version=result.version,
                            url=f'http://127.0.0.1:{server.server_port}/bundle.zip')))
            synced = robot.wait(lambda s:s.get('map_version')==result.version,15)
            check(f'traffic: {rid} downloads occupancy bundle',synced is not None)
            robot.hint(x,y,th)
            check(f'traffic: {rid} localized',robot.wait(lambda s:s['localization']=='LOCALIZED',10) is not None)
        for name,x,y,th in [('east',13,3,0),('west',3,7,math.pi)]:
            goal = transform(x,y,th)
            registry.location_store('hq').create(name,'f1',goal['x'],goal['y'],goal['theta'])
        a = jobs.create('hq',[dict(type='move',target='east')],'T1')
        b = jobs.create('hq',[dict(type='move',target='west')],'T2')
        coord.dispatch(a.job_id);coord.dispatch(b.job_id)
        max_inside, waited, violation = 0, False, False
        t0 = time.time()
        while time.time()-t0 < 65 and not (a.status.terminal and b.status.terminal):
            coord.allocate_pending()
            states = [r.state for r in robots]
            inside = [s for s in states if s and 6.5 <= original_x(s['pose']) <= 9.5]
            max_inside = max(max_inside,len(inside))
            waited |= any(s and s['sub_state']=='WAITING' and 4.9 <= original_x(s['pose']) <= 11.1 for s in states)
            for s in inside:
                owner = coord.traffic.data['leases'].get('zone:hq/f1:crossing',{}).get('robot')
                violation |= owner != s['robot_id']
            time.sleep(.05)
        check('traffic: both jobs finish',a.status.value=='success' and b.status.value=='success',f'{a.status.value}, {b.status.value}')
        check('traffic: at most one chassis inside zone',max_inside==1 and not violation,f'max={max_inside} unauthorized={violation}')
        check('traffic: waits at designated node',waited)
        check('traffic: inserted nodes do not inflate progress',a.done_commands==1 and b.done_commands==1)
        coord.tick_traffic()
        check('traffic: reservations cleared',not coord.traffic.data['leases'])
        # FMS를 거치지 않은 오더도 미허가 존을 가로지를 수 없다.
        r = robots[0]
        raw_goal = transform(3,3)
        r.order('unauthorized',[f'{raw_goal["x"]},{raw_goal["y"]}'])
        error = r.wait(lambda s:any(e['error_type']=='NO_PATH' for e in s['errors']),5)
        check('traffic: raw unauthorized order is blocked',error is not None and original_x(r.pose())>9.5)
        r.instant('RESET_ERROR')
        r.wait(lambda s:s['state']=='IDLE',5)
        raw_start = transform(14,3)
        r.order('unauthorized-straight',[f'{raw_start["x"]},{raw_start["y"]},{raw_start["theta"]}'],action='STRAIGHT',params=['distance=-10'])
        error = r.wait(lambda s:any(e['error_type']=='TRAFFIC_BLOCKED' for e in s['errors']),15)
        check('traffic: unauthorized straight motion is blocked',error is not None and original_x(r.pose())>9.5)
    finally:
        coord.stop();transport.stop()
        for robot in robots: robot.stop()
        server.shutdown();server.server_close();thread.join(2)


ALL = {
    "move": test_move, "straight": test_straight, "dock": test_dock_park, "collision": test_collision,
    "traffic": test_traffic,
    "traffic-rotated": lambda: test_traffic(45),
    "pause": test_pause_cancel, "ghost": test_ghost_start, "lowfric": test_low_friction, "slip": test_normal_slip_small,
    "loop": test_loop_map, "longrun": test_longrun, "obstacle": test_obstacle, "dockint": test_dock_interrupt, "gui": test_gui_offscreen, "topic": test_physics_topic, "estimate": test_estimated_pose, "localization": test_localization, "unlocalized": test_unlocalized, "charger": test_charger, "battery": test_battery,
}

if __name__ == "__main__":
    for need in (f"{REPO}/robot_client", f"{REPO}/tools/send_order", f"{REPO}/tools/place_obstacle"):
        if not os.path.exists(need):
            sys.exit(f"{need} 없음: 먼저 make 를 실행하라")
    print(f"로그/기록 폴더: {SCR}")
    names = sys.argv[1:] or list(ALL)
    shutil.rmtree(REC, ignore_errors=True)
    mq = subprocess.Popen(["mosquitto", "-p", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(mq)
    time.sleep(1.0)
    try:
        for n in names:
            print(f"== {n}", flush=True)
            try:
                ALL[n]()
            except Exception as e:  # noqa
                check(f"{n}: exception", False, repr(e))
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} PASS")
    sys.exit(1 if bad else 0)
