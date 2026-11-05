"""python3 -I test_records.py   (또는 python3 -I -m unittest test_records)

기록 업로더(records_sync.py), 판정(records_check.py), 수신기(records_pull.py) 시험. 번호는 docs/records-impl-spec.md 8절.
- 모든 데이터는 합성이고 임시 폴더에만 만든다. 실제 hands, ~/.local/share/sharebox, 실제 토큰은 쓰지 않는다.
- 실서버 시험: 환경변수 RECORDS_TEST_SHAREBOXD=<shareboxd 바이너리> 가 있을 때만 (없으면 SKIP).
  임시 SHAREBOX_DATA 에서 token add / cert-pin / serve 를 실행한다. 토큰은 변수에만 둔다.
"""
import ast
import base64
import collections
import glob
import builtins
import datetime
import hashlib
import http.client
import http.server
import json
import os
import random
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gto_dist_common as C  # noqa: E402
import records_check as RC  # noqa: E402
import records_pull as RP  # noqa: E402
import records_sync as RS  # noqa: E402

FIXTURES = os.path.join(HERE, "testdata", "records_fixtures.jsonl")
# sharebox server/testdata/records_fixtures.jsonl 과 바이트 동일해야 한다. 고치면 양쪽 상수와 사본을 같이 고친다.
FIXTURES_SHA256 = "14a69b43907b8f04ef7c8bb58a5b5eb45fe0466ab5301fed16d3088e45cce3f6"
# 스키마 정규화 덤프의 sha256. sharebox server/records_check_test.go 의 recordsSchemaSHA256 과 같아야 한다.
RECORDS_SCHEMA_SHA256 = "48ce811c3112c3b830ba56546e0b6bb91bad379bb8d208d67934de115054bbc5"
BIN = os.environ.get("RECORDS_TEST_SHAREBOXD")
TMP = tempfile.mkdtemp(prefix="records-test-")
REAL_HOME = os.environ.get("HOME")
NICK = "SynthNick01"
TODAY = datetime.date(2026, 11, 2)
D1, D0 = "2026-11-01.jsonl", "decisions_2026-11-01.jsonl"
HID0 = "20261105_120000-0001"
DID0 = HID0 + "/1"


WIN = os.name == "nt"
# 수신기(records_pull)와 POSIX 권한 검사는 리눅스 전용이다
linux_only = unittest.skipIf(WIN, "리눅스 전용 (수신기, POSIX 권한)")


def symlink_or_skip(src, dst):
    """Windows 는 개발자 모드가 아니면 심볼릭 링크를 못 만든다 (WinError 1314): 시험을 건너뛴다."""
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError, AttributeError) as e:
        raise unittest.SkipTest("심볼릭 링크를 만들 수 없음: %s" % type(e).__name__)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def need_real(fn):
    return unittest.skipUnless(BIN and os.access(BIN, os.X_OK), "RECORDS_TEST_SHAREBOXD 없음")(fn)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def hline(i, extra=""):
    return ('{"id":"20261105_120000-%04d","seats":{"hero":{"hero":true,"pid":null,"pos":"BTN"},"seat_TC":{"pid":"p_0123abcd"}}%s}\n'
            % (i, extra)).encode()


def dline(i, typ="decision", extra=""):
    return ('{"id":"20261105_120000-0001/%d","type":"%s","seats":{"hero":{"hero":true,"pid":null,"pos":"BTN"}}%s}\n'
            % (i, typ, extra)).encode()


def fixture_cases():
    with open(FIXTURES, encoding="utf-8") as f:
        for ln in f:
            c = json.loads(ln)
            if "parts" in c:
                b = "".join(s * n for s, n in c["parts"]).encode()
            elif "body_b64" in c:
                b = base64.b64decode(c["body_b64"])
            else:
                b = c["body"].encode("utf-8")
            yield c, b


def find_fixture_source():
    """sharebox 원본 픽스처 찾기: 환경 변수, 그 다음 위쪽 폴더들의 sharebox/ 또는 */sharebox/ (폴더 이름을 코드에 넣지 않는다)."""
    rel = os.path.join("sharebox", "server", "testdata", "records_fixtures.jsonl")
    env = os.environ.get("RECORDS_SHAREBOX_SRC")
    if env and os.path.isfile(env):
        return env
    d = HERE
    for _ in range(5):
        d = os.path.dirname(d)
        cands = [os.path.join(d, rel)] + sorted(glob.glob(os.path.join(d, "*", rel)))
        for c in cands:
            if os.path.isfile(c):
                return c
    return None


def case_accept(c):
    a = c.get("accept")
    return tuple(a) if a else (RC.FILTER_V, RC.FILTER_V)


# ----------------------------------------------------------------------------------------------
class Real:
    """임시 데이터 폴더의 실제 shareboxd (한 번만 띄워 모든 실서버 시험이 공유)."""
    inst = None

    def __init__(self):
        self.data = os.path.join(TMP, "sbdata")
        os.makedirs(self.data)
        self.env = {**os.environ, "SHAREBOX_DATA": self.data, "HOME": os.path.join(TMP, "sbhome")}
        os.makedirs(self.env["HOME"], exist_ok=True)
        self.hp, self.tp = free_port(), free_port()
        assert self.hp not in (8737, 1213) and self.tp not in (8737, 1213)
        self.n = 0
        self.pin = self.sb("cert-pin")
        self.proc = None
        self.start()

    @classmethod
    def get(cls):
        if cls.inst is None:
            cls.inst = cls()
        return cls.inst

    def sb(self, *a):
        r = subprocess.run([BIN, *a], env=self.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, "shareboxd %s 실패" % a[0]
        return r.stdout.strip()

    def start(self):
        self.proc = subprocess.Popen([BIN, "serve", "-listen", "127.0.0.1:%d" % self.hp, "-tls-listen",
                                      "127.0.0.1:%d" % self.tp, "-data", self.data], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.hp), 0.2).close()
                socket.create_connection(("127.0.0.1", self.tp), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("shareboxd 시작 실패")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()

    def new_src(self, role="space:records:write"):
        self.n += 1
        name = "t%d%s" % (self.n, "r" if role.endswith("read") else "w")
        return name, self.sb("token", "add", name, role)

    def cfg(self, tok, **kw):
        c = {"enabled": True, "urls": ["https://127.0.0.1:%d" % self.tp], "token": tok, "cert_sha256": self.pin,
             "interval_sec": 900, "window_days": 7, "allow_shrink": []}
        c.update(kw)
        return c

    def http(self, method, path, tok, body=None, headers=None, tls=True):
        c = C.PinnedConnection("127.0.0.1", self.tp, self.pin) if tls else \
            http.client.HTTPConnection("127.0.0.1", self.hp, timeout=30)
        h = {"Authorization": "Bearer " + tok}
        h.update(headers or {})
        try:
            c.request(method, path, body=body, headers=h)
        except (BrokenPipeError, ConnectionResetError):
            pass
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def src_dir(self, src):
        return os.path.join(self.data, "spaces", "records", "src", src)


def tearDownModule():      # noqa: F811
    if Real.inst:
        Real.inst.stop()
    shutil.rmtree(TMP, ignore_errors=True)


class Rec(RS.Uploader):
    """PUT 을 세는 업로더. fake=True 이면 네트워크 없이 빈 서버를 흉내 낸다."""

    def __init__(self, *a, fake=False, **kw):
        super().__init__(*a, **kw)
        self.puts = []
        self.fake = fake

    def _put(self, n, body, hdrs, sha, shrink):
        self.puts.append((n, body, dict(hdrs), shrink))
        if self.fake:
            return 201, {}
        return super()._put(n, body, hdrs, sha, shrink)

    def _request(self, method, path, headers=None, body=None):
        if self.fake:
            class R:
                status = 200
            return 200, R(), json.dumps({"blocked": False, "files": []}).encode()
        return super()._request(method, path, headers, body)


def fake_cfg(**kw):
    c = {"enabled": True, "urls": ["https://127.0.0.1:1"], "token": "synthetic-token", "cert_sha256": "0" * 64,
         "interval_sec": 900, "window_days": 7, "allow_shrink": []}
    c.update(kw)
    return c


def mkbase(name):
    b = os.path.join(TMP, name)
    shutil.rmtree(b, ignore_errors=True)
    os.makedirs(os.path.join(b, "hands"))
    return b


def wr(base, rel, data):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def mk(base, cfg, cls=Rec, **kw):
    kw.setdefault("require_audit", False)         # 점검 관문은 시험 56 만 켠다
    return cls(base, cfg, today=lambda: TODAY, **kw)


def run(up, **kw):
    err = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        return up.cycle(**kw)
    finally:
        sys.stderr.close()
        sys.stderr = err


def server_get(real, rtok, src, file):
    code, data, _ = real.http("GET", "/api/spaces/records/src/%s/files/%s" % (src, file), rtok)
    return code, data


# ----------------------------------------------------------------------------------------------
class T18_Truncated(unittest.TestCase):
    def test_18_partial_last_line_basis(self):
        full = hline(1) + hline(2)
        cut = full + hline(3)[:20]
        b1, n1, _ = RS.build_body(D1, full)
        b2, n2, _ = RS.build_body(D1, cut)
        self.assertEqual(b1, b2)
        self.assertEqual(hashlib.sha256(b1).digest(), hashlib.sha256(b2).digest())
        self.assertEqual((n1, n2), (2, 2))

    def test_18_multibyte_cut(self):
        line = dline(3, extra=',"advice":{"reason":"한글"}')
        k = line.index("한".encode()) + 1          # 한 글자의 중간
        raw = dline(1) + line[:k]
        b, n, d = RS.build_body(D0, raw)
        self.assertEqual((n, d), (1, 0))
        b2, _, _ = RS.build_body(D0, dline(1) + line)
        self.assertTrue(b2.startswith(b))

    @need_real
    def test_18_real_completes_next_cycle(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t18")
        p = wr(base, "hands/" + D1, hline(1) + hline(2)[:15])
        up = mk(base, real.cfg(tok))
        self.assertEqual(run(up), 0)
        self.assertEqual(len(up.puts), 1)
        with open(p, "ab") as f:
            f.write(hline(2)[15:])
        up2 = mk(base, real.cfg(tok))
        self.assertEqual(run(up2), 0)
        self.assertEqual(len(up2.puts), 1)
        self.assertEqual(up2.puts[0][2].get("If-Match") is not None, True)


class T19_Deterministic(unittest.TestCase):
    def test_19_crlf_same_as_lf(self):
        lf = hline(1) + hline(2)
        crlf = lf.replace(b"\n", b"\r\n")
        self.assertEqual(RS.build_body(D1, lf)[0], RS.build_body(D1, crlf)[0])
        self.assertTrue(all(b'"redacted":5' in l for l in RS.build_body(D1, lf)[0].splitlines()))

    @need_real
    def test_19_real_second_cycle_no_put(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t19")
        wr(base, "hands/" + D1, (hline(1) + hline(2)).replace(b"\n", b"\r\n"))
        a, b = mk(base, real.cfg(tok)), mk(base, real.cfg(tok))
        self.assertEqual(run(a), 0)
        self.assertEqual(run(b), 0)
        self.assertEqual((len(a.puts), len(b.puts)), (1, 0))
        self.assertIn("If-None-Match", a.puts[0][2])
        self.assertEqual(a.files[D1]["sha256"], hashlib.sha256(a.puts[0][1]).hexdigest())


class T20_DateRollover(unittest.TestCase):
    @need_real
    def test_20_window_and_backfill(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t20")
        old = "2026-10-01.jsonl"
        wr(base, "hands/" + D1, hline(1))
        wr(base, "hands/" + D0, dline(1))
        wr(base, "hands/" + old, hline(9))
        wr(base, "hands/2026-09-01.jsonl", hline(8))
        up = mk(base, real.cfg(tok))
        self.assertEqual(run(up), 0)
        self.assertEqual(sorted(p[0] for p in up.puts), sorted([D0, D1]))
        with open(os.path.join(base, "hands", D1), "ab") as f:
            f.write(hline(2))
        with open(os.path.join(base, "hands", D0), "ab") as f:
            f.write(dline(2, "resolution"))
        up2 = mk(base, real.cfg(tok))
        self.assertEqual(run(up2), 0)
        self.assertEqual(sorted(p[0] for p in up2.puts), sorted([D0, D1]))
        up3 = mk(base, real.cfg(tok))
        self.assertEqual(run(up3, backfill=(datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))), 0)
        self.assertEqual([p[0] for p in up3.puts], [old])
        self.assertFalse(os.path.exists(os.path.join(real.src_dir(src), "2026-09-01.jsonl")))

    def test_20_window_selection_fake(self):
        base = mkbase("t20f")
        for n in (D1, "2026-10-20.jsonl", "2026-11-03.jsonl", "2026-11-04.jsonl", D0):
            wr(base, "hands/" + n, hline(1) if not n.startswith("dec") else dline(1))
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(up.targets(TODAY - datetime.timedelta(days=7), TODAY + datetime.timedelta(days=1)),
                         sorted([D1, D0, "2026-11-03.jsonl"]))


class T21_LocalShrink(unittest.TestCase):
    @need_real
    def test_21_shrink_local_and_allow(self):
        real = Real.get()
        src, tok = real.new_src()
        rsrc, rtok = real.new_src("space:records:read")
        base = mkbase("t21")
        wr(base, "hands/" + D1, hline(1) + hline(2) + hline(3))
        wr(base, "hands/" + D0, dline(1))
        self.assertEqual(run(mk(base, real.cfg(tok))), 0)
        code, before = server_get(real, rtok, src, D1)
        self.assertEqual(code, 200)
        wr(base, "hands/" + D1, hline(1))             # 로컬 잘림
        wr(base, "hands/" + D0, dline(1) + dline(2))  # 다른 파일은 정상
        up = mk(base, real.cfg(tok))
        self.assertEqual(run(up), 1)
        self.assertEqual([p[0] for p in up.puts], [D0])
        self.assertEqual(up.files[D1]["result"], "shrink_local")
        self.assertEqual(server_get(real, rtok, src, D1)[1], before)
        up2 = mk(base, real.cfg(tok, allow_shrink=[D1]))
        self.assertEqual(run(up2), 0)
        self.assertEqual([p[0] for p in up2.puts], [D1])
        self.assertTrue(os.path.exists(os.path.join(real.src_dir(src), D1 + ".prev")))


class T22_Recovery(unittest.TestCase):
    @need_real
    def test_22_response_lost_then_rerun(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t22")
        wr(base, "hands/" + D1, hline(1))

        class Lossy(Rec):
            def _put(self, n, body, hdrs, sha, shrink):
                super()._put(n, body, hdrs, sha, shrink)      # 서버는 받는다
                self._close()
                raise RS.ConnFail()
        up = mk(base, real.cfg(tok), cls=Lossy)
        self.assertEqual(run(up), 3)
        up2 = mk(base, real.cfg(tok))
        self.assertEqual(run(up2), 0)
        self.assertEqual(up2.files[D1]["result"], "unchanged")
        os.unlink(os.path.join(base, RS.STATE_NAME))
        up3 = mk(base, real.cfg(tok))
        self.assertEqual(run(up3), 0)
        self.assertEqual(up3.puts, [])

    @need_real
    def test_22_412_refetches_status(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t22b")
        wr(base, "hands/" + D1, hline(1))
        self.assertEqual(run(mk(base, real.cfg(tok))), 0)
        stale = {}

        class Stale(Rec):
            first = True

            def get_status(self):
                b, files = super().get_status()
                if Stale.first:
                    Stale.first = False
                    stale.update(files)
                    return b, files
                return b, files
        # 서버 쪽을 다른 업로더가 먼저 갱신해 두고, 이 업로더는 낡은 status 로 시작한다.
        wr(base, "hands/" + D1, hline(1) + hline(2))
        self.assertEqual(run(mk(base, real.cfg(tok))), 0)
        wr(base, "hands/" + D1, hline(1) + hline(2) + hline(3))

        class Old(Rec):
            n = 0

            def get_status(self):
                b, files = RS.Uploader.get_status(self)
                Old.n += 1
                if Old.n == 1:
                    e = dict(files[D1])
                    e["sha256"] = hashlib.sha256(RS.build_body(D1, hline(1))[0]).hexdigest()
                    e["lines"], e["size"] = 1, 1
                    files = {D1: e}
                return b, files
        up = mk(base, real.cfg(tok), cls=Old)
        self.assertEqual(run(up), 0)
        self.assertEqual(len(up.puts), 2)       # 412 뒤 한 번 더
        self.assertEqual(up.files[D1]["result"], "uploaded")


class T23_BrokenLine(unittest.TestCase):
    def test_23_bad_json_line_dropped(self):
        raw = hline(1) + b'{"id":"x", broken\n' + hline(2)
        body, n, dropped = RS.build_body(D1, raw)
        self.assertEqual((n, dropped), (2, 1))
        self.assertEqual(RC.check_body(D1, body).cls, "ok")
        base = mkbase("t23")
        wr(base, "hands/" + D1, raw)
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 0)
        self.assertEqual(up.files[D1]["dropped"], 1)
        with open(os.path.join(base, RS.STATE_NAME)) as f:
            st = json.load(f)
        self.assertEqual(st["files"][D1]["dropped"], 1)
        self.assertNotIn("broken", json.dumps(st))

    def test_23_invalid_utf8_and_too_deep_dropped(self):
        raw = hline(1) + b'{"id":"\xff"}\n' + b'{"id":"20261105_120000-0001","d":' + b"[" * 40 + b"]" * 40 + b"}\n"
        self.assertEqual(RS.build_body(D1, raw)[1:], (1, 2))


class T24_Block(unittest.TestCase):
    CASES = {
        "seat_name": (D1, b'{"id":"20261105_120000-0001","seats":{"hero":{"name":"%s"}}}\n'),
        "deep_name_null": (D1, b'{"id":"20261105_120000-0001","a":{"b":{"c":{"name":null}}}}\n'),
        "bad_pid": (D1, b'{"id":"20261105_120000-0001","seats":{"hero":{"pid":"%s"}}}\n'),
        "dup_key": (D1, b'{"id":"20261105_120000-0001","seats":{},"seats":{"hero":{"pos":"%s"}}}\n'),
        "hero_name": (D1, b'{"id":"20261105_120000-0001","hero_name":"%s"}\n'),
        "opp_name": (D1, b'{"id":"20261105_120000-0001","seats":{"seat_TC":{"opp_name":"%s"}}}\n'),
        "names": (D0, b'{"id":"20261105_120000-0001/1","type":"decision","names":["%s"]}\n'),
        "nicks": (D0, b'{"id":"20261105_120000-0001/1","type":"decision","advice":{"gto":{"nicks":["%s"]}}}\n'),
        "fullwidth_name": (D1, '{"id":"20261105_120000-0001","ｎａｍｅ":"%s"}\n'.encode()),
        "zero_width_name": (D1, b'{"id":"20261105_120000-0001","na\\u200bme":"%s"}\n'),
        "fullwidth_nick": (D1, '{"id":"20261105_120000-0001","Ｎｉｃｋ":"%s"}\n'.encode()),
        "advice_reason_shape": (D0, b'{"id":"20261105_120000-0001/1","type":"decision","advice":{"reason":"x User123456 y"}}\n'),
    }

    def _check(self, raw, code_expected=2, file=D1):
        base = mkbase("t24")
        wr(base, "hands/" + file, raw)
        wr(base, "hands/2026-11-02.jsonl", hline(5))       # 다른 정상 파일도 보내지 않는다
        up = mk(base, fake_cfg(), fake=True)
        out = []
        up.on_error = lambda *a: out.append(a)
        self.assertEqual(run(up), code_expected)
        self.assertEqual(up.puts, [])
        with open(os.path.join(base, RS.STATE_NAME)) as f:
            txt = f.read()
        self.assertTrue(json.loads(txt)["disabled"])
        self.assertNotIn(NICK, txt + repr(out))
        self.assertNotIn("User123456", txt + repr(out))
        # disabled 이면 다음 실행도 2
        up2 = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up2), 2)
        self.assertEqual(up2.puts, [])
        return base

    def test_24_blocks(self):
        for name, (file, raw) in self.CASES.items():
            with self.subTest(name):
                self._check(raw.replace(b"%s", NICK.encode()) if b"%s" in raw else raw, file=file)

    def test_24_extra_names(self):
        base = mkbase("t24e")
        wr(base, "hands/" + D0, dline(1, extra=',"advice":{"reason":"nice play by synth nick01 here"}'))
        up = mk(base, fake_cfg(), fake=True, extra_names=[NICK])
        self.assertEqual(run(up), 1)
        self.assertEqual(up.puts, [])
        with open(os.path.join(base, RS.STATE_NAME)) as f:
            st = json.load(f)
        self.assertNotIn("disabled", st)
        self.assertEqual(st["held"][D0]["rule"], "known_name")
        self.assertEqual(st["last_result"], "held")
        self.assertNotIn("nick", json.dumps(st).lower().replace("nickname", ""))

    def test_24_reset_disabled(self):
        base = self._check(self.CASES["deep_name_null"][1])
        self.assertEqual(RS.main(["--base", base, "--reset-disabled"]), 0)
        wr(base, "hands/" + D1, hline(1))
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 0)
        self.assertEqual(len(up.puts), 2)


class T25_Pid(unittest.TestCase):
    def test_25_pid_preserved_and_redacted(self):
        raw = hline(1) + hline(2).replace(b'"pid":null', b'"pid":"p_deadbeef"', 1)
        body, n, _ = RS.build_body(D1, raw)
        lines = [json.loads(l) for l in body.decode().splitlines()]
        self.assertEqual(lines[0]["seats"]["hero"]["pid"], None)
        self.assertEqual(lines[1]["seats"]["hero"]["pid"], "p_deadbeef")
        self.assertEqual(lines[0]["seats"]["seat_TC"]["pid"], "p_0123abcd")
        self.assertTrue(all(l["redacted"] == RC.FILTER_V for l in lines))
        self.assertNotIn(b"p_", RS.build_body(D1, b'{"id":"20261105_120000-0001","seats":{"hero":{"hero":true}}}\n')[0])


class T26_ReadScope(unittest.TestCase):
    IMG = [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".PNG", ".JPG", ".JPEG", ".Bmp",
           ".GIF", ".WEBP", ".TIF", ".TIFF"]

    def _layout(self, name):
        base = mkbase(name)
        outside = os.path.join(TMP, name + "-outside.jsonl")
        with open(outside, "wb") as f:
            f.write(hline(99))
        wr(base, "hands/" + D1, hline(1))
        wr(base, "hands/" + D0, dline(1))
        wr(base, "hands/snaps/x.jpg", b"\xff\xd8img")
        wr(base, "hands/snaps/2026-11-01.jsonl", hline(77))
        wr(base, "hands/read_scores.json", b"{}")
        wr(base, "hands/2026-11-01.jsonl.bak", hline(66))
        wr(base, "hands/decisions_2026-11-01.jsonl.1", dline(66))
        wr(base, "debug.log", b"x")
        wr(base, "debug.log.1", b"x")
        wr(base, "replay_hands/2026-01-01.jsonl", hline(55))
        wr(base, "dist/hands/2026-01-01.jsonl", hline(55))
        wr(base, "dist/recordings/a.png", b"x")
        wr(base, "dist/recordings/2026-11-01.jsonl", hline(44))
        wr(base, "samples/s.png", b"x")
        wr(base, "samples/2026-11-01.jsonl", hline(44))
        for ext in self.IMG:
            wr(base, "hands/pic" + ext, b"img")
            wr(base, "pic" + ext, b"img")
            wr(base, "hands/2026-11-01" + ext, b"img")
        symlink_or_skip(outside, os.path.join(base, "hands", "2026-11-02.jsonl"))
        os.makedirs(os.path.join(base, "hands", "2026-11-03.jsonl"))
        return base

    def test_26_only_allowed_files_touched(self):
        base = self._layout("t26")
        hands = os.path.join(base, "hands")
        opened, scanned, listed = [], [], []
        real_open, real_scandir, real_listdir, real_walk = builtins.open, os.scandir, os.listdir, os.walk

        def spy_open(f, *a, **k):
            opened.append(os.path.abspath(f) if isinstance(f, (str, bytes, os.PathLike)) else f)
            return real_open(f, *a, **k)

        real_os_open = os.open

        def spy_os_open(p, *a, **k):
            opened.append(os.path.abspath(p))
            return real_os_open(p, *a, **k)

        def spy_scandir(p="."):
            scanned.append(os.path.abspath(p))
            return real_scandir(p)

        def spy_listdir(p="."):
            listed.append(p)
            return real_listdir(p)

        def spy_walk(*a, **k):
            listed.append("walk")
            return real_walk(*a, **k)
        up = mk(base, fake_cfg(), fake=True)
        builtins.open, os.scandir, os.listdir, os.walk = spy_open, spy_scandir, spy_listdir, spy_walk
        os.open = spy_os_open
        RS._open = spy_open
        try:
            self.assertEqual(run(up), 0)
        finally:
            builtins.open, os.scandir, os.listdir, os.walk = real_open, real_scandir, real_listdir, real_walk
            os.open = real_os_open
            RS._open = real_open
        allowed_open = {os.path.join(hands, D1), os.path.join(hands, D0),
                        os.path.join(base, RS.LOCK_NAME), os.path.join(base, RS.STATE_NAME),
                        os.path.join(base, RS.STATE_NAME + ".tmp"), os.devnull}
        self.assertTrue(set(opened) <= allowed_open, sorted(set(opened) - allowed_open))
        self.assertTrue({os.path.join(hands, D1), os.path.join(hands, D0)} <= set(opened))
        self.assertEqual(scanned, [hands])
        self.assertEqual(listed, [])
        self.assertEqual(sorted(p[0] for p in up.puts), sorted([D0, D1]))
        names = [p[0] for p in up.puts]
        self.assertTrue(all(RC.file_kind(n) for n in names))
        blob = b"".join(p[1] for p in up.puts)
        for marker in (77, 66, 55, 44, 99):
            self.assertNotIn(('"id":"20261105_120000-%04d"' % marker).encode(), blob)
        self.assertNotIn(b"img", blob)

    def test_26_profiles_db_refuses_without_network(self):
        for extra in ("profiles.db", "profiles.db-wal", "profiles.db-shm", "profiles.db-journal"):
            base = mkbase("t26p")
            wr(base, "hands/" + D1, hline(1))
            wr(base, extra, b"")
            up = mk(base, fake_cfg(), fake=True)

            def boom(*a, **k):
                raise AssertionError("network")
            up._request = boom
            self.assertEqual(run(up), 2)
            self.assertEqual(up.files, {})
            with open(os.path.join(base, RS.STATE_NAME)) as f:
                self.assertEqual(json.load(f)["last_result"], "scrub_not_done")

    def test_26_hands_dir_symlink_refused(self):
        base = mkbase("t26l")
        real = os.path.join(TMP, "t26l-real")
        os.makedirs(real, exist_ok=True)
        wr(real, D1, hline(1))
        shutil.rmtree(os.path.join(base, "hands"))
        symlink_or_skip(real, os.path.join(base, "hands"))
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 2)
        self.assertEqual(up.puts, [])

    def test_26_base_required(self):
        with self.assertRaises(SystemExit):
            RS.main(["--once"])


class T27_ServerDown(unittest.TestCase):
    def test_27_down_exit3_fast_files_untouched(self):
        base = mkbase("t27")
        p = wr(base, "hands/" + D1, hline(1))
        before = open(p, "rb").read()
        urls = ["https://127.0.0.1:%d" % free_port(), "https://127.0.0.1:%d" % free_port()]
        up = mk(base, fake_cfg(urls=urls))
        t = time.time()
        self.assertEqual(run(up), 3)
        self.assertLess(time.time() - t, 5 * len(urls) + 2)
        self.assertEqual(open(p, "rb").read(), before)
        self.assertEqual(os.listdir(os.path.join(base, "hands")), [D1])

    @need_real
    def test_27_recovers_when_server_returns(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t27r")
        wr(base, "hands/" + D1, hline(1))
        port = free_port()
        down = real.cfg(tok, urls=["https://127.0.0.1:%d" % port])
        self.assertEqual(run(mk(base, down)), 3)
        self.assertEqual(run(mk(base, real.cfg(tok))), 0)


class _TlsCounter:
    """shareboxd 가 만든 인증서로 TLS 를 받는 가짜 서버. 요청(=토큰이 실린 데이터) 개수를 센다."""

    def __init__(self, data_dir):
        self.requests = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests += 1
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            do_PUT = do_GET

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(data_dir, "tls.crt"), os.path.join(data_dir, "tls.key"))
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class T28_Pin(unittest.TestCase):
    @need_real
    def test_28_fingerprint_mismatch_sends_nothing(self):
        real = Real.get()
        src, tok = real.new_src()
        srv = _TlsCounter(real.data)
        try:
            base = mkbase("t28")
            wr(base, "hands/" + D1, hline(1))
            bad = real.cfg(tok, urls=["https://127.0.0.1:%d" % srv.port], cert_sha256="ab" * 32)
            self.assertEqual(run(mk(base, bad)), 3)
            self.assertEqual(srv.requests, 0)
            good = real.cfg(tok, urls=["https://127.0.0.1:%d" % srv.port])
            run(mk(base, good))
            self.assertGreater(srv.requests, 0)           # 대조군: 지문이 맞으면 요청이 간다
        finally:
            srv.close()


class T29_Fixtures(unittest.TestCase):
    def test_29_fixture_sha_and_source_copy(self):
        with open(FIXTURES, "rb") as f:
            raw = f.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FIXTURES_SHA256)
        src = find_fixture_source()
        if not src:
            self.skipTest("sharebox 원본 없음 (RECORDS_SHAREBOX_SRC 로 경로를 준다)")
        with open(src, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_29_fixture_pinned_binary(self):
        """CRLF 로 체크아웃되면 SHA 가 달라진다 (git core.autocrlf): .gitattributes 로 -text 를 고정했는지 본다."""
        with open(os.path.join(HERE, "testdata", "records_fixtures.jsonl"), "rb") as f:
            self.assertNotIn(b"\r", f.read())
        ga = os.path.join(HERE, ".gitattributes")
        if not os.path.isfile(ga):
            self.skipTest("card-reader 폴더 밖에서 복사해 쓰는 중 (.gitattributes 없음)")
        with open(ga, encoding="utf-8") as f:
            rules = [x.split() for x in f.read().splitlines() if x.strip() and not x.startswith("#")]
        self.assertIn(["testdata/records_fixtures.jsonl", "-text"], rules)

    def test_29_all_cases(self):
        n = 0
        for c, b in fixture_cases():
            v = RC.check_body(c["file"], b, case_accept(c))
            e = c["expect"]
            self.assertEqual((v.cls, v.rule, v.line), (e["class"], e["rule"], e["line"]), c["name"])
            if "path" in e:
                self.assertEqual(v.path, e["path"], c["name"])
            n += 1
        self.assertGreaterEqual(n, 40)

    def test_29_check_line_api(self):
        self.assertEqual(RC.file_kind("2026-02-30.jsonl"), None)
        self.assertEqual(RC.file_kind("decisions_2026-02-28.jsonl"), "decisions")
        self.assertEqual(RC.check_line("hands", b'{"id":"20261105_120000-0001","redacted":5}').cls, "ok")
        self.assertEqual(RC.check_line("hands", b'{"id":"20261105_120000-0001","redacted":5}', (5, 6)).cls, "ok")
        self.assertEqual(RC.check_line("hands", b'{"id":"20261105_120000-0001","redacted":6}').rule, "filter_version")
        self.assertEqual(RC.check_line("hands", b'{"id":"20261105_120000-0001","redacted":4}', (5, 5)).rule, "filter_version")

    def test_29_uploader_body_passes_check(self):
        body = RS.build_body(D1, hline(1) + hline(2))[0]
        self.assertTrue(RC.check_body(D1, body).ok())

    @need_real
    def test_29_real_201(self):
        real = Real.get()
        src, tok = real.new_src()
        base = mkbase("t29")
        wr(base, "hands/" + D1, hline(1))
        up = mk(base, real.cfg(tok))
        self.assertEqual(run(up), 0)
        self.assertEqual(up.files[D1]["result"], "uploaded")

    @need_real
    def test_29_go_python_agree(self):
        """모든 사례를 실서버에 올려 Go 판정과 Python 판정이 같은지 본다."""
        real = Real.get()
        src, tok = real.new_src()
        n = 0
        for c, b in fixture_cases():
            if len(b) > 4 << 20 and c["expect"]["class"] != "too_large":
                continue
            if "accept" in c:
                continue                  # 서버는 정확히 FILTER_V 만 받는다
            sha = hashlib.sha256(b).hexdigest()
            code, data, _ = real.http("PUT", "/api/spaces/records/files/" + c["file"], tok, body=b,
                                      headers={"X-Content-SHA256": sha, "If-None-Match": "*",
                                               "Content-Type": "application/x-ndjson"}) \
                if len(b) <= 16 << 20 else (413, b'{"error":"file_too_large"}', None)
            if code == 412:             # 같은 이름의 이전 사례가 남아 있음: 조건 없이 덮어쓴다
                _, cur, _ = real.http("GET", "/api/spaces/records/status", tok)
                cur = {e["file"]: e for e in json.loads(cur)["files"]}[c["file"]]
                code, data, _ = real.http("PUT", "/api/spaces/records/files/" + c["file"] + "?allow_shrink=1", tok, body=b,
                                          headers={"X-Content-SHA256": sha, "If-Match": '"%s"' % cur["sha256"]})
            v = RC.check_body(c["file"], b)
            j = json.loads(data) if data else {}
            name = c["name"]
            if v.cls == "ok":
                self.assertIn(code, (200, 201), name)
            elif v.cls == "format":
                self.assertEqual((code, j.get("error"), j.get("rule"), j.get("line")), (422, "bad_record", v.rule, v.line), name)
            elif v.cls == "too_large":
                self.assertEqual((code, j.get("error")), (413, v.rule), name)
            else:
                self.assertEqual((code, j.get("error"), j.get("rule"), j.get("line")), (422, "nickname_field", v.rule, v.line), name)
                real.sb("records", "unblock")
            n += 1
        self.assertGreaterEqual(n, 40)


# ----------------------------------------------------------------------------------------------
class FakeServer:
    """수신기 시험용 가짜 루프백 HTTP 서버."""

    def __init__(self):
        self.files = {}        # (src, file) -> dict(body, rewrites, tamper)
        self.blocked = False
        self.gets = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer synthetic-pull-token":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/spaces/records/status":
                    ents = [{"src": s, "file": f, "sha256": hashlib.sha256(d["body"]).hexdigest(),
                             "size": len(d["body"]), "lines": d["body"].count(b"\n"), "ver": 1,
                             "updated": 1, "rewrites": d.get("rewrites", 0), "prev_sha256": ""}
                            for (s, f), d in sorted(outer.files.items())]
                    out = json.dumps({"blocked": outer.blocked, "files": ents}).encode()
                    ctype = "application/json"
                else:
                    parts = self.path.split("/")
                    outer.gets.append((parts[5], parts[7]))
                    d = outer.files[(parts[5], parts[7])]
                    if d.get("force304") and self.headers.get("If-None-Match"):
                        self.send_response(304)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    out = d.get("serve", d["body"])
                    ctype = "application/x-ndjson"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(out)))
                if ctype != "application/json":
                    self.send_header("X-Content-SHA256", hashlib.sha256(d["body"]).hexdigest())
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def set(self, src, file, body, **kw):
        self.files[(src, file)] = dict(body=body, **kw)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def good_body(name, n=2):
    return RS.build_body(name, b"".join(hline(i) for i in range(n)) if not name.startswith("dec")
                         else b"".join(dline(i) for i in range(n)))[0]


class PullEnv:
    """수신기 시험 공용 준비 (가짜 서버, 임시 HOME, pull.json)."""

    WIN_OK = ()           # Windows 에서도 도는 시험 이름 (수신기를 쓰지 않는 것)

    def setUp(self):
        if WIN and self._testMethodName not in self.WIN_OK:
            self.skipTest("수신기는 리눅스 전용")
        self.fs = FakeServer()
        self.home = os.path.join(TMP, "pullhome")
        shutil.rmtree(self.home, ignore_errors=True)
        os.makedirs(self.home)
        os.chmod(self.home, 0o700)
        os.environ["HOME"] = self.home
        self.data = os.path.join(self.home, "data")
        self.work = os.path.join(self.home, "workspace")
        os.makedirs(self.work)
        self.inbox = os.path.join(self.work, "inbox.md")
        self.cfgp = os.path.join(self.home, "pull.json")
        self.write_cfg()

    def tearDown(self):
        if not hasattr(self, "fs"):
            return
        self.fs.close()
        os.environ["HOME"] = REAL_HOME

    def write_cfg(self, **kw):
        c = {"url": "http://127.0.0.1:%d" % self.fs.port, "token": "synthetic-pull-token",
             "data_dir": self.data, "work_dir": self.work, "inbox": self.inbox}
        c.update(kw)
        fd = os.open(self.cfgp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(c, f)
        os.chmod(self.cfgp, 0o600)

    def pull(self, *extra):
        return RP.main(["--config", self.cfgp, *extra])

    def inbox_text(self):
        try:
            return open(self.inbox, encoding="utf-8").read()
        except OSError:
            return ""

    def tree(self):
        out = {}
        for r, _, fs in os.walk(self.data):
            for f in fs:
                if f in ("pull_state.json",) or f.startswith("20") and "Z-" in f:
                    continue
                p = os.path.join(r, f)
                out[os.path.relpath(p, self.data)] = open(p, "rb").read()
        return out


class T30_Pull(PullEnv, unittest.TestCase):
    def test_30_hash_skip_and_changed_only(self):
        self.fs.set("a", D1, good_body(D1))
        self.fs.set("a", D0, good_body(D0))
        self.assertEqual(self.pull(), 0)
        self.assertEqual(len(self.fs.gets), 2)
        self.assertEqual(self.pull(), 0)
        self.assertEqual(len(self.fs.gets), 2)         # 해시 같음: GET 0회
        self.fs.set("a", D1, good_body(D1, 3))
        self.assertEqual(self.pull(), 0)
        self.assertEqual(self.fs.gets[2:], [("a", D1)])
        with open(os.path.join(self.data, "hands", D1), "rb") as f:
            self.assertEqual(f.read(), good_body(D1, 3))

    def test_30_same_run_twice_same_result(self):
        self.fs.set("a", D1, good_body(D1))
        self.fs.set("b", D1, good_body(D1, 3))
        self.pull()
        t1 = self.tree()
        self.pull()
        self.assertEqual(t1, self.tree())

    def test_30_sha_mismatch_discarded(self):
        self.fs.set("a", D1, good_body(D1), serve=good_body(D1, 3))
        self.assertEqual(self.pull(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.data, "raw", "a", D1)))
        self.assertFalse(os.path.exists(os.path.join(self.data, "hands", D1)))

    def test_30_shrink_and_rewrite_reported(self):
        self.fs.set("a", D1, good_body(D1, 3))
        self.pull()
        self.fs.set("a", D1, good_body(D1, 2), rewrites=1)
        self.pull()
        t = self.inbox_text()
        self.assertIn("shrink_applied a " + D1, t)
        self.assertIn("rewrite a " + D1, t)
        self.assertIn("[홀덤연구소/records_pull]", t)
        self.pull()
        self.assertEqual(self.inbox_text(), t)           # 같은 이벤트는 한 번만

    def test_30_two_sources_merge_dedupe(self):
        l1, l2, l3 = (RS.build_body(D1, hline(i))[0] for i in (1, 2, 3))
        self.fs.set("a", D1, l1 + l2)
        self.fs.set("b", D1, l2 + l3)
        self.assertEqual(self.pull(), 0)
        with open(os.path.join(self.data, "hands", D1), "rb") as f:
            self.assertEqual(f.read(), l1 + l2 + l3)

    def test_30_single_source_byte_copy(self):
        b = good_body(D1)
        self.fs.set("only", D1, b)
        self.pull()
        self.assertEqual(open(os.path.join(self.data, "hands", D1), "rb").read(), b)

    def test_30_blocked_reported_once(self):
        self.fs.blocked = True
        self.pull()
        self.pull()
        self.assertEqual(self.inbox_text().count(" blocked "), 1)

    def test_30_check_failure_quarantine_metadata_only(self):
        bad = ('{"id":"20261105_120000-0001","redacted":5,"seats":{"hero":{"name":"%s"}}}\n' % NICK).encode()
        self.fs.set("a", D1, bad)
        self.fs.set("a", D0, good_body(D0))
        self.assertEqual(self.pull(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.data, "raw", "a", D1)))
        self.assertFalse(os.path.exists(os.path.join(self.data, "hands", D1)))
        qd = os.path.join(self.data, "quarantine")
        names = os.listdir(qd)
        self.assertEqual(len(names), 1)
        txt = open(os.path.join(qd, names[0])).read()
        self.assertNotIn(NICK, txt)
        self.assertNotIn(NICK, self.inbox_text())
        self.assertEqual(json.loads(txt)["rule"], "forbidden_key")
        self.assertTrue(os.path.exists(os.path.join(self.data, "hands", D0)))

    def test_30_recheck(self):
        self.fs.set("a", D1, good_body(D1))
        self.pull()
        with open(os.path.join(self.data, "raw", "a", D1), "ab") as f:
            f.write(b'{"id":"x","redacted":5,"name":"%s"}\n' % NICK.encode())
        self.assertEqual(self.pull("--recheck"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.data, "raw", "a", D1)))

    def test_30_refuses_data_dir_in_work_or_git(self):
        self.write_cfg(data_dir=os.path.join(self.work, "x"))
        self.assertEqual(self.pull(), 2)
        self.assertFalse(os.path.exists(os.path.join(self.work, "x")))
        g = os.path.join(self.home, "repo")
        os.makedirs(os.path.join(g, ".git"))
        self.write_cfg(data_dir=os.path.join(g, "sub", "data"))
        self.assertEqual(self.pull(), 2)
        # inbox 는 작업 폴더 안이어도 된다
        self.write_cfg()
        self.fs.set("a", D1, good_body(D1))
        self.fs.blocked = True
        self.assertEqual(self.pull(), 0)
        self.assertIn("blocked", open(os.path.join(self.work, "inbox.md")).read())

    def test_30_permissions(self):
        self.fs.set("a", D1, good_body(D1))
        self.pull()
        for r, ds, fs in os.walk(self.data):
            self.assertEqual(os.stat(r).st_mode & 0o777, 0o700, r)
            for f in fs:
                self.assertEqual(os.stat(os.path.join(r, f)).st_mode & 0o777, 0o600, f)

    def test_30_non_loopback_url_rejected(self):
        for u in ("http://192.0.2.1:8737", "https://127.0.0.1:8737", "http://localhost:8737", "http://127.0.0.1.evil:1"):
            self.write_cfg(url=u)
            self.assertEqual(self.pull(), 2, u)

    def test_30_unreachable_exit3(self):
        self.write_cfg(url="http://127.0.0.1:%d" % free_port())
        self.assertEqual(self.pull(), 3)

    @need_real
    def test_30_real_server_roundtrip(self):
        real = Real.get()
        wsrc, wtok = real.new_src()
        _, rtok = real.new_src("space:records:read")
        base = mkbase("t30")
        wr(base, "hands/" + D1, hline(1) + hline(2))
        wr(base, "hands/" + D0, dline(1))
        self.assertEqual(run(mk(base, real.cfg(wtok))), 0)
        self.write_cfg(url="http://127.0.0.1:%d" % real.hp, token=rtok)
        self.assertEqual(self.pull(), 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.data, "raw", wsrc))), sorted([D0, D1]))
        self.assertEqual(self.pull(), 0)
        # 쓰기 토큰으로는 받을 수 없다
        self.assertEqual(real.http("GET", "/api/spaces/records/src/%s/files/%s" % (wsrc, D1), wtok, tls=False)[0], 403)
        self.write_cfg(url="http://127.0.0.1:%d" % real.hp, token="synthetic-wrong-token")
        self.assertEqual(self.pull(), 1)


class T31_Static(unittest.TestCase):
    def test_31_gitignore(self):
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True)
        if top.returncode != 0:
            self.skipTest("git 저장소 아님")
        for n in ("records_upload.json", "records_state.json", "records_state.json.tmp", "records_pull.json",
                  ".records-sync.lock"):
            r = subprocess.run(["git", "check-ignore", "-q", os.path.join(HERE, n)], cwd=HERE)
            self.assertEqual(r.returncode, 0, n)

    def test_31_records_check_imports(self):
        with open(os.path.join(HERE, "records_check.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add((n.module or "").split(".")[0])
        std = set(sys.stdlib_module_names)
        self.assertTrue(names <= std, names - std)

    def test_31_deploy_units_no_dash_I(self):
        for n in ("records-pull.service", "records-pull.timer"):
            with open(os.path.join(HERE, "deploy", n), encoding="utf-8") as f:
                t = f.read()
            self.assertNotIn(" -I", t)
            self.assertNotIn("python3 -I", t)
        svc = open(os.path.join(HERE, "deploy", "records-pull.service"), encoding="utf-8").read()
        self.assertIn("ExecStart=/usr/bin/python3 %h/.local/lib/holdem-records/records_pull.py", svc)
        self.assertIn("OnCalendar=*-*-* *:10:00", open(os.path.join(HERE, "deploy", "records-pull.timer")).read())

    def test_31_pull_only_needs_two_files(self):
        with open(os.path.join(HERE, "records_pull.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        local = {os.path.splitext(n)[0] for n in os.listdir(HERE) if n.endswith(".py")}
        for n in ast.walk(tree):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                [n.module] if isinstance(n, ast.ImportFrom) else []
            for m in mods:
                if m.split(".")[0] in local:
                    self.assertEqual(m, "records_check")


# ----------------------------------------------------------------------------------------------
# 개정 2 (명세 12.10): 시험 36~50
RAW_EXTRA_HAND = (b'{"id":"20261105_120000-0001","debug_extra":1,"notes":["x"],"issues":[{"code":"gap","detail":"x"}],'
                  b'"seats":{"hero":{"hero":true,"pid":null,"pos":"BTN","stats":{"foo":1,"hands":3}}}}\n')
RAW_EXTRA_DEC = (b'{"id":"20261105_120000-0001/1","type":"decision","seats":{"hero":{"pos":"BTN","profile":{"foo":1,"vpip":2}}},'
                 b'"advice":{"gto":{"actions":{"Bet33":1,"bet33":2}}}}\n')


class T36_Filter(unittest.TestCase):
    def test_36_deterministic_and_drops(self):
        for name, raw, gone in ((D1, RAW_EXTRA_HAND, (b"debug_extra", b"notes", b"detail", b'"foo"')),
                                (D0, RAW_EXTRA_DEC, (b'"foo"', b"Bet33"))):
            st1 = {"keys": 0, "paths": __import__("collections").Counter()}
            st2 = {"keys": 0, "paths": __import__("collections").Counter()}
            b1 = RS.build_body(name, raw, None, st1)[0]
            b2 = RS.build_body(name, raw, None, st2)[0]
            self.assertEqual(hashlib.sha256(b1).digest(), hashlib.sha256(b2).digest())
            for g in gone:
                self.assertNotIn(g, b1, (name, g))
            self.assertEqual(st1["keys"], st2["keys"])
            self.assertTrue(RC.check_body(name, b1).ok())
        st = {"keys": 0, "paths": __import__("collections").Counter()}
        RS.build_body(D1, RAW_EXTRA_HAND, None, st)
        self.assertEqual(st["keys"], 4)
        self.assertEqual(dict(st["paths"]), {"debug_extra": 1, "notes": 1, "issues.[].detail": 1, "seats.hero.stats.?": 1})
        st = {"keys": 0, "paths": __import__("collections").Counter()}
        RS.build_body(D0, RAW_EXTRA_DEC, None, st)
        self.assertEqual(dict(st["paths"]), {"seats.hero.profile.?": 1, "advice.gto.actions.?": 1})

    def test_36_state_has_counts_not_values(self):
        base = mkbase("t36")
        wr(base, "hands/" + D1, RAW_EXTRA_HAND)
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 0)
        with open(os.path.join(base, RS.STATE_NAME)) as f:
            st = json.load(f)
        e = st["files"][D1]
        self.assertEqual(e["dropped_keys"], 4)
        self.assertEqual(e["dropped_paths"]["issues.[].detail"], 1)
        self.assertEqual(e["dropped"], 0)
        self.assertNotIn('"foo"', json.dumps(st))
        self.assertEqual(st["name_check"], "shape_only")

    def test_36_check_only_prints_counts(self):
        base = mkbase("t36c")
        wr(base, "hands/" + D1, RAW_EXTRA_HAND)
        up = mk(base, fake_cfg(), fake=True)
        import io
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            self.assertEqual(run(up, check_only=True), 0)
        finally:
            sys.stdout = old
        self.assertIn("dropped_keys=4", buf.getvalue())
        self.assertIn("issues.[].detail", buf.getvalue())


class T37_PreCheck(unittest.TestCase):
    LINES = {
        "fullwidth_name": '{"id":"20261105_120000-0001","ｎａｍｅ":1}\n'.encode(),
        "zero_width_name": b'{"id":"20261105_120000-0001","na\\u200bme":1}\n',
        "fullwidth_nick": '{"id":"20261105_120000-0001","Ｎｉｃｋ":1}\n'.encode(),
        "players_in_unknown_subtree": b'{"id":"20261105_120000-0001","advice":{"players":1}}\n',
    }

    def test_37_uploader_stops_with_forbidden_key(self):
        for name, raw in self.LINES.items():
            with self.subTest(name):
                with self.assertRaises(RS.Block) as cm:
                    RS.build_body(D1, raw)
                self.assertEqual(cm.exception.rule, "forbidden_key")

    def test_37_shared_check_differs_for_fullwidth(self):
        for name in ("fullwidth_name", "zero_width_name", "fullwidth_nick"):
            v = RC.check_line("hands", self.LINES[name].rstrip(b"\n").replace(b'"id":"20261105_120000-0001"', b'"id":"20261105_120000-0001","redacted":5'))
            self.assertEqual((v.cls, v.rule), ("block", "unknown_key"), name)
        v = RC.check_line("hands", b'{"id":"20261105_120000-0001","redacted":5,"advice":{"players":1}}')
        self.assertEqual(v.rule, "forbidden_key")


class T38_NickValue(unittest.TestCase):
    @staticmethod
    def dec_reason(text):
        return ('{"id":"%s","type":"decision","advice":{"reason":"%s"}}\n' % (DID0, text)).encode()

    def test_38_nick_shapes_stop(self):
        for val in ("User123456", "User84\u2026", "kim12345"):
            with self.subTest(val):
                with self.assertRaises(RS.Block) as cm:
                    RS.build_body(D0, self.dec_reason(val))
                self.assertEqual((cm.exception.rule, cm.exception.path), ("nick_value", "advice.reason"))

    def test_38_fullwidth_only_uploader_catches(self):
        val = "\uff35\uff53\uff45\uff52\uff11\uff12\uff13\uff14"          # Ｕｓｅｒ１２３４
        with self.assertRaises(RS.Block) as cm:
            RS.build_body(D0, self.dec_reason(val))
        self.assertEqual(cm.exception.rule, "nick_value")
        line = ('{"id":"%s","type":"decision","redacted":5,"advice":{"reason":"%s"}}' % (DID0, val)).encode()
        self.assertTrue(RC.check_line("decisions", line).ok())            # 공용 판정은 통과 (업로더가 더 넓게 멈춘다)

    def test_38_false_positive_guard(self):
        vals = ["User", "user1", "abc123", "SPR 4.1", "한글 추천 문장", "x_bet33_c", "bet33"]
        raw = json.dumps({"id": DID0, "type": "decision", "flop_line": "x_bet33_c", "hand_id": HID0,
                          "advice": {"detail": vals, "gto": {"combo": "AhKd"}}}, ensure_ascii=False).encode() + b"\n"
        body, n, d = RS.build_body(D0, raw)
        self.assertEqual((n, d), (1, 0))
        self.assertTrue(RC.check_body(D0, body).ok())


class T39_Issues(unittest.TestCase):
    def test_39_issues_code_only(self):
        raw = (b'{"id":"20261105_120000-0001","issues":[{"code":"tracker_error","detail":"KeyError(\'User123456\')"},'
               b'{"code":"gap","detail":7},{"code":"Bad Code"},"str",5],"notes":["SynthNick01"]}\n')
        body, n, d = RS.build_body(D1, raw)
        rec = json.loads(body)
        self.assertEqual(rec["issues"], [{"code": "tracker_error"}, {"code": "gap"}])
        self.assertNotIn(b"detail", body)
        self.assertNotIn(b"notes", body)
        self.assertNotIn(b"SynthNick01", body)
        self.assertNotIn(b"User123456", body)
        res = (b'{"id":"20261105_120000-0001/1","type":"resolution","outcome":{"issues":["gap","Bad Code",5],"shown":{"BTN":["As","Kd"]}}}\n')
        body, _, _ = RS.build_body(D0, res)
        self.assertEqual(json.loads(body)["outcome"]["issues"], ["gap"])


class T40_ShrinkFilter(unittest.TestCase):
    def _run(self, fv):
        base = mkbase("t40")
        wr(base, "hands/" + D1, hline(1))

        class Srv(Rec):
            def _request(self, method, path, headers=None, body=None):
                class R:
                    status = 200
                e = {"src": "x", "file": D1, "sha256": "a" * 64, "size": 10 ** 6, "lines": 100, "ver": 1,
                     "updated": 1, "rewrites": 0, "prev_sha256": ""}
                if fv is not None:
                    e["fv"] = fv
                return 200, R(), json.dumps({"blocked": False, "files": [e]}).encode()
        up = mk(base, fake_cfg(), cls=Srv, fake=True)
        return up, run(up)

    def test_40_old_filter_version_shrinks(self):
        up, code = self._run(1)
        self.assertEqual(code, 0)
        self.assertEqual(len(up.puts), 1)
        self.assertTrue(up.puts[0][3])                              # allow_shrink=1
        self.assertIn("If-Match", up.puts[0][2])
        self.assertEqual(up.files[D1]["result"], "shrink_filter")

    def test_40_same_or_missing_version_is_local_shrink(self):
        for fv in (RC.FILTER_V, None, "1", True):
            with self.subTest(fv):
                up, code = self._run(fv)
                self.assertEqual(code, 1)
                self.assertEqual(up.puts, [])
                self.assertEqual(up.files[D1]["result"], "shrink_local")


@linux_only
class T41_PullRange(unittest.TestCase):
    def test_41_accept_min(self):
        home = os.path.join(TMP, "t41")
        shutil.rmtree(home, ignore_errors=True)
        os.makedirs(os.path.join(home, "data", "raw", "a"))
        os.makedirs(os.path.join(home, "ws"))
        for n in ("", "data", "data/raw", "data/raw/a"):
            os.chmod(os.path.join(home, n), 0o700)
        good = RS.build_body(D1, hline(1))[0]
        old = good.replace(b'"redacted":5', b'"redacted":4')
        self.assertEqual(RC.ACCEPT_MIN, 5)
        self.assertEqual(RC.FILTER_V, 5)
        wr(os.path.join(home, "data"), "raw/a/" + D1, good)
        wr(os.path.join(home, "data"), "raw/a/2026-11-02.jsonl", old)
        fd = os.open(os.path.join(home, "pull.json"), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"url": "http://127.0.0.1:1", "token": "synthetic-pull-token", "work_dir": os.path.join(home, "ws"),
                       "data_dir": os.path.join(home, "data"), "inbox": ""}, f)
        self.assertEqual(RP.main(["--config", os.path.join(home, "pull.json"), "--recheck"]), 1)
        self.assertTrue(os.path.exists(os.path.join(home, "data", "raw", "a", D1)))
        self.assertFalse(os.path.exists(os.path.join(home, "data", "raw", "a", "2026-11-02.jsonl")))
        q = os.listdir(os.path.join(home, "data", "quarantine"))
        self.assertEqual(len(q), 1)
        self.assertEqual(json.load(open(os.path.join(home, "data", "quarantine", q[0])))["rule"], "filter_version")


class T42_Stale304(PullEnv, unittest.TestCase):
    def test_42_stale_304_keeps_state(self):
        a = good_body(D1, 2)
        b = good_body(D1, 3)
        self.fs.set("a", D1, a)
        self.assertEqual(self.pull(), 0)
        sha_a = hashlib.sha256(a).hexdigest()
        self.fs.set("a", D1, b, force304=True)
        self.assertEqual(self.pull(), 0)
        with open(os.path.join(self.data, "pull_state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["files"]["a/" + D1]["sha256"], sha_a)
        with open(os.path.join(self.data, "hands", D1), "rb") as f:
            self.assertEqual(f.read(), a)
        self.assertEqual(self.pull(), 0)
        self.assertEqual(self.inbox_text().count("stale_304"), 1)
        n = len(self.fs.gets)
        self.fs.set("a", D1, b)
        self.assertEqual(self.pull(), 0)
        self.assertEqual(len(self.fs.gets), n + 1)
        with open(os.path.join(self.data, "hands", D1), "rb") as f:
            self.assertEqual(f.read(), b)


@linux_only
class T43_ConfigPerms(unittest.TestCase):
    def setUp(self):
        self.d = os.path.join(TMP, "t43")
        shutil.rmtree(self.d, ignore_errors=True)
        os.makedirs(self.d)
        os.chmod(self.d, 0o700)
        self.p = os.path.join(self.d, "pull.json")
        fd = os.open(self.p, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            os.makedirs(os.path.join(self.d, "ws"), exist_ok=True)
            json.dump({"url": "http://127.0.0.1:1", "token": "synthetic-pull-token",
                       "data_dir": os.path.join(self.d, "data"), "work_dir": os.path.join(self.d, "ws")}, f)
        os.chmod(self.p, 0o600)

    def main(self):
        err, sys.stderr = sys.stderr, open(os.devnull, "w")
        try:
            return RP.main(["--config", self.p])
        finally:
            sys.stderr.close()
            sys.stderr = err

    def test_43_pull_config(self):
        cfg, _ = RP.load_config(self.p)                      # 0600, 0700 → 정상
        self.assertEqual(cfg["token"], "synthetic-pull-token")
        os.chmod(self.p, 0o644)
        self.assertEqual(self.main(), 2)
        os.chmod(self.p, 0o640)
        self.assertEqual(self.main(), 2)
        os.chmod(self.p, 0o600)
        os.chmod(self.d, 0o777)
        self.assertEqual(self.main(), 2)
        os.chmod(self.d, 0o700)
        real = self.p + ".real"
        os.rename(self.p, real)
        symlink_or_skip(real, self.p)
        self.assertEqual(self.main(), 2)
        os.unlink(self.p)
        os.rename(real, self.p)
        keep = os.geteuid
        os.geteuid = lambda: keep() + 1
        try:
            self.assertEqual(self.main(), 2)
        finally:
            os.geteuid = keep
        with self.assertRaises(RP.Refuse) as cm:
            os.chmod(self.p, 0o644)
            RP.load_config(self.p)
        self.assertIn("chmod 600", str(cm.exception))
        self.assertNotIn("synthetic-pull-token", str(cm.exception))

    @linux_only
    def test_43_upload_config_posix(self):
        base = mkbase("t43u")
        os.chmod(base, 0o700)
        p = os.path.join(base, RS.CONFIG_NAME)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(fake_cfg(), f)
        os.chmod(p, 0o644)
        with self.assertRaises(RS.Refuse) as cm:
            RS.load_config(base)
        self.assertEqual(cm.exception.code, "config_perms")
        os.chmod(p, 0o600)
        self.assertEqual(RS.load_config(base)["interval_sec"], 900)


class T44_OpenRecheck(PullEnv, unittest.TestCase):
    WIN_OK = ("test_44_changed_during_open",)

    def test_44_changed_during_open(self):
        base = mkbase("t44")
        p = wr(base, "hands/" + D1, hline(1))

        class Swap(Rec):
            def targets(self, lo, hi):
                names = super().targets(lo, hi)
                tmp = p + ".new"
                with open(tmp, "wb") as f:
                    f.write(hline(1) + hline(2))
                os.replace(tmp, p)                          # 다른 inode
                return names
        up = mk(base, fake_cfg(), cls=Swap, fake=True)
        self.assertEqual(run(up), 1)
        self.assertEqual(up.files[D1]["result"], "changed_during_open")
        self.assertEqual(up.puts, [])

    def test_44_pull_raw_src_link_refused(self):
        os.makedirs(os.path.join(self.data, "raw"), mode=0o700)
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        symlink_or_skip(outside, os.path.join(self.data, "raw", "a"))
        self.fs.set("a", D1, good_body(D1))
        self.assertEqual(self.pull(), 2)
        self.assertEqual(os.listdir(outside), [])

    def test_44_pull_dir_link_refused(self):
        os.makedirs(self.data, mode=0o700)
        outside = os.path.join(self.home, "outside2")
        os.makedirs(outside)
        symlink_or_skip(outside, os.path.join(self.data, "quarantine"))
        self.assertEqual(self.pull(), 2)

    def test_44_raw_file_link_skipped(self):
        self.fs.set("a", D1, good_body(D1))
        self.assertEqual(self.pull(), 0)
        raw = os.path.join(self.data, "raw", "a", D1)
        other = os.path.join(self.home, "other.jsonl")
        with open(other, "wb") as f:
            f.write(good_body(D1, 3))
        os.unlink(raw)
        symlink_or_skip(other, raw)
        self.assertEqual(self.pull("--recheck"), 1)
        self.assertIn("symlink_skipped a " + D1, self.inbox_text())
        self.assertTrue(os.path.islink(raw))                  # 링크는 그대로 둔다 (읽지 않음)
        p = RP.Puller({}, __import__("urllib.parse").parse.urlsplit("http://127.0.0.1:1"), self.data)
        p.load_state()
        p.merge(D1)                                           # 링크는 건너뛰고 병합본이 지워진다
        self.assertFalse(os.path.exists(os.path.join(self.data, "hands", D1)))


class T45_ResetDisabled(unittest.TestCase):
    def test_45_busy_and_clean_reset(self):
        base = mkbase("t45")
        p = os.path.join(base, RS.STATE_NAME)
        with open(p, "w") as f:
            json.dump({"disabled": True, "disabled_rule": "x", "disabled_path": "a", "last_run": 5, "files": {"k": 1}}, f)
        lock = RS._lock(base)
        try:
            err, sys.stderr = sys.stderr, open(os.devnull, "w")
            try:
                self.assertEqual(RS.main(["--base", base, "--reset-disabled"]), 2)
            finally:
                sys.stderr.close()
                sys.stderr = err
            self.assertTrue(json.load(open(p))["disabled"])
        finally:
            lock.close()
        self.assertEqual(RS.main(["--base", base, "--reset-disabled"]), 0)
        st = json.load(open(p))
        self.assertEqual(st, {"last_run": 5, "files": {"k": 1}})
        self.assertFalse(os.path.exists(p + ".tmp"))

    def test_45_no_state_file_writes_nothing(self):
        base = mkbase("t45n")
        self.assertEqual(RS.main(["--base", base, "--reset-disabled"]), 0)
        self.assertFalse(os.path.exists(os.path.join(base, RS.STATE_NAME)))

    def test_45_save_state_atomic_cleans_tmp_on_error(self):
        base = mkbase("t45e")
        p = os.path.join(base, RS.STATE_NAME)
        with self.assertRaises(TypeError):
            RS.save_state_atomic(p, {"x": object()})
        self.assertEqual(os.listdir(base), ["hands"])

    def test_45_tmp_is_gitignored(self):
        r = subprocess.run(["git", "check-ignore", "-q", os.path.join(HERE, "records_state.json.tmp")], cwd=HERE)
        if r.returncode == 128:
            self.skipTest("git 저장소 아님")
        self.assertEqual(r.returncode, 0)


class T46_Limits(PullEnv, unittest.TestCase):
    def test_46_constants(self):
        self.assertEqual((RP.MAX_ENTRIES, RP.MAX_SRCS, RP.MAX_GETS, RP.MAX_RUN_BYTES, RP.MAX_QUARANTINE, RP.MAX_STATUS_BYTES),
                         (5000, 8, 400, 512 << 20, 500, 8 << 20))

    def test_46_entries_cap_reported_once(self):
        keep = RP.MAX_ENTRIES
        RP.MAX_ENTRIES = 3
        try:
            for i in range(1, 6):
                self.fs.set("a", "2026-11-%02d.jsonl" % i, good_body(D1))
            self.assertEqual(self.pull(), 0)
            self.assertEqual(len(self.fs.gets), 3)
            self.assertEqual(self.pull(), 0)
            self.assertEqual(self.inbox_text().count("too_many_entries"), 1)
        finally:
            RP.MAX_ENTRIES = keep

    def test_46_srcs_cap(self):
        for s in "abcdefghi":
            self.fs.set(s, D1, good_body(D1))
        self.assertEqual(self.pull(), 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.data, "raw"))), list("abcdefgh"))
        self.assertEqual(self.inbox_text().count("too_many_srcs"), 1)

    def test_46_max_gets_continues_next_run(self):
        keep = RP.MAX_GETS
        RP.MAX_GETS = 2
        try:
            for i in range(1, 5):
                self.fs.set("a", "2026-11-%02d.jsonl" % i, good_body(D1))
            self.assertEqual(self.pull(), 0)
            self.assertEqual(len(self.fs.gets), 2)
            self.assertEqual(self.pull(), 0)
            self.assertEqual(len(self.fs.gets), 4)
            self.assertEqual(self.pull(), 0)
            self.assertEqual(len(self.fs.gets), 4)
        finally:
            RP.MAX_GETS = keep

    def test_46_quarantine_cap(self):
        qd = os.path.join(self.data, "quarantine")
        os.makedirs(qd, mode=0o700)
        keep = RP.MAX_QUARANTINE
        RP.MAX_QUARANTINE = 3
        try:
            now = time.time()
            for i in range(6):
                p = os.path.join(qd, "q%d.json" % i)
                with open(p, "w") as f:
                    f.write("{}")
                os.utime(p, (now - 100 + i, now - 100 + i))
            self.fs.set("a", D1, good_body(D1))
            self.assertEqual(self.pull(), 0)
            self.assertEqual(sorted(os.listdir(qd)), ["q3.json", "q4.json", "q5.json"])
        finally:
            RP.MAX_QUARANTINE = keep

    def _stderr(self, fn):
        import io
        buf, old = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            fn()
        finally:
            sys.stderr = old
        return buf.getvalue()

    def test_46_logs_when_limits_hit(self):
        keep = (RP.MAX_ENTRIES, RP.MAX_SRCS, RP.MAX_GETS)
        RP.MAX_ENTRIES, RP.MAX_SRCS, RP.MAX_GETS = 3, 1, 1
        try:
            for s in "ab":
                for i in (1, 2):
                    self.fs.set(s, "2026-11-%02d.jsonl" % i, good_body(D1))
            out = self._stderr(self.pull)
        finally:
            RP.MAX_ENTRIES, RP.MAX_SRCS, RP.MAX_GETS = keep
        self.assertIn("too_many_entries", out)
        self.assertIn("too_many_srcs", out)
        self.assertIn("run limit reached", out)
        self.assertNotIn("synthetic", out)

    def test_46_reported_and_quarantine_pruned_with_log(self):
        keep = (RP.MAX_REPORTED, RP.MAX_QUARANTINE)
        RP.MAX_REPORTED, RP.MAX_QUARANTINE = 5, 2
        try:
            os.makedirs(self.data, mode=0o700)
            now = int(time.time())
            with open(os.path.join(self.data, "pull_state.json"), "w") as f:
                json.dump({"files": {}, "reported": {"k%d" % i: now - 100 + i for i in range(9)}}, f)
            qd = os.path.join(self.data, "quarantine")
            os.makedirs(qd, mode=0o700)
            for i in range(4):
                p = os.path.join(qd, "q%d.json" % i)
                open(p, "w").write("{}")
                os.utime(p, (now - 50 + i, now - 50 + i))
            self.fs.set("a", D1, good_body(D1))
            out = self._stderr(self.pull)
        finally:
            RP.MAX_REPORTED, RP.MAX_QUARANTINE = keep
        self.assertIn("reported pruned", out)
        self.assertIn("quarantine pruned 2", out)
        rep = json.load(open(os.path.join(self.data, "pull_state.json")))["reported"]
        self.assertEqual(len(rep), 5)
        self.assertNotIn("k0", rep)
        self.assertIn("k8", rep)
        self.assertEqual(sorted(os.listdir(qd)), ["q2.json", "q3.json"])
        self.assertEqual(RP.MAX_REPORTED, 5000)

    def test_46_status_too_big(self):
        keep = RP.MAX_STATUS_BYTES
        RP.MAX_STATUS_BYTES = 50
        try:
            self.fs.set("a", D1, good_body(D1))
            self.assertEqual(self.pull(), 1)
            self.assertIn("bad_status", self.inbox_text())
        finally:
            RP.MAX_STATUS_BYTES = keep


class T47_Unit(unittest.TestCase):
    def test_47_service_directives(self):
        with open(os.path.join(HERE, "deploy", "records-pull.service"), encoding="utf-8") as f:
            t = f.read()
        lines = [l.strip() for l in t.splitlines()]
        for want in ("Nice=19", "CPUQuota=100%", "IOSchedulingClass=idle", "NoNewPrivileges=yes", "ProtectSystem=strict",
                     "PrivateTmp=yes", "ReadWritePaths=%h/.local/share/holdem-records", "UMask=0077",
                     "RestrictAddressFamilies=AF_INET AF_UNIX", "TimeoutStartSec=15min", "Type=oneshot"):
            self.assertIn(want, lines)
        self.assertEqual([l for l in lines if l.startswith("ReadWritePaths=")], ["ReadWritePaths=%h/.local/share/holdem-records"])
        self.assertNotIn(" -I", t)
        self.assertNotIn("python3 -I", t)
        with open(os.path.join(HERE, "deploy", "records-pull.inbox.conf.example"), encoding="utf-8") as f:
            ex = f.read()
        self.assertIn("ReadWritePaths=-%h/<작업 폴더>/<수신함 파일>", ex)


class T48_ExtraNames(unittest.TestCase):
    def test_48_main_is_shape_only(self):
        base = mkbase("t48")
        wr(base, "hands/" + D1, hline(1))
        seen = {}

        class Spy(Rec):
            def __init__(self, base, cfg, *a, **kw):
                seen["kw"] = dict(kw)
                kw.pop("today", None)
                kw.setdefault("require_audit", False)
                super().__init__(base, cfg, *a, today=lambda: TODAY, fake=True, **kw)
        keep_u, keep_c = RS.Uploader, RS.load_config
        RS.Uploader, RS.load_config = Spy, lambda b: fake_cfg()
        try:
            err, sys.stderr = sys.stderr, open(os.devnull, "w")
            try:
                self.assertEqual(RS.main(["--base", base, "--once"]), 0)
            finally:
                sys.stderr.close()
                sys.stderr = err
        finally:
            RS.Uploader, RS.load_config = keep_u, keep_c
        self.assertIn("extra_names", seen["kw"])
        self.assertIsNone(seen["kw"]["extra_names"])
        self.assertEqual(json.load(open(os.path.join(base, RS.STATE_NAME)))["name_check"], "shape_only")

    def test_48_callable_called_each_cycle(self):
        base = mkbase("t48c")
        wr(base, "hands/" + D0, dline(1, extra=',"advice":{"reason":"fine"}'))
        calls = []

        def names():
            calls.append(1)
            return [NICK]
        up = mk(base, fake_cfg(), fake=True, extra_names=names)
        self.assertEqual(run(up), 0)
        self.assertEqual(run(up), 0)
        self.assertEqual(len(calls), 2)
        wr(base, "hands/" + D0, dline(1, extra=',"advice":{"reason":"x synthnick01 y"}'))
        up2 = mk(base, fake_cfg(), fake=True, extra_names=names)
        self.assertEqual(run(up2), 1)
        with open(os.path.join(base, RS.STATE_NAME)) as f:
            txt = f.read()
        self.assertEqual(json.loads(txt)["held"][D0]["rule"], "known_name")
        self.assertEqual(json.loads(txt)["name_check"], "known_names")
        self.assertNotIn(NICK.lower(), txt.lower())


class T49_SchemaSha(unittest.TestCase):
    def test_49_schema_sha(self):
        self.assertEqual(RC.schema_sha256(), RECORDS_SCHEMA_SHA256)


def _const(path, name):
    with open(os.path.join(HERE, path), encoding="utf-8") as f:
        for n in ast.parse(f.read()).body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                return ast.literal_eval(n.value)
    raise KeyError((path, name))


class T50_SchemaVsSource(unittest.TestCase):
    """card-reader 가 키를 늘리면 실패해 판 올림을 강제한다 (import 하지 않고 ast 로 읽는다)."""

    def test_50_constants_in_schema(self):
        dec = RC.SCHEMA_DECISION["k"]
        self.assertEqual(set(_const("decisionlog.py", "PROFILE_KEYS")), set(RC.PROFILE["k"]))
        for k in _const("decisionlog.py", "CTX_DIRECT"):
            self.assertIn(k, dec, k)
        for k in _const("decisionlog.py", "TOURNEY_KEYS"):
            self.assertIn(k, RC.TOURNEY["k"], k)
        self.assertEqual(set(_const("seattypes.py", "STAT_KEYS")), set(RC.TYPE_STATS["k"]))
        self.assertEqual(_const("handread.py", "CLASSES_POST") + _const("handread.py", "CLASSES_PRE"), list(RC.CLASSES))
        pos = set(_const("handlog.py", "PRE_RANK"))
        self.assertEqual(pos, set(_const("seattypes.py", "POST_ORDER")))
        for p in pos:
            self.assertTrue(RC._RES["POS"].fullmatch(p), p)
        self.assertEqual(len(pos), 11)


# ----------------------------------------------------------------------------------------------
# 개정 3 (명세 13.6): 시험 51~57, 50 확장
KO = "가나다"                                    # 합성 한글 이름


def _stats():
    return {"keys": 0, "paths": collections.Counter(), "values": 0, "vpaths": collections.Counter()}


class T51_DropValues(unittest.TestCase):
    RAW_HAND = (('{"id":"%s","players":"6","hero_cards":["As","Xx"],"streets":"preflop2","end_reason":"zzzvalue","pot":5,'
                 '"seats":{"hero":{"hero":true,"pid":null,"pos":"qqqvalue"}}}\n') % HID0).encode()

    def test_51_hand_wrong_formats_dropped(self):
        st1, st2 = _stats(), _stats()
        b1 = RS.build_body(D1, self.RAW_HAND, None, st1)[0]
        b2 = RS.build_body(D1, self.RAW_HAND, None, st2)[0]
        self.assertEqual(hashlib.sha256(b1).digest(), hashlib.sha256(b2).digest())
        rec = json.loads(b1)
        for k in ("players", "hero_cards", "streets", "end_reason"):
            self.assertNotIn(k, rec)
        self.assertEqual(rec["pot"], 5)
        self.assertNotIn("pos", rec["seats"]["hero"])
        self.assertEqual(st1["values"], 5)
        self.assertEqual(dict(st1["vpaths"]), {"players": 1, "hero_cards": 1, "streets": 1, "end_reason": 1,
                                               "seats.hero.pos": 1})
        self.assertEqual(st1["keys"], 0)
        self.assertNotIn(b"zzzvalue", b1)
        self.assertNotIn("qqqvalue", json.dumps(dict(st1["vpaths"])))
        self.assertTrue(RC.check_body(D1, b1).ok())

    def test_51_text_wrong_dropped(self):
        raw = (('{"id":"%s","type":"decision","advice":{"reason":"%s","detail":["x","User​1234"],"plan":"ok"},'
                '"hero_pos":"%s"}\n') % (DID0, "한" * 401, KO)).encode()
        st = _stats()
        body = RS.build_body(D0, raw, None, st)[0]
        rec = json.loads(body)
        self.assertEqual(rec["advice"], {"plan": "ok"})
        self.assertNotIn("hero_pos", rec)
        self.assertEqual(dict(st["vpaths"]), {"advice.reason": 1, "advice.detail": 1, "hero_pos": 1})
        self.assertTrue(RC.check_body(D0, body).ok())

    def test_51_state_counts_not_values(self):
        base = mkbase("t51")
        wr(base, "hands/" + D1, self.RAW_HAND)
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 0)
        st = json.load(open(os.path.join(base, RS.STATE_NAME)))
        e = st["files"][D1]
        self.assertEqual(e["dropped_values"], 5)
        self.assertEqual(e["dropped_value_paths"]["seats.hero.pos"], 1)
        self.assertNotIn("zzzvalue", json.dumps(st))
        self.assertNotIn("qqqvalue", json.dumps(st))


class T52_SeatRef(unittest.TestCase):
    CASES = {
        "action": (D1, '{"id":"%s","actions":{"preflop":[{"seat":"%s","act":"call"}]}}\n' % (HID0, KO)),
        "turns": (D1, '{"id":"%s","turns":[{"street":"flop","seat":"%s"}]}\n' % (HID0, KO)),
        "dealer": (D1, '{"id":"%s","dealer":"%s"}\n' % (HID0, KO)),
        "showdown_seats": (D1, '{"id":"%s","showdown":{"seats":["%s"]}}\n' % (HID0, KO)),
        "bettor": (D0, '{"id":"%s","type":"decision","bettor":{"seat":"%s"}}\n' % (DID0, KO)),
        "later_pos": (D0, '{"id":"%s","type":"resolution","later":[{"pos":"%s"}]}\n' % (DID0, KO)),
    }

    def test_52_seat_ref_stops_without_put(self):
        import io
        import contextlib
        for name, (file, text) in self.CASES.items():
            with self.subTest(name):
                base = mkbase("t52")
                wr(base, "hands/" + file, text.encode())
                up = mk(base, fake_cfg(), fake=True)
                got = []
                up.on_error = lambda *a: got.append(a)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = up.cycle()
                self.assertEqual(code, 2)
                self.assertEqual(up.puts, [])
                txt = open(os.path.join(base, RS.STATE_NAME), encoding="utf-8").read()
                st = json.loads(txt)
                self.assertTrue(st["disabled"])
                self.assertEqual(st["disabled_rule"], "seat_ref")
                for blob in (txt, out.getvalue(), err.getvalue(), repr(got)):
                    self.assertNotIn(KO, blob)


class T53_GeneratedValues(unittest.TestCase):
    def test_53_enums_cover_source_constants(self):
        def consts(path, name):
            with open(os.path.join(HERE, path), encoding="utf-8") as f:
                for n in ast.parse(f.read()).body:
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                        return n.value
            self.skipTest("%s.%s 없음" % (path, name))

        def str_keys(node):
            return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]

        def str_vals(node):
            return [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        E = RC.ENUMS
        act = consts("decisionlog.py", "ACTION_EN")
        self.assertTrue(set(str_keys(act)) <= set(E["ADV_ACTION_KO"]))
        self.assertTrue(set(str_vals(act)) <= set(E["ADV_ACTION"]))
        self.assertTrue(set(str_vals(consts("advisor.py", "COLOR"))) <= set(E["COLOR_KEY"]))
        self.assertEqual(set(str_keys(consts("seattypes.py", "STYLES_KO"))), set(E["STYLE"]))
        self.assertTrue(set(str_keys(consts("icm.py", "STAGE_LABEL"))) <= set(E["STAGE"]))
        self.assertEqual(set(str_keys(consts("icm.py", "STRUCTURES"))), set(E["STRUCTURE"]))
        self.assertTrue(set(str_keys(consts("layout.py", "SLOTS"))) <= set(E["SLOT"]))
        self.assertEqual(tuple(ast.literal_eval(consts("handlog.py", "STREETS"))), E["STREET"])
        sit = set(E["SITUATION"])
        self.assertTrue(set(ast.literal_eval(consts("advisor.py", "SHORT_SITUATIONS"))) <= sit)
        for n in ast.literal_eval(consts("evaluator.py", "CATEGORY_NAMES")):
            self.assertTrue(RC._RES["HAND_NAME"].fullmatch(n) or RC._RES["HAND_NAME"].match(n + " (A)") or
                            n.split()[0] in n, n)

    @staticmethod
    def _import_td():
        """생성 코드 시험 도우미. numpy 가 없으면(리눅스) 빈 numpy 모듈로 대신해서 numpy 없이 가져온다
        (test_handlog 는 numpy 를 import 만 하고 이 시험이 쓰는 경로에서는 쓰지 않는다)."""
        try:
            import numpy  # noqa: F401
        except ImportError:
            import types
            sys.modules.setdefault("numpy", types.ModuleType("numpy"))
        import test_decisionlog as TD
        import decisionlog as dl
        return TD, dl

    def test_53_real_generated_lines_pass_filter(self):
        TD, dl = self._import_td()
        import evaluator as ev
        d = tempfile.mkdtemp(prefix="rec53-")
        try:
            tb = TD.play_to_flop_bet(tag=None)          # 실제 앱처럼 기본 세션 id (시간 기반)
            log = dl.DecisionLog(d)
            name = ev.hand_name(ev.evaluate([ev.parse_card(c) for c in ("Ah", "Kd", "Ac", "7c", "2d")]))
            log.record(TD.make_ctx(hand_name=name, outs=None), TD.make_advice(), state=tb.tr, t=time.time())
            rec = TD.finish_call_to_showdown(tb)
            log.link_hand(rec)
            lines = []
            for f in sorted(glob.glob(os.path.join(d, "hands", "decisions_*.jsonl"))):
                lines += [(RC.file_kind(os.path.basename(f)), x) for x in open(f, encoding="utf-8").read().splitlines() if x]
            lines.append(("hands", json.dumps(rec, ensure_ascii=False)))
            self.assertGreaterEqual(len(lines), 2)
            for kind, text in lines:
                st = _stats()
                _, dk = RC.filter_record(kind, RC.parse_line(text), st["paths"], st)
                # hands 의 notes(자유 문장)는 스키마에 없어 일부러 버려진다. 그 밖에는 버려지거나 형식이 틀린 값이 없어야 한다
                self.assertLessEqual(set(st["paths"]), {"notes"}, (kind, dict(st["paths"])))
                self.assertEqual(st["values"], 0, (kind, dict(st["paths"]), dict(st["vpaths"])))
            ids = [json.loads(x) for k, x in lines]
            self.assertTrue(RC._RES["HID"].fullmatch(rec["id"]), "hand id")
            self.assertTrue(RC._RES["SESSION"].fullmatch(rec["session"]), "session")
            for r in ids:
                if r.get("type") in ("decision", "resolution"):
                    self.assertTrue(RC._RES["DID"].fullmatch(r["id"]), "decision id")
                if r.get("type") == "decision":
                    self.assertTrue(RC._RES["HID"].fullmatch(r["hand_id"]), "hand_id")
                    self.assertTrue(RC._RES["DAY"].fullmatch(r["day"]), "day")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_53_decision_id_without_hand(self):
        """핸드 id 가 없을 때의 결정 id 모양 (t<밀리초>-<번호>)."""
        TD, dl = self._import_td()
        d = tempfile.mkdtemp(prefix="rec53b-")
        try:
            log = dl.DecisionLog(d)
            did = log.record(TD.make_ctx(hand_name="원페어 (A)"), TD.make_advice(), t=1793821519.5)
            self.assertTrue(RC._RES["DID"].fullmatch(did), "id shape")
            self.assertTrue(did.startswith("t"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_53_hand_name_generated_forms(self):
        """evaluator.hand_name 과 reader.preflop_name 이 만드는 모든 값이 HAND_NAME 에 맞는다 (소스를 그대로 가져와 실행)."""
        import evaluator as ev
        with open(os.path.join(HERE, "reader.py"), encoding="utf-8") as f:
            fn = next(n for n in ast.parse(f.read()).body if isinstance(n, ast.FunctionDef) and n.name == "preflop_name")
        ns = {"ev": ev}
        exec(compile(ast.Module([fn], []), "reader.py", "exec"), ns)
        pre = ns["preflop_name"]
        pat = RC._RES["HAND_NAME"]
        names = set()
        for a in range(52):
            for b in range(a + 1, 52):
                names.add(pre([a, b]))
        rnd = random.Random(53)
        cats = set()
        deck = list(range(52))
        for _ in range(4000):
            cs = rnd.sample(deck, rnd.choice((5, 6, 7)))
            sc = ev.evaluate(cs)
            cats.add(sc[0])
            names.add(ev.hand_name(sc))
        for cards in (["As", "Ks", "Qs", "Js", "Ts"], ["As", "2h", "3d", "4c", "5s"], ["9s", "9h", "9d", "9c", "2s"],
                      ["Ks", "Kh", "Kd", "2c", "2s"], ["2s", "2h", "5d", "5c", "9s", "9h"]):
            sc = ev.evaluate([ev.parse_card(c) for c in cards])
            cats.add(sc[0])
            names.add(ev.hand_name(sc))
        self.assertEqual(cats, set(range(9)))
        self.assertGreater(len(names), 100)
        for n in sorted(names):
            self.assertTrue(pat.fullmatch(n), n)


class T54_Text(unittest.TestCase):
    def body(self, reason):
        raw = json.dumps({"id": DID0, "type": "decision", "advice": {"reason": reason}}).encode() + b"\n"   # ascii 이스케이프
        st = _stats()
        b = RS.build_body(D0, raw, None, st)[0]
        return json.loads(b), st

    def test_54_length(self):
        rec, st = self.body("한" * 400)
        self.assertEqual(len(rec["advice"]["reason"]), 400)
        rec, st = self.body("한" * 401)
        self.assertNotIn("reason", rec.get("advice", {}))
        self.assertEqual(st["values"], 1)

    def test_54_forbidden_code_point_range_ends(self):
        bad = [0x00, 0x1F, 0x7F, 0x9F, 0x200B, 0x200F, 0x202A, 0x202E, 0x2060, 0x206F, 0xD800, 0xDFFF, 0xE000, 0xF8FF, 0xFEFF,
               0xFFF0, 0xFFFF]
        ok = [0x20, 0x7E, 0xA0, 0x200A, 0x2010, 0x2029, 0x205F, 0x2070, 0xF900, 0xFFEF]
        for cp in bad:
            with self.subTest("bad %04X" % cp):
                rec, st = self.body("a" + chr(cp) + "b")
                self.assertNotIn("reason", rec.get("advice", {}))
                self.assertEqual(st["values"], 1)
        for cp in ok:
            with self.subTest("ok %04X" % cp):
                rec, st = self.body("a" + chr(cp) + "b")
                self.assertEqual(rec["advice"]["reason"], "a" + chr(cp) + "b")

    def test_54_pid_nick_shaped_passes_but_text_stops(self):
        """정상 pid 는 닉네임 모양처럼 보여도 통과한다 (명세 14.1). 자유 텍스트의 같은 모양은 여전히 막는다."""
        pids = ["p_abc12345", "p_def45678", "p_fed00001"]
        seats = {"opp%d" % (i + 1): {"pid": p, "type": "LAG", "type_src": "popup"} for i, p in enumerate(pids)}
        raw = json.dumps({"id": DID0, "type": "decision", "seats": seats}).encode() + b"\n"
        st = _stats()
        b = RS.build_body(D0, raw, None, st)[0]
        self.assertEqual(sorted(x["pid"] for x in json.loads(b)["seats"].values()), sorted(pids))
        self.assertTrue(RC.check_body(D0, b).ok())
        with self.assertRaises(RS.Block) as cm:
            self.body("call abc1234 now")
        self.assertEqual(cm.exception.rule, "nick_value")

    def test_54_fullwidth_still_stops(self):
        with self.assertRaises(RS.Block) as cm:
            self.body("Ｕｓｅｒ１２３４")
        self.assertEqual(cm.exception.rule, "nick_value")


def _capture(fn):
    import io
    import contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn()
    return code, out.getvalue(), err.getvalue()


def _audit_base(name, files):
    base = mkbase(name)
    for n, data in files.items():
        wr(base, "hands/" + n, data)
    return base


def _dec(i, **extra):
    d = {"id": "20261105_120000-0001/%d" % i, "type": "decision"}
    d.update(extra)
    return (json.dumps(d, ensure_ascii=False) + "\n").encode()


class T55_Audit(unittest.TestCase):
    def audit(self, base, **kw):
        up = mk(base, {}, fake=True)
        return _capture(lambda: up.audit(**kw))

    def test_55_clean_ok(self):
        base = _audit_base("t55a", {D1: hline(1) + hline(2), D0: _dec(1, advice={"reason": "폴드"})})
        code, out, err = self.audit(base)
        self.assertEqual(code, 0, out)
        self.assertIn("verdict: OK", out)
        self.assertIn("OK", out.splitlines()[-1] + out)

    def test_55_unknown_korean_word_check(self):
        base = _audit_base("t55b", {D0: _dec(1, advice={"reason": KO + " 폴드"}) + _dec(2, advice={"reason": NICK})})
        code, out, err = self.audit(base)
        self.assertEqual(code, 1, out)
        self.assertIn("unk_ko=1", out)
        for blob in (out, err, open(os.path.join(base, RS.STATE_NAME), encoding="utf-8").read()):
            self.assertNotIn(KO, blob)
            self.assertNotIn(NICK, blob)

    def test_55_wrong_format_check_counts(self):
        raw = (hline(1, ',"players":"%s"' % NICK) + hline(2, ',"players":"x"') + hline(3))
        base = _audit_base("t55c", {D1: raw})
        code, out, err = self.audit(base, as_json=True)
        self.assertEqual(code, 1)
        j = json.loads(out)
        row = next(r for r in j["paths"] if r["path"] == "players")
        self.assertEqual(row["fail"], 2)
        self.assertNotIn(NICK, out + err)

    def test_55_stop_cases(self):
        for name, file, raw in (("seat_ref", D1, ('{"id":"%s","dealer":"%s"}\n' % (HID0, KO)).encode()),
                                ("name_key", D1, ('{"id":"%s","hero_name":"%s"}\n' % (HID0, NICK)).encode()),
                                ("dup", D1, ('{"id":"%s","pot":1,"pot":2}\n' % HID0).encode())):
            with self.subTest(name):
                base = _audit_base("t55d", {file: raw})
                code, out, err = self.audit(base)
                self.assertEqual(code, 2, out)
                self.assertIn("blocked\t", out)
                for blob in (out, err):
                    self.assertNotIn(KO, blob)
                    self.assertNotIn(NICK, blob)

    def test_55_profiles_db_stops(self):
        base = _audit_base("t55e", {D1: hline(1)})
        wr(base, "profiles.db", b"")
        code, out, err = self.audit(base)
        self.assertEqual(code, 2)
        self.assertIn("profiles_db", out)

    def test_55_distinct_and_keys(self):
        raw = b"".join(hline(i, ',"pot":%d' % (i % 3)) for i in range(1, 10))
        base = _audit_base("t55f", {D1: raw})
        code, out, err = self.audit(base, as_json=True)
        j = json.loads(out)
        pot = next(r for r in j["paths"] if r["path"] == "pot")
        self.assertEqual((pot["n"], pot["distinct"]), (9, 3))
        self.assertEqual(j["keys"]["seat"], 2)                      # hero, seat_TC
        self.assertEqual(j["head"]["lines"], 9)
        self.assertEqual(code, 0)

    def test_55_no_network(self):
        base = _audit_base("t55g", {D1: hline(1)})
        keep = socket.socket

        def boom(*a, **k):
            raise AssertionError("network")
        socket.socket = boom
        try:
            code, out, err = self.audit(base)
        finally:
            socket.socket = keep
        self.assertEqual(code, 0)

    def test_55_state_record_without_values(self):
        base = _audit_base("t55h", {D1: hline(1, ',"players":"%s"' % NICK)})
        self.audit(base)
        st = json.load(open(os.path.join(base, RS.STATE_NAME)))
        a = st["audit"]
        self.assertEqual(a["filter_v"], RC.FILTER_V)
        self.assertEqual(a["through"], "2026-11-01")
        self.assertEqual(sorted(a["files"][D1]), ["accepted", "sha256", "verdict"])
        self.assertNotIn(NICK, json.dumps(st))

    def test_55_cli_exit_codes_and_options(self):
        base = _audit_base("t55i", {D1: hline(1)})
        code, out, err = _capture(lambda: RS.main(["--base", base, "--audit", "--to", "2026-11-01", "--json"]))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["verdict"], "OK")


class T56_AuditGate(unittest.TestCase):
    def up(self, base):
        return mk(base, fake_cfg(), fake=True, require_audit=True)

    def test_56_gate(self):
        TODAY_F = "2026-11-02.jsonl"
        base = _audit_base("t56", {D1: hline(1), TODAY_F: hline(2)})
        a = self.up(base)
        self.assertEqual(run(a), 1)                                  # 점검 기록이 없으면 모든 파일이 막힌다
        self.assertEqual(a.puts, [])
        self.assertEqual({a.files[n]["result"] for n in (D1, TODAY_F)}, {"audit_required"})
        # 점검 뒤: 어제까지는 OK 인 것만, 오늘 파일은 관문이 없다
        self.assertEqual(self.up(base).audit(), 0)
        b = self.up(base)
        self.assertEqual(run(b), 0)
        self.assertEqual(sorted(p[0] for p in b.puts), sorted([D1, TODAY_F]))
        # 원본이 바뀌면 다시 점검해야 한다
        with open(os.path.join(base, "hands", D1), "ab") as f:
            f.write(hline(3))
        c = self.up(base)
        self.assertEqual(run(c), 1)
        self.assertEqual(c.files[D1]["result"], "audit_required")
        self.assertEqual([p[0] for p in c.puts], [TODAY_F])
        # 판이 다르면 다시 점검해야 한다
        self.assertEqual(self.up(base).audit(), 0)
        p = os.path.join(base, RS.STATE_NAME)
        st = json.load(open(p))
        st["audit"]["filter_v"] = RC.FILTER_V - 1
        json.dump(st, open(p, "w"))
        d = self.up(base)
        self.assertEqual(run(d), 1)
        self.assertEqual(d.files[D1]["result"], "audit_required")

    def test_56_check_needs_accept(self):
        base = _audit_base("t56c", {D0: _dec(1, advice={"reason": KO})})
        self.assertEqual(self.up(base).audit(), 1)
        a = self.up(base)
        self.assertEqual(run(a), 1)
        self.assertEqual(a.files[D0]["result"], "audit_required")
        self.assertEqual(self.up(base).audit(accept=True), 1)
        b = self.up(base)
        self.assertEqual(run(b), 0)
        self.assertEqual([p[0] for p in b.puts], [D0])

    def test_56_stop_never_accepted(self):
        base = _audit_base("t56s", {D1: ('{"id":"%s","dealer":"%s"}\n' % (HID0, KO)).encode()})
        self.assertEqual(self.up(base).audit(accept=True), 2)
        a = self.up(base)
        self.assertEqual(run(a), 1)
        self.assertEqual(a.files[D1]["result"], "audit_required")


@linux_only
class T57_WorkDir(unittest.TestCase):
    def cfg(self, name, **kw):
        d = os.path.join(TMP, name)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(os.path.join(d, "ws", "sub"))
        os.chmod(d, 0o700)
        c = {"url": "http://127.0.0.1:1", "token": "synthetic-pull-token", "data_dir": os.path.join(d, "data"),
             "work_dir": os.path.join(d, "ws")}
        c.update(kw)
        for k in [k for k, v in c.items() if v is _MISSING]:
            del c[k]
        p = os.path.join(d, "pull.json")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(c, f)
        return d, p

    def main(self, p):
        return _capture(lambda: RP.main(["--config", p]))

    def test_57_bad_config(self):
        for label, v in (("missing", _MISSING), ("empty", ""), ("relative", "rel/path"), ("number", 5)):
            with self.subTest(label):
                d, p = self.cfg("t57" + label, work_dir=v)
                code, out, err = self.main(p)
                self.assertEqual(code, 2)
                self.assertIn("bad_config", err)
                self.assertNotIn("synthetic-pull-token", err)
        d, p = self.cfg("t57nodir", work_dir=os.path.join(TMP, "t57nodir", "nope"))
        code, out, err = self.main(p)
        self.assertEqual(code, 2)
        self.assertIn("work_dir_missing", err)

    def test_57_data_dir_relations(self):
        d, p = self.cfg("t57rel")
        ws = os.path.join(d, "ws")
        for label, data in (("same", ws), ("inside", os.path.join(ws, "sub", "data")), ("contains", d)):
            with self.subTest(label):
                with self.assertRaises(RP.Refuse) as cm:
                    RP.check_data_dir(data, ws)
                self.assertEqual(str(cm.exception), "data_dir_in_work")
        self.assertTrue(RP.check_data_dir(os.path.join(d, "elsewhere"), ws))

    def test_57_inbox_inside_work_is_fine(self):
        d, p = self.cfg("t57inbox", inbox=os.path.join(TMP, "t57inbox", "ws", "inbox.md"))
        cfg, _ = RP.load_config(p)
        self.assertTrue(RP.check_data_dir(cfg["data_dir"], cfg["work_dir"]))

    def test_57_no_private_words_in_public_files(self):
        words = [w.strip().lower() for w in os.environ.get("RECORDS_PRIVATE_WORDS", "").split(",") if w.strip()]
        if not words:
            self.skipTest("RECORDS_PRIVATE_WORDS 없음")
        files = glob.glob(os.path.join(HERE, "records_*.py")) + [os.path.join(HERE, "test_records.py"),
                                                              os.path.join(HERE, "README.md")] + \
            glob.glob(os.path.join(HERE, "deploy", "*"))
        for f in files:
            with open(f, encoding="utf-8") as fh:
                text = fh.read().lower()
            for w in words:
                self.assertNotIn(w, text, os.path.basename(f))


class _ZeroIdEntry:
    """Windows 의 os.scandir 흉내: DirEntry.stat() 이 st_ino=0, st_dev=0 을 준다 (나머지는 그대로)."""

    def __init__(self, e):
        self._e = e

    def __getattr__(self, k):
        return getattr(self._e, k)

    def stat(self, *a, **kw):
        st = self._e.stat(*a, **kw)
        l = list(st)
        l[1], l[2] = 0, 0                   # st_ino, st_dev
        return os.stat_result(l)


class _ZeroIdScandir:
    def __init__(self, real):
        self.real = real

    def __call__(self, *a, **kw):
        it = self.real(*a, **kw)
        outer = self

        class W:
            def __enter__(s):
                s.it = it.__enter__()
                return s

            def __exit__(s, *x):
                return it.__exit__(*x)

            def __iter__(s):
                return (_ZeroIdEntry(e) for e in s.it)
        return W()


class T58_WindowsIdent(unittest.TestCase):
    """리눅스에서 Windows 동작(DirEntry.stat 의 ino/dev 가 0)을 흉내 낸 회귀 시험."""

    def setUp(self):
        self.real_scandir = RS.os.scandir
        RS.os.scandir = _ZeroIdScandir(self.real_scandir)

    def tearDown(self):
        RS.os.scandir = self.real_scandir

    def test_58_targets_and_open_ok(self):
        base = mkbase("t58a")
        wr(base, "hands/" + D1, hline(1) + hline(2))
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 0)
        self.assertNotIn("changed_during_open", str(up.files))
        self.assertEqual(up.errors, [])
        self.assertEqual(len(up.puts), 1)
        self.assertNotEqual(up.idents[D1][:2], (0, 0))

    def test_58_replacement_still_caught(self):
        base = mkbase("t58b")
        p = wr(base, "hands/" + D1, hline(1))

        class Swap(Rec):
            def targets(self, lo, hi):
                names = super().targets(lo, hi)
                with open(p + ".new", "wb") as f:
                    f.write(hline(1) + hline(2))
                os.replace(p + ".new", p)
                return names
        up = mk(base, fake_cfg(), cls=Swap, fake=True)
        self.assertEqual(run(up), 1)
        self.assertEqual(up.files[D1]["result"], "changed_during_open")
        self.assertEqual(up.puts, [])

    def test_58_audit_no_unreadable(self):
        base = _audit_base("t58c", {D1: hline(1) + hline(2), D0: _dec(1, advice={"reason": "폴드"})})
        up = mk(base, {}, fake=True)
        code, out, err = _capture(lambda: up.audit())
        self.assertEqual(code, 0, out)
        self.assertIn("unreadable=0", out)

    def test_58_same_file_fallback_without_ino(self):
        """ino 를 못 믿을 때: 커지는 것(이어 쓰기)은 같은 파일, 줄거나 mtime 이 되돌아가면 다른 파일."""
        base = mkbase("t58d")
        p = wr(base, "hands/" + D1, hline(1) + hline(2))
        st = os.stat(p)
        ident = (0, 0, st.st_size, st.st_mtime_ns)
        self.assertTrue(RS._same_file(st, ident))
        self.assertTrue(RS._same_file(st, (0, 0, st.st_size - 5, st.st_mtime_ns - 1)))       # 이어 쓰는 중
        self.assertFalse(RS._same_file(st, (0, 0, st.st_size + 5, st.st_mtime_ns)))          # 줄어듦
        self.assertFalse(RS._same_file(st, (0, 0, st.st_size, st.st_mtime_ns + 1)))          # mtime 되돌아감
        # 양쪽 ino 가 실제 값이면 ino 비교
        self.assertFalse(RS._same_file(st, (st.st_dev, st.st_ino + 1, st.st_size, st.st_mtime_ns)))

    def test_58_zero_ident_open_checked(self):
        base = mkbase("t58e")
        p = wr(base, "hands/" + D1, hline(1) + hline(2))
        st = os.stat(p)
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(up._open_checked(p, (0, 0, st.st_size, st.st_mtime_ns)), hline(1) + hline(2))
        with open(p + ".new", "wb") as f:
            f.write(hline(1))
        os.replace(p + ".new", p)                                    # 더 작은 다른 파일
        with self.assertRaises(RS.FileError):
            up._open_checked(p, (0, 0, st.st_size, st.st_mtime_ns))


# 개정 5 (명세 13.7): 컨테이너 형태 강제 (container_type)
KO_NAME = "홍길동"
NICK_ARR = "SomeNick77"
CONTAINER_RULES = frozenset(["container_type", "seats_not_object", "seat_not_object", "reads_key_format",
                             "shown_key_format", "type_stats_field", "profile_field", "issue_code"])


def _container_nodes():
    """스키마의 모든 컨테이너(객체·맵) 노드: [(스키마 이름, 경로 튜플)]. 뿌리는 뺀다. "*" 는 맵 값."""
    out = []

    def walk(node, name, path):
        if isinstance(node, str):
            return
        if path:
            out.append((name, path))
        if "m" in node:
            walk(node["v"], name, path + ("*",))
        else:
            for k, c in node["k"].items():
                walk(c, name, path + (k,))
    for name, sch in (("hand", RC.HAND), ("decision", RC.DECISION), ("resolution", RC.RESOLUTION)):
        walk(sch, name, ())
    return out


class T59_ContainerType(unittest.TestCase):
    RAW = {   # 업로더가 그 문자열을 절대 보내지 않아야 하는 재현 사례
        "actions_str": (D1, '{"id":"%s","actions":"%s"}\n' % (HID0, KO_NAME), "actions"),
        "actions_arr": (D1, '{"id":"%s","actions":["%s"]}\n' % (HID0, KO_NAME), "actions"),
        "actions_flop": (D1, '{"id":"%s","actions":{"flop":"%s"}}\n' % (HID0, KO_NAME), "actions.flop"),
        "turns_str": (D1, '{"id":"%s","turns":"%s"}\n' % (HID0, KO_NAME), "turns"),
        "turns_arr": (D1, '{"id":"%s","turns":["%s"]}\n' % (HID0, KO_NAME), "turns"),
        "turns_arr_second": (D1, '{"id":"%s","turns":[{"street":"flop"},"%s"]}\n' % (HID0, KO_NAME), "turns"),
        "street_pot_str": (D1, '{"id":"%s","street_pot":"%s"}\n' % (HID0, NICK_ARR), "street_pot"),
        "street_pot_arr": (D1, '{"id":"%s","street_pot":["%s"]}\n' % (HID0, KO_NAME), "street_pot"),
        "advice_str": (D0, '{"id":"%s","type":"decision","advice":"%s"}\n' % (DID0, KO_NAME), "advice"),
        "advice_arr": (D0, '{"id":"%s","type":"decision","advice":["%s"]}\n' % (DID0, KO_NAME), "advice"),
        "gto_actions": (D0, '{"id":"%s","type":"decision","advice":{"gto":{"actions":"%s"}}}\n' % (DID0, KO_NAME),
                        "advice.gto.actions"),
        "outcome_str": (D0, '{"id":"%s","type":"resolution","outcome":"%s"}\n' % (DID0, KO_NAME), "outcome"),
    }

    def test_59_uploader_drops_wrong_shape_and_never_sends_it(self):
        for name, (file, text, path) in self.RAW.items():
            with self.subTest(name):
                st1, st2 = _stats(), _stats()
                b1 = RS.build_body(file, text.encode(), None, st1)[0]
                b2 = RS.build_body(file, text.encode(), None, st2)[0]
                self.assertEqual(hashlib.sha256(b1).digest(), hashlib.sha256(b2).digest())      # 결정적
                for secret in (KO_NAME, NICK_ARR):
                    self.assertNotIn(secret.encode(), b1)
                self.assertEqual(st1["values"], 1)
                self.assertEqual(dict(st1["vpaths"]), {path: 1})
                self.assertTrue(RC.check_body(file, b1).ok())

    def test_59_uploader_blocks_seat_related(self):
        cases = {
            "seat_profile": (D0, '{"id":"%s","type":"decision","seats":{"hero":{"profile":"%s"}}}\n' % (DID0, KO_NAME)),
            "seat_stats": (D1, '{"id":"%s","seats":{"hero":{"stats":["%s"]}}}\n' % (HID0, KO_NAME)),
            "reads_value": (D0, '{"id":"%s","type":"decision","reads":{"hero":"%s"}}\n' % (DID0, KO_NAME)),
            "reads_classes": (D0, '{"id":"%s","type":"decision","reads":{"hero":{"classes":"%s"}}}\n' % (DID0, KO_NAME)),
            "showdown_str": (D1, '{"id":"%s","showdown":"%s"}\n' % (HID0, KO_NAME)),
            "showdown_arr": (D1, '{"id":"%s","showdown":["%s"]}\n' % (HID0, NICK_ARR)),
        }
        for name, (file, text) in cases.items():
            with self.subTest(name):
                with self.assertRaises(RS.Block) as cm:
                    RS.build_body(file, text.encode(), None, _stats())
                self.assertEqual(cm.exception.rule, "container_type")
                for secret in (KO_NAME, NICK_ARR):
                    self.assertNotIn(secret, repr(cm.exception.__dict__) + str(cm.exception))

    def test_59_num0_negative_dropped(self):
        raw = ('{"id":"%s","street_pot":{"flop":-1,"turn":0,"river":5}}\n' % HID0).encode()
        st = _stats()
        rec = json.loads(RS.build_body(D1, raw, None, st)[0])
        self.assertEqual(rec["street_pot"], {"turn": 0, "river": 5})
        self.assertEqual(dict(st["vpaths"]), {"street_pot.flop": 1})

    def test_59_null_and_empty_stay_ok(self):
        raw = ('{"id":"%s","actions":null,"showdown":{},"street_pot":null,"turns":[{}],"issues":[]}\n' % HID0).encode()
        st = _stats()
        body = RS.build_body(D1, raw, None, st)[0]
        self.assertEqual(st["values"], 0)
        self.assertTrue(RC.check_body(D1, body).ok())

    def test_59_every_container_fixture_is_filtered(self):
        """container_type 계열 거부 픽스처 전부: 업로더는 멈추거나, 그 문자열 없이 검사를 통과하는 줄만 만든다."""
        n = 0
        for c, b in fixture_cases():
            e = c["expect"]
            if not c["name"].startswith("container_") or e["class"] != "block":
                continue
            line = b.split(b"\n")[e["line"] - 1]
            kind = RC.file_kind(c["file"])
            try:
                out, _ = RC.filter_record(kind, RC.parse_line(line.decode("utf-8")), None, _stats())
            except RC.FilterBlock:
                n += 1
                continue
            data = RS._dump(out).encode("utf-8")
            for secret in (KO_NAME, NICK_ARR):
                self.assertNotIn(secret.encode(), data, c["name"])
            self.assertTrue(RC.check_line(kind, data).ok(), c["name"])
            n += 1
        self.assertGreaterEqual(n, 100)

    def test_59_audit_counts_wrong_shape_as_check(self):
        base = _audit_base("t59a", {D1: ('{"id":"%s","actions":"%s"}\n' % (HID0, KO_NAME)).encode()})
        code, out, err = _capture(lambda: mk(base, {}, fake=True).audit(as_json=True))
        self.assertEqual(code, 1, out)
        j = json.loads(out)
        self.assertEqual(j["files"][0]["fail"], 1)
        self.assertEqual(next(r for r in j["paths"] if r["path"] == "actions")["fail"], 1)
        self.assertNotIn(KO_NAME, out + err)
        base = _audit_base("t59b", {D1: ('{"id":"%s","seats":{"hero":{"stats":"%s"}}}\n' % (HID0, KO_NAME)).encode()})
        code, out, err = _capture(lambda: mk(base, {}, fake=True).audit())
        self.assertEqual(code, 2, out)
        self.assertIn("container_type", out)
        self.assertNotIn(KO_NAME, out + err)

    def test_59_audit_filter_v3_needs_recheck(self):
        base = _audit_base("t59c", {D1: hline(1)})
        up = mk(base, fake_cfg(), fake=True, require_audit=True)
        self.assertEqual(_capture(up.audit)[0], 0)
        p = os.path.join(base, RS.STATE_NAME)
        st = json.load(open(p))
        self.assertEqual(st["audit"]["filter_v"], RC.FILTER_V)
        st["audit"]["filter_v"] = 3
        json.dump(st, open(p, "w"))
        d = mk(base, fake_cfg(), fake=True, require_audit=True)
        self.assertEqual(run(d), 1)
        self.assertEqual(d.files[D1]["result"], "audit_required")

    def test_59_every_schema_container_has_reject_fixture(self):
        """스키마의 모든 컨테이너 노드에 거부 픽스처가 최소 하나 있어야 한다 (스키마에 컨테이너를 더하면 여기서 잡힌다)."""
        seen = set()
        for c, b in fixture_cases():
            e = c["expect"]
            if e["class"] != "block" or e["rule"] not in CONTAINER_RULES or "path" not in e:
                continue
            top = RC.parse_line(b.split(b"\n")[e["line"] - 1].decode("utf-8"))
            sch = RC.schema_for(RC.file_kind(c["file"]), top)
            name = {id(RC.HAND): "hand", id(RC.DECISION): "decision", id(RC.RESOLUTION): "resolution"}.get(id(sch))
            segs = tuple(x for x in e["path"].split(".") if x != "[]")
            seen.add((name, segs))

        def covered(name, path):
            return any(n == name and len(sg) == len(path) and all(p in ("*", s) for p, s in zip(path, sg))
                       for n, sg in seen)
        nodes = _container_nodes()
        self.assertGreaterEqual(len(nodes), 40)
        missing = [(n, ".".join(p)) for n, p in nodes if not covered(n, p)]
        self.assertEqual(missing, [], "컨테이너 노드에 거부 픽스처가 없음")


def _fixture_by_prefix(*prefixes):
    return [(c, b) for c, b in fixture_cases() if c["name"].startswith(prefixes)]


class T60_TextShape(unittest.TestCase):
    """명세 16.2~16.4: 자유 텍스트 잎의 모양 (TEXT 는 문자열만, TEXTS 는 평평한 문자열 배열)."""

    def test_60_wrong_shape_dropped_not_blocked(self):
        cases = [(c, b) for c, b in _fixture_by_prefix("text_shape_") if c["expect"]["class"] == "block"]
        self.assertEqual(len(cases), 32)
        for c, b in cases:
            with self.subTest(c["name"]):
                kind = RC.file_kind(c["file"])
                st1, st2 = _stats(), _stats()
                out1, dk = RC.filter_record(kind, RC.parse_line(b.decode("utf-8").splitlines()[0]), st1["paths"], st1)
                out2, _ = RC.filter_record(kind, RC.parse_line(b.decode("utf-8").splitlines()[0]), st2["paths"], st2)
                self.assertEqual(st1["values"], 1)                          # 그 키 하나만 버려짐
                self.assertEqual(list(st1["vpaths"]), [c["expect"]["path"]])
                self.assertEqual(dk, 1 if c["name"] == "text_shape_obj_elem" else 0)   # 객체 원소의 키 x 는 스키마 밖이라 먼저 버려진다
                body = (RS._dump(out1) + "\n").encode("utf-8")
                self.assertNotIn(KO.encode(), body)
                self.assertNotIn(KO, json.dumps(dict(st1["vpaths"])))
                self.assertEqual(body, (RS._dump(out2) + "\n").encode("utf-8"))   # 같은 원본 -> 같은 바이트
                self.assertEqual(RC.check_line(kind, body.rstrip(b"\n")).cls, "ok")     # D2 뒤 본문은 18 에 안 걸림

    def test_60_ok_cases_drop_nothing(self):
        cases = _fixture_by_prefix("ok_text_flat_arrays", "ok_texts_bare_string")
        self.assertEqual(len(cases), 2)
        for c, b in cases:
            with self.subTest(c["name"]):
                st = _stats()
                RC.filter_record(RC.file_kind(c["file"]), RC.parse_line(b.decode("utf-8").splitlines()[0]), st["paths"], st)
                self.assertEqual(st["values"], 0)
                self.assertEqual(dict(st["paths"]), {})

    def test_60_leaf_list_ok_follows_type_table(self):
        n17 = ["x"] * 17
        self.assertFalse(RC.leaf_list_ok("TEXT", []))
        self.assertFalse(RC.leaf_list_ok("TEXT", ["x"]))
        self.assertTrue(RC.leaf_list_ok("TEXTS", []))
        self.assertTrue(RC.leaf_list_ok("TEXTS", ["x"] * 16))
        self.assertFalse(RC.leaf_list_ok("TEXTS", n17))
        for bad in (["x", ["y"]], ["x", None], [1], [True], [{}]):
            self.assertFalse(RC.leaf_list_ok("TEXTS", bad))
        for typ, t in RC.TYPES.items():
            if not t["text"]:
                self.assertTrue(RC.leaf_list_ok(typ, [[["x"]]]), typ)
        self.assertTrue(RC.leaf_list_ok("X", [None]))
        self.assertEqual([k for k, t in RC.TYPES.items() if t["text"]], ["TEXT", "TEXTS"])

    def test_60_every_text_leaf_has_shape_fixtures(self):
        names = {c["name"] for c, _ in fixture_cases()}
        leaves = []

        def walk(node, name, path):
            if isinstance(node, str):
                if RC.TYPES.get(node, {}).get("text"):
                    leaves.append((name, path, node))
            elif "m" in node:
                walk(node["v"], name, path + ("star",))
            else:
                for k, c in node["k"].items():
                    walk(c, name, path + (k,))
        for name, sch in (("hand", RC.HAND), ("dec", RC.DECISION), ("res", RC.RESOLUTION)):
            walk(sch, name, ())
        self.assertEqual(len(leaves), 8)
        for name, path, typ in leaves:
            base = "text_shape_%s_%s" % (name, "_".join(path))
            want = [base + "_nested", base + "_nullelem"] + ([base + "_over"] if RC.TYPES[typ]["list"] else
                                                             [base + "_list", base + "_emptylist"])
            for w in want:
                self.assertIn(w, names)

    def test_60_dump_has_list_and_items_max(self):
        d = json.loads(RC.schema_dump())
        self.assertEqual(d["text_items_max"], 16)
        self.assertEqual((d["v"], d["accept_min"]), (5, 5))
        self.assertEqual(d["nick_types"], ["LINE", "NODE", "TEXT", "TEXTS", "X"])
        self.assertEqual(d["types"]["TEXTS"]["list"], 1)
        self.assertTrue(all(t["list"] == 0 for k, t in d["types"].items() if k != "TEXTS"))

    def test_60_uploader_state_counts_dropped_paths(self):
        raw = (('{"id":"%s","type":"decision","advice":{"reason":[["%s"]],"detail":["a",null],"plan":["ok"]},'
                '"reads":{"hero":{"line":["%s"]}}}\n') % (DID0, KO, KO)).encode()
        base = mkbase("t60u")
        wr(base, "hands/" + D0, raw)
        up = mk(base, fake_cfg(), fake=True)
        self.assertEqual(run(up), 0)
        st = json.load(open(os.path.join(base, RS.STATE_NAME)))
        e = st["files"][D0]
        self.assertEqual(e["dropped_values"], 3)
        self.assertNotIn(KO, json.dumps(st))
        self.assertEqual(sorted(e["dropped_value_paths"]), ["advice.detail", "advice.reason", "reads.hero.line"])


class T61_AuditEnglish(unittest.TestCase):
    """명세 16.6: --audit 의 unk_en, where 줄, AUDIT_V, 사전."""

    def audit(self, base, **kw):
        up = mk(base, {}, fake=True)
        return _capture(lambda: up.audit(**kw))

    def test_61_app_words_are_known(self):
        texts = ["폴드 AhKd AKs T9o QQ BTN_BB x_bet33_c SPR 4.1", "4.2BB 3x UTG 레이즈 AKQ As 100 2026 GTO ICM",
                 "bet raise call check fold flush top_heavy"]
        base = _audit_base("t61a", {D0: _dec(1, advice={"reason": texts[0], "detail": texts[1:], "plan": [texts[2]]})})
        code, out, err = self.audit(base, as_json=True)
        self.assertEqual(code, 0, out)
        j = json.loads(out)
        self.assertEqual(j["files"][0]["unk_en"], 0, out)
        self.assertTrue(all(r.get("unk_en", 0) == 0 for r in j["paths"]))

    def test_61_unknown_token_check_and_no_value_output(self):
        base = _audit_base("t61b", {D0: _dec(1, advice={"reason": "Tiger"})})
        code, out, err = self.audit(base)
        self.assertEqual(code, 1, out)
        self.assertIn("unk_en=1", out)
        for blob in (out, err, open(os.path.join(base, RS.STATE_NAME), encoding="utf-8").read()):
            self.assertNotIn("Tiger", blob)
            self.assertNotIn("tiger", blob)

    def test_61_distinct_count_and_fullwidth(self):
        def count(*texts):
            base = _audit_base("t61c%d" % abs(hash(texts)), {D0: "".join(_dec(i + 1, advice={"reason": t}).decode()
                                                                         for i, t in enumerate(texts)).encode()})
            _, out, _ = self.audit(base, as_json=True)
            return json.loads(out)["files"][0]["unk_en"]
        self.assertEqual(count("kim12", "KIM12"), 1)
        self.assertEqual(count("kim12", "kim13"), 2)
        self.assertEqual(count("Ｔｉｇｅｒ"), 1)         # 전각 Tiger
        self.assertEqual(count("Tiger", "tiger"), 1)

    def test_61_list_elements_counted_on_their_row(self):
        base = _audit_base("t61d", {D0: _dec(1, advice={"detail": ["폴드", "Tiger"], "gto": {"notes": ["Lion"]}})})
        code, out, err = self.audit(base, as_json=True)
        self.assertEqual(code, 1)
        rows = {r["path"]: r for r in json.loads(out)["paths"]}
        self.assertEqual(rows["advice.detail"]["unk_en"], 1)
        self.assertEqual(rows["advice.detail"]["type"], "TEXTS")
        self.assertEqual(rows["advice.gto.notes"]["unk_en"], 1)
        self.assertNotIn("Tiger", out)

    def test_61_vocab_en_sources(self):
        d = tempfile.mkdtemp(prefix="rec61-")
        try:
            with open(os.path.join(d, "app.py"), "w", encoding="utf-8") as f:
                f.write('"""moduledoc docword"""\nX = "constword Hello"\n\ndef f():\n    """fdoc"""\n    "baretext"\n    return f"fpart {X}"\n')
            for n, w in (("test_x.py", "testword"), ("records_x.py", "recword")):
                with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                    f.write('Y = "%s"\n' % w)
            v = RS.load_vocab_en(here=d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        for w in ("constword", "hello", "fpart", "bet", "raise", "flush", "top", "heavy"):
            self.assertIn(w, v)
        for w in ("moduledoc", "docword", "fdoc", "baretext", "testword", "recword"):
            self.assertNotIn(w, v)

    def test_61_where_lines_physical_and_capped(self):
        lines = []
        for i in range(1, 40):
            if i == 5:
                lines.append("\n")                                       # 빈 줄
            else:
                lines.append(_dec(i, advice={"reason": "Word%dxx" % i if i % 3 == 0 else "폴드"}).decode())
        raw = "".join(lines).encode()
        base = _audit_base("t61f", {D0: raw})
        code, out, err = self.audit(base, as_json=True)
        self.assertEqual(code, 1)
        f = json.loads(out)["files"][0]
        want = [i for i in range(1, 40) if i % 3 == 0][:10]          # 빈 줄(5번)도 물리 줄 번호로 센다
        self.assertEqual(f["where"]["unk_en"], want)
        self.assertEqual(f["where"]["unk_ko"], [])
        code, out, err = self.audit(base)
        self.assertIn("where\t%s\tunk_en\t%s" % (D0, ",".join(map(str, want))), out)
        self.assertNotIn("Word3x", out + err)
        # 빈 줄 뒤의 줄 번호가 밀리지 않음
        raw2 = (_dec(1, advice={"reason": "폴드"}).decode() + "\n" + _dec(2, advice={"reason": "Tiger"}).decode()).encode() + b"\n"
        base = _audit_base("t61g", {D0: raw2})
        self.assertEqual(json.loads(self.audit(base, as_json=True)[1])["files"][0]["where"]["unk_en"], [3])

    def test_61_no_network(self):
        base = _audit_base("t61h", {D0: _dec(1, advice={"reason": "Tiger"})})
        keep = socket.socket

        def boom(*a, **k):
            raise AssertionError("network")
        socket.socket = boom
        try:
            code, out, err = self.audit(base)
        finally:
            socket.socket = keep
        self.assertEqual(code, 1)

    def test_61_state_and_gate(self):
        base = _audit_base("t61i", {D1: hline(1)})
        self.assertEqual(self.audit(base)[0], 0)
        p = os.path.join(base, RS.STATE_NAME)
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        a = st["audit"]
        self.assertEqual(RS.AUDIT_V, 2)
        self.assertEqual(a["audit_v"], RS.AUDIT_V)
        self.assertEqual(len(a["vocab_en_sha"]), 64)
        self.assertEqual(a["vocab_en_sha"], hashlib.sha256("\n".join(sorted(RS.load_vocab_en())).encode()).hexdigest())

        def gate(mut):
            with open(p, encoding="utf-8") as f:
                s2 = json.load(f)
            mut(s2["audit"])
            with open(p, "w", encoding="utf-8") as f:
                json.dump(s2, f)
            d = mk(base, fake_cfg(), fake=True, require_audit=True)
            run(d)
            return d.files[D1]["result"]
        self.assertNotEqual(gate(lambda a_: None), "audit_required")
        # 사전 sha 는 관문에 쓰지 않는다
        self.assertNotEqual(gate(lambda a_: a_.update(vocab_en_sha="0" * 64, vocab_sha="0" * 64)), "audit_required")
        self.assertEqual(gate(lambda a_: a_.update(audit_v=1)), "audit_required")
        self.assertNotEqual(gate(lambda a_: a_.update(audit_v=RS.AUDIT_V)), "audit_required")
        self.assertEqual(gate(lambda a_: a_.pop("audit_v")), "audit_required")

    def test_61_old_audit_not_merged(self):
        base = _audit_base("t61j", {D1: hline(1)})
        self.assertEqual(self.audit(base)[0], 0)
        p = os.path.join(base, RS.STATE_NAME)
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        st["audit"]["audit_v"] = 1
        st["audit"]["files"]["2026-10-30.jsonl"] = {"sha256": "0" * 64, "verdict": "OK", "accepted": False}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(st, f)
        self.assertEqual(self.audit(base)[0], 0)
        with open(p, encoding="utf-8") as f:
            a = json.load(f)["audit"]
        self.assertNotIn("2026-10-30.jsonl", a["files"])
        self.assertEqual(a["audit_v"], RS.AUDIT_V)

    def test_61_generated_lines_have_no_unknown_words(self):
        TD, dl = T53_GeneratedValues._import_td()
        import evaluator as ev
        d = tempfile.mkdtemp(prefix="rec61g-")
        try:
            tb = TD.play_to_flop_bet(tag=None)
            log = dl.DecisionLog(d)
            name = ev.hand_name(ev.evaluate([ev.parse_card(c) for c in ("Ah", "Kd", "Ac", "7c", "2d")]))
            log.record(TD.make_ctx(hand_name=name, outs=None), TD.make_advice(), state=tb.tr, t=time.time())
            rec = TD.finish_call_to_showdown(tb)
            log.link_hand(rec)
            dec = b""
            for f in sorted(glob.glob(os.path.join(d, "hands", "decisions_*.jsonl"))):
                dec += open(f, "rb").read()
            hand = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
        finally:
            shutil.rmtree(d, ignore_errors=True)
        base = _audit_base("t61k", {D0: dec, D1: hand})
        code, out, err = self.audit(base, as_json=True)
        j = json.loads(out)
        for f in j["files"]:
            self.assertEqual((f["unk_ko"], f["unk_en"]), (0, 0), f)


def _hand_pid(pid):
    return ('{"id":"20261105_120000-0001","seats":{"hero":{"hero":true,"pid":null,"pos":"BTN"},"seat_TC":{"pid":"%s"}}}\n'
            % pid).encode()


def _held_base(name, names, reason=None, extra=None, other=True):
    """D0 에 reason(또는 extra) 이 든 결정 한 줄, D1 에 정상 손 한 줄. (base, up, code)"""
    base = mkbase(name)
    ex = extra if extra is not None else ',"advice":{"reason":%s}' % json.dumps(reason, ensure_ascii=False)
    wr(base, "hands/" + D0, dline(1, extra=ex))
    if other:
        wr(base, "hands/" + D1, hline(1))
    errs = []
    up = mk(base, fake_cfg(), fake=True, extra_names=names, on_error=lambda *a: errs.append(a))
    up.errs = errs
    return base, up, run(up)


def _state(base):
    with open(os.path.join(base, RS.STATE_NAME), encoding="utf-8") as f:
        return json.load(f)


class T62_NameHold(unittest.TestCase):
    """알려진 이름 검사: 고정 칸 제외, 토큰 일치, 파일 보류(held)."""

    def test_62_no_false_positive_pid_hex(self):
        base = mkbase("t62a")
        wr(base, "hands/" + D1, _hand_pid("p_cafe1234") + _hand_pid("p_beef0000").replace(b"0001", b"0002"))
        up = mk(base, fake_cfg(), fake=True, extra_names=["cafe", "beef", "face"])
        self.assertEqual(run(up), 0)
        self.assertEqual(len(up.puts), 1)
        self.assertEqual(_state(base)["held"], {})

    def test_62_no_false_positive_action_words(self):
        ex = (',"advice":{"action":"fold","action_ko":"폴드","color_key":"call","gto":{"actions":{"fold":1,"call":2,"check":3},'
              '"line":"x_bet33_c","node":"check:call"}},"hero_pos":"BTN","street":"flop"')
        base, up, code = _held_base("t62b", ["fold", "call", "check"], extra=ex, other=False)
        self.assertEqual(code, 0, _state(base))
        self.assertEqual(len(up.puts), 1)
        self.assertEqual(_state(base)["held"], {})

    def test_62_no_false_positive_cards_and_substring(self):
        ex = ',"hero":["Js","Jh"],"hand_name":"원페어 (J)","advice":{"gto":{"combo":"JsJh"},"reason":"jack77 is fine"}'
        base, up, code = _held_base("t62c", ["Jack"], extra=ex, other=False)
        self.assertEqual(code, 0, _state(base))
        self.assertEqual(len(up.puts), 1)

    def test_62_true_hits_free_text(self):
        for i, (names, text) in enumerate([
                (["Jack"], "Jack"), (["Jack"], "(Jack)"), (["Jack"], "Jack,"), (["Jack"], "vs JACK raise"),
                (["Synth Nick01"], "synth nick01"), (["Synth Nick01"], "synthnick01"), (["SynthNick01"], "synth nick01"),
                (["Jack"], "Jack이 레이즈"), (["테스트닉"], "테스트닉님이 콜"), (["테스트닉"], "테스트닉이"),
                (["테스트닉"], "[테스트닉]"), (["Ｊack"], "jack")]):
            with self.subTest(text=text):
                base, up, code = _held_base("t62d%d" % i, names, reason=text)
                self.assertEqual(code, 1)
                self.assertEqual(sorted(_state(base)["held"]), [D0])
                self.assertEqual([p[0] for p in up.puts], [D1])

    def test_62_code_field_hit(self):
        ex = ',"advice":{"gto":{"node":"kingfish"}}'
        base, up, code = _held_base("t62e", ["kingfish"], extra=ex)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(_state(base)["held"]), [D0])

    def test_62_held_d0_uploads_d1_only_and_no_names_in_output(self):
        base, up, code = _held_base("t62f", ["Jack"], reason="hi Jack")
        self.assertEqual(code, 1)
        self.assertEqual([p[0] for p in up.puts], [D1])
        st = _state(base)
        self.assertEqual(st["last_result"], "held")
        self.assertEqual(st["files"][D0]["result"], "held")
        self.assertEqual(st["files"][D1]["result"], "uploaded")
        h = st["held"][D0]
        self.assertEqual((h["rule"], h["line"]), ("known_name", 1))
        self.assertEqual(len(h["prefix_sha256"]), 64)
        blob = (json.dumps(st) + repr(up.errs)).lower()
        self.assertNotIn("jack", blob)
        self.assertEqual(up.errs, [("records_sync", "held", "known_name", D0, 1)])

    def test_62_held_persists_without_names_and_on_append(self):
        base, up, code = _held_base("t62g", ["Jack"], reason="hi Jack")
        since = _state(base)["held"][D0]["since"]
        up2 = mk(base, fake_cfg(), fake=True, extra_names=[])
        self.assertEqual(run(up2), 1)
        self.assertEqual([x for x in up2.puts if x[0] == D0], [])
        self.assertEqual(_state(base)["held"][D0]["since"], since)
        with open(os.path.join(base, "hands", D0), "ab") as f:
            f.write(dline(2, extra=',"advice":{"reason":"fine"}'))
        up3 = mk(base, fake_cfg(), fake=True, extra_names=["Jack"])
        self.assertEqual(run(up3), 1)
        self.assertEqual([x for x in up3.puts if x[0] == D0], [])
        self.assertIn(D0, _state(base)["held"])

    def test_62_release_by_edit(self):
        base, up, code = _held_base("t62h", ["Jack"], reason="hi Jack")
        wr(base, "hands/" + D0, dline(1, extra=',"advice":{"reason":"hi there"}'))
        up2 = mk(base, fake_cfg(), fake=True, extra_names=[])          # 해시가 바뀌어도 이름이 없으면 유지
        self.assertEqual(run(up2), 1)
        self.assertIn(D0, _state(base)["held"])
        up3 = mk(base, fake_cfg(), fake=True, extra_names=["Jack"])
        self.assertEqual(run(up3), 0)
        self.assertIn(D0, [p[0] for p in up3.puts])
        self.assertEqual(_state(base)["held"], {})

    def test_62_release_held_command(self):
        base, up, code = _held_base("t62i", ["Jack"], reason="hi Jack")
        self.assertEqual(RS.main(["--base", base, "--release-held", D0]), 0)
        self.assertEqual(_state(base)["held"], {})
        up2 = mk(base, fake_cfg(), fake=True, extra_names=["Jack"])
        self.assertEqual(run(up2), 1)                                    # 다시 검사해서 또 보류
        self.assertIn(D0, _state(base)["held"])
        self.assertEqual(RS.main(["--base", base, "--release-held", "all"]), 0)
        self.assertEqual(_state(base)["held"], {})
        self.assertEqual(RS.main(["--base", base, "--reset-disabled"]), 0)

    def test_62_reset_disabled_keeps_held(self):
        base, up, code = _held_base("t62j", ["Jack"], reason="hi Jack")
        st = _state(base)
        st["disabled"] = True
        RS.save_state_atomic(os.path.join(base, RS.STATE_NAME), st)
        self.assertEqual(RS.main(["--base", base, "--reset-disabled"]), 0)
        st = _state(base)
        self.assertNotIn("disabled", st)
        self.assertIn(D0, st["held"])

    def test_62_format_block_still_disables_and_keeps_held(self):
        base, up, code = _held_base("t62k", ["Jack"], reason="hi Jack", other=False)
        wr(base, "hands/" + D1, b'{"id":"20261105_120000-0001","id":"20261105_120000-0002"}\n')
        up2 = mk(base, fake_cfg(), fake=True, extra_names=["Jack"])
        self.assertEqual(run(up2), 2)
        st = _state(base)
        self.assertEqual(st["disabled_rule"], "dup_key")
        self.assertIn(D0, st["held"])

    def test_62_held_saved_when_network_fails(self):
        base = mkbase("t62l")
        wr(base, "hands/" + D0, dline(1, extra=',"advice":{"reason":"hi Jack"}'))
        wr(base, "hands/" + D1, hline(1))
        up = mk(base, fake_cfg(), extra_names=["Jack"])                  # fake 아님: 연결 실패
        up._connect = lambda: (_ for _ in ()).throw(RS.ConnFail())
        self.assertEqual(run(up), 3)
        self.assertIn(D0, _state(base)["held"])

    def test_62_held_pruned_when_file_gone(self):
        base, up, code = _held_base("t62m", ["Jack"], reason="hi Jack")
        os.remove(os.path.join(base, "hands", D0))
        up2 = mk(base, fake_cfg(), fake=True, extra_names=["Jack"])
        run(up2)
        self.assertEqual(_state(base)["held"], {})

    def test_62_name_check_report(self):
        for i, (names, want, cnt) in enumerate([
                (lambda: [], "shape_only", 0), (["abc", "ab"], "shape_only", 0), (["12345678"], "shape_only", 0),
                (["Jack", "abc", "Jack"], "known_names", 1), (["Jack", "테스트닉"], "known_names", 2)]):
            with self.subTest(i=i):
                base, up, code = _held_base("t62n%d" % i, names, reason="fine", other=False)
                st = _state(base)
                self.assertEqual((st["name_check"], st["name_count"]), (want, cnt))

    def test_62_korean_names_vs_app_phrases(self):
        # 합성 이름만. 앱 고정 문구와 겹치는 짧은 이름은 건너뛴다
        txt = "SB 블라인드 방어, 플러시 드로우, 기타, 중간·약한 페어, 브로드웨이 올인"
        base, up, code = _held_base("t62k1", ["블라인드", "플러시드로", "기타", "브로드웨이", "중간"], reason=txt)
        self.assertEqual(code, 0)
        self.assertEqual(_state(base)["held"], {})

    def test_62_korean_short_names_word_boundary(self):
        ns = RS.NameSet(["라마", "가나다", "바사아자"])
        self.assertEqual(len(ns), 3)
        self.assertTrue(ns.hit("text", "그 라마 가 말함"))
        self.assertTrue(ns.hit("text", "라마"))
        self.assertFalse(ns.hit("text", "올라마다"))
        self.assertFalse(ns.hit("text", "라마가"))
        self.assertTrue(RS.NameSet(["라마"]).hit("text", "[라마]"))
        self.assertFalse(RS.NameSet(["라마"]).hit("text", "라마1"))

    def test_62_korean_names_with_josa(self):
        ns = RS.NameSet(["가나다", "바사아자"])
        for t in ("가나다이", "가나다님이", "가나다의", "바사아자는 콜", "x 바사아자님"):
            self.assertTrue(ns.hit("text", t), t)
        base, up, code = _held_base("t62k2", ["바사아자"], reason="바사아자님이 레이즈")
        self.assertEqual(code, 1)
        self.assertEqual(_state(base)["held"][D0]["rule"], "known_name")

    def test_62_short_and_numeric_names_ignored(self):
        base, up, code = _held_base("t62o", ["abc", "12345", "ab"], reason="abc 12345 ab")
        self.assertEqual(code, 0)

    def test_62_name_check_strings_selection(self):
        rec = RC.parse_line(json.dumps({
            "id": DID0, "type": "decision", "seats": {"seat_TC": {"pid": "p_cafe1234", "type": "LAG"}},
            "advice": {"action": "fold", "reason": "hello", "gto": {"line": "x_c", "actions": {"fold": 1}}}}))
        got = RC.name_check_strings(RC.file_kind(D0), rec)
        self.assertIn(("text", "hello"), got)
        self.assertIn(("code", "x_c"), got)
        self.assertIn(("code", "fold"), got)
        vals = [s for _, s in got]
        self.assertNotIn("p_cafe1234", vals)
        self.assertNotIn("LAG", vals)
        self.assertEqual(vals.count("fold"), 1)          # 행동 enum 값은 없고 맵 키만


_MISSING = object()


class T63_LineCache(unittest.TestCase):
    """앱 안 업로더의 줄별 기억(LineCache): 캐시 없이 거른 것과 결과(본문·줄 수·뺀 줄·통계·차단 규칙과 줄 번호)가 같다.
    이름이 늘거나 바뀌어도, 파일이 뒤로 자라도."""

    @staticmethod
    def _run(name, raw, names, cache=None):
        st = {"keys": 0, "paths": collections.Counter()}
        try:
            r = RS.build_body(name, raw, names, st, cache=cache)
            return ("ok", r, st["keys"], dict(st["paths"]), st["values"], dict(st["vpaths"]))
        except (RS.Block, RS.FileError) as e:
            return ("err", type(e).__name__, getattr(e, "rule", None), getattr(e, "line", None))

    def test_63_same_as_uncached(self):
        lines = [dline(1, extra=',"advice":{"reason":"fine"}'), b"{broken\n", RAW_EXTRA_DEC.rstrip(b"\n") + b"\n",
                 dline(4, extra=',"advice":{"reason":"x synthnick01 y"}'), dline(5, typ="resolution"),
                 dline(6, extra=',"advice":{"reason":"\\ud14c\\uc2a4\\ud2b8\\ub2c9 \\uc785\\ub2c8\\ub2e4"}')]
        full = b"".join(lines)
        half = b"".join(lines[:3])
        hand = RAW_EXTRA_HAND + hline(2) + hline(3)
        cache = RS.LineCache()
        seqs = (None, ["Jack"], ["Jack", "Zelda"], ["Zelda", NICK], ["Jack"], ["테스트닉"], ["Jack", "Zelda", "테스트닉", NICK], [])
        seen_block = set()
        for names in seqs:
            for name, raw in ((D0, half), (D0, full), (D1, hand)):
                want = self._run(name, raw, names)
                got = self._run(name, raw, names, cache)
                self.assertEqual(got, want, (names, name, len(raw)))
                if want[0] == "err":
                    seen_block.add((want[2], want[3]))
            cache.sweep()
        self.assertIn(("known_name", 4), seen_block)    # 영문 닉네임: 4번째 줄
        self.assertIn(("known_name", 6), seen_block)    # 한글 닉네임 (부분 일치): 6번째 줄
        self.assertGreater(cache.hits, 0)

    def test_63_check_bytes_line_check_hook(self):
        body = RS.build_body(D0, dline(1) + dline(2, typ="resolution"))[0]
        calls = []

        def lc(k, line, accept):
            calls.append(line)
            return RC.check_line(k, line, accept)
        a = RC.check_bytes(RC.file_kind(D0), body)
        b = RC.check_bytes(RC.file_kind(D0), body, line_check=lc)
        self.assertEqual((a.cls, a.rule, a.lines), (b.cls, b.rule, b.lines))
        self.assertEqual(len(calls), 2)

    def test_63_sweep_drops_unused(self):
        cache = RS.LineCache()
        RS.build_body(D1, hline(1), None, cache=cache)
        cache.sweep()
        cache.sweep()
        RS.build_body(D1, hline(2), None, cache=cache)
        cache.sweep()
        self.assertEqual(len(cache.lines), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
