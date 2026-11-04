"""python test_build_gto_safety.py  - 러너의 PID 안전(잠금, kill, 우선순위) 시험.
임시 PREFLOP_TOOLS 만 쓰고 가짜 프로세스(sleep)만 띄운다. 실제 solves/ 와 실행 중인 러너는 건드리지 않는다."""
import ast
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="gto-safety-")
os.environ["PREFLOP_TOOLS"] = TMP
os.environ.pop("GTO_LIB_DIR", None)
sys.path.insert(0, HERE)
results = []


def test(name, ok, info=""):
    results.append(("PASS" if ok else "FAIL", name, info))


# 임포트 전에 코드로 확인: 모듈 최상위(함수·클래스 밖)에서 파일을 쓰거나 지우는 호출이 없다
tree = ast.parse(open(os.path.join(HERE, "build_gto_lib.py"), encoding="utf-8").read())
bad = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        continue
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if nm in ("open", "makedirs", "remove", "unlink", "write_json", "rename", "replace", "rmtree", "mkdir", "kill", "system", "Popen", "run"):
                bad.append(nm)
test("임포트만으로 쓰기·종료 호출 없음(최상위 코드)", not bad, str(bad))

import build_gto_lib as bl   # noqa: E402

test("모듈 경로가 임시 PREFLOP_TOOLS 아래", all(os.path.realpath(p).startswith(os.path.realpath(TMP)) for p in (bl.SOLVES, bl.PROGRESS, bl.LOCK, bl.CONTROL, bl.STOP_FILE)))
test("임포트가 임시 폴더에도 아무것도 만들지 않음", not os.path.exists(bl.SOLVES))

# --- 큐 순서 (order_queue) ---
_f = [(0, "A", 30, "f1"), (1, "B", 30, "f1"), (1, "B", 30, "f2"), (2, "C", 20, "f1")]
_s = [(0, "X", 30, "f1"), (0, "X", 30, "f2"), (1, "Y", 20, "f1")]
_q = bl.order_queue(_f, _s)
test("큐: 같은 단계의 fresh 가 stale 보다 앞", _q.index(_f[0]) < _q.index(_s[0]) and _q.index(_f[2]) < _q.index(_s[2]))
test("큐: 앞 단계 stale 이 뒷 단계 fresh 보다 앞", _q.index(_s[0]) < _q.index(_f[1]) and _q.index(_s[1]) < _q.index(_f[3]))
test("큐: 단계 안 순서 유지 + 항목 보존",
     _q == [_f[0], _s[0], _s[1], _f[1], _f[2], _s[2], _f[3]] and bl.order_queue([], []) == [] and bl.order_queue(_f, []) == _f)
_rs = bl.current_ranges()
_narrow = bl.narrow_spots(_rs)
test("좁은 스팟: BB 방어 수정 뒤 BTN_SB 20BB 만 남음", _narrow == {("BTN_SB", 20)}, str(sorted(_narrow)))

if bl.WINDOWS or not os.path.isdir("/proc/self"):
    print("[SKIP] /proc 없음")
    sys.exit(0)

procs = []


def spawn(argv, **kw):
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    procs.append(p)
    time.sleep(0.15)
    return p


def state(pid):
    return os.getpriority(os.PRIO_PROCESS, pid), sorted(os.sched_getaffinity(pid))


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def alive(p):
    return p.poll() is None


def exits(fn):
    try:
        quiet(fn)
    except SystemExit as e:
        return e.code
    return None


try:
    os.makedirs(bl.SOLVES)
    sleep_bin = shutil.which("sleep")
    fake_dir = os.path.join(TMP, "TexasSolver")
    os.makedirs(fake_dir)
    solver_bin = os.path.join(fake_dir, "console_solver")
    shutil.copy(sleep_bin, solver_bin)
    os.chmod(solver_bin, 0o755)
    runner_py = os.path.join(TMP, "build_gto_lib.py")
    open(runner_py, "w").write("import time\ntime.sleep(300)\n")

    # 판정 함수
    other = spawn([sleep_bin, "300"])
    solver = spawn([solver_bin, "300"])
    runner = spawn([sys.executable, runner_py])
    test("is_solver_pid: sleep 은 거부", not bl.is_solver_pid(other.pid))
    test("is_solver_pid: console_solver 는 통과", bl.is_solver_pid(solver.pid))
    test("is_runner_pid: sleep 은 거부", not bl.is_runner_pid(other.pid))
    test("is_runner_pid: build_gto_lib 러너는 통과", bl.is_runner_pid(runner.pid))
    test("is_runner_pid: console_solver 는 거부", not bl.is_runner_pid(solver.pid))
    test("없는 pid/None/쓰레기 값은 거부", not any(f(x) for f in (bl.is_runner_pid, bl.is_solver_pid) for x in (None, 0, "x", 2 ** 22 + 99)))

    # 잠금
    bl.write_json(bl.LOCK, {"pid": other.pid})
    test("낡은 잠금(다른 프로그램이 pid 재사용): 통과하고 내 pid 로 갱신",
         exits(bl.acquire_lock) is None and bl.read_json(bl.LOCK)["pid"] == os.getpid())
    dead = spawn([sleep_bin, "0"])
    dead.wait()
    bl.write_json(bl.LOCK, {"pid": dead.pid})
    test("죽은 pid 잠금: 통과", exits(bl.acquire_lock) is None)
    bl.write_json(bl.LOCK, {"pid": runner.pid})
    test("살아 있는 진짜 러너 잠금: 거부(exit 1)", exits(bl.acquire_lock) == 1 and bl.read_json(bl.LOCK)["pid"] == runner.pid)
    bl.write_json(bl.LOCK, {"pid": os.getpid()})
    test("자기 자신 pid 잠금: 통과", exits(bl.acquire_lock) is None)
    os.remove(bl.LOCK)
    test("잠금 없음: 통과", exits(bl.acquire_lock) is None)

    # progress 에 가짜 pid(sleep)와 진짜 대상(console_solver)을 둘 다 넣는다
    # thermal_hold: 제어기가 죽거나 파일이 이상해도 러너는 멈추지 않는다
    test("thermal_hold: 파일 없음 -> False", bl.thermal_hold() is False)
    bl.write_json(bl.THERMAL, {"hold": True, "updated": time.time() - bl.THERMAL_STALE - 30})
    test("thermal_hold: hold=true 라도 오래됨 -> False", bl.thermal_hold() is False)
    with open(bl.THERMAL, "w") as f:
        f.write("{깨진 json")
    test("thermal_hold: 깨진 파일 -> False", bl.thermal_hold() is False)
    bl.write_json(bl.THERMAL, {"hold": True})
    test("thermal_hold: updated 없음 -> False", bl.thermal_hold() is False)
    bl.write_json(bl.THERMAL, {"hold": True, "updated": "abc"})
    test("thermal_hold: updated 가 숫자 아님 -> False", bl.thermal_hold() is False)
    bl.write_json(bl.THERMAL, {"hold": False, "updated": time.time()})
    test("thermal_hold: 새것 + hold=false -> False", bl.thermal_hold() is False)
    bl.write_json(bl.THERMAL, {"hold": True, "updated": time.time()})
    test("thermal_hold: 새것 + hold=true -> True", bl.thermal_hold() is True)
    bl.write_json(bl.THERMAL, {"hold": True, "updated": time.time() + 3600})
    test("thermal_hold: 미래 시각(시계 이상) -> False", bl.thermal_hold() is False)
    os.remove(bl.THERMAL)

    before = state(other.pid)
    bl.write_json(bl.PROGRESS, {"workers": [{"solver_pid": other.pid}], "current": {"solver_pid": other.pid}})
    quiet(bl.set_mode, "low")
    test("set_mode(low): sleep 의 nice·affinity 그대로", state(other.pid) == before, f"{before} -> {state(other.pid)}")
    quiet(bl.set_mode, "full")
    test("set_mode(full): sleep 의 nice·affinity 그대로", state(other.pid) == before)
    quiet(bl.stop, True)
    test("stop(now): sleep 은 살아 있음", alive(other))
    test("stop(now): STOP 파일은 임시 폴더에 생성", os.path.exists(bl.STOP_FILE))

    sb = state(solver.pid)
    bl.write_json(bl.PROGRESS, {"workers": [{"solver_pid": solver.pid}]})
    quiet(bl.set_mode, "low")
    nice, aff = state(solver.pid)
    test("set_mode(low): console_solver 는 nice 10 + affinity 축소", nice == bl.POSIX_LOW_NICE and len(aff) == bl.low_threads() and len(aff) < len(sb[1]) + 1, f"{sb} -> {(nice, aff)}")
    quiet(bl.set_mode, "full")
    test("set_mode(full): console_solver affinity 복원", len(state(solver.pid)[1]) == bl.CPU)

    # 혼합: sleep 은 살고 console_solver 만 죽는다
    bl.write_json(bl.PROGRESS, {"workers": [{"solver_pid": other.pid}, {"solver_pid": solver.pid}]})
    quiet(bl.stop, True)
    for _ in range(30):
        if not alive(solver):
            break
        time.sleep(0.1)
    test("stop(now): 진짜 console_solver 는 kill -9", not alive(solver) and solver.returncode == -9)
    test("stop(now): 같이 적힌 sleep 은 여전히 살아 있음", alive(other))
finally:
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()
    shutil.rmtree(TMP, ignore_errors=True)

fails = 0
for st, name, info in results:
    print(f"[{st}] {name}" + (f"  {info}" if info and st == "FAIL" else ""))
    fails += st == "FAIL"
print(f"\n{len(results) - fails}/{len(results)} 통과")
sys.exit(1 if fails else 0)
