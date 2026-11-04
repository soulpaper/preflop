"""python deploy/test_gto_thermal.py - 온도 제어기 시험. Controller 는 가짜 시계·가짜 온도열로, 파일/시스템은 임시 폴더와 가짜 IO 로만.
실제 solves/, 솔버, sched_setaffinity 는 건드리지 않는다."""
import io as _io
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gto_thermal as gt   # noqa: E402

results = []


def test(name, ok, info=""):
    results.append(("PASS" if ok else "FAIL", name, info))


CFG = gt.load_config({}, NCPU=12)
FULL, NORMAL, COOL = tuple(range(12)), tuple(range(10)), (0, 2, 4)


def run(ctl, temps, ceiling=3, solves=(), start=1000.0, dt_=10.0):
    """온도열(리스트) 또는 (개수, 온도) 를 10초 간격으로 먹이고 마지막 Decision 과 전체를 돌려준다."""
    out, t = [], start
    for x in temps:
        out.append(ctl.step(t, x, ceiling, list(solves)))
        t += dt_
    return out


def steady(temp, secs, dt_=10.0):
    return [temp] * int(secs / dt_)


# 시작 직후: 표본이 DOWN_DWELL(120초) 만큼 쌓이기 전에는 5분 평균 규칙으로 내리지 않음
ds = run(gt.Controller(CFG), steady(92, 100), ceiling=2)
test("시작 직후 92도 100초: 아직 normal", all(d.level == 2 for d in ds))
ds = run(gt.Controller(CFG), steady(92, 200), ceiling=2)
test("시작 후 92도 200초: cool 로 내림", ds[-1].level == 1)

# 정상 유지
c = gt.Controller(CFG)
ds = run(c, steady(89, 1800))
test("정상(89도): full 상한에서 계속 full", all(d.level == 3 and not d.hold for d in ds))
c = gt.Controller(CFG)
ds = run(c, steady(89, 600), ceiling=2)
test("정상(89도): low 상한이면 normal 유지", all(d.level == 2 for d in ds))
test("normal/full 선호도 집합", ds[-1].cpus_by_pid == {} and c.sets[2] == NORMAL and c.sets[3] == FULL and c.sets[1] == COOL)

# 5분 평균 > 91 -> 한 단계 내림
c = gt.Controller(CFG)
ds = run(c, steady(92, 600))
test("5분 평균 92: 표본 120초 쌓인 뒤 한 단계 내림(full -> normal)", ds[11].level == 3 and ds[12].level == 2, str([d.level for d in ds[10:14]]))
test("내린 뒤 120초 전에는 또 내리지 않음", all(d.level == 2 for d in ds[12:24]), str([d.level for d in ds[12:25]]))
test("120초 뒤 계속 높으면 cool 까지 내림, hold 는 아님", ds[24].level == 1 and not ds[-1].hold, str([d.level for d in ds[22:26]]))
test("cool 아래로는 평균만으로 안 내려감", ds[-1].level == 1)

# 93 즉시 cool
c = gt.Controller(CFG)
run(c, steady(89, 600))
ds = run(c, steady(94, 30), start=1600.0)
test("30초 평균 93 이상: 체류 시간 무시하고 즉시 cool", any(d.level == 1 for d in ds[:3]) and ds[-1].level == 1, str([round(d.avg30, 1) for d in ds]))
c = gt.Controller(CFG)
d = run(c, [89, 89, 89, 94, 94, 94, 94, 94], start=1000.0)
test("순간 스파이크 한두 번(평균 93 미만)은 반응 안 함", run(gt.Controller(CFG), [89] * 20 + [96])[-1].level == 3)

# 95 60초 -> hold, 90 미만 풀림
c = gt.Controller(CFG)
ds = run(c, steady(96, 50))
test("95 이상 50초: 아직 hold 아님", not ds[-1].hold and ds[-1].level == 1)
ds = run(c, steady(96, 40), start=1500.0)
test("95 이상 60초 이어지면 hold (level 0)", ds[-1].hold and ds[-1].level == 0 and ds[-1].name == "hold")
ds = run(c, steady(92, 100), start=1900.0)
test("hold 는 30초 평균 90 이상이면 유지", ds[5].hold)
ds2 = run(c, steady(85, 60), start=2000.0)
test("30초 평균 90 미만이면 hold 풀림, 단계는 cool", not ds2[-1].hold and ds2[-1].level == 1, str([(d.hold, d.level) for d in ds2]))
c = gt.Controller(CFG)
run(c, steady(96, 100))
d = run(c, [80], start=1100.0)[0]
test("hold 중 평균이 아직 높으면(섞여서) 즉시 안 풀림", d.hold)

# 복귀
c = gt.Controller(CFG)
run(c, steady(94, 40))   # cool
ds = run(c, steady(86, 600), start=1100.0)
lv = [d.level for d in ds]
first_up = next(i for i, l in enumerate(lv) if l > 1)
test("cool 에서 식으면 한 단계씩 올라가 상한(full)까지 복귀", lv[0] == 1 and lv[-1] == 3 and 2 in lv)
test("복귀는 cool 진입(t=1000) 후 300초 이상 지나서", 1100 + first_up * 10 - 1000 >= 300, f"i={first_up}")
test("두 번째 올림도 직전 변경 후 300초 이상", 1100 + lv.index(3) * 10 - (1100 + first_up * 10) >= 300)
c = gt.Controller(CFG)
run(c, steady(94, 40))
ds = run(c, steady(90, 900), start=1100.0)
test("히스테리시스: 평균 90(88.5~91 사이)이면 올리지도 내리지도 않음", all(d.level == 1 for d in ds), str({d.level for d in ds}))
c = gt.Controller(CFG)
run(c, steady(94, 40))
ds = run(c, steady(80, 150) + steady(91.5, 200), start=1100.0)
test("30초 평균이 91 이상이면 5분 평균이 낮고 체류가 끝나도 안 올림", all(d.level == 1 for d in ds), str([d.level for d in ds]))

# 상한
c = gt.Controller(CFG)
ds = run(c, steady(80, 2000), ceiling=2)
test("ceiling=normal: 아무리 시원해도 full 로 안 감", all(d.level == 2 for d in ds))
c = gt.Controller(CFG)
run(c, steady(80, 600), ceiling=3)
run(c, steady(93.5, 40), ceiling=3, start=1600.0)
ds = run(c, steady(89, 100), ceiling=3, start=1700.0)
test("ceiling=full 이어도 온도 단계(cool)보다 위로 안 감", all(d.level == 1 for d in ds))
c = gt.Controller(CFG)
run(c, steady(80, 100), ceiling=3)
d = run(c, [80], ceiling=2, start=1200.0)[0]
test("상한이 full -> low 로 바뀌면 바로 normal", d.level == 2)
ds = run(c, steady(80, 400), ceiling=3, start=1300.0)
test("low -> full 로 상한이 올라가면 300초 체류 후 full 복귀", ds[0].level == 2 and ds[-1].level == 3 and ds[19].level == 2, str([d.level for d in ds[::5]]))
test("control.json 해석: low->2 / full->3 / 없음->2", gt.read_ceiling({"low": True}) == 2 and gt.read_ceiling({"low": False}) == 3 and gt.read_ceiling(None) == 2 and gt.read_ceiling({}) == 2)

# 50분 넘은 솔브
c = gt.Controller(CFG)
now0 = 100000.0
solves = [(11, now0 - 3100), (12, now0 - 600), (13, None)]
d = c.step(now0, 94, 3, solves)   # cool
test("cool: 오래된 솔브만 normal, 나머지는 cool", d.level == 1 and d.cpus_by_pid == {11: NORMAL, 12: COOL, 13: COOL}, str(d.cpus_by_pid))
for i in range(1, 8):
    d = c.step(now0 + i * 10, 97, 3, solves)
test("hold: 오래된 솔브는 여전히 normal", d.hold and d.cpus_by_pid[11] == NORMAL and d.cpus_by_pid[12] == COOL)
c = gt.Controller(CFG)
d = c.step(now0, 80, 3, solves)
test("full: 모든 솔브 full 집합", set(d.cpus_by_pid.values()) == {FULL})

# 온도 읽기 실패
c = gt.Controller(CFG)
run(c, steady(80, 100))
ds = run(c, [None, None], start=1100.0)
test("읽기 실패 2번까지는 단계 유지", ds[-1].level == 3)
d = run(c, [None], start=1120.0)[0]
test("읽기 실패 3번 연속 -> cool + hold=false", d.level == 1 and not d.hold and "실패" in d.note, d.note)
c = gt.Controller(CFG)
run(c, steady(97, 100))
d = run(c, [None] * 3, start=1100.0)[-1]
test("hold 중 읽기 실패 3번 -> hold 해제(배정은 하되 cool)", d.level == 1 and not d.hold)
c = gt.Controller(CFG)
run(c, [None, None])
d = run(c, [80], start=1020.0)[0]
d2 = run(c, [None, None, 80], start=1030.0)[-1]
test("실패가 끊기면 횟수 초기화", d2.level == 3)

# 환경변수 덮어쓰기
cfg2 = gt.load_config({"GTO_THERMAL_SLOW_AVG5": "85", "GTO_THERMAL_FAIL_MAX": "1", "GTO_THERMAL_UP_DWELL": "bad"}, NCPU=12)
test("환경변수 덮어쓰기(숫자 아니면 무시)", cfg2["SLOW_AVG5"] == 85.0 and cfg2["FAIL_MAX"] == 1 and cfg2["UP_DWELL"] == 300.0)
test("환경변수로 임계 낮추면 반영", run(gt.Controller(cfg2), steady(87, 130))[-1].level == 2)

# --- 파일 / IO 계층 (임시 폴더, 가짜 IO) ---
TMP = tempfile.mkdtemp(prefix="gto-thermal-test-")


class FakeIO:
    def __init__(self, temps, solvers):
        self.temps, self.solvers = list(temps), solvers   # solvers: {pid: [tid,...]}
        self.aff = {t: set(range(12)) for ts in solvers.values() for t in ts}
        self.set_calls = []
        self.notsolver = set()

    def read_temp(self):
        return self.temps.pop(0) if self.temps else None

    def read_power(self):
        return 33.0

    def is_solver(self, pid):
        return pid in self.solvers and pid not in self.notsolver

    def tids(self, pid):
        return self.solvers.get(pid, [])

    def getaff(self, tid):
        return self.aff[tid]

    def setaff(self, tid, cpus):
        self.set_calls.append((tid, tuple(sorted(cpus))))
        self.aff[tid] = set(cpus)

    def ticks(self, pid):
        return 1000


try:
    solves_dir = os.path.join(TMP, "solves")
    state = os.path.join(TMP, "state")
    os.makedirs(solves_dir)
    t0 = 500000.0
    json.dump({"workers": [{"solver_pid": 9001, "t0": t0 - 100}, {"solver_pid": 9002, "t0": t0 - 3500}, {"solver_pid": 9003, "t0": t0}, None]},
              open(os.path.join(solves_dir, "progress.json"), "w"))
    json.dump({"low": False}, open(os.path.join(solves_dir, "control.json"), "w"))

    # dry-run: 선호도·파일 없음
    fio = FakeIO([97] * 20, {9001: [9001, 9101], 9002: [9002]})
    r = gt.Runner(solves_dir, fio, CFG, state, dry_run=True)
    with redirect_stdout(_io.StringIO()):
        for i in range(8):
            d = r.cycle(t0 + i * 10)
        r.finish(t0 + 100)
    test("dry-run: setaffinity 호출 없음", fio.set_calls == [])
    test("dry-run: thermal.json·csv 안 씀", not os.path.exists(os.path.join(solves_dir, "thermal.json")) and not os.path.exists(state))
    test("dry-run: 그래도 판단은 함(hold)", d.hold)

    # 실제 모드 (가짜 IO)
    fio = FakeIO([97] * 8 + [80] * 3, {9001: [9001, 9101], 9002: [9002], 9003: [9003]})
    fio.notsolver.add(9003)   # console_solver 가 아닌 pid 재사용
    r = gt.Runner(solves_dir, fio, CFG, state, dry_run=False)
    buf = _io.StringIO()
    with redirect_stdout(buf):
        d = r.cycle(t0)
    test("cool 적용: 새 솔브 [0,2,4], 50분 넘은 건 normal", fio.aff[9001] == {0, 2, 4} and fio.aff[9101] == {0, 2, 4} and fio.aff[9002] == set(NORMAL), str(fio.aff))
    test("console_solver 가 아닌 pid 는 안 건드림", 9003 not in [c[0] for c in fio.set_calls])
    n = len(fio.set_calls)
    with redirect_stdout(buf):
        r.cycle(t0 + 10)
    test("이미 같은 값이면 setaffinity 생략", len(fio.set_calls) == n)
    th = json.load(open(os.path.join(solves_dir, "thermal.json")))
    test("thermal.json 형식", set(th) == {"level", "name", "hold", "temp", "avg5m", "updated"} and th["name"] == "cool" and th["hold"] is False and th["level"] == 1, str(th))
    test("thermal.json tmp 가 남지 않음", not [f for f in os.listdir(solves_dir) if f.endswith(".tmp")])
    with redirect_stdout(buf):
        for i in range(2, 8):
            r.cycle(t0 + i * 10)
    th = json.load(open(os.path.join(solves_dir, "thermal.json")))
    test("hold 시 thermal.json hold=true, level 0", th["hold"] is True and th["level"] == 0 and th["name"] == "hold", str(th))
    with redirect_stdout(buf):
        r.finish(t0 + 200)
    th = json.load(open(os.path.join(solves_dir, "thermal.json")))
    test("종료: hold=false 로 씀", th["hold"] is False and th["level"] == 3)
    test("종료: 선호도가 상한(full) 집합으로 복원", fio.aff[9001] == set(FULL) and fio.aff[9002] == set(FULL))
    test("단계 변경이 stdout 에 한 줄", "단계" in buf.getvalue() and "cool" in buf.getvalue())

    # 러너 쪽 CSV
    rows = open(os.path.join(state, "thermal.csv")).read().strip().split("\n")
    test("CSV 헤더와 행(9열)", rows[0] == gt.CsvLog.HEADER.strip() and len(rows) == 9 and all(len(x.split(",")) == 9 for x in rows), f"{len(rows)}")

    # CSV 순환·보관
    sd = os.path.join(TMP, "rot")
    os.makedirs(sd)
    log = gt.CsvLog(sd, 14)
    day = lambda n: time.time() - n * 86400
    old = os.path.join(sd, "thermal-%s.csv" % time.strftime("%Y%m%d", time.localtime(day(20))))
    keep = os.path.join(sd, "thermal-%s.csv" % time.strftime("%Y%m%d", time.localtime(day(3))))
    base = os.path.join(sd, "baseline.csv")
    for p in (old, keep, base):
        open(p, "w").write("x\n")
    open(log.path, "w").write(gt.CsvLog.HEADER + "1,2,3,4,5,6,7,8,9\n")
    os.utime(log.path, (day(1), day(1)))
    log.write(time.time(), "a,b,c,d,e,f,g,h,i")
    yd = os.path.join(sd, "thermal-%s.csv" % time.strftime("%Y%m%d", time.localtime(day(1))))
    test("하루 지난 CSV 는 thermal-YYYYMMDD.csv 로 순환", os.path.exists(yd) and len(open(log.path).read().strip().split("\n")) == 2)
    test("14일 넘은 순환 파일만 삭제(baseline.csv 보존)", not os.path.exists(old) and os.path.exists(keep) and os.path.exists(base))

    # 입력 해석
    test("read_solves: 중복·None·잘못된 항목 무시", gt.read_solves({"workers": [{"solver_pid": 5, "t0": 1}, {"solver_pid": 5}, None, {"solver_pid": "x"}]}) == [(5, 1)])
    test("read_solves: workers 가 없으면 current", gt.read_solves({"current": {"solver_pid": 7, "t0": 2}}) == [(7, 2)])
    tf = os.path.join(TMP, "temp")
    open(tf, "w").write("91.5\n")
    sio = gt.SystemIO(tf)
    test("GTO_THERMAL_TEMP_FILE 주입", sio.read_temp() == 91.5)
    open(tf, "w").write("")
    test("온도 파일이 비면 None", sio.read_temp() is None)
    test("is_solver: 자기 자신(python)과 없는 pid 는 거부", not sio.is_solver(os.getpid()) and not sio.is_solver(2 ** 22 + 5) and not sio.is_solver("x"))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

fails = 0
for st, name, info in results:
    print(f"[{st}] {name}" + (f"  {info}" if info and st == "FAIL" else ""))
    fails += st == "FAIL"
print(f"\n{len(results) - fails}/{len(results)} 통과")
sys.exit(1 if fails else 0)
