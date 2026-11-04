#!/usr/bin/env python3
"""GTO 러너 온도 적응 제어기 (홀덤연구소/thermal-design.md 4.1).

10초마다 CPU 온도(k10temp Tctl)를 읽어 단계(full/normal/cool/hold)를 정하고,
돌고 있는 console_solver 의 CPU 선호도(affinity)만 바꾼다. 스레드 수·nice 는 건드리지 않는다.
새 솔브 배정 중지(hold)는 $PREFLOP_TOOLS/solves/thermal.json 으로 러너에 알린다 (build_gto_lib.thermal_hold).

  python3 gto_thermal.py [--once] [--dry-run]
  --once     한 주기만 돌고 끝낸다
  --dry-run  온도만 읽고 판단·출력한다. 선호도 변경·파일 쓰기 없음

환경변수: PREFLOP_TOOLS(필수), GTO_THERMAL_TEMP_FILE(온도 주입, 시험용), GTO_THERMAL_STATE_DIR(기록 폴더),
          GTO_THERMAL_<상수이름> (아래 DEFAULTS 덮어쓰기, 예: GTO_THERMAL_SLOW_AVG5=90)
표준 라이브러리만 쓴다."""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

NAMES = {0: "hold", 1: "cool", 2: "normal", 3: "full"}

# 기본값 = 설계 2·3장. 환경변수 GTO_THERMAL_<이름> 으로 덮어쓴다.
DEFAULTS = {
    "NCPU": 0,              # 0 이면 os.cpu_count()
    "INTERVAL": 10.0,       # 측정 주기(초)
    "SLOW_AVG5": 91.0,      # 5분 평균이 이보다 높으면 한 단계 내림
    "FAST_AVG30": 93.0,     # 30초 평균이 이 이상이면 즉시 cool
    "HOLD_TEMP": 95.0,      # 30초 평균이 이 이상이
    "HOLD_SECS": 60.0,      # 이 초 이어지면 hold
    "HOLD_RELEASE": 90.0,   # hold 중 30초 평균이 이 미만이면 풀림
    "UP_AVG5": 88.5,        # 복귀: 5분 평균이 이 미만이고
    "UP_AVG30": 91.0,       # 30초 평균이 이 미만이고
    "UP_DWELL": 300.0,      # 지금 단계에 이 초 이상 있었을 때 한 단계 올림
    "DOWN_DWELL": 120.0,    # 단계를 바꾼 뒤 다시 내리려면 최소 이 초 (93/95 경보는 무시)
    "OLD_SOLVE": 3000.0,    # 시작 후 이 초(50분)가 지난 솔브는 cool/hold 에서도 normal 선호도
    "FAIL_MAX": 3,          # 온도 읽기 연속 실패 허용 횟수
    "LOG_KEEP_DAYS": 14,
    # 연속 CPUQuota 내부 루프 (thermal-quota-spec.md). --runtime 으로만 적용, 영구 drop-in 은 건드리지 않음.
    "QUOTA_ENABLE": 0,      # 1이면 루프 동작. 0이면 systemctl 을 한 번도 부르지 않음(고정 800%)
    "QUOTA_BASE": 800, "QUOTA_MIN": 600, "QUOTA_MAX": 1000, "QUOTA_STEP": 50,
    "QUOTA_TARGET": 89.0,   # 60초 평균 목표 (정보·로그용)
    "QUOTA_UP_A60": 88.0,   # 60초 평균 < 이 값이고
    "QUOTA_UP_A5": 89.0,    # 5분 평균도 < 이 값이면 +STEP
    "QUOTA_DOWN_A60": 90.0, # 60초 평균 > 이 값이거나
    "QUOTA_DOWN_A5": 90.0,  # 5분 평균 > 이 값이면 -STEP
    "QUOTA_UP_GAP": 120.0,  # 올림 사이 최소 초
    "QUOTA_DOWN_GAP": 60.0, # 내림 사이 최소 초
    "QUOTA_EMERG_A60": 91.5,  # 60초 평균이 이 이상이면 간격 무시하고 즉시 -STEP
}


def load_config(env=None, **over):
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    for k, d in DEFAULTS.items():
        v = env.get("GTO_THERMAL_" + k)
        if v not in (None, ""):
            try:
                cfg[k] = type(d)(v)
            except ValueError:
                print(f"GTO_THERMAL_{k}={v!r} 무시 (숫자가 아님)", flush=True)
    cfg.update(over)
    if not cfg["NCPU"]:
        cfg["NCPU"] = os.cpu_count() or 4
    cfg["QUOTA_MAX"] = max(cfg["QUOTA_MAX"], cfg["QUOTA_MIN"])   # MIN <= MAX 보장
    cfg["QUOTA_BASE"] = min(max(cfg["QUOTA_BASE"], cfg["QUOTA_MIN"]), cfg["QUOTA_MAX"])
    return cfg


class Decision:
    def __init__(self, level, hold, cpus_by_pid, temp, avg30, avg5, ceiling, changed=False, note=""):
        self.level, self.hold, self.cpus_by_pid = level, hold, cpus_by_pid
        self.temp, self.avg30, self.avg5, self.ceiling = temp, avg30, avg5, ceiling
        self.changed, self.note = changed, note

    @property
    def name(self):
        return NAMES[self.level]


class Controller:
    """순수 로직. 파일·시스템 접근 없음. step(now, temp, ceiling, solves) -> Decision.
    solves: [(pid, t0), ...]  (t0 는 솔브 시작 epoch, 모르면 None). ceiling: 2(normal) 또는 3(full)."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        n = self.cfg["NCPU"]
        self.sets = {
            3: tuple(range(n)),
            2: tuple(range(max(1, n - 2))),
            1: tuple(c for c in (0, 2, 4) if c < n) or (0,),
        }
        self.sets[0] = self.sets[1]
        self.samples = collections.deque()   # (시각, 온도)
        self.tlevel = None                   # 온도가 정한 단계 1~3 (hold 는 별도 플래그)
        self.since = float("-inf")           # tlevel 이 마지막으로 바뀐 시각
        self.hold = False
        self.hot_since = None                # 30초 평균이 HOLD_TEMP 이상이 된 시각
        self.fails = 0

    def _avg(self, now, secs):
        v = [t for ts, t in self.samples if ts > now - secs]
        return sum(v) / len(v) if v else None

    def _set_level(self, now, lv):
        if lv != self.tlevel:
            self.tlevel, self.since = lv, now
            return True
        return False

    def step(self, now, temp, ceiling, solves):
        c = self.cfg
        ceiling = 3 if ceiling == 3 else 2
        changed, note = False, ""
        if self.tlevel is None:
            self.tlevel = ceiling            # 처음에는 상한에서 시작, 체류 시간 제한 없음
        elif self.tlevel > ceiling:
            changed |= self._set_level(now, ceiling)   # 상한이 내려가면 따라 내려감
        if temp is None:
            self.fails += 1
            if self.fails >= c["FAIL_MAX"]:
                # 읽기 실패가 이어지면 안전하게 cool, 배정은 계속 (hold 풀어 둠)
                changed |= self._set_level(now, 1)
                self.hold, self.hot_since = False, None
                note = f"온도 읽기 {self.fails}회 연속 실패 -> cool"
        else:
            self.fails = 0
            self.samples.append((now, temp))
            while self.samples and self.samples[0][0] <= now - 300:
                self.samples.popleft()
            a30, a5 = self._avg(now, 30), self._avg(now, 300)
            if a30 >= c["FAST_AVG30"] and self.tlevel > 1:
                changed |= self._set_level(now, 1)
                note = f"30초 평균 {a30:.1f} >= {c['FAST_AVG30']:g} -> cool"
            elif (a5 > c["SLOW_AVG5"] and self.tlevel > 1 and now - self.since >= c["DOWN_DWELL"]
                  and now - self.samples[0][0] >= c["DOWN_DWELL"]):   # 시작 직후 표본 몇 개로 5분 평균을 판단하지 않음
                changed |= self._set_level(now, self.tlevel - 1)
                note = f"5분 평균 {a5:.1f} > {c['SLOW_AVG5']:g} -> 한 단계 내림"
            elif (a5 < c["UP_AVG5"] and a30 < c["UP_AVG30"] and now - self.since >= c["UP_DWELL"]
                  and self.tlevel < ceiling and not self.hold):
                changed |= self._set_level(now, self.tlevel + 1)
                note = f"5분 평균 {a5:.1f} < {c['UP_AVG5']:g} -> 한 단계 올림"
            if a30 >= c["HOLD_TEMP"]:
                if self.hot_since is None:
                    self.hot_since = now
                if not self.hold and now - self.hot_since >= c["HOLD_SECS"]:
                    self.hold, changed = True, True
                    note = f"30초 평균 {a30:.1f} >= {c['HOLD_TEMP']:g} 가 {c['HOLD_SECS']:g}초 -> 배정 중지"
            else:
                self.hot_since = None
                if self.hold and a30 < c["HOLD_RELEASE"]:
                    self.hold, changed = False, True
                    note = f"30초 평균 {a30:.1f} < {c['HOLD_RELEASE']:g} -> 배정 재개"
        return self._decision(now, ceiling, solves, temp, changed, note)

    def _decision(self, now, ceiling, solves, temp, changed, note):
        level = 0 if self.hold else min(ceiling, self.tlevel)
        normal = self.sets[min(2, ceiling)]
        cpus = {}
        for pid, t0 in solves or []:
            cs = self.sets[level]
            try:
                old = t0 is not None and now - float(t0) > self.cfg["OLD_SOLVE"]
            except (TypeError, ValueError):
                old = False
            if old and level <= 1:
                cs = normal      # JOB_TIMEOUT(75분) 보호: 오래 돈 솔브는 normal 이상
            cpus[pid] = cs
        return Decision(level, self.hold, cpus, temp, self._avg(now, 30), self._avg(now, 300), ceiling, changed, note)

    def ceiling_cpus(self, ceiling):
        return self.sets[3 if ceiling == 3 else 2]


class QuotaLoop:
    """연속 CPUQuota 순수 로직. 파일·시스템 접근 없음. step(now, level, a60, a5, warm) -> (목표 %, 메모).
    적용에 성공했을 때만 commit(now, target) 을 부른다."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.cur = None                      # 마지막으로 적용 확인된 %
        self.last_up = float("-inf")
        self.last_down = float("-inf")

    def step(self, now, level, a60, a5, warm):
        c = self.cfg
        if self.cur is None:
            return c["QUOTA_BASE"], "시작 BASE"
        if level <= 1:
            self.last_up = now               # 복귀 직후 올림 금지
            return c["QUOTA_BASE"], "cool/hold -> BASE"
        if a60 is None or not warm:
            return self.cur, "대기"
        if a60 >= c["QUOTA_EMERG_A60"]:
            return max(c["QUOTA_MIN"], self.cur - c["QUOTA_STEP"]), "긴급 내림"
        if (a60 > c["QUOTA_DOWN_A60"] or a5 > c["QUOTA_DOWN_A5"]) and now - self.last_down >= c["QUOTA_DOWN_GAP"]:
            return max(c["QUOTA_MIN"], self.cur - c["QUOTA_STEP"]), "내림"
        if (a60 < c["QUOTA_UP_A60"] and a5 < c["QUOTA_UP_A5"]
                and now - self.last_up >= c["QUOTA_UP_GAP"] and now - self.last_down >= c["QUOTA_UP_GAP"]):
            return min(c["QUOTA_MAX"], self.cur + c["QUOTA_STEP"]), "올림"
        return self.cur, "유지"

    def commit(self, now, target):
        if self.cur is not None:
            if target > self.cur:
                self.last_up = now
            elif target < self.cur:
                self.last_down = now
        self.cur = target


# --- 시스템 접근 (main 루프 쪽) -----------------------------------------------------------------
def find_hwmon(name, root="/sys/class/hwmon"):
    for d in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        try:
            if open(os.path.join(d, "name")).read().strip() == name:
                return d
        except OSError:
            pass
    return None


def read_num(path, scale):
    try:
        return float(open(path).read().strip()) / scale
    except (OSError, ValueError):
        return None


class SystemIO:
    """실제 /proc, /sys 접근. 시험에서는 같은 메서드를 가진 가짜로 바꾼다."""

    def __init__(self, temp_file=None, hwmon_root="/sys/class/hwmon", unit="gto-runner.service", cpu_max=None):
        self.unit = unit
        self.cpu_max = cpu_max or (f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service"
                                   f"/app.slice/{unit}/cpu.max")
        self.temp_file = temp_file
        self.cpu_hwmon = find_hwmon("k10temp", hwmon_root)
        self.gpu_hwmon = find_hwmon("amdgpu", hwmon_root)

    def read_temp(self):
        if self.temp_file:
            try:
                return float(open(self.temp_file).read().strip())
            except (OSError, ValueError):
                return None
        return read_num(os.path.join(self.cpu_hwmon, "temp1_input"), 1000.0) if self.cpu_hwmon else None

    def read_power(self):
        return read_num(os.path.join(self.gpu_hwmon, "power1_input"), 1e6) if self.gpu_hwmon else None

    def is_solver(self, pid):
        try:
            return os.path.basename(os.readlink(f"/proc/{int(pid)}/exe")) == "console_solver"
        except (OSError, ValueError, TypeError):
            return False

    def tids(self, pid):
        try:
            return [int(t) for t in os.listdir(f"/proc/{pid}/task")]
        except (OSError, ValueError):
            return []

    def getaff(self, tid):
        return os.sched_getaffinity(tid)

    def setaff(self, tid, cpus):
        os.sched_setaffinity(tid, cpus)

    def set_quota(self, pct):
        """systemctl --user set-property --runtime 으로 CPUQuota 적용 후 cpu.max 를 읽어 실제 %를 돌려준다. 실패는 None."""
        try:
            r = subprocess.run(["systemctl", "--user", "set-property", "--runtime", self.unit, f"CPUQuota={pct}%"],
                               timeout=15, capture_output=True)
            if r.returncode != 0:
                return None
            q, per = open(self.cpu_max).read().split()[:2]
            return 0 if q == "max" else int(round(int(q) / int(per) * 100))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def ticks(self, pid):
        """utime+stime (clock tick). 읽지 못하면 None."""
        try:
            s = open(f"/proc/{pid}/stat").read()
            f = s[s.rindex(")") + 2:].split()
            return int(f[11]) + int(f[12])
        except (OSError, ValueError, IndexError):
            return None


def read_solves(progress):
    """progress.json -> [(pid, t0)] (중복 제거)."""
    pr = progress if isinstance(progress, dict) else {}
    ws = [w for w in (pr.get("workers") or []) if isinstance(w, dict)]
    if not ws and isinstance(pr.get("current"), dict):
        ws = [pr["current"]]
    out, seen = [], set()
    for w in ws:
        pid = w.get("solver_pid")
        if isinstance(pid, int) and pid > 0 and pid not in seen:
            seen.add(pid)
            out.append((pid, w.get("t0")))
    return out


def read_ceiling(control):
    """control.json 의 low 플래그 -> 2(low) / 3(full). 파일이 없거나 깨졌으면 2."""
    if not isinstance(control, dict) or "low" not in control:
        return 2
    return 2 if control.get("low") else 3


def read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(p, data):
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def thermal_payload(level, hold, temp, avg5, now):
    r = lambda x: None if x is None else round(x, 2)
    return {"level": level, "name": NAMES[level], "hold": bool(hold), "temp": r(temp), "avg5m": r(avg5), "updated": round(now, 1)}


def apply_affinity(io, cpus_by_pid, dry_run=False):
    """각 pid 가 console_solver 인지 확인하고 모든 스레드(tid)에 적용. 같은 값이면 건너뜀. 바꾼 tid 수를 돌려준다."""
    n = 0
    if dry_run:
        return 0
    for pid, cpus in cpus_by_pid.items():
        if not io.is_solver(pid):
            continue
        want = set(cpus)
        for tid in io.tids(pid):
            try:
                if set(io.getaff(tid)) != want:
                    io.setaff(tid, want)
                    n += 1
            except OSError:
                pass   # 스레드가 그 사이 끝남
    return n


class CsvLog:
    HEADER = "ts,tctl_c,ppt_w,avg30s,avg5m,level,hold,ceiling,solver_pcpu,quota\n"

    def __init__(self, state_dir, keep_days=14):
        self.dir, self.keep = state_dir, keep_days
        self.path = os.path.join(state_dir, "thermal.csv")

    def write(self, now, row):
        os.makedirs(self.dir, exist_ok=True)
        today = dt.date.fromtimestamp(now)
        if os.path.exists(self.path) and dt.date.fromtimestamp(os.path.getmtime(self.path)) < today:
            self._rotate(dt.date.fromtimestamp(os.path.getmtime(self.path)))
        new = not os.path.exists(self.path)
        with open(self.path, "a", encoding="utf-8") as f:
            if new:
                f.write(self.HEADER)
            f.write(row + "\n")

    def _rotate(self, day):
        dst = os.path.join(self.dir, f"thermal-{day:%Y%m%d}.csv")
        if os.path.exists(dst):   # 같은 날짜 파일이 있으면 이어 붙임 (덮어써서 잃지 않게)
            with open(dst, "a", encoding="utf-8") as o, open(self.path, encoding="utf-8") as i:
                next(i, None)
                o.write(i.read())
            os.remove(self.path)
        else:
            os.replace(self.path, dst)
        limit = dt.date.fromtimestamp(time.time()) - dt.timedelta(days=self.keep)
        for p in glob.glob(os.path.join(self.dir, "thermal-*.csv")):
            m = re.fullmatch(r"thermal-(\d{8})\.csv", os.path.basename(p))
            if m:
                try:
                    if dt.datetime.strptime(m.group(1), "%Y%m%d").date() < limit:
                        os.remove(p)
                except (ValueError, OSError):
                    pass


class Runner:
    def __init__(self, solves_dir, io, cfg=None, state_dir=None, dry_run=False, ctl=None):
        self.cfg = cfg or load_config()
        self.ctl = ctl or Controller(self.cfg)
        self.io, self.dry = io, dry_run
        self.solves_dir = solves_dir
        self.thermal = os.path.join(solves_dir, "thermal.json")
        self.csv = CsvLog(state_dir, self.cfg["LOG_KEEP_DAYS"]) if state_dir else None
        self.last_level = None
        self.last_ticks = {}
        self.last_tick_time = None
        self.qloop = QuotaLoop(self.cfg) if self.cfg["QUOTA_ENABLE"] else None

    def _quota(self, now, d):
        """CPUQuota 루프 한 주기. 실패해도 cycle 전체를 죽이지 않는다."""
        if self.qloop is None or self.dry or not hasattr(self.io, "set_quota"):
            return
        try:
            q = self.qloop
            a60 = self.ctl._avg(now, 60)
            sm = self.ctl.samples
            warm = bool(sm) and sm[0][0] <= now - 300 + self.cfg["INTERVAL"]
            t, _ = q.step(now, d.level, a60, d.avg5, warm)
            if t == q.cur:
                return
            got = self.io.set_quota(t)
            ts = f"{dt.datetime.fromtimestamp(now):%m-%d %H:%M:%S}"
            if got == t:
                old = "-" if q.cur is None else q.cur
                q.commit(now, t)
                s60 = "?" if a60 is None else f"{a60:.1f}"
                s5 = "?" if d.avg5 is None else f"{d.avg5:.1f}"
                print(f"{ts} quota {old} -> {t}% (60초 {s60}, 5분 {s5}, 단계 {d.name})", flush=True)
            else:
                print(f"{ts} quota 설정 실패 (목표 {t}%, 읽은 값 {got})", flush=True)
        except Exception as e:
            print("quota 오류:", repr(e), flush=True)

    def _pcpu(self, now, pids):
        """솔버 프로세스들의 CPU 사용률 합(%, 코어 1개 = 100). 첫 주기는 빈칸."""
        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        cur = {p: self.io.ticks(p) for p in pids}
        out = ""
        if self.last_tick_time is not None and now > self.last_tick_time:
            d = sum(max(0, cur[p] - self.last_ticks[p]) for p in cur if cur[p] is not None and self.last_ticks.get(p) is not None)
            out = f"{d / hz / (now - self.last_tick_time) * 100:.0f}"
        self.last_ticks = {p: v for p, v in cur.items() if v is not None}
        self.last_tick_time = now
        return out

    def cycle(self, now=None):
        now = time.time() if now is None else now
        ceiling = read_ceiling(read_json(os.path.join(self.solves_dir, "control.json")))
        solves = [(p, t) for p, t in read_solves(read_json(os.path.join(self.solves_dir, "progress.json"))) if self.io.is_solver(p)]
        temp = self.io.read_temp()
        d = self.ctl.step(now, temp, ceiling, solves)
        if d.note:
            print(f"{dt.datetime.fromtimestamp(now):%m-%d %H:%M:%S} {d.note}", flush=True)
        if d.level != self.last_level:
            t = "?" if d.temp is None else f"{d.temp:.1f}"
            a5 = "?" if d.avg5 is None else f"{d.avg5:.1f}"
            print(f"{dt.datetime.fromtimestamp(now):%m-%d %H:%M:%S} 단계 {NAMES.get(self.last_level, '-')} -> {d.name}"
                  f" (온도 {t}, 5분 평균 {a5}, 상한 {NAMES[d.ceiling]}, 솔버 {len(d.cpus_by_pid)}개{', 시험 모드' if self.dry else ''})", flush=True)
            self.last_level = d.level
        self._quota(now, d)
        if not self.dry:
            apply_affinity(self.io, d.cpus_by_pid)
            os.makedirs(self.solves_dir, exist_ok=True)
            write_atomic(self.thermal, thermal_payload(d.level, d.hold, d.temp, d.avg5, now))
            if self.csv:
                f = lambda x, n=2: "" if x is None else f"{x:.{n}f}"
                pw = self.io.read_power()
                self.csv.write(now, ",".join([f"{now:.0f}", f(d.temp), f(pw, 1), f(d.avg30), f(d.avg5), str(d.level),
                                               str(int(d.hold)), str(d.ceiling), self._pcpu(now, [p for p, _ in solves]),
                                               "" if self.qloop is None or self.qloop.cur is None else str(self.qloop.cur)]))
        return d

    def finish(self, now=None):
        """종료(SIGTERM): 솔버 선호도를 상한 단계 것으로 되돌리고 thermal.json 의 hold 를 false 로."""
        if self.dry:
            return
        now = time.time() if now is None else now
        ceiling = read_ceiling(read_json(os.path.join(self.solves_dir, "control.json")))
        solves = [p for p, _ in read_solves(read_json(os.path.join(self.solves_dir, "progress.json")))]
        cs = self.ctl.ceiling_cpus(ceiling)
        apply_affinity(self.io, {p: cs for p in solves})
        os.makedirs(self.solves_dir, exist_ok=True)
        write_atomic(self.thermal, thermal_payload(ceiling, False, None, None, now))
        print(f"종료: 선호도를 상한({NAMES[ceiling]})으로 복원, hold=false", flush=True)
        if self.qloop is not None and hasattr(self.io, "set_quota"):
            base = self.cfg["QUOTA_BASE"]
            try:
                got = self.io.set_quota(base)
            except Exception as e:
                got = repr(e)
            if got == base:
                print(f"종료: quota {base}% 복원", flush=True)
            else:
                print(f"경고: quota {base}% 복원 실패 (읽은 값 {got})", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    tools = os.environ.get("PREFLOP_TOOLS")
    if not tools:
        print("PREFLOP_TOOLS 가 없습니다", file=sys.stderr)
        return 1
    cfg = load_config()
    state = os.environ.get("GTO_THERMAL_STATE_DIR") or os.path.expanduser("~/.local/state/gto-thermal")
    io = SystemIO(os.environ.get("GTO_THERMAL_TEMP_FILE"))
    if not args.dry_run and not io.temp_file and not io.cpu_hwmon:
        print("k10temp hwmon 을 찾지 못함: 온도 읽기 실패로 처리됩니다", flush=True)
    runner = Runner(os.path.join(tools, "solves"), io, cfg, state, args.dry_run)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    print(f"gto_thermal 시작{' (dry-run)' if args.dry_run else ''}: 주기 {cfg['INTERVAL']:g}초, CPU {cfg['NCPU']}", flush=True)
    while True:
        t0 = time.time()
        try:
            runner.cycle(t0)
        except Exception as e:   # 한 주기 실패로 제어기가 죽지 않게
            print("주기 오류:", repr(e), flush=True)
        if args.once:
            return 0
        if stop.wait(max(0.5, cfg["INTERVAL"] - (time.time() - t0))):
            break
    runner.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
