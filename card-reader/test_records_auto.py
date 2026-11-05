"""records_auto (앱 안 기록 업로드 스케줄러) 시험. 네트워크 없음: records_sync.Uploader / load_config 를 가짜로 바꾼다.

python test_records_auto.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import records_auto as RA  # noqa: E402
import records_sync as RS  # noqa: E402

CFG = {"enabled": True, "urls": ["https://example.invalid:1"], "token": "SECRET-TOKEN-XYZ",
       "cert_sha256": "0" * 64, "interval_sec": 900, "window_days": 7, "allow_shrink": []}


class FakeUp:
    """records_sync.Uploader 자리. 부를 때마다 스레드·인자를 적고 정해 둔 결과를 돌려준다."""
    calls = []
    code, result, exc = 0, "ok", None
    delay = 0.0

    def __init__(self, base, cfg, on_error=None, extra_names=None, **kw):
        self.base, self.cfg, self.on_error, self.extra_names = base, cfg, on_error, extra_names

    def cycle(self):
        FakeUp.calls.append({"t": time.monotonic(), "thread": threading.current_thread(),
                             "names": self.extra_names() if callable(self.extra_names) else self.extra_names})
        if FakeUp.delay:
            time.sleep(FakeUp.delay)
        if FakeUp.exc is not None:
            raise FakeUp.exc
        self.last_result = FakeUp.result
        return FakeUp.code


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="records-auto-")
        self.keep = (RS.Uploader, RS.load_config)
        FakeUp.calls, FakeUp.code, FakeUp.result, FakeUp.exc, FakeUp.delay = [], 0, "ok", None, 0.0
        self.logs = []
        self.syncers = []

    def tearDown(self):
        for s in self.syncers:
            s.stop(wait=2.0)
        RS.Uploader, RS.load_config = self.keep
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake(self, interval=900):
        RS.Uploader = FakeUp
        RS.load_config = lambda base: dict(CFG, interval_sec=interval)
        with open(os.path.join(self.dir, RS.CONFIG_NAME), "w", encoding="utf-8") as f:
            json.dump({"enabled": True}, f)        # start() 는 파일이 있는지만 본다 (검사는 가짜 load_config)

    def syncer(self, first_delay=0.05, debounce=0.3):
        s = RA.Syncer(self.dir, log=self.logs.append, first_delay=first_delay, debounce=debounce)
        s.start()
        self.syncers.append(s)
        return s

    def wait_calls(self, n, timeout=3.0):
        end = time.monotonic() + timeout
        while len(FakeUp.calls) < n and time.monotonic() < end:
            time.sleep(0.01)
        return len(FakeUp.calls)


class NoConfig(Base):
    def test_missing_config_does_nothing(self):
        def boom(*a, **k):
            raise AssertionError("Uploader 를 만들면 안 됨")
        RS.Uploader = boom
        n = threading.active_count()
        self.assertIsNone(RA.start(self.dir, log=self.logs.append, first_delay=0))
        time.sleep(0.1)
        self.assertEqual(threading.active_count(), n)
        self.assertEqual(self.logs, [])

    def test_disabled_config_does_nothing(self):
        RS.Uploader = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no"))
        with open(os.path.join(self.dir, RS.CONFIG_NAME), "w", encoding="utf-8") as f:
            json.dump(dict(CFG, enabled=False), f)
        self.assertIsNone(RA.start(self.dir, log=self.logs.append, first_delay=0))
        with open(os.path.join(self.dir, RS.CONFIG_NAME), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(RA.start(self.dir, log=self.logs.append, first_delay=0))
        self.assertEqual(self.logs, [])

    def test_enabled_config_starts(self):
        self.fake()
        s = RA.start(self.dir, log=self.logs.append, first_delay=0.01)
        self.assertIsNotNone(s)
        self.syncers.append(s)
        self.assertEqual(self.wait_calls(1), 1)


class Triggers(Base):
    def test_start_runs_backlog_off_main_thread(self):
        self.fake()
        self.syncer(first_delay=0.05)
        self.assertEqual(self.wait_calls(1), 1)
        c = FakeUp.calls[0]
        self.assertIsNot(c["thread"], threading.main_thread())
        self.assertEqual(c["thread"].name, "records-sync")
        time.sleep(0.2)
        self.assertEqual(len(FakeUp.calls), 1)          # 간격(900초) 전에는 더 없음

    def test_hand_end_debounce(self):
        self.fake()
        s = self.syncer(first_delay=0.01, debounce=0.4)
        self.wait_calls(1)
        t0 = time.monotonic()
        for _ in range(5):                               # 빠른 핸드 5개 → 한 번
            s.hand_done()
            time.sleep(0.03)
        time.sleep(0.15)
        self.assertEqual(len(FakeUp.calls), 1)           # 아직 묶는 중
        self.assertEqual(self.wait_calls(2), 2)
        self.assertGreaterEqual(FakeUp.calls[1]["t"] - t0, 0.35)
        time.sleep(0.6)
        self.assertEqual(len(FakeUp.calls), 2)
        self.assertIsNot(FakeUp.calls[1]["thread"], threading.main_thread())

    def test_continuous_hands_do_not_postpone_forever(self):
        self.fake()
        s = self.syncer(first_delay=0.01, debounce=0.3)
        self.wait_calls(1)
        end = time.monotonic() + 0.9
        while time.monotonic() < end:                    # 쉬지 않고 끝나는 핸드
            s.hand_done()
            time.sleep(0.05)
        self.assertGreaterEqual(len(FakeUp.calls), 3)

    def test_interval(self):
        self.fake(interval=0.2)
        self.syncer(first_delay=0.01)
        self.assertGreaterEqual(self.wait_calls(4, timeout=3.0), 4)
        gaps = [b["t"] - a["t"] for a, b in zip(FakeUp.calls, FakeUp.calls[1:4])]
        self.assertTrue(all(g >= 0.15 for g in gaps), gaps)

    def test_one_run_at_a_time(self):
        self.fake(interval=0.05)
        FakeUp.delay = 0.1
        active, peak = [0], [0]
        orig = FakeUp.cycle

        def cyc(self_):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            try:
                return orig(self_)
            finally:
                active[0] -= 1
        FakeUp.cycle = cyc
        try:
            s = self.syncer(first_delay=0.01, debounce=0.01)
            for _ in range(20):
                s.hand_done()
                time.sleep(0.01)
            self.wait_calls(3)
        finally:
            FakeUp.cycle = orig
        self.assertEqual(peak[0], 1)

    def test_stop_ends_thread(self):
        self.fake()
        s = self.syncer(first_delay=0.01)
        self.wait_calls(1)
        s.stop(wait=2.0)
        self.assertFalse(s.thread.is_alive())
        s.hand_done()
        time.sleep(0.1)
        self.assertEqual(len(FakeUp.calls), 1)

    def test_names_are_passed_as_callable_snapshot(self):
        self.fake()
        s = RA.Syncer(self.dir, log=self.logs.append, first_delay=10, debounce=0.05)
        s.add_names(["Alpha", "", None, "Bravo", "Alpha"])
        s.start()
        self.syncers.append(s)
        s.hand_done()
        self.wait_calls(1)
        self.assertEqual(sorted(FakeUp.calls[0]["names"]), ["Alpha", "Bravo"])
        s.add_names("n%d" % i for i in range(RA.NAMES_MAX + 10))
        self.assertEqual(len(s.names_now()), RA.NAMES_MAX)


class Failures(Base):
    def test_exception_swallowed_and_logged_once(self):
        self.fake(interval=0.05)
        FakeUp.exc = RuntimeError("boom " + CFG["token"])
        s = self.syncer(first_delay=0.01)
        self.assertGreaterEqual(self.wait_calls(4), 4)
        self.assertTrue(s.thread.is_alive())
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertIn("RuntimeError", self.logs[0])
        self.assertNotIn(CFG["token"], "".join(self.logs))
        self.assertEqual(s.status_text(), "기록 전송: 오류")

    def test_status_texts(self):
        self.fake()
        s = RA.Syncer(self.dir, log=self.logs.append)
        self.assertIsNone(s.status_text())
        for code, result, want in ((3, "unreachable", "기록 전송: 대기(네트워크)"),
                                   (2, "blocked_rule", "기록 전송: 멈춤(점검 필요)"),
                                   (2, "disabled", "기록 전송: 멈춤(점검 필요)"),
                                   (2, "auth", "기록 전송: 인증 오류")):
            FakeUp.code, FakeUp.result = code, result
            s.run_once()
            self.assertEqual(s.status_text(), want)
        FakeUp.code, FakeUp.result = 2, "busy"            # 명령줄 실행이 잠금을 쥠: 상태 그대로, 로그 없음
        n = len(self.logs)
        s.run_once()
        self.assertEqual(s.status_text(), "기록 전송: 인증 오류")
        self.assertEqual(len(self.logs), n)
        FakeUp.code, FakeUp.result = 0, "ok"
        s.run_once()
        self.assertRegex(s.status_text(), r"^기록 전송: 정상 · \d\d:\d\d$")
        FakeUp.code, FakeUp.result = 3, "unreachable"     # 성공 뒤 같은 실패는 다시 한 번 적는다
        s.run_once()
        s.run_once()
        self.assertEqual(sum("unreachable" in m for m in self.logs), 2)

    def test_short_status_texts(self):
        s = RA.Syncer(self.dir)
        self.assertEqual(s.status_texts(), [])
        s.status = "대기(네트워크)"
        self.assertEqual(s.status_texts(), ["기록 전송: 대기(네트워크)", "전송: 대기(네트워크)", "전송: 대기"])
        s.status = "정상 · 21:15"
        self.assertEqual(s.status_texts()[-1], "전송: 정상")

    def test_same_failure_logged_once(self):
        self.fake()
        FakeUp.code, FakeUp.result = 3, "unreachable"
        s = RA.Syncer(self.dir, log=self.logs.append)
        for _ in range(5):
            s.run_once()
        self.assertEqual(len(self.logs), 1)

    def test_config_turned_off_while_running(self):
        self.fake()
        s = RA.Syncer(self.dir, log=self.logs.append)

        def off(base):
            raise RS.Refuse("disabled_by_config")
        RS.load_config = off
        s.run_once()
        self.assertEqual(s.status_text(), "기록 전송: 꺼짐")
        self.assertEqual(FakeUp.calls, [])
        self.assertEqual(self.logs, [])

    def test_bad_config_logged_without_secret(self):
        self.fake()
        s = RA.Syncer(self.dir, log=self.logs.append)

        def bad(base):
            raise RS.Refuse("bad_config", "token")
        RS.load_config = bad
        s.run_once()
        s.run_once()
        self.assertEqual(s.status_text(), "기록 전송: 설정 오류")
        self.assertEqual(len(self.logs), 1)

    def test_blocked_callback_logs_rule_not_path(self):
        self.fake()
        s = RA.Syncer(self.dir, log=self.logs.append)
        s._on_error("records_sync", "blocked", "known_name", "2026-11-01.jsonl", 3, "seats.SomeNick")
        s._on_error("records_sync", "blocked", "known_name", "2026-11-01.jsonl", 3, "seats.SomeNick")
        self.assertEqual(len(self.logs), 1)
        self.assertIn("known_name", self.logs[0])
        self.assertNotIn("SomeNick", self.logs[0])

    def test_logger_failure_swallowed(self):
        self.fake()
        FakeUp.code, FakeUp.result = 3, "unreachable"

        def bad_log(msg):
            raise OSError("disk")
        s = RA.Syncer(self.dir, log=bad_log)
        s.run_once()
        self.assertEqual(s.status_text(), "기록 전송: 대기(네트워크)")


class Static(unittest.TestCase):
    def test_reader_wiring(self):
        with open(os.path.join(HERE, "reader.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('_optional("records_auto")', src)
        self.assertIn("rs.hand_done()", src)
        self.assertIn("self.records_sync.stop(", src)
        tree = ast.parse(src)
        for node in ast.walk(tree):                       # 업로더 직접 호출은 reader 에 없어야 한다 (records_auto 스레드만)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("cycle", "Uploader"))

    def test_readme_hidden_imports(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            build = next(ln for ln in f if "--name CardReader" in ln)
        for m in ("records_auto", "records_sync", "records_check", "gto_dist_common"):
            self.assertIn("--hidden-import %s " % m, build)


if __name__ == "__main__":
    unittest.main()
