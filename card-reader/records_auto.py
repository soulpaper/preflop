"""CardReader 안에서 records_sync 업로더를 백그라운드로 돌린다 (records_sync 의 판정·필터는 그대로 쓴다).

BASE/records_upload.json 이 있고 enabled 일 때만 동작한다. 없거나 꺼져 있으면 start() 가 None (스레드·네트워크 없음).
언제 올리나: 앱 시작 직후 한 번 (꺼져 있던 동안 쌓인 기록), 핸드가 끝나면 DEBOUNCE_SEC 뒤 한 번 (그 사이 끝난 핸드는 묶음),
그리고 마지막 실행 뒤 interval_sec(기본 900초)마다. 한 번에 하나만 (스레드 하나 + records_sync 의 잠금 파일).
실패(네트워크, 서버 오류, 인증서 불일치, 필터 차단)는 앱을 멈추지 않는다: 같은 실패는 debug.log 에 한 번만, 화면에는 짧은 상태 글자만.
토큰·본문·이름은 어디에도 적지 않는다.
공개: start(base, log=None, first_delay=FIRST_DELAY) -> Syncer | None
"""
import os
import sys
import threading
import time

import records_sync as RS

FIRST_DELAY = 15        # 앱 시작 뒤 밀린 기록을 올리기까지 (창이 뜨는 동안 일을 보태지 않게)
DEBOUNCE_SEC = 60       # 핸드가 끝난 뒤 이만큼 기다렸다가 한 번 (빠른 핸드 여러 개 → 한 번)
DEFAULT_INTERVAL = 900
NAMES_MAX = 2000        # 이번 실행에서 본 닉네임 (메모리 전용, known_name 검사용)

PREFIX = "기록 전송: "
STOPPED = ("disabled", "blocked", "blocked_rule", "nickname_field", "scrub_not_done", "hands_is_link")


def _config_on(base):
    """설정 파일이 있고 enabled 인가 (검사 실패 = 켜져 있지만 설정 오류 → 그래도 True, 실행 때 알린다)."""
    try:
        RS.load_config(base)
        return True
    except RS.Refuse as r:
        return r.code not in ("no_config", "disabled_by_config")
    except Exception:
        return False


def start(base, log=None, first_delay=FIRST_DELAY):
    """설정이 없거나 꺼져 있으면 None. 그 밖에는 데몬 스레드를 띄우고 즉시 반환."""
    base = os.fspath(base)
    if not os.path.isfile(os.path.join(base, RS.CONFIG_NAME)) or not _config_on(base):
        return None
    s = Syncer(base, log=log, first_delay=first_delay)
    s.start()
    return s


class Syncer:
    def __init__(self, base, log=None, first_delay=FIRST_DELAY, debounce=DEBOUNCE_SEC):
        self.base = base
        self.log = log or (lambda msg: None)
        self.first_delay = first_delay
        self.debounce = debounce
        self.interval = DEFAULT_INTERVAL
        self.stop_ev = threading.Event()
        self.wake = threading.Event()
        self.mu = threading.Lock()
        self.hand_due = None        # monotonic 시각: 핸드 끝 뒤 실행할 때
        self.names = {}             # 닉네임 -> None (넣은 순서 유지, 오래된 것부터 버림)
        self.thread = None
        self.running = False
        self.runs = 0
        self.status = None          # 화면용 짧은 글자 (None = 아직 없음)
        self.logged = set()         # 이미 debug.log 에 적은 실패 (성공하면 비움): 같은 실패는 한 번만

    # --- 수명 ------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self.run, name="records-sync", daemon=True)
        self.thread.start()

    def stop(self, wait=2.0):
        """앱 종료: 기다리는 중이면 바로 끝난다. 올리는 중이면 wait 초까지 기다리고, 그래도 안 끝나면 버린다
        (데몬 스레드. 상태 파일은 원자 교체, 서버 쓰기는 If-Match 라 중간에 끊겨도 다음 실행이 이어 맞춘다)."""
        self.stop_ev.set()
        self.wake.set()
        t = self.thread
        if t is not None and t is not threading.current_thread() and wait:
            t.join(wait)

    # --- 다른 스레드에서 부름 (가볍게: 잠금 + 이벤트만) ----------------------------------
    def hand_done(self):
        """핸드가 끝남. 이미 예약돼 있으면 그대로 (계속 끝나는 핸드가 실행을 미루지 않게)."""
        with self.mu:
            if self.hand_due is None:
                self.hand_due = time.monotonic() + self.debounce
        self.wake.set()

    def add_names(self, names):
        """닉네임(잘리지 않은 것)을 메모리에만 모은다. records_sync 가 본문에 이 이름이 있으면 멈춘다 (known_name)."""
        with self.mu:
            for n in names:
                if isinstance(n, str) and n:
                    self.names.pop(n, None)
                    self.names[n] = None
            while len(self.names) > NAMES_MAX:
                self.names.pop(next(iter(self.names)))

    def names_now(self):
        with self.mu:
            return list(self.names)

    def status_text(self):
        s = self.status
        return PREFIX + s if s else None

    def status_texts(self):
        """좁은 화면용: 긴 것부터 짧은 것 순서 ('기록 전송: 대기(네트워크)' → '전송: 대기(네트워크)' → '전송: 대기')."""
        s = self.status
        if not s:
            return []
        out = [PREFIX + s, "전송: " + s, "전송: " + s.split("(")[0].split(" · ")[0]]
        return list(dict.fromkeys(out))

    # --- 스레드 ------------------------------------------------------------------
    def run(self):
        try:
            if sys.platform == "win32":
                import ctypes
                k = ctypes.windll.kernel32
                k.SetThreadPriority(k.GetCurrentThread(), -1)
        except Exception:
            pass
        next_run = time.monotonic() + self.first_delay        # 시작 직후: 밀린 기록
        while not self.stop_ev.is_set():
            now = time.monotonic()
            with self.mu:
                due = next_run if self.hand_due is None else min(next_run, self.hand_due)
            if now >= due:
                with self.mu:
                    self.hand_due = None
                self.run_once()
                next_run = time.monotonic() + self.interval
                continue
            self.wake.wait(due - now)
            self.wake.clear()

    def run_once(self):
        """한 번 올린다. 어떤 예외도 밖으로 내보내지 않는다."""
        self.running = True
        try:
            self._run_once()
        except Exception as e:
            self._fail("error", "오류", "exception %s" % type(e).__name__)
        finally:
            self.running = False
            self.runs += 1

    def _run_once(self):
        try:
            cfg = RS.load_config(self.base)
        except RS.Refuse as r:
            if r.code in ("no_config", "disabled_by_config"):
                self.status = "꺼짐"
                self.logged.clear()
                return
            self._fail("config", "설정 오류", "config %s" % r.code)
            return
        iv = cfg.get("interval_sec", DEFAULT_INTERVAL)
        if isinstance(iv, (int, float)) and iv > 0:
            self.interval = iv
        up = RS.Uploader(self.base, cfg, on_error=self._on_error, extra_names=self.names_now)
        code = up.cycle()
        result = str(getattr(up, "last_result", "") or "")
        if code == 0:
            self.status = "정상 · " + time.strftime("%H:%M")
            self.logged.clear()
        elif code == 3:
            self._fail("net", "대기(네트워크)", "unreachable")
        elif code == 2 and result == "busy":
            pass                                    # 다른 실행(명령줄)이 잡고 있음: 다음 차례에 다시
        elif code == 2 and result in STOPPED:
            self._fail(result, "멈춤(점검 필요)", "%s (code 2)" % result)
        elif code == 2 and result == "auth":
            self._fail(result, "인증 오류", "auth (code 2)")
        elif code == 1:
            self._fail(result, "일부 실패 · " + time.strftime("%H:%M"), "%s (code 1)" % result)
        else:
            self._fail(result, "오류", "%s (code %s)" % (result, code))

    def _on_error(self, src, what, *a):
        """records_sync 의 알림. 규칙 이름·파일 이름·줄 번호만 (필드 경로는 이름일 수 있어 뺀다)."""
        if what == "blocked" and a:
            detail = "rule=%s file=%s line=%s" % tuple((list(a[:3]) + ["", "", ""])[:3])
        else:
            detail = ""
        self._log_once(("cb", what, detail), "records_sync %s %s" % (what, detail))

    def _fail(self, key, status, msg):
        self.status = status
        self._log_once(key, "records_sync: " + msg)

    def _log_once(self, sig, msg):
        if sig in self.logged:
            return
        self.logged.add(sig)
        try:
            self.log(msg)
        except Exception:
            pass
