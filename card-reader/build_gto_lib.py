"""TexasSolver 배치 러너 → gto_lib/ 갱신 (재개 가능, 우선순위 큐).

사용
  python build_gto_lib.py                 배치 시작/재개 (끝난 솔브는 건너뜀). 기본: 모든 스레드, 보통 우선순위, 워커 2
  python build_gto_lib.py --workers 3     동시에 푸는 솔브 수 (full: 워커마다 전체 스레드, low: 나눠 씀 10 → 5+5)
  python build_gto_lib.py --low           낮 시간용: 우선순위 '보통 이하' + 전체 스레드 cpu-2 (워커들이 나눠 씀)
  python build_gto_lib.py --set-mode low  돌고 있는 배치를 낮 모드로 (다음 솔브부터 스레드 cpu-2,
                                          지금 솔버들은 우선순위를 낮추고 논리 CPU cpu-2 개 안에서만 돌게)
  python build_gto_lib.py --set-mode full [--workers N]  다시 전체 속도 (워커 수도 바꿀 수 있음, 시작 때 수 이하)
  python build_gto_lib.py --status        진행 (완료/전체, 평균 시간, ETA, 현재 작업)
  python build_gto_lib.py --stop [--now]  지금 솔브가 끝나면 멈춤 (--now: 솔버도 바로 종료). 다시 시작하면 이어서 한다
  python build_gto_lib.py --check-ranges  preflop.py 레인지가 바뀌어 다시 풀어야 하는 스팟 목록
  python build_gto_lib.py --rebuild       solves/ 의 압축 결과로 gto_lib/*.json.gz 전부 다시 만들기
  python build_gto_lib.py --pilot BTN_BB 40 As7h2d,Ks7h2d   지정 플랍만 풀기 (파이프라인 확인용)
  python build_gto_lib.py --eval-mapping  풀린 플랍끼리 서로 근사했을 때의 전략 오차 측정
  python build_gto_lib.py --extract-turn  원본 덤프(.json.gz)에서 턴 노드 다시 추출

워커: 6코어/12스레드에서 솔버 하나는 12스레드를 다 못 쓴다 (트리 생성·덤프는 거의 단일 스레드, CFR 도 선형 확장이 아님).
솔브 2개를 동시에 돌리고 (측정: 1×12 대비 CFR 속도 ≈ 1.19배, 3개는 1.17배), 덤프 읽기·압축·라이브러리 갱신은
다음 솔브와 겹쳐서 한다.
RAM 은 (스팟, 스택) 별 솔버 최대 메모리를 기록해 두고 합이 MEM_BUDGET_GB 를 넘지 않게 시작을 미룬다.

레인지가 바뀌면 (preflop.py 수정 후) 그냥 다시 실행하면 된다: 러너는 시작할 때 현재 레인지를 읽고,
range_hash 가 다른 솔브는 '안 끝난 것'으로 보고 우선순위 순서대로 다시 푼다 (그동안 예전 결과는 stale 로 계속 쓰인다).

파일
  preflop-tools/solves/<spot>_<stack>bb/<flop>.txt/.log/.json.gz(원본 덤프, 턴 포함)/.compact.json/.turn.json.gz
  preflop-tools/solves/progress.json      진행 기록
  card-reader/gto_lib/<spot>_<stack>bb.json.gz        플랍 라이브러리 (솔브가 끝날 때마다 갱신)
  card-reader/gto_lib/turn/<spot>_<stack>bb/<flop>.json.gz   턴 노드 (선택)
"""
import argparse
import ctypes
import datetime as dt
import glob
import gzip
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import solverlib as sl

SOLVES = sl.SOLVES_DIR
PROGRESS = os.path.join(SOLVES, "progress.json")
STOP_FILE = os.path.join(SOLVES, "STOP")
LOCK = os.path.join(SOLVES, "runner.lock")
CONTROL = os.path.join(SOLVES, "control.json")
EXE = os.path.join(sl.SOLVER_DIR, "console_solver.exe")
CPU = os.cpu_count() or 4
BELOW_NORMAL, NORMAL = 0x00004000, 0x00000020
CREATE_NO_WINDOW = 0x08000000
JOB_TIMEOUT = 75 * 60
DEFAULT_ACCURACY = 0.8
DEFAULT_MAX_ITER = 200
DUMP_ROUNDS = 2          # 턴까지 덤프 (턴 노드 추출용). 원본은 gzip 으로 보관
DEFAULT_WORKERS = 2      # 동시에 돌리는 솔브 수. Ryzen 5 3600 측정: 2개가 1×12 보다 CFR 처리량 ≈ 1.19배, 3개 1.17배

# 우선순위 계획: (스팟들, 스택들, 플랍 서브셋 구간).  같은 단계 안에서는 플랍 순서대로 스팟을 번갈아 푼다.
PRI = ["BTN_BB", "CO_BB"]
SB = ["SB_BB"]
EARLY = ["HJ_BB", "UTG_BB"]
ALL = PRI + SB + EARLY + ["BTN_SB"]
# 한 솔브 ≈ 10분 (12스레드, 0.8%) → 하룻밤 ≈ 50~60개. 앞 단계는 8개씩 끊어 우선 스팟에 고르게.
PLAN = [
    (PRI, (40, 30), (0, 8)),
    (SB, (40, 30), (0, 8)),
    (PRI, (40, 30), (8, 16)),
    (EARLY, (40, 30), (0, 8)),
    (SB, (40, 30), (8, 16)),
    (PRI, (40, 30), (16, 25)),
    (["BTN_SB"], (40, 30), (0, 8)),
    (PRI + SB, (20, 60), (0, 8)),
    (EARLY, (40, 30), (8, 16)),
    (SB, (40, 30), (16, 25)),
    (PRI, (40, 30), (25, 49)),
    (EARLY + ["BTN_SB"], (40, 30), (8, 25)),
    (PRI + SB, (20, 60), (8, 25)),
    (SB + EARLY + ["BTN_SB"], (40, 30), (25, 49)),
    (EARLY + ["BTN_SB"], (20, 60), (0, 25)),
    (ALL, (20, 60), (25, 49)),
    (PRI, (40, 30), (49, 95)),
    (SB + EARLY + ["BTN_SB"], (40, 30), (49, 95)),
    (ALL, (20, 60), (49, 95)),
    (ALL, (40, 30, 20, 60), (95, 150)),
    (ALL, (40, 30, 20, 60), (150, 200)),
]


def jobs():
    """우선순위 순서의 (tier, spot, stack, flop) 목록 (중복 없음)."""
    out, seen = [], set()
    for tier, (spots, stacks, (a, b)) in enumerate(PLAN):
        for flop in sl.SUBSET[a:b]:
            for stack in stacks:
                for spot in spots:
                    k = (spot, stack, flop)
                    if k not in seen:
                        seen.add(k)
                        out.append((tier, spot, stack, flop))
    return out


def job_dir(spot, stack):
    return os.path.join(SOLVES, f"{spot}_{stack}bb")


def compact_path(spot, stack, flop):
    return os.path.join(job_dir(spot, stack), f"{flop}.compact.json")


def read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _replace(tmp, p, tries=20):
    """os.replace — Windows 에서 다른 프로세스가 대상 파일을 읽는 순간이면 PermissionError 가 나므로 잠깐 뒤 다시."""
    for k in range(tries):
        try:
            return os.replace(tmp, p)
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(0.1)


def write_json(p, data, gz=False):
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"   # 워커 스레드끼리 tmp 가 겹치지 않게
    if gz:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(data, f, separators=(",", ":"))
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    _replace(tmp, p)


def current_ranges():
    """preflop.py 를 다시 읽어 지금 레인지 (스팟, 스택) → (rngs, hash)."""
    try:
        import preflop
        importlib.reload(preflop)
    except Exception as e:   # 다른 작업이 편집 중이면 잠시 깨져 있을 수 있다
        print("preflop reload 실패:", e)
    out = {}
    for spot in sl.SPOTS:
        for stack in sl.STACKS:
            r = sl.spot_ranges(spot, stack)
            out[(spot, stack)] = (r, sl.range_hash(r))
    return out


MIN_RANGE_PCT = 4.0   # 콜 레인지가 이보다 좁으면 (예: 20BB 에서 BB 가 거의 3벳 잼/폴드) 그 스팟은 풀지 않는다


def narrow_spots(rs):
    out = set()
    for (spot, stack), (r, _h) in rs.items():
        for side in ("ip", "oop"):
            pct = sum(w * (6 if len(h) == 2 else 4 if h.endswith("s") else 12) for h, w in r[side].items()) / 1326 * 100
            if pct < MIN_RANGE_PCT:
                out.add((spot, stack))
    return out


def is_done(spot, stack, flop, rh):
    c = read_json(compact_path(spot, stack, flop))
    return bool(c) and c.get("rh") == rh and c.get("th") == sl.tree_hash()


# --- 프로세스 유틸 (Windows) -----------------------------------------------------------
def pid_alive(pid):
    if not pid:
        return False
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000 | 0x1000, False, int(pid))   # SYNCHRONIZE | QUERY_LIMITED
        if not h:
            return False
        r = k.WaitForSingleObject(h, 0)
        k.CloseHandle(h)
        return r == 0x102   # WAIT_TIMEOUT → 아직 살아 있음
    except Exception:
        return False


def low_threads():
    return max(1, CPU - 2)


def set_priority(pid, low, affinity=True):
    """우선순위 + (affinity=True 면) CPU 선호도. low 면 마지막 논리 CPU 2개를 비워 둔다
    → 이미 돌고 있는 솔버도 스레드를 바꾸지 않고 바로 cpu-2 개 안에서만 돈다."""
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x0200 | 0x1000, False, int(pid))   # SET_INFORMATION | QUERY_LIMITED
        if h:
            k.SetPriorityClass(h, BELOW_NORMAL if low else NORMAL)
            if affinity and CPU <= 64:
                mask = (1 << (low_threads() if low else CPU)) - 1
                k.SetProcessAffinityMask(h, ctypes.c_size_t(mask))
            k.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def split_threads(total, n):
    """전체 스레드를 워커 n 개에 나눔 (앞 워커가 나머지를 가져감). 스레드보다 워커가 많으면 뒤 워커는 0 (쉼)."""
    n = max(1, n)
    return [total // n + (1 if i < total % n else 0) for i in range(n)]


def worker_threads(total, low, n):
    """워커별 솔버 스레드 수.
    full: 워커마다 전체 스레드 (과다 할당). 둘 다 CFR 중이면 6+6 과 같은 속도로 측정됐고, 한쪽이 트리 생성·덤프
          (거의 단일 스레드) 중일 때 다른 쪽이 CPU 를 다 쓴다.
    low : 전체 스레드를 나눔 (cpu-2 → 5+5) + 솔버들을 논리 CPU cpu-2 개에 묶음 → 사용자 몫 2개는 항상 빈다."""
    return split_threads(total, n) if low or n <= 1 else [total] * n


def mode():
    """control.json → (전체 threads, low, workers)."""
    c = read_json(CONTROL, {}) or {}
    low = bool(c.get("low"))
    threads = int(c.get("threads") or (low_threads() if low else CPU))
    workers = max(1, int(c.get("workers") or DEFAULT_WORKERS))
    return threads, low, workers


# --- 솔브 1회 -------------------------------------------------------------------------
def parse_log(text):
    it = re.findall(r"Iter: (\d+)", text)
    ex = re.findall(r"Total exploitability (-?[0-9.]+)", text)
    tm = re.findall(r"time used: ([0-9.]+)", text)
    return (int(it[-1]) if it else None, float(ex[-1]) if ex else None, float(tm[-1]) if tm else None)


def solver_cmd(inp):
    return [EXE, "-i", inp]


def solve_proc(spot, stack, flop, rngs, threads, low, accuracy, max_iter, progress_cb=None):
    """솔버 프로세스만 돌림 → 원본 덤프 경로 + 로그 정보. (덤프 읽기/압축은 post_solve 에서, 다음 솔브와 겹쳐서)"""
    d = job_dir(spot, stack)
    os.makedirs(d, exist_ok=True)
    raw = os.path.join(d, f"{flop}.json")
    inp = os.path.join(d, f"{flop}.txt")
    logp = os.path.join(d, f"{flop}.log")
    with open(inp, "w", encoding="utf-8") as f:
        f.write(sl.solver_input(spot, stack, flop, raw, threads, rngs, accuracy=accuracy, max_iter=max_iter,
                                dump_rounds=DUMP_ROUNDS))
    if os.path.exists(raw):
        os.remove(raw)
    t0 = time.time()
    with open(logp, "w", encoding="utf-8") as lf:
        p = subprocess.Popen(solver_cmd(inp), cwd=sl.SOLVER_DIR, stdout=lf, stderr=subprocess.STDOUT,
                             creationflags=(BELOW_NORMAL if low else NORMAL) | CREATE_NO_WINDOW)
        if low:   # 낮 모드: 다른 워커와 함께 마지막 논리 CPU 2개는 비워 둔다
            set_priority(p.pid, True)
        if progress_cb:
            progress_cb(p.pid)
        try:
            p.wait(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise RuntimeError("timeout")
        mem = peak_mem_gb(getattr(p, "_handle", 0))   # 이 스팟·스택의 메모리 기록 (워커 스케줄링용)
    secs = time.time() - t0
    with open(logp, encoding="utf-8", errors="replace") as f:
        iters, expl, _tm = parse_log(f.read())
    if p.returncode != 0 or not os.path.exists(raw):
        raise RuntimeError(f"solver rc={p.returncode}, dump missing" if not os.path.exists(raw) else f"rc={p.returncode}")
    return {"raw": raw, "secs": round(secs, 1), "iters": iters, "exploit": expl, "threads": threads,
            "accuracy": accuracy, "mem_gb": mem}


def post_solve(spot, stack, flop, rh, s, extra=None):
    """원본 덤프 → .compact.json (+ .turn.json.gz). 원본은 gzip 보관. compact 를 쓰는 순간 '끝난 솔브'가 된다."""
    d = job_dir(spot, stack)
    raw = s["raw"]
    with open(raw, encoding="utf-8") as f:
        dump = json.load(f)
    c = sl.compact_dump(dump, spot, stack, flop)
    c.update(rh=rh, th=sl.tree_hash(), exploit=s["exploit"], iters=s["iters"], secs=s["secs"], threads=s["threads"],
             accuracy=s["accuracy"], solved_at=dt.datetime.now().isoformat(timespec="seconds"), **(extra or {}))
    try:
        turn = sl.compact_turn(dump, spot, stack, flop)
        write_json(os.path.join(d, f"{flop}.turn.json.gz"), {"rh": rh, "th": sl.tree_hash(), **turn}, gz=True)
    except Exception as e:  # 턴 추출 실패는 플랍 결과에 영향 없음
        print("  turn extract failed:", e, flush=True)
    del dump
    write_json(compact_path(spot, stack, flop), c)
    return c


def gzip_raw(raw):
    """원본 덤프는 gzip 으로 보관 (tmp 에 쓰고 교체 → 중간에 끊겨도 깨진 .gz 가 남지 않음)."""
    try:
        tmp = raw + ".gz.tmp"
        with open(raw, "rb") as fi, gzip.open(tmp, "wb", compresslevel=3) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        _replace(tmp, raw + ".gz")
        os.remove(raw)
    except OSError as e:
        print("  gzip failed:", e, flush=True)


def run_solve(spot, stack, flop, rngs, rh, threads, low, accuracy, max_iter, progress_cb=None):
    """솔브 1회 (순차). 배치 러너는 solve_proc / post_solve 를 나눠서 겹쳐 돌린다."""
    s = solve_proc(spot, stack, flop, rngs, threads, low, accuracy, max_iter, progress_cb)
    c = post_solve(spot, stack, flop, rh, s)
    th = threading.Thread(target=gzip_raw, args=(s["raw"],), daemon=False)
    th.start()
    return c, th


# --- 라이브러리 파일 ---------------------------------------------------------------------
def build_lib(spot, stack, rngs=None, rh=None):
    d = job_dir(spot, stack)
    th = sl.tree_hash()
    flops = {}
    for p in sorted(glob.glob(os.path.join(d, "*.compact.json"))):
        c = read_json(p)
        if not c or c.get("th") != th:
            continue
        flops[c["board"]] = {"nodes": c["nodes"], "exploit": c.get("exploit"), "rh": c.get("rh"),
                             "iters": c.get("iters")}
    if not flops:
        return None
    if rngs is None:
        rngs = sl.spot_ranges(spot, stack)
        rh = sl.range_hash(rngs)
    info = sl.spot_info(spot, stack)
    data = {
        "version": sl.LIB_VERSION, "spot": spot, "stack": stack, "pot": info["pot"], "behind": info["behind"],
        "ip": info["ip"], "oop": info["oop"], "open": info["open"], "ante_total": sl.ANTE_TOTAL, "scale": sl.SCALE,
        "range_hash": rh, "ranges": {"ip": sl.to_ts_range(rngs["ip"]), "oop": sl.to_ts_range(rngs["oop"])},
        "tree": sl.TREE, "tree_hash": th, "solver": "TexasSolver v0.2.0 (console_solver)",
        "built": dt.datetime.now().isoformat(timespec="seconds"), "flops": flops,
    }
    os.makedirs(sl.LIB_DIR, exist_ok=True)
    out = os.path.join(sl.LIB_DIR, sl.lib_name(spot, stack))
    write_json(out, data, gz=True)
    # 턴 파일
    tdir = os.path.join(sl.LIB_DIR, "turn", f"{spot}_{stack}bb")
    for p in glob.glob(os.path.join(d, "*.turn.json.gz")):
        dst = os.path.join(tdir, os.path.basename(p).replace(".turn.json.gz", ".json.gz"))
        if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(p):
            os.makedirs(tdir, exist_ok=True)
            shutil.copyfile(p, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
    return out


# --- 진행 / 상태 -------------------------------------------------------------------------
_PLOCK = threading.RLock()   # progress.json (워커 스레드들이 같이 씀)


def load_progress():
    return read_json(PROGRESS, {}) or {}


def save_progress(pr):
    with _PLOCK:
        pr["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        os.makedirs(SOLVES, exist_ok=True)
        write_json(PROGRESS, pr)


def _fmt_secs(s):
    s = int(s or 0)
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def eff_secs(c):
    """솔브 1개가 차지한 실효 시간 (워커 n 개가 동시에 돌았으면 secs / n)."""
    return (c.get("secs") or 0) / max(1, c.get("workers") or 1)


def time_model(cs):
    """끝난 compact 들 → (스팟, 스택) 별 평균 실효 시간 / 스택별 평균. 남은 작업 ETA 추정용."""
    by, st = {}, {}
    for (spot, stack), c in cs:
        e = eff_secs(c)
        if e > 0:
            by.setdefault((spot, stack), []).append(e)
            st.setdefault(stack, []).append(e)
    mean = lambda v: sum(v) / len(v)
    return {k: mean(v) for k, v in by.items()}, {k: mean(v) for k, v in st.items()}


def est_secs(model, spot, stack, default=300.0):
    by, st = model
    return by.get((spot, stack)) or st.get(stack) or (sum(st.values()) / len(st) if st else default)


def solver_pids(pr):
    """돌고 있는 솔버 pid 들 (워커별 current + 예전 형식 current)."""
    pids = [(w or {}).get("solver_pid") for w in (pr.get("workers") or [])]
    pids.append((pr.get("current") or {}).get("solver_pid"))
    return sorted({p for p in pids if p})


def status():
    rs = current_ranges()
    skip = narrow_spots(rs)
    js = [j for j in jobs() if (j[1], j[2]) not in skip]
    done, stale, per_tier = 0, 0, {}
    cs, left = [], []
    for tier, spot, stack, flop in js:
        c = read_json(compact_path(spot, stack, flop))
        t = per_tier.setdefault(tier, [0, 0, 0.0])
        t[1] += 1
        if c and c.get("th") == sl.tree_hash():
            cs.append(((spot, stack), c))
            if c.get("rh") == rs[(spot, stack)][1]:
                done += 1
                t[0] += 1
                continue
            stale += 1
        left.append((tier, spot, stack))
    total = len(js)
    pr = load_progress()
    alive = pid_alive(pr.get("runner_pid"))
    model = time_model(cs)
    for tier, spot, stack in left:
        per_tier[tier][2] += est_secs(model, spot, stack)
    rem = total - done
    print(f"GTO 라이브러리 배치: {done}/{total} 완료 ({done / total * 100:.1f}%)" + (f", 레인지 변경으로 다시 풀 것 {stale}" if stale else ""))
    eta = sum(t[2] for t in per_tier.values())
    if alive and pr.get("eff_secs") and pr.get("recent"):
        # 지금 러너의 최근 처리 속도로 보정 (모델은 예전 설정의 실효 시간)
        rec = pr["recent"][-12:]
        m = sum(est_secs(model, h["spot"], h["stack"]) for h in rec) / len(rec)
        scale = pr["eff_secs"] / m if m else 1.0
    else:
        scale = 1.0
    eta *= scale
    if eta:
        print(f"남은 {rem}개 ETA {_fmt_secs(eta)} (≈ {(dt.datetime.now() + dt.timedelta(seconds=eta)).strftime('%m-%d %H:%M')})"
              + (f", 최근 실효 {pr['eff_secs']:.0f}s/플랍" if alive and pr.get("eff_secs") else "")
              + " — 스팟·스택별 평균 시간으로 추정")
    print(f"러너: {'실행 중 (pid %s)' % pr.get('runner_pid') if alive else '멈춤'}"
          + (f", 모드 {'low' if pr.get('low') else 'full'} · 스레드 {pr.get('threads')} · 워커 {pr.get('nworkers', 1)}"
             if alive else ""))
    if alive:
        curs = [w for w in (pr.get("workers") or []) if w] or ([pr["current"]] if pr.get("current") else [])
        for cur in curs:
            el = time.time() - cur.get("t0", time.time())
            print(f"현재{' w%d' % cur['worker'] if 'worker' in cur else ''}: {cur['spot']} {cur['stack']}BB {cur['flop']} "
                  f"(스레드 {cur.get('threads', '?')}, {el / 60:.1f}분 경과)")
    left_acc = 0.0
    for tier, (a, b, s) in sorted(per_tier.items()):
        spots, stacks, rng = PLAN[tier]
        mark = "✓" if a == b else ("…" if a else " ")
        left_acc += s * scale
        eta = f"  ~{(dt.datetime.now() + dt.timedelta(seconds=left_acc)).strftime('%m-%d %H:%M')} 까지" if a < b else ""
        print(f"  {mark} 단계 {tier + 1:2d}: {a:3d}/{b:3d}  {'/'.join(spots)} {'/'.join(map(str, stacks))}BB "
              f"플랍 {rng[0] + 1}-{rng[1]}{eta}")
    if skip:
        print("레인지가 너무 좁아 건너뜀:", ", ".join(f"{s} {k}BB" for s, k in sorted(skip)))
    libs = sl.available(refresh=True)
    print("gto_lib:", ", ".join(f"{s} {sorted(v)}" for s, v in sorted(libs.items())) or "(없음)")
    if pr.get("failed"):
        print("실패:", ", ".join(f"{f['spot']} {f['stack']} {f['flop']} ({f['error']})" for f in pr["failed"][-5:]))


# --- 메모리 (워커 여러 개일 때 RAM 초과 방지) ---------------------------------------------------
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def avail_gb():
    try:
        m = _MemStatus()
        m.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1e9
    except Exception:
        return 99.0


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_mem_gb(handle):
    """끝난 (아직 핸들이 열린) 프로세스의 최대 메모리 (GB). 실패하면 None."""
    try:
        m = _PMC()
        m.cb = ctypes.sizeof(_PMC)
        ps = ctypes.WinDLL("psapi")
        ps.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
        if ps.GetProcessMemoryInfo(ctypes.c_void_p(int(handle)), ctypes.byref(m), m.cb):
            return round(max(m.PeakWorkingSetSize, m.PeakPagefileUsage) / 1e9, 2)
    except Exception:
        pass
    return None


MEM_BUDGET_GB = 26.0   # 솔버들 메모리 합 상한
MEM_RESERVE_GB = 3.0   # 새 솔브를 시작한 뒤에도 남겨 둘 여유 RAM (다른 프로그램 + 덤프 읽는 파이썬)
POST_MEM_GB = 2.0      # post_solve 가 원본 덤프를 읽는 동안 파이썬이 쓰는 메모리 (대략)


class MemModel:
    """(스팟, 스택) 별 솔버 최대 메모리 기록 → 새 솔브를 시작해도 되는지 판단."""

    def __init__(self, cs):
        self.by = {}
        for (spot, stack), c in cs:
            if c.get("mem_gb"):
                self.add(spot, stack, c["mem_gb"])

    def add(self, spot, stack, gb):
        k = (spot, stack)
        self.by[k] = max(self.by.get(k, 0), gb)

    def est(self, spot, stack):
        if (spot, stack) in self.by:
            return self.by[(spot, stack)] * 1.1
        same = [v for (s, k), v in self.by.items() if k == stack]   # 같은 스택의 다른 스팟 중 최대
        return max(same) * 1.2 if same else 4.0 + stack * 0.12      # 기록이 없으면 넉넉히 (60BB ≈ 11GB)


# --- 러너 ---------------------------------------------------------------------------------
def acquire_lock():
    old = read_json(LOCK, {}) or {}
    if pid_alive(old.get("pid")) and old.get("pid") != os.getpid():
        print(f"이미 실행 중 (pid {old.get('pid')}). --stop 으로 먼저 멈추세요.")
        sys.exit(1)
    os.makedirs(SOLVES, exist_ok=True)
    write_json(LOCK, {"pid": os.getpid(), "started": dt.datetime.now().isoformat(timespec="seconds")})


def run(args, only=None):
    """워커 n 개 (스레드 = 전체 스레드를 나눔) 가 우선순위 큐에서 차례로 가져가 푼다.
    솔버가 끝나면 덤프 읽기·압축·라이브러리 갱신은 백그라운드로 넘기고 그 워커는 바로 다음 솔브를 시작한다."""
    acquire_lock()
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    ctl = read_json(CONTROL, {}) or {}
    if args.low or args.full:
        ctl.update(low=bool(args.low), threads=args.threads)
    elif args.threads:
        ctl["threads"] = args.threads
    if getattr(args, "workers", None):
        ctl["workers"] = args.workers
    if args.low or args.full or args.threads or getattr(args, "workers", None):
        write_json(CONTROL, ctl)
    rs = current_ranges()
    js = only if only is not None else jobs()
    skip = narrow_spots(rs)
    if skip:
        print("레인지가 너무 좁아 건너뜀:", ", ".join(f"{s} {k}BB" for s, k in sorted(skip)))
    js = [j for j in js if (j[1], j[2]) not in skip]
    # 새로 풀 것 (우선순위 순) → 그 다음 레인지가 바뀐(stale) 예전 솔브 재솔브 (그동안 예전 결과를 계속 쓴다)
    fresh, stale, cs = [], [], []
    for j in js:
        c = read_json(compact_path(j[1], j[2], j[3]))
        if not c or c.get("th") != sl.tree_hash():
            fresh.append(j)
            continue
        cs.append(((j[1], j[2]), c))
        if c.get("rh") != rs[(j[1], j[2])][1]:
            stale.append(j)
    queue = fresh + stale
    mem = MemModel(cs)
    pr = load_progress()
    nmax = max(mode()[2], getattr(args, "workers", None) or 1)
    pr.update(runner_pid=os.getpid(), total=len(js), started=dt.datetime.now().isoformat(timespec="seconds"),
              accuracy=args.accuracy, max_iter=args.max_iter, failed=pr.get("failed", [])[-20:],
              workers=[None] * nmax, current=None, done_total=len(js) - len(queue))
    save_progress(pr)
    print(f"대기 {len(queue)} / 전체 {len(js)}, 워커 최대 {nmax}", flush=True)
    deadline = time.time() + args.hours * 3600 if args.hours else None
    qlock, plock, outlock = threading.Lock(), _PLOCK, threading.Lock()
    lib_locks = {}
    state = {"next": 0, "started": 0, "running_mem": {}, "done_times": []}
    hist = list(pr.get("recent", []))

    def say(*a):
        with outlock:
            print(*a, flush=True)

    def stopping():
        return os.path.exists(STOP_FILE) or (deadline and time.time() > deadline)

    def lib_lock(spot, stack):
        with qlock:
            return lib_locks.setdefault((spot, stack), threading.Lock())

    def publish(i, cur):
        with plock:
            pr["workers"][i] = cur
            act = [w for w in pr["workers"] if w]
            pr["current"] = act[0] if act else None
            save_progress(pr)

    def claim(i):
        """다음 작업 (우선순위 순). 메모리가 모자라면 다른 워커가 끝날 때까지 기다린다 → (n, job) / None."""
        while True:
            if stopping():
                return None
            with qlock:
                if state["next"] >= len(queue):
                    return None
                job = queue[state["next"]]
                g = mem.est(job[1], job[2])
                used = sum(state["running_mem"].values())
                others = len(state["running_mem"])
                fits = others == 0 or (used + g <= MEM_BUDGET_GB and avail_gb() - g >= MEM_RESERVE_GB)
                if fits:
                    n = state["next"]
                    state["next"] += 1
                    state["running_mem"][i] = g
                    return n, job
            time.sleep(3)

    def finish(i, n, job, s, nw):
        nonlocal hist
        tier, spot, stack, flop = job
        rngs, rh = rs[(spot, stack)]
        try:
            c = post_solve(spot, stack, flop, rh, s, {"workers": nw, "mem_gb": s.get("mem_gb")})
        except Exception as e:
            say(f"[w{i}] {spot} {stack}BB {flop} 후처리 실패:", e)
            with plock:
                pr.setdefault("failed", []).append({"spot": spot, "stack": stack, "flop": flop, "error": f"post: {e}"[:200],
                                                    "at": dt.datetime.now().isoformat(timespec="seconds")})
                save_progress(pr)
            return
        with lib_lock(spot, stack):
            try:
                build_lib(spot, stack, rngs, rh)
            except Exception as e:
                say(f"  build_lib 실패 ({spot} {stack}BB):", e)
        now = time.time()
        with plock:
            hist = (hist + [{"spot": spot, "stack": stack, "flop": flop, "secs": c["secs"], "exploit": c["exploit"],
                             "iters": c["iters"], "threads": c["threads"], "workers": nw, "mem_gb": c.get("mem_gb"),
                             "at": c["solved_at"]}])[-30:]
            dts = state["done_times"] = (state["done_times"] + [now])[-13:]
            recent = hist[-12:]
            if len(dts) >= 4:   # 실제 처리 간격 (워커 여러 개면 완료 간격)
                eff = (dts[-1] - dts[0]) / (len(dts) - 1)
            else:
                eff = sum(h["secs"] / max(1, h.get("workers") or 1) for h in recent) / len(recent)
            pr["done_total"] = pr.get("done_total", 0) + 1
            with qlock:
                left = len(queue) - state["next"] + len(state["running_mem"])
            pr.update(recent=hist, avg_secs=round(sum(h["secs"] for h in recent) / len(recent), 1),
                      eff_secs=round(eff, 1), eta_secs=round(left * eff),
                      eta_time=(dt.datetime.now() + dt.timedelta(seconds=left * eff)).isoformat(timespec="minutes"))
            save_progress(pr)
        gzip_raw(s["raw"])

    def work_one(i, post):
        """작업 하나 가져와서 풀기 → 새 후처리 스레드 (없으면 post 그대로). 큐가 끝났거나 멈출 때는 False."""
        with qlock:
            if state["next"] >= len(queue):
                return False, post
        threads_total, low, nw = mode()
        thr = worker_threads(threads_total, low, nw)[i] if i < nw else 0
        if thr <= 0:   # 이 워커는 지금 쉼 (--set-mode --workers 로 줄였을 때)
            time.sleep(5)
            return True, post
        got = claim(i)
        if got is None:
            return False, post
        n, job = got
        tier, spot, stack, flop = job
        rngs, rh = rs[(spot, stack)]
        cur = {"worker": i, "tier": tier + 1, "spot": spot, "stack": stack, "flop": flop, "t0": time.time(),
               "threads": thr}
        with plock:
            pr.update(threads=threads_total, low=low, nworkers=nw, done_this_run=n, queue_left=len(queue) - n)
        publish(i, cur)

        def on_start(pid, cur=cur):
            cur["solver_pid"] = pid
            publish(i, cur)
        say(f"[w{i} {n + 1}/{len(queue)}] {spot} {stack}BB {flop} 시작 (단계 {tier + 1}, 스레드 {thr}"
            f"{', low' if low else ''}, 메모리 예상 {state['running_mem'].get(i, 0):.1f}GB)")
        try:
            s = solve_proc(spot, stack, flop, rngs, thr, low, args.accuracy, args.max_iter, on_start)
        except Exception as e:  # 실패는 기록하고 다음으로
            say(f"[w{i}] {spot} {stack}BB {flop} 실패:", e)
            with plock:
                pr.setdefault("failed", []).append({"spot": spot, "stack": stack, "flop": flop, "error": str(e)[:200],
                                                    "at": dt.datetime.now().isoformat(timespec="seconds")})
                save_progress(pr)
            return True, post
        finally:
            with qlock:
                state["running_mem"].pop(i, None)
            publish(i, None)
        if s.get("mem_gb"):
            with qlock:
                mem.add(spot, stack, s["mem_gb"])
        say(f"[w{i} {n + 1}/{len(queue)}] {spot} {stack}BB {flop} {s['secs']:.0f}s, iter {s['iters']}, "
            f"expl {s['exploit']}%" + (f", {s['mem_gb']}GB" if s.get("mem_gb") else ""))
        if post:
            post.join()   # 워커당 후처리는 하나씩만 (메모리)
        post = threading.Thread(target=finish, args=(i, n, job, s, nw), daemon=False)
        post.start()
        return True, post

    def worker(i):
        post = None
        try:
            while not stopping():
                try:
                    more, post = work_one(i, post)
                except Exception as e:   # 예상 못 한 오류로 워커가 죽지 않게 (그 작업은 다음 실행 때 다시)
                    say(f"[w{i}] 워커 오류:", repr(e))
                    time.sleep(10)
                    continue
                if not more:
                    break
        finally:
            if post:
                post.join()

    ws = [threading.Thread(target=worker, args=(i,), daemon=False) for i in range(nmax)]
    for k, w in enumerate(ws):
        w.start()
        if k + 1 < len(ws):
            time.sleep(1)   # 시작 순서 = 우선순위 순서
    for w in ws:
        w.join()
    if os.path.exists(STOP_FILE):
        print("STOP 파일 → 종료", flush=True)
    elif deadline and time.time() > deadline:
        print("시간 제한 → 종료", flush=True)
    pr.update(current=None, workers=[], runner_pid=None, finished=dt.datetime.now().isoformat(timespec="seconds"))
    save_progress(pr)
    try:
        os.remove(LOCK)
    except OSError:
        pass


def stop(now=False):
    os.makedirs(SOLVES, exist_ok=True)
    open(STOP_FILE, "w").close()
    print("STOP 파일 생성: 지금 솔브(들)가 끝나면 멈춥니다.")
    if now:
        for spid in solver_pids(load_progress()):
            if pid_alive(spid):
                subprocess.run(["taskkill", "/F", "/PID", str(spid)], capture_output=True)
                print("솔버 종료:", spid)


def set_mode(m, workers=None):
    low = m == "low"
    c = read_json(CONTROL, {}) or {}
    nw = max(1, int(workers or c.get("workers") or DEFAULT_WORKERS))
    total = low_threads() if low else CPU
    write_json(CONTROL, {"low": low, "threads": total, "workers": nw})
    for spid in solver_pids(load_progress()):
        if pid_alive(spid):
            set_priority(spid, low)
            print(f"현재 솔버(pid {spid}) 우선순위 → {'보통 이하 + 논리 CPU %d개만' % total if low else '보통 + 모든 CPU'}")
    split = worker_threads(total, low, nw)
    print(f"모드 {m}: 다음 솔브부터 전체 스레드 {total} (워커 {nw}개: {'+'.join(map(str, split))}"
          + (f", 논리 CPU {total}개에 묶음)" if low else ")"))


def check_ranges():
    rs = current_ranges()
    for spot in sl.SPOTS:
        for stack in sl.STACKS:
            d = job_dir(spot, stack)
            cs = [read_json(p) for p in glob.glob(os.path.join(d, "*.compact.json"))]
            cs = [c for c in cs if c]
            if not cs:
                continue
            stale = sum(1 for c in cs if c.get("rh") != rs[(spot, stack)][1])
            print(f"{spot} {stack}BB: 풀린 {len(cs)}, 레인지 바뀜 {stale}" + (" → 다시 실행하면 재솔브" if stale else ""))


def rebuild():
    for spot in sl.SPOTS:
        for stack in sl.STACKS:
            p = build_lib(spot, stack)
            if p:
                print("built", p, os.path.getsize(p))


def extract_turn():
    for p in sorted(glob.glob(os.path.join(SOLVES, "*_*bb", "*.json.gz"))):
        if p.endswith(".turn.json.gz"):
            continue
        m = re.search(r"([A-Z]+_[A-Z]+)_(\d+)bb[\\/]([2-9TJQKA][shdc][2-9TJQKA][shdc][2-9TJQKA][shdc])\.json\.gz$", p)
        if not m:
            continue
        spot, stack, flop = m.group(1), int(m.group(2)), m.group(3)
        c = read_json(compact_path(spot, stack, flop))
        if not c:
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            dump = json.load(f)
        turn = sl.compact_turn(dump, spot, stack, flop)
        write_json(os.path.join(job_dir(spot, stack), f"{flop}.turn.json.gz"),
                   {"rh": c.get("rh"), "th": c.get("th"), **turn}, gz=True)
        print("turn", spot, stack, flop)
    rebuild()


def eval_mapping():
    """같은 스팟·스택에서 풀린 플랍 A 를 나머지 중 가장 가까운 B 로 근사했을 때, A 의 실제 전략과의 차이."""
    tot_err, tot_n = 0.0, 0
    for spot, stacks in sorted(sl.available(refresh=True).items()):
        for stack, path in sorted(stacks.items()):
            lib = sl.load_lib(path)
            keys = list(lib["flops"])
            if len(keys) < 2:
                continue
            for a in keys:
                others = [k for k in keys if k != a]
                bkey, dist = sl.nearest_flop(sl.parse_board(a), others)
                fa, fb = lib["flops"][a]["nodes"], lib["flops"][bkey]["nodes"]
                ba, bb = sl.parse_board(a), sl.parse_board(bkey)
                errs = []
                for nk in ("oop_first", "ip_vs_check", "oop_vs_bet:bet33", "ip_vs_bet:bet33"):
                    if nk not in fa or nk not in fb or fa[nk]["a"] != fb[nk]["a"]:
                        continue
                    _idx, lst = sl.combo_order(ba)
                    for combo in lst:
                        va = sl.decode(fa[nk], combo, ba)
                        if va is None:
                            continue
                        mapped, _n = sl.map_hand(list(combo), ba, bkey)
                        if len(set(mapped)) < 2 or set(mapped) & set(bb):
                            continue
                        vb = sl.decode(fb[nk], mapped, bb)
                        if vb is None:
                            continue
                        errs.append(sum(abs(x - y) for x, y in zip(va, vb)) / 2)
                if errs:
                    e = sum(errs) / len(errs)
                    tot_err += e * len(errs)
                    tot_n += len(errs)
                    print(f"{spot} {stack}BB {a} → {bkey} (거리 {dist:.1f}): 콤보 전략 차이 평균 {e * 100:.1f}%")
    if tot_n:
        print(f"전체 평균 {tot_err / tot_n * 100:.1f}% (콤보 빈도 총변동거리)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--low", action="store_true", help="보통 이하 우선순위 + 스레드 cpu-2")
    ap.add_argument("--full", action="store_true", help="보통 우선순위 + 모든 스레드 (기본)")
    ap.add_argument("--set-mode", choices=("low", "full"))
    ap.add_argument("--threads", type=int, help="전체 스레드 (워커들이 나눠 씀)")
    ap.add_argument("--workers", type=int, help=f"동시에 돌릴 솔브 수 (기본 {DEFAULT_WORKERS}; --set-mode 와 같이 쓰면 바꾸기)")
    ap.add_argument("--accuracy", type=float, default=DEFAULT_ACCURACY)
    ap.add_argument("--max-iter", type=int, default=DEFAULT_MAX_ITER)
    ap.add_argument("--hours", type=float)
    ap.add_argument("--check-ranges", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--extract-turn", action="store_true")
    ap.add_argument("--eval-mapping", action="store_true")
    ap.add_argument("--pilot", nargs=3, metavar=("SPOT", "STACK", "FLOPS"))
    args = ap.parse_args()
    if args.status:
        return status()
    if args.stop:
        return stop(args.now)
    if args.set_mode:
        return set_mode(args.set_mode, args.workers)
    if args.check_ranges:
        return check_ranges()
    if args.rebuild:
        return rebuild()
    if args.extract_turn:
        return extract_turn()
    if args.eval_mapping:
        return eval_mapping()
    if args.pilot:
        spot, stack, flops = args.pilot
        only = [(-1, spot, int(stack), sl.canon_flop(sl.parse_board(f))[0]) for f in flops.split(",")]
        return run(args, only)
    return run(args)


if __name__ == "__main__":
    main()
