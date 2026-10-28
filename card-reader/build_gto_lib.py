"""TexasSolver 배치 러너 → gto_lib/ 갱신 (재개 가능, 우선순위 큐).

사용
  python build_gto_lib.py                 배치 시작/재개 (끝난 솔브는 건너뜀). 기본: 모든 스레드, 보통 우선순위
  python build_gto_lib.py --low           낮 시간용: 우선순위 '보통 이하' + 스레드 cpu-2
  python build_gto_lib.py --set-mode low  돌고 있는 배치를 낮 모드로 (다음 솔브부터 스레드 cpu-2, 지금 솔버는 우선순위만 낮춤)
  python build_gto_lib.py --set-mode full 다시 전체 속도
  python build_gto_lib.py --status        진행 (완료/전체, 평균 시간, ETA, 현재 작업)
  python build_gto_lib.py --stop [--now]  지금 솔브가 끝나면 멈춤 (--now: 솔버도 바로 종료). 다시 시작하면 이어서 한다
  python build_gto_lib.py --check-ranges  preflop.py 레인지가 바뀌어 다시 풀어야 하는 스팟 목록
  python build_gto_lib.py --rebuild       solves/ 의 압축 결과로 gto_lib/*.json.gz 전부 다시 만들기
  python build_gto_lib.py --pilot BTN_BB 40 As7h2d,Ks7h2d   지정 플랍만 풀기 (파이프라인 확인용)
  python build_gto_lib.py --eval-mapping  풀린 플랍끼리 서로 근사했을 때의 전략 오차 측정
  python build_gto_lib.py --extract-turn  원본 덤프(.json.gz)에서 턴 노드 다시 추출

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


def write_json(p, data, gz=False):
    tmp = p + ".tmp"
    if gz:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(data, f, separators=(",", ":"))
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


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


def set_priority(pid, low):
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x0200 | 0x1000, False, int(pid))   # SET_INFORMATION | QUERY_LIMITED
        if h:
            k.SetPriorityClass(h, BELOW_NORMAL if low else NORMAL)
            k.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def mode():
    """control.json → (threads, low)."""
    c = read_json(CONTROL, {}) or {}
    low = bool(c.get("low"))
    threads = int(c.get("threads") or (max(1, CPU - 2) if low else CPU))
    return threads, low


# --- 솔브 1회 -------------------------------------------------------------------------
def parse_log(text):
    it = re.findall(r"Iter: (\d+)", text)
    ex = re.findall(r"Total exploitability (-?[0-9.]+)", text)
    tm = re.findall(r"time used: ([0-9.]+)", text)
    return (int(it[-1]) if it else None, float(ex[-1]) if ex else None, float(tm[-1]) if tm else None)


def run_solve(spot, stack, flop, rngs, rh, threads, low, accuracy, max_iter, progress_cb=None):
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
        p = subprocess.Popen([EXE, "-i", inp], cwd=sl.SOLVER_DIR, stdout=lf, stderr=subprocess.STDOUT,
                             creationflags=(BELOW_NORMAL if low else NORMAL) | CREATE_NO_WINDOW)
        if progress_cb:
            progress_cb(p.pid)
        try:
            p.wait(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise RuntimeError("timeout")
    secs = time.time() - t0
    with open(logp, encoding="utf-8", errors="replace") as f:
        iters, expl, _tm = parse_log(f.read())
    if p.returncode != 0 or not os.path.exists(raw):
        raise RuntimeError(f"solver rc={p.returncode}, dump missing" if not os.path.exists(raw) else f"rc={p.returncode}")
    with open(raw, encoding="utf-8") as f:
        dump = json.load(f)
    c = sl.compact_dump(dump, spot, stack, flop)
    c.update(rh=rh, th=sl.tree_hash(), exploit=expl, iters=iters, secs=round(secs, 1), threads=threads,
             accuracy=accuracy, solved_at=dt.datetime.now().isoformat(timespec="seconds"))
    try:
        turn = sl.compact_turn(dump, spot, stack, flop)
        write_json(os.path.join(d, f"{flop}.turn.json.gz"), {"rh": rh, "th": sl.tree_hash(), **turn}, gz=True)
    except Exception as e:  # 턴 추출 실패는 플랍 결과에 영향 없음
        print("  turn extract failed:", e, flush=True)
    del dump
    write_json(compact_path(spot, stack, flop), c)
    # 원본 덤프는 gzip 으로 보관 (다음 솔브와 겹쳐서 압축)
    def _gz():
        try:
            with open(raw, "rb") as fi, gzip.open(raw + ".gz", "wb", compresslevel=3) as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
            os.remove(raw)
        except OSError as e:
            print("  gzip failed:", e, flush=True)
    th = threading.Thread(target=_gz, daemon=False)
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
def load_progress():
    return read_json(PROGRESS, {}) or {}


def save_progress(pr):
    pr["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    os.makedirs(SOLVES, exist_ok=True)
    write_json(PROGRESS, pr)


def _fmt_secs(s):
    s = int(s or 0)
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def status():
    rs = current_ranges()
    skip = narrow_spots(rs)
    js = [j for j in jobs() if (j[1], j[2]) not in skip]
    done, stale, per_tier = 0, 0, {}
    secs = []
    for tier, spot, stack, flop in js:
        c = read_json(compact_path(spot, stack, flop))
        t = per_tier.setdefault(tier, [0, 0])
        t[1] += 1
        if c and c.get("th") == sl.tree_hash():
            if c.get("rh") == rs[(spot, stack)][1]:
                done += 1
                t[0] += 1
                secs.append((c.get("solved_at", ""), c.get("secs") or 0))
            else:
                stale += 1
    total = len(js)
    pr = load_progress()
    recent = [s for _t, s in sorted(secs)[-12:]]
    avg = sum(recent) / len(recent) if recent else pr.get("avg_secs")
    rem = total - done
    alive = pid_alive(pr.get("runner_pid"))
    print(f"GTO 라이브러리 배치: {done}/{total} 완료 ({done / total * 100:.1f}%)" + (f", 레인지 변경으로 다시 풀 것 {stale}" if stale else ""))
    if avg:
        eta = rem * avg
        print(f"평균 {avg / 60:.1f}분/플랍 → 남은 {rem}개 ETA {_fmt_secs(eta)} "
              f"(≈ {(dt.datetime.now() + dt.timedelta(seconds=eta)).strftime('%m-%d %H:%M')})")
    print(f"러너: {'실행 중 (pid %s)' % pr.get('runner_pid') if alive else '멈춤'}"
          + (f", 모드 {'low' if pr.get('low') else 'full'} · 스레드 {pr.get('threads')}" if alive else ""))
    cur = pr.get("current")
    if alive and cur:
        el = time.time() - cur.get("t0", time.time())
        print(f"현재: {cur['spot']} {cur['stack']}BB {cur['flop']} ({el / 60:.1f}분 경과)")
    left_acc = 0
    for tier, (a, b) in sorted(per_tier.items()):
        spots, stacks, rng = PLAN[tier]
        mark = "✓" if a == b else ("…" if a else " ")
        left_acc += b - a
        eta = f"  ~{(dt.datetime.now() + dt.timedelta(seconds=left_acc * avg)).strftime('%m-%d %H:%M')} 까지"             if (avg and a < b) else ""
        print(f"  {mark} 단계 {tier + 1:2d}: {a:3d}/{b:3d}  {'/'.join(spots)} {'/'.join(map(str, stacks))}BB "
              f"플랍 {rng[0] + 1}-{rng[1]}{eta}")
    if skip:
        print("레인지가 너무 좁아 건너뜀:", ", ".join(f"{s} {k}BB" for s, k in sorted(skip)))
    libs = sl.available(refresh=True)
    print("gto_lib:", ", ".join(f"{s} {sorted(v)}" for s, v in sorted(libs.items())) or "(없음)")
    if pr.get("failed"):
        print("실패:", ", ".join(f"{f['spot']} {f['stack']} {f['flop']} ({f['error']})" for f in pr["failed"][-5:]))


# --- 러너 ---------------------------------------------------------------------------------
def acquire_lock():
    old = read_json(LOCK, {}) or {}
    if pid_alive(old.get("pid")) and old.get("pid") != os.getpid():
        print(f"이미 실행 중 (pid {old.get('pid')}). --stop 으로 먼저 멈추세요.")
        sys.exit(1)
    os.makedirs(SOLVES, exist_ok=True)
    write_json(LOCK, {"pid": os.getpid(), "started": dt.datetime.now().isoformat(timespec="seconds")})


def run(args, only=None):
    acquire_lock()
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    if args.low or args.full:
        write_json(CONTROL, {"low": bool(args.low), "threads": args.threads})
    elif args.threads:
        c = read_json(CONTROL, {}) or {}
        c["threads"] = args.threads
        write_json(CONTROL, c)
    rs = current_ranges()
    js = only if only is not None else jobs()
    skip = narrow_spots(rs)
    if skip:
        print("레인지가 너무 좁아 건너뜀:", ", ".join(f"{s} {k}BB" for s, k in sorted(skip)))
    js = [j for j in js if (j[1], j[2]) not in skip]
    # 새로 풀 것 (우선순위 순) → 그 다음 레인지가 바뀐(stale) 예전 솔브 재솔브 (그동안 예전 결과를 계속 쓴다)
    fresh, stale = [], []
    for j in js:
        c = read_json(compact_path(j[1], j[2], j[3]))
        if not c or c.get("th") != sl.tree_hash():
            fresh.append(j)
        elif c.get("rh") != rs[(j[1], j[2])][1]:
            stale.append(j)
    queue = fresh + stale
    pr = load_progress()
    pr.update(runner_pid=os.getpid(), total=len(js), started=dt.datetime.now().isoformat(timespec="seconds"),
              accuracy=args.accuracy, max_iter=args.max_iter, failed=pr.get("failed", [])[-20:])
    hist = pr.get("recent", [])
    save_progress(pr)
    print(f"대기 {len(queue)} / 전체 {len(js)}", flush=True)
    deadline = time.time() + args.hours * 3600 if args.hours else None
    pending_gz = []
    for n, (tier, spot, stack, flop) in enumerate(queue):
        if os.path.exists(STOP_FILE):
            print("STOP 파일 → 종료", flush=True)
            break
        if deadline and time.time() > deadline:
            print("시간 제한 → 종료", flush=True)
            break
        threads, low = mode()
        rngs, rh = rs[(spot, stack)]
        cur = {"tier": tier + 1, "spot": spot, "stack": stack, "flop": flop, "t0": time.time()}
        pr.update(current=cur, threads=threads, low=low, done_this_run=n, queue_left=len(queue) - n)
        save_progress(pr)

        def on_start(pid, cur=cur):
            cur["solver_pid"] = pid
            save_progress(pr)
        print(f"[{n + 1}/{len(queue)}] {spot} {stack}BB {flop} (단계 {tier + 1}, 스레드 {threads}{', low' if low else ''})",
              end=" ", flush=True)
        try:
            c, th = run_solve(spot, stack, flop, rngs, rh, threads, low, args.accuracy, args.max_iter, on_start)
            pending_gz.append(th)
        except Exception as e:  # 실패는 기록하고 다음으로
            print("실패:", e, flush=True)
            pr.setdefault("failed", []).append({"spot": spot, "stack": stack, "flop": flop, "error": str(e)[:200],
                                                "at": dt.datetime.now().isoformat(timespec="seconds")})
            save_progress(pr)
            if os.path.exists(STOP_FILE):
                break
            continue
        print(f"{c['secs']:.0f}s, iter {c['iters']}, expl {c['exploit']}%", flush=True)
        build_lib(spot, stack, rngs, rh)
        hist = (hist + [{"spot": spot, "stack": stack, "flop": flop, "secs": c["secs"], "exploit": c["exploit"],
                         "iters": c["iters"], "at": c["solved_at"]}])[-30:]
        recent = [h["secs"] for h in hist[-12:]]
        avg = sum(recent) / len(recent)
        left = len(queue) - n - 1
        pr.update(recent=hist, avg_secs=round(avg, 1), eta_secs=round(left * avg),
                  eta_time=(dt.datetime.now() + dt.timedelta(seconds=left * avg)).isoformat(timespec="minutes"),
                  done_total=sum(1 for j in js if is_done(j[1], j[2], j[3], rs[(j[1], j[2])][1])))
        pending_gz = [t for t in pending_gz if t.is_alive()]
    for t in pending_gz:
        t.join()
    pr.update(current=None, runner_pid=None, finished=dt.datetime.now().isoformat(timespec="seconds"))
    save_progress(pr)
    try:
        os.remove(LOCK)
    except OSError:
        pass


def stop(now=False):
    os.makedirs(SOLVES, exist_ok=True)
    open(STOP_FILE, "w").close()
    print("STOP 파일 생성: 지금 솔브가 끝나면 멈춥니다.")
    if now:
        pr = load_progress()
        cur = pr.get("current") or {}
        spid = cur.get("solver_pid")
        if spid and pid_alive(spid):
            subprocess.run(["taskkill", "/F", "/PID", str(spid)], capture_output=True)
            print("솔버 종료:", spid)


def set_mode(m):
    low = m == "low"
    write_json(CONTROL, {"low": low, "threads": max(1, CPU - 2) if low else CPU})
    pr = load_progress()
    spid = (pr.get("current") or {}).get("solver_pid")
    if spid and pid_alive(spid):
        set_priority(spid, low)
        print(f"현재 솔버(pid {spid}) 우선순위 → {'보통 이하' if low else '보통'}")
    print(f"모드 {m}: 다음 솔브부터 스레드 {max(1, CPU - 2) if low else CPU}")


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
    ap.add_argument("--threads", type=int)
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
        return set_mode(args.set_mode)
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
