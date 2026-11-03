"""python test_gto_dist.py

GTO 데이터 공유(sharebox 공간 API) 업로더 gto_dist_sync.py 와 갱신기 gto_update.py 의 시험.
- 가짜 HTTP/HTTPS 서버(이 파일 안)가 설계의 API 계약을 흉내 낸다. 자체 서명 인증서와 모든 데이터는 임시 폴더에만 만든다.
- 실제 sharebox 서비스, 토큰, 홈 설정, 실제 gto_lib 은 쓰지 않는다. 데이터는 전부 합성 픽스처다.
- 실제 shareboxd 통합 시험: 환경변수 GTO_TEST_SHAREBOXD=<shareboxd 바이너리 경로> 가 있을 때만 실행 (없으면 SKIP).
"""
import ast
import gzip
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gto_dist_common as C  # noqa: E402
import gto_dist_sync as S  # noqa: E402
import gto_update as G  # noqa: E402

results = []
TMP = tempfile.mkdtemp(prefix="gto-dist-test-")
os.environ["HOME"] = os.path.join(TMP, "home")           # server_marker() 가 진짜 ~/.config 를 보지 않게
os.environ["USERPROFILE"] = os.environ["HOME"]           # Windows 의 expanduser("~") 는 HOME 이 아니라 이것을 본다
os.makedirs(os.environ["HOME"])
G._sleep = lambda s: None                                 # 해시 간 쉼, 공유 위반 재시도 대기 제거
UP_TOKEN, RD_TOKEN = "synthetic-upload-token", "synthetic-read-token"


def test(name, ok, info=""):
    results.append(("PASS" if ok else "FAIL", name, info))


def skip(name, why):
    results.append(("SKIP", name, why))


def source(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def case(fn):
    try:
        fn()
    except Exception:
        results.append(("FAIL", fn.__name__, "예외: " + traceback.format_exc().strip().splitlines()[-1]))
        sys.stderr.write(traceback.format_exc())
    return fn


@contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# --- 합성 픽스처 -----------------------------------------------------------------------------
def make_obj(kind, n, v=0):
    if kind == "lib":
        d = {k: [n, v] for k in C.LIB_KEYS}
        d.update(version=1, spot="BTN_BB", stack=20)
    else:
        d = {"board": "7s5h3d", "lines": [n, v], "rh": "x", "th": "y"}
    return gzip.compress(json.dumps(d).encode(), mtime=0)


RANKS = "23456789TJQKA"
FLOPS = ["%ss%sh3d" % (a, b) for i, a in enumerate(RANKS) for b in RANKS[i + 1:]][:60]
NAMES = ["BTN_BB_20bb.json.gz", "CO_BB_20bb.json.gz", "SB_BB_30bb.json.gz"] + \
        ["turn/BTN_BB_20bb/%s.json.gz" % f for f in FLOPS]


def write_lib(d, v=0, names=None):
    for i, rel in enumerate(names or NAMES):
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(make_obj("turn" if rel.startswith("turn/") else "lib", i, v))


def deep_json_obj(depth):
    inner = "1"
    return gzip.compress(('{"board":"x","lines":' + "[" * depth + inner + "]" * depth + ',"rh":1,"th":1}').encode(), mtime=0)


def bigint_obj(n=4400):
    return gzip.compress(('{"board":"x","lines":[1' + "0" * n + '],"rh":1,"th":1}').encode(), mtime=0)


def py_verdict_variants():
    """Go 와 판정이 다를 수 있는 변종. (이름, 바이트, Go 기대 통과 여부)."""
    base = json.dumps({"board": "x", "lines": [1], "rh": 1, "th": 1})
    return [
        ("UTF-8 BOM", gzip.compress(b"\xef\xbb\xbf" + base.encode(), mtime=0), False),
        ("UTF-16 JSON", gzip.compress(base.encode("utf-16"), mtime=0), False),
        ("UTF-32 JSON", gzip.compress(base.encode("utf-32"), mtime=0), False),
        ("gzip 뒤 0바이트 패딩", gzip.compress(base.encode(), mtime=0) + b"\0" * 8, False),
        ("깊이 1100 중첩", deep_json_obj(1100), True),
        ("깊이 9990 중첩", deep_json_obj(9990), True),
        ("깊이 10001 중첩", deep_json_obj(10001), False),
        ("4400자리 정수(stack 외)", bigint_obj(), True),
    ]


def _py_rejects(data):
    try:
        C.validate_object(data)
        return False
    except C.ValidationError:
        return True


def snap(d):
    """{rel: (sha, mtime_ns)} — .update-tmp 와 숨김 폴더 제외."""
    out = {}
    for dp, dn, fn in os.walk(d):
        dn[:] = [x for x in dn if not x.startswith(".")]
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d)] = (hashlib.sha256(fh.read()).hexdigest(), os.stat(p).st_mtime_ns)
    return out


def tree_state(d):
    """스냅샷 + 모든 항목(폴더 포함)의 이름과 mtime — 아무것도 만들거나 바꾸지 않았는지."""
    out = {}
    for dp, dn, fn in os.walk(d):
        for x in dn + fn:
            p = os.path.join(dp, x)
            out[os.path.relpath(p, d)] = os.stat(p).st_mtime_ns
    return out, snap(d)


def file_bytes(d, rel):
    with open(os.path.join(d, *rel.split("/")), "rb") as f:
        return f.read()


# --- 가짜 sharebox GTO 공간 (설계 8절 API 계약) --------------------------------------------------
OBJ_RE = re.compile(r"/api/spaces/gto/objects/[0-9a-f]{64}")


class Share:
    def __init__(self):
        self.objects, self.manifest, self.raw_manifest = {}, None, None
        self.log, self.auth_attempts, self.conns = [], 0, 0
        self.drop_idle, self.cut_after = False, None
        self.tokens = {UP_TOKEN: "write", RD_TOKEN: "read"}

    def count(self, method, prefix, code=None):
        return sum(1 for m, p, c in self.log if m == method and p.startswith(prefix) and (code is None or c == code))

    def obj_gets(self):
        return self.count("GET", C.PATH_OBJECTS, 200)


def make_handler(share):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True

        def log_message(self, *a):
            pass

        def setup(self):
            super().setup()
            share.conns += 1

        def send(self, code, body=b"", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            share.log.append((self.command, self.path, code))
            if share.cut_after is not None and code == 200 and OBJ_RE.fullmatch(self.path):
                self.wfile.write(body[:share.cut_after])      # Content-Length 보다 짧게 보내고 끊는다
                self.close_connection = True
                return
            self.wfile.write(body)
            if share.drop_idle:
                self.close_connection = True                  # Connection: close 헤더 없이 끊음 (유휴 종료 흉내)

        def err(self, code, name):
            self.send(code, {"error": name})

        def body(self):
            n = self.headers.get("Content-Length")
            return self.rfile.read(int(n)) if n else b""

        def route(self):
            path = self.path.split("?")[0]
            a = self.headers.get("Authorization")
            if a is not None:
                share.auth_attempts += 1
            role = share.tokens.get(a[7:]) if a and a.startswith("Bearer ") else None
            if role is None:
                self.close_connection = True
                return self.err(401, "unauthorized")
            m = self.command
            if path == C.PATH_MANIFEST and m == "GET" and role in ("read", "write"):
                cur = share.manifest
                if cur is None and share.raw_manifest is None:
                    return self.err(404, "no_manifest")
                tag = '"v%d"' % (cur["version"] if cur else 0)
                if self.headers.get("If-None-Match") == tag and share.raw_manifest is None:
                    return self.send(304, b"", {"ETag": tag})
                return self.send(200, share.raw_manifest or cur, {"ETag": tag})
            if path == C.PATH_MISSING and m == "POST" and role == "write":
                try:
                    shas = json.loads(self.body())["sha256"]
                    assert all(isinstance(s, str) and C.SHA_RE.fullmatch(s) for s in shas)
                except Exception:
                    return self.err(422, "bad_request")
                return self.send(200, {"missing": [s for s in shas if s not in share.objects]})
            if OBJ_RE.fullmatch(path) and m == "GET" and role == "read":
                o = share.objects.get(path.rsplit("/", 1)[1])
                return self.send(200, o, {"Content-Type": "application/octet-stream"}) if o is not None \
                    else self.err(404, "not_found")
            if OBJ_RE.fullmatch(path) and m == "PUT" and role == "write":
                n = self.headers.get("Content-Length")
                if n is None:
                    self.close_connection = True
                    return self.err(411, "length_required")
                if int(n) > C.MAX_OBJECT:
                    self.close_connection = True
                    return self.err(413, "too_large")
                data, sha = self.body(), path.rsplit("/", 1)[1]
                if C.sha256_hex(data) != sha:
                    return self.err(422, "hash_mismatch")
                try:
                    C.validate_object(data)
                except C.ValidationError as e:
                    return self.err(422, e.code)
                if sha in share.objects:
                    return self.send(200, {"stored": False})   # 덮어쓰지 않는다
                share.objects[sha] = data
                return self.send(201, {"stored": True})
            if path == C.PATH_MANIFEST and m == "POST" and role == "write":
                try:
                    b = json.loads(self.body())
                    base, files, shrink = b["base_version"], b["files"], bool(b.get("allow_shrink"))
                    C.validate_manifest_files(files)
                except C.ValidationError as e:
                    return self.err(422, e.code)
                except Exception:
                    return self.err(422, "bad_request")
                cur = share.manifest
                curv = cur["version"] if cur else 0
                if base != curv:
                    return self.err(409, "stale_base")
                for e in files.values():
                    o = share.objects.get(e["sha256"])
                    if o is None or len(o) != e["size"]:
                        return self.err(422, "missing_object")
                if cur and len(files) < len(cur["files"]) * 0.5 and not shrink:
                    return self.err(409, "shrink_refused")
                if cur and files == cur["files"]:
                    return self.send(200, {"version": curv, "changed": False})
                share.manifest = {"format": 1, "version": curv + 1, "created": "2026-01-01T00:00:00Z",
                                  "file_count": len(files), "total_size": sum(e["size"] for e in files.values()),
                                  "files": files}
                return self.send(200, {"version": curv + 1, "changed": True})
            known = path in (C.PATH_MANIFEST, C.PATH_MISSING) or OBJ_RE.fullmatch(path)
            return self.err(403 if known else 404, "forbidden" if known else "not_found")   # 권한 밖 / 없는 경로

        do_GET = do_PUT = do_POST = do_DELETE = route

    return H


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def start_server(share, tls=None, max_tls=None):
    srv = Srv(("127.0.0.1", 0), make_handler(share))
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        if max_tls:
            ctx.maximum_version = max_tls
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_cert(d, name):
    key, crt = os.path.join(d, name + ".key"), os.path.join(d, name + ".crt")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", key, "-out", crt, "-days", "2", "-subj", "/CN=gto-test",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    with open(crt) as f:
        fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()
    return key, crt, fp


HAVE_OPENSSL = shutil.which("openssl") is not None
if HAVE_OPENSSL:
    KEY1, CRT1, FP1 = make_cert(TMP, "one")
    KEY2, CRT2, FP2 = make_cert(TMP, "two")


class Env:
    """한 시나리오: 소스 lib, 클라이언트 대상 lib, 앱 폴더, 가짜 서버(HTTP 업로드용 + HTTPS 수신용)."""
    n = 0

    def __init__(self, v=0, populate=True):
        Env.n += 1
        self.dir = os.path.join(TMP, "env%d" % Env.n)
        self.src, self.dst, self.base = (os.path.join(self.dir, x) for x in ("src", "dst", "base"))
        for d in (self.src, self.dst, self.base):
            os.makedirs(d)
        if populate:
            write_lib(self.src, v)
        self.share = Share()
        self.http = start_server(self.share)
        self.https = start_server(self.share, (CRT1, KEY1))
        self.done, self.errs = [], []
        self.cache = os.path.join(self.dir, "cache.json")

    def close(self):
        for s in (self.http, self.https):
            s.shutdown()
            s.server_close()

    def up_cfg(self, **kw):
        c = {"url": "http://127.0.0.1:%d" % self.http.server_port, "token": UP_TOKEN, "lib_dir": self.src,
             "cache": self.cache}
        c.update(kw)
        return c

    def upload(self, **kw):
        stats = {}
        rc = S.run(self.up_cfg(**kw), stats)
        return rc, stats

    def url(self):
        return "https://127.0.0.1:%d" % self.https.server_port

    def updater(self, **kw):
        cfg = {"enabled": True, "urls": [self.url()], "token": RD_TOKEN, "cert_sha256": FP1, "lib_dir": self.dst,
               "check_interval_sec": 1800, "max_bytes_per_sec": 0}
        cfg.update(kw)
        return G.Updater(self.base, cfg, on_done=self.done.append, on_error=lambda *a: self.errs.append(a), first_delay=0)

    def state(self):
        try:
            with open(os.path.join(self.base, G.STATE_NAME)) as f:
                return json.load(f)
        except OSError:
            return None


def applied(env):
    st = env.state()
    return st["applied_version"] if st else 0


# =====================================================================================================
# 공통 규칙 / 정적 확인 (openssl 불필요)
# =====================================================================================================
@case
def common_rules():
    test("common: 포트 1213, 객체 상한 16MiB, 공간 API 경로", C.DEFAULT_PORT == 1213 and C.MAX_OBJECT == 16 * 1024 * 1024
         and C.PATH_MANIFEST == "/api/spaces/gto/manifest" and C.PATH_OBJECTS == "/api/spaces/gto/objects/"
         and C.PATH_MISSING == "/api/spaces/gto/objects/missing")
    test("common: 클라이언트 TLS 최소 1.3", "TLSv1_3" in source("gto_dist_common.py")
         and C.PinnedConnection("127.0.0.1", 1, "0" * 64)._context.minimum_version == ssl.TLSVersion.TLSv1_3)
    good = ["BTN_BB_20bb.json.gz", "SB_BB_100bb.json.gz", "turn/BTN_BB_20bb/7s5h3d.json.gz"]
    bad = ["../x", "hands/a.json.gz", "/etc/passwd", "C:\\x", "x.tmp", "BTN_BB_20bb.json.gz.123.tmp", "turn/../x.json.gz",
           "BTN_BB_20bb.json.gz\n", "turn/BTN_BB_20bb/7s5h3D.json.gz", "turn\\BTN_BB_20bb\\7s5h3d.json.gz", "", None, 5]
    test("common: 허용 경로만 통과(fullmatch)", all(C.valid_path(p) for p in good) and not any(C.valid_path(p) for p in bad))
    nick = gzip.compress(json.dumps({"players": [{"nick": "synthetic"}], "hands": 3}).encode())
    cases = {
        "정상 lib": (make_obj("lib", 1), True), "정상 turn": (make_obj("turn", 1), True),
        "gzip 아님": (b"plain text", False), "잘린 gzip": (make_obj("lib", 1)[:-12], False),
        "닉네임 JSON(키 불일치)": (nick, False),
        "키 하나 부족": (gzip.compress(json.dumps({"board": "x", "lines": 1, "rh": 1}).encode()), False),
        "키 하나 추가": (gzip.compress(json.dumps({"board": "x", "lines": 1, "rh": 1, "th": 1, "extra": 1}).encode()), False),
        "NaN": (gzip.compress(b'{"board":"x","lines":NaN,"rh":1,"th":1}'), False),
        "stack 이 bool": (gzip.compress(json.dumps({**{k: 0 for k in C.LIB_KEYS}, "version": 1, "spot": "a",
                                                    "stack": True}).encode()), False),
        "배열 최상위": (gzip.compress(b"[1]"), False),
    }
    for name, (data, want) in cases.items():
        try:
            C.validate_object(data)
            got = True
        except C.ValidationError:
            got = False
        test("validate_object: " + name, got == want)
    bomb = gzip.compress(b" " * (C.MAX_INFLATED + 10), 1)
    try:
        C.validate_object(bomb)
        ok = False
    except C.ValidationError as e:
        ok = e.code == "bad_gzip"
    test("validate_object: 압축 폭탄 거부(해제 상한)", ok, "압축 %dKB" % (len(bomb) // 1024))
    # Go 서버 판정과의 차이(회귀): Go 가 거부하는 것을 Python 이 통과시키면 게시가 막히므로 업로더에서 먼저 차단한다
    jb = json.dumps({"board": "x", "lines": [1], "rh": 1, "th": 1})
    for name, data, go_ok in py_verdict_variants():
        got = not _py_rejects(data)
        # 안전 조건: Go 가 거부하는 파일을 업로더가 통과시키면 안 된다(게시가 422 로 막힘). 반대(더 엄격)는 허용.
        test("판정 차이: %s — Go %s, 업로더 %s (Python 단독 통과 없음)" % (name, "통과" if go_ok else "거부", "통과" if got else "거부"),
             go_ok or not got)
    test("판정: 다중 멤버 gzip 은 Go 와 같이 통과", _py_rejects(gzip.compress(jb[:20].encode()) + gzip.compress(jb[20:].encode())) is False)
    ent = {"sha256": "a" * 64, "size": 1}
    for name, files in {"..": {"../x": ent}, "hands/": {"hands/a.json.gz": ent}, "절대": {"/x/BTN_BB_20bb.json.gz": ent},
                        "C:": {"C:\\x": ent}, ".tmp": {"BTN_BB_20bb.json.gz.tmp": ent},
                        "대문자 해시": {"BTN_BB_20bb.json.gz": {"sha256": "A" * 64, "size": 1}},
                        "큰 크기": {"BTN_BB_20bb.json.gz": {"sha256": "a" * 64, "size": C.MAX_OBJECT + 1}},
                        "항목 키 추가": {"BTN_BB_20bb.json.gz": {**ent, "mtime": 1}}}.items():
        try:
            C.validate_manifest_files(files)
            ok = False
        except C.ValidationError:
            ok = True
        test("validate_manifest_files: 거부 — " + name, ok)


@case
def uploader_static():
    src = source("gto_dist_sync.py")
    tree = ast.parse(src)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    test("업로더: 러너/솔버/잠금/프로세스 모듈을 import 하지 않음 (프로세스·잠금 비공유)",
         not imported & {"build_gto_lib", "solverlib", "fcntl", "msvcrt", "subprocess", "multiprocessing", "psutil", "signal"}
         and "flock" not in src, str(sorted(imported)))
    test("러너: gto_dist 모듈을 참조하지 않음", all("gto_dist" not in source(f) for f in ("build_gto_lib.py", "solverlib.py")))
    writers = []

    class V(ast.NodeVisitor):
        fn = None

        def visit_FunctionDef(self, n):
            old, self.fn = self.fn, n.name
            self.generic_visit(n)
            self.fn = old

        def visit_Call(self, n):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if name == "open":
                mode = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) else "r"
                if any(c in mode for c in "wax+"):
                    writers.append((self.fn, "open"))
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("os", "shutil") \
                    and name in ("replace", "rename", "remove", "unlink", "makedirs", "mkdir", "rmtree", "copyfile", "utime", "chmod", "truncate"):
                writers.append((self.fn, name))
            self.generic_visit(n)

    V().visit(tree)
    test("업로더: 파일 쓰기는 save_cache(캐시 파일) 안에만 있음", writers and all(w[0] == "save_cache" for w in writers), str(writers))
    for url, want in {"http://127.0.0.1:8737": True, "http://localhost:1": True, "http://[::1]:8737": True,
                      "http://192.168.1.5:8737": False, "http://example.com": False, "https://127.0.0.1:8737": False}.items():
        try:
            S.parse_loopback(url)
            got = True
        except ValueError:
            got = False
        test("업로더 주소: %s → %s" % (url, "허용" if want else "거부"), got == want)
    test("업로더: HTTPConnection 사용, PUT 전에 validate_object", "HTTPConnection(" in src and
         src.index("validate_object") < src.index('"PUT"'))
    test("별도 서버 흔적 없음: 8743 / gto_dist_server / admin / v1 경로",
         all(not re.search(r"8743|gto_dist_server|gto_dist_admin|/v1/", source(f)) for f in
             ("gto_dist_common.py", "gto_dist_sync.py", "gto_update.py", "README.md", "reader.py"))
         and not any(os.path.exists(os.path.join(HERE, f)) for f in ("gto_dist_server.py", "gto_dist_admin.py")))
    test("서버 PC 표식은 sync.json", G.server_marker().endswith(os.path.join(".config", "gto-dist", "sync.json")))


if not HAVE_OPENSSL:
    skip("가짜 HTTPS 서버 기반 시나리오 전부", "openssl 없음")
    HAVE_TLS = False
else:
    HAVE_TLS = True


# =====================================================================================================
# 업로드
# =====================================================================================================
@case
def upload_flow():
    e = Env()
    try:
        before = tree_state(e.src)
        for dp, dn, fn in os.walk(e.src):          # 소스 lib 을 읽기 전용으로: 쓰기를 시도하면 실패한다
            for x in fn:
                os.chmod(os.path.join(dp, x), 0o444)
            os.chmod(dp, 0o555)
        try:
            rc, st = e.upload()
        finally:
            for dp, dn, fn in os.walk(e.src):
                os.chmod(dp, 0o755)
                for x in fn:
                    os.chmod(os.path.join(dp, x), 0o644)
        after = tree_state(e.src)
        m = e.share.manifest
        test("업로드: 버전 1, 전 파일 게시, 객체 존재", rc == 0 and m and m["version"] == 1 and len(m["files"]) == len(NAMES)
             and all(x["sha256"] in e.share.objects for x in m["files"].values()), "put=%s" % st.get("put"))
        test("업로드: 원본 폴더 해시·mtime·목록 전후 동일(읽기 전용 상태에서)", before == after)
        test("업로드: 읽기 토큰 아닌 쓰기 토큰으로 인증", e.share.auth_attempts > 0)
        n_put = e.share.count("PUT", C.PATH_OBJECTS)
        rc, st = e.upload()
        test("재동기화(동일 내용): 버전 불변, PUT 0회, POST missing 0회",
             rc == 0 and e.share.manifest["version"] == 1 and e.share.count("PUT", C.PATH_OBJECTS) == n_put
             and e.share.count("POST", C.PATH_MISSING) == 2 and st["put"] == 0)   # 존재 확인 1회만 추가
        # 파일 1개 변경
        write_lib(e.src, v=1, names=[NAMES[5]])
        rc, st = e.upload()
        test("1개 변경 업로드: 버전 2, PUT 정확히 1회", rc == 0 and e.share.manifest["version"] == 2
             and e.share.count("PUT", C.PATH_OBJECTS) == n_put + 1 and st["put"] == 1)
        # 낡은 base_version(서버에서 게시가 바뀐 상황)
        api = S.Api(e.up_cfg()["url"], UP_TOKEN)
        stat, _ = api.json("POST", C.PATH_MANIFEST, {"base_version": 1, "files": e.share.manifest["files"],
                                                     "allow_shrink": False})
        test("낡은 base_version 게시 → 409", stat == 409)
        small = dict(list(e.share.manifest["files"].items())[:5])
        stat, data = api.json("POST", C.PATH_MANIFEST, {"base_version": 2, "files": small, "allow_shrink": False})
        test("축소 게시 → 409 shrink_refused", stat == 409 and b"shrink_refused" in data)
        stat, _ = api.json("POST", C.PATH_MANIFEST, {"base_version": 2, "files": {
            "../x": {"sha256": "a" * 64, "size": 1}}, "allow_shrink": False})
        test("경로 조작 게시 → 422, 현재 매니페스트 불변", stat == 422 and e.share.manifest["version"] == 2)
    finally:
        e.close()


@case
def upload_race_replace():
    e = Env()
    try:
        orig = S.Api.json
        target = os.path.join(e.src, NAMES[2])

        def hooked(self, method, path, obj):
            r = orig(self, method, path, obj)
            if path == C.PATH_MISSING:           # 해시 계산 후 PUT 전에 러너가 파일을 교체한 상황
                with open(target, "wb") as f:
                    f.write(make_obj("lib", 99, 7))
            return r

        with patched(S.Api, "json", hooked):
            rc, st = e.upload()
        new_sha = C.sha256_hex(make_obj("lib", 99, 7))
        test("업로드 도중 파일 교체: 새 해시로 게시", rc == 0 and e.share.manifest["files"][NAMES[2]]["sha256"] == new_sha
             and new_sha in e.share.objects)
    finally:
        e.close()


@case
def upload_rejects():
    e = Env()
    try:
        bad = gzip.compress(json.dumps({"players": [{"nick": "synthetic"}]}).encode())
        os.makedirs(os.path.join(e.src, "hands"))
        with open(os.path.join(e.src, "hands", "a.json.gz"), "wb") as f:
            f.write(bad)
        with open(os.path.join(e.src, "BTN_BB_20bb.json.gz.123.4.tmp"), "wb") as f:
            f.write(b"partial")
        rc, st = e.upload()
        test("hands/ 와 .tmp 는 스캔에서 제외되어 게시에 없음", rc == 0 and all(
            p.startswith(("turn/", "BTN", "CO", "SB")) and not p.endswith(".tmp") for p in e.share.manifest["files"]))
        # 닉네임 JSON 이 허용 경로 이름으로 들어온 경우: 업로더가 막는다. 변경분이 그 파일뿐일 때 PUT 0회.
        puts, ver = e.share.count("PUT", C.PATH_OBJECTS), e.share.manifest["version"]
        with open(os.path.join(e.src, NAMES[0]), "wb") as f:
            f.write(bad)
        try:
            S.run(e.up_cfg(), {})
            ok = False
        except S.Fail as ex:
            ok = "invalid_object" in str(ex) and "bad_schema" in str(ex)
        test("닉네임 JSON: 업로더 단계에서 차단, PUT 0회, 게시 없음", ok and e.share.count("PUT", C.PATH_OBJECTS) == puts
             and e.share.manifest["version"] == ver)
        for name, data in {"gzip 아님": b"hello", "잘린 gzip": make_obj("lib", 3)[:-10]}.items():
            with open(os.path.join(e.src, NAMES[0]), "wb") as f:
                f.write(data)
            try:
                S.run(e.up_cfg(), {})
                ok = False
            except S.Fail:
                ok = True
            test("손상 파일(%s): 업로더 차단, PUT 증가 없음" % name, ok and e.share.count("PUT", C.PATH_OBJECTS) == puts)
        # 서버 쪽 계약 흉내: 해시 불일치/스키마/길이 없음
        api = S.Api(e.up_cfg()["url"], UP_TOKEN)
        good = make_obj("lib", 50)
        sha = C.sha256_hex(good)
        before = len(e.share.objects)
        r1 = api.req("PUT", C.PATH_OBJECTS + "0" * 64, good)[0]
        r2 = api.req("PUT", C.PATH_OBJECTS + C.sha256_hex(bad), bad)[0]
        conn = http.client.HTTPConnection("127.0.0.1", e.http.server_port)
        conn.putrequest("PUT", C.PATH_OBJECTS + sha)
        conn.putheader("Authorization", "Bearer " + UP_TOKEN)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"0\r\n\r\n")
        r3 = conn.getresponse().status
        conn.close()
        test("계약 모의: 해시 불일치 422 / 스키마 위반 422 / 길이 없음(chunked) 411, 객체 미생성",
             (r1, r2, r3) == (422, 422, 411) and len(e.share.objects) == before, str((r1, r2, r3)))
        # 같은 해시 재PUT 은 덮어쓰지 않는다
        api.req("PUT", C.PATH_OBJECTS + sha, good)
        keep = e.share.objects[sha]
        api.req("PUT", C.PATH_OBJECTS + sha, good)
        test("계약 모의: 같은 해시 재PUT 200, 객체 그대로", e.share.objects[sha] is keep)
    finally:
        e.close()


@case
def upload_failure_keeps_runner_safe():
    e = Env()
    try:
        cfg_path = os.path.join(e.dir, "sync.json")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

        def run_cli(cfg):
            with open(cfg_path, "w") as f:
                json.dump(cfg, f)
            return subprocess.run([sys.executable, "-I", os.path.join(HERE, "gto_dist_sync.py"), "--config", cfg_path],
                                  capture_output=True, text=True, env=env, timeout=120)

        st0 = tree_state(e.src)
        p = run_cli(e.up_cfg(url="http://127.0.0.1:%d" % closed_port()))
        test("업로드 실패(서버 다운): exit 1, lib 폴더 불변, 토큰 미출력", p.returncode == 1 and tree_state(e.src) == st0
             and UP_TOKEN not in p.stdout + p.stderr, p.stderr.strip()[-80:])
        p = run_cli(e.up_cfg(token="wrong-token"))
        test("업로드 실패(인증 거부): exit 1, 게시 없음, lib 불변", p.returncode == 1 and e.share.manifest is None
             and tree_state(e.src) == st0 and "wrong-token" not in p.stderr)
        p = run_cli(e.up_cfg(url="http://192.0.2.1:8737"))
        test("업로드: 루프백 외 주소는 접속 없이 exit 1", p.returncode == 1 and tree_state(e.src) == st0)
        with open(os.path.join(e.src, NAMES[1]), "wb") as f:
            f.write(b"garbage")
        st1 = tree_state(e.src)
        p = run_cli(e.up_cfg())
        test("업로드 실패(손상 파일): exit 1, 게시 없음, lib 불변", p.returncode == 1 and e.share.manifest is None
             and tree_state(e.src) == st1)
        write_lib(e.src, names=[NAMES[1]])
        p = run_cli(e.up_cfg())
        test("복구 후 다음 타이머: exit 0, 게시 성공", p.returncode == 0 and e.share.manifest["version"] == 1, p.stderr.strip()[-80:])
        shutil.rmtree(e.src)
        os.makedirs(e.src)
        p = run_cli(e.up_cfg())
        test("스캔 0개: exit 1 (빈 폴더로 게시하지 않음)", p.returncode == 1 and e.share.manifest["version"] == 1)
        test("업로더 실패는 러너와 무관: lib 에 .update-tmp/잠금 파일 미생성", not os.path.exists(os.path.join(e.src, ".update-tmp")))
    finally:
        e.close()


# =====================================================================================================
# 갱신기
# =====================================================================================================
@case
def update_first_and_delta():
    e = Env()
    try:
        e.upload()
        p = G.C.PinnedConnection("127.0.0.1", e.https.server_port, FP1)
        p.connect()
        test("지문 일치 시 TLS 1.3 연결", p.sock.version() == "TLSv1.3")
        p.close()
        u = e.updater()
        s1 = u.cycle()
        test("첫 갱신: 빈 대상에 전 파일 설치, 내용 동일", s1 == "updated" and snap(e.dst).keys() == snap(e.src).keys()
             and all(snap(e.dst)[k][0] == snap(e.src)[k][0] for k in snap(e.src)), s1)
        test("첫 갱신: applied_version 기록, 큰 설치는 토스트 1회", applied(e) == 1 and len(e.done) == 1 and "63" in e.done[0])
        test("갱신 후 스테이징 잔여물 없음(lock 제외)", sorted(os.listdir(os.path.join(e.dst, G.TMP_NAME))) == ["lock"])
        g0 = e.share.obj_gets()
        s2 = u.cycle()
        test("매니페스트 불변: 304, 객체 다운로드 0회", s2 == "unchanged" and e.share.obj_gets() == g0
             and any(m == "GET" and c == 304 for m, p_, c in e.share.log))
        # 변경 1개
        write_lib(e.src, v=1, names=[NAMES[7]])
        e.upload()
        before = snap(e.dst)
        e.done.clear()
        s3 = u.cycle()
        after = snap(e.dst)
        changed = [k for k in after if after[k] != before[k]]
        test("1개 변경 갱신: 객체 GET 정확히 1회", s3 == "updated" and e.share.obj_gets() == g0 + 1, "gets=%d" % (e.share.obj_gets() - g0))
        test("1개 변경 갱신: 나머지 62개 mtime 불변, 바뀐 파일은 그 1개뿐", changed == [os.path.normpath(NAMES[7])]
             and after[os.path.normpath(NAMES[7])][0] == snap(e.src)[os.path.normpath(NAMES[7])][0])
        test("작은 갱신(1개)은 토스트 없음(50개 미만)", e.done == [])
        # 신규 Updater(앱 재시작 흉내): 상태 파일로 mtime 신뢰, 재다운로드 0
        g1 = e.share.obj_gets()
        u2 = e.updater()
        test("재시작 후 첫 실행: 재다운로드 0회", u2.cycle() == "unchanged" and e.share.obj_gets() == g1)
        # 로컬 파일은 삭제하지 않는다
        extra = os.path.join(e.dst, "ZZ_BB_20bb.json.gz")
        with open(extra, "wb") as f:
            f.write(b"local only")
        write_lib(e.src, v=2, names=[NAMES[8]])
        e.upload()
        u2.cycle()
        test("로컬 전용 파일은 삭제하지 않음", os.path.exists(extra))
        test("에러 콜백 없음(정상 경로)", e.errs == [])
    finally:
        e.close()


@case
def update_existing_data_first_run():
    e = Env()
    try:
        e.upload()
        shutil.copytree(e.src, e.dst, dirs_exist_ok=True)
        before = snap(e.dst)
        u = e.updater()
        s = u.cycle()
        test("기존 데이터가 있는 첫 실행: 재다운로드 0회, 파일 불변", s == "unchanged" and e.share.obj_gets() == 0
             and snap(e.dst) == before and applied(e) == 1)
    finally:
        e.close()


@case
def update_corrupt_received():
    e = Env()
    try:
        e.upload()
        e.updater().cycle()
        old = snap(e.dst)
        write_lib(e.src, v=1, names=NAMES[:4])
        e.upload()
        victim = e.share.manifest["files"][NAMES[1]]["sha256"]
        e.share.objects[victim] = b"x" * len(e.share.objects[victim])      # 같은 길이 변조
        u = e.updater()
        s = u.cycle()
        test("변조 수신(같은 길이): hash_mismatch", s == "hash_mismatch", s)
        test("변조 수신: 기존 파일 전부 동일, applied_version 불변", snap(e.dst) == old and applied(e) == 1)
        test("변조 수신: 오류 콜백 호출, 토큰 미포함", e.errs and all(RD_TOKEN not in repr(a) for a in e.errs))
        test("변조 수신: 손상 객체는 설치되지 않음(.part 잔여 없음)",
             not any(n.endswith(".part") for n in os.listdir(os.path.join(e.dst, G.TMP_NAME))))
        e.share.objects[victim] = e.share.objects[victim][:-5]               # 짧게
        s = e.updater().cycle()
        test("변조 수신(짧은 객체): hash_mismatch, 기존 파일 동일", s == "hash_mismatch" and snap(e.dst) == old)
        e.share.objects[victim] = e.share.objects[victim] + b"y" * 50          # 길게
        s = e.updater().cycle()
        test("변조 수신(긴 객체): 선언 크기 초과 즉시 중단", s == "hash_mismatch" and snap(e.dst) == old)
    finally:
        e.close()


def all_old_or_new(dst, old_bytes, new_bytes):
    bad = []
    for rel in old_bytes:
        cur = file_bytes(dst, rel)
        if cur != old_bytes[rel] and cur != new_bytes[rel]:
            bad.append(rel)
        else:
            try:
                C.validate_object(cur)
            except C.ValidationError:
                bad.append(rel)
    return bad


@case
def update_interrupted():
    e = Env()
    try:
        e.upload()
        e.updater().cycle()
        old = {n: file_bytes(e.dst, n) for n in NAMES}
        write_lib(e.src, v=1)                       # 전 파일 변경
        e.upload()
        new = {n: file_bytes(e.src, n) for n in NAMES}
        snap_old = snap(e.dst)

        # (a) 스테이징 중 예외: 세 번째 다운로드에서 실패 → 아무것도 설치되지 않음
        u = e.updater()
        calls = {"n": 0}
        real = G.Updater.download

        def boom(self, sha, size, part):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("injected staging failure")
            return real(self, sha, size, part)

        with patched(G.Updater, "download", boom):
            s = u.cycle()
        test("스테이징 중 예외: error 로 끝, 기존 파일 전부 동일(설치 0)", s == "error" and snap(e.dst) == snap_old and applied(e) == 1, s)
        gets0 = e.share.obj_gets()
        s = e.updater().cycle()
        test("재시도: 이미 받은 .ok 재사용, 나머지만 다운로드 후 완료", s == "updated"
             and e.share.obj_gets() - gets0 == len(NAMES) - 2 and all(file_bytes(e.dst, n) == new[n] for n in NAMES),
             "gets=%d" % (e.share.obj_gets() - gets0))

        # (b) 설치 중 예외: 5번째 교체에서 OSError(공유 위반이 아닌 오류) — 각 파일은 옛것 또는 새것으로 온전
        write_lib(e.src, v=2)
        e.upload()
        new2 = {n: file_bytes(e.src, n) for n in NAMES}
        cnt = {"n": 0}
        real_replace = G._replace

        def flaky(src, dst):
            cnt["n"] += 1
            if cnt["n"] == 5:
                raise OSError(28, "injected disk full")
            return real_replace(src, dst)

        with patched(G, "_replace", flaky):
            s = e.updater().cycle()
        n_new = sum(file_bytes(e.dst, n) == new2[n] for n in NAMES)
        test("설치 중 예외: error 로 끝, 모든 파일이 옛것 또는 새것(온전·검증 통과)", s == "error"
             and not all_old_or_new(e.dst, new, new2) and n_new == 4, "새 파일 %d개" % n_new)
        test("설치 중 예외: applied_version 불변(미완료)", applied(e) == 2)
        # 턴 먼저 설치되는 순서: 루트는 아직 옛것
        test("설치 순서: 턴 파일 먼저(루트 3개는 아직 옛것)", all(file_bytes(e.dst, n) == new[n] for n in NAMES[:3]))
        s = e.updater().cycle()
        test("예외 제거 후 다음 사이클에서 마무리", s == "updated" and all(file_bytes(e.dst, n) == new2[n] for n in NAMES)
             and applied(e) == 3)

        # (c) 스테이징 중 연결이 끊김(본문 도중) — 설치 0
        write_lib(e.src, v=3)
        e.upload()
        snap_c = snap(e.dst)
        e.share.cut_after = 100
        s = e.updater().cycle()
        e.share.cut_after = None
        test("수신 도중 연결 끊김: 기존 파일 불변, 완료 처리 안 됨", s in ("error", "hash_mismatch") and snap(e.dst) == snap_c and applied(e) == 3, s)
        test("수신 도중 연결 끊김 후 복구", e.updater().cycle() == "updated")
    finally:
        e.close()


@case
def update_server_down_and_start():
    e = Env()
    try:
        e.upload()
        e.updater().cycle()
        before = snap(e.dst)
        port = closed_port()
        u = e.updater(urls=["https://127.0.0.1:%d" % port])
        s = u.cycle()
        test("서버 다운: unreachable, 기존 파일 유지, 오류 콜백·예외 없음", s == "unreachable" and snap(e.dst) == before and not e.errs)
        # 응답 없는 서버(핸드셰이크 정지)에서도 start() 즉시 반환
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        u2 = e.updater(urls=["https://127.0.0.1:%d" % silent.getsockname()[1]])
        t0 = time.monotonic()
        u2.start()
        dt = time.monotonic() - t0
        test("응답 없는 서버: Updater.start() 0.5초 이내 반환(데몬 스레드)", dt < 0.5 and u2.thread.daemon, "%.3fs" % dt)
        u2.stop()
        # gto_update.start (설정 파일 경유)
        with open(os.path.join(e.base, G.CONFIG_NAME), "w") as f:
            json.dump({"enabled": True, "urls": ["https://127.0.0.1:%d" % port], "token": RD_TOKEN, "cert_sha256": FP1,
                       "lib_dir": e.dst}, f)
        t0 = time.monotonic()
        up = G.start(e.base)
        dt = time.monotonic() - t0
        test("서버 장애 설정: gto_update.start() 0.5초 이내 반환, 스레드 동작", up is not None and dt < 0.5 and up.thread.is_alive(), "%.3fs" % dt)
        up.stop()
        silent.close()
    finally:
        e.close()


@case
def update_config_and_marker():
    base = os.path.join(TMP, "cfgbase")
    os.makedirs(base)

    def put(cfg):
        with open(os.path.join(base, G.CONFIG_NAME), "w") as f:
            json.dump(cfg, f)

    ok = {"enabled": True, "urls": ["https://127.0.0.1:1"], "token": "t", "cert_sha256": "a" * 64}
    test("start: 설정 파일 없음 → None", G.start(base) is None)
    for name, cfg in {"enabled false": {**ok, "enabled": False}, "http URL": {**ok, "urls": ["http://127.0.0.1:1"]},
                      "지문 형식 오류": {**ok, "cert_sha256": "xyz"}, "토큰 없음": {**ok, "token": ""},
                      "urls 비어 있음": {**ok, "urls": []}}.items():
        put(cfg)
        test("start: %s → None" % name, G.start(base) is None)
    mk = G.server_marker()
    os.makedirs(os.path.dirname(mk))
    with open(mk, "w") as f:
        f.write("{}")
    put(ok)
    test("start: sync.json(서버 PC 표식) 존재 → None (러너 원본 보호)", G.start(base) is None)
    os.unlink(mk)
    up = G.start(base)
    test("start: 표식 없고 설정 정상 → Updater", up is not None)
    if up:
        up.stop()
    test("HOME 이 임시 폴더 — 실제 ~/.config/gto-dist 를 건드리지 않음", mk.startswith(TMP))


@case
def update_pin():
    e = Env()
    try:
        e.upload()
        wrong = "0" * 64
        a0, l0 = e.share.auth_attempts, len(e.share.log)
        s = e.updater(cert_sha256=wrong).cycle()
        test("지문 불일치: pin_failed, 서버 인증 시도 0회(토큰 미전송), 요청 0건", s == "pin_failed" and e.share.auth_attempts == a0
             and len(e.share.log) == l0 and os.listdir(e.dst) == [G.TMP_NAME], s)
        test("지문 불일치: 오류 콜백 통지", [a[1] for a in e.errs] == ["pin_failed"])
        closed = "https://127.0.0.1:%d" % closed_port()
        s = e.updater(cert_sha256=wrong, urls=[e.url(), closed]).cycle()
        test("지문 불일치 후 다른 URL 이 연결 불가여도 pin_failed 유지(마지막 오류만 보지 않음)", s == "pin_failed" and e.share.auth_attempts == a0)
        u = e.updater(urls=[closed, e.url()])
        s = u.cycle()
        test("첫 URL 불가 → 다음 URL 로 성공", s == "updated" and u.last_url == e.url())
        u2 = e.updater(urls=[closed, e.url()])
        u2.last_url = e.url()
        order = []
        real = C.PinnedConnection

        class Spy(real):
            def __init__(self, host, port, *a, **k):
                order.append(port)
                super().__init__(host, port, *a, **k)

        with patched(G.C, "PinnedConnection", Spy):
            u2._connect()
        test("URL 순서: 마지막 성공 URL 먼저", order[0] == e.https.server_port)
        # TLS 1.2 만 되는 서버는 거부
        sh2 = Share()
        s12 = start_server(sh2, (CRT1, KEY1), max_tls=ssl.TLSVersion.TLSv1_2)
        try:
            s = e.updater(urls=["https://127.0.0.1:%d" % s12.server_port]).cycle()
            test("TLS 1.2 서버는 연결 불가(최소 1.3)", s == "unreachable" and sh2.auth_attempts == 0)
        finally:
            s12.shutdown()
            s12.server_close()
    finally:
        e.close()


@case
def update_auth_and_missing():
    e = Env()
    try:
        s = e.updater(token="wrong").cycle()
        test("토큰 오류: 서버가 매니페스트 404 전에 401 → unauthorized 통지", s == "unauthorized" and e.errs)
        s = e.updater().cycle()
        test("게시 전(매니페스트 404): no_manifest, 오류 통지 없음", s == "no_manifest" and len(e.errs) == 1)
        e.upload()
        s = e.updater(token=UP_TOKEN).cycle()
        test("쓰기 토큰은 객체 GET 불가(403) → 설치 없음", s in ("download_http_403",) and snap(e.dst) == {}, s)
    finally:
        e.close()


@case
def update_stale_manifest():
    e = Env()
    try:
        e.upload()
        e.updater().cycle()
        old = snap(e.dst)
        write_lib(e.src, v=1, names=NAMES[:3])
        e.upload()
        victim = e.share.manifest["files"][NAMES[2]]["sha256"]
        del e.share.objects[victim]                        # 서버 정리와 겹침
        u = e.updater()
        errs0 = len(e.errs)
        s = u.cycle()
        test("정리된 객체 404: stale_manifest, 오류 통지 없음, 기존 파일 유지", s == "stale_manifest" and len(e.errs) == errs0
             and snap(e.dst) == old and applied(e) == 1, s)
        waits = []

        class Ev:
            def wait(self, d):
                waits.append(d)
                return len(waits) > 1

            def set(self):
                pass

            def is_set(self):
                return False

        for status in ("stale_manifest", "partial", "updated"):
            u2 = e.updater()
            u2.stop_ev = Ev()
            waits.clear()
            u2.cycle = lambda st=status: st
            u2.run()
            test("재시도 간격: %s → %s초" % (status, waits[1]), waits[1] == (G.RETRY_SEC if status != "updated" else 1800))
    finally:
        e.close()


@case
def update_idle_disconnect():
    e = Env()
    try:
        e.upload()
        u = e.updater()
        u._connect()
        e.share.drop_idle = True          # 이 응답 직후 서버가 (Connection: close 없이) 연결을 끊는다
        r = u._request(C.PATH_MANIFEST)
        r.read()
        sha = next(iter(e.share.manifest["files"].values()))["sha256"]
        # 서버가 마지막 응답 직후 연결을 끊은 상태에서 다음 요청
        time.sleep(0.2)
        c0 = e.share.conns
        r = u._request(C.PATH_OBJECTS + sha)
        r.read()
        test("유휴 끊김 후 _request: 1회 재연결로 성공(200)", r.status == 200 and e.share.conns == c0 + 1, "conns +%d" % (e.share.conns - c0))
        u._close()
        # 응답마다 서버가 끊는 상태에서도 한 사이클이 완료된다
        s = e.updater().cycle()
        test("매 응답 후 서버가 연결을 끊어도 사이클 완료", s == "updated" and len(snap(e.dst)) == len(NAMES), s)
        # 재연결도 실패하면 예외 없이 unreachable
        u3 = e.updater()
        u3._connect()
        u3.cfg["urls"] = ["https://127.0.0.1:%d" % closed_port()]
        u3.conn.sock.close()
        try:
            u3._request(C.PATH_MANIFEST)
            ok = False
        except G.CycleFail as ex:
            ok = ex.status == "unreachable"
        test("재연결 실패는 CycleFail(unreachable)", ok)
        # 해시 계산이 길어지는 경우: 사이클 중간(해시 후)에 연결을 새로 한다
        e.share.drop_idle = False
        shutil.rmtree(e.dst)
        os.makedirs(e.dst)
        shutil.copytree(e.src, e.dst, dirs_exist_ok=True)
        write_lib(e.src, v=1, names=[NAMES[4]])
        e.upload()
        e.updater().cycle()
        c1 = e.share.conns
        write_lib(e.src, v=2, names=[NAMES[4]])
        e.upload()
        u4 = e.updater()
        u4.cycle()
        test("해시 계산 뒤 연결 재수립(매니페스트용 + 다운로드용 2개 연결)", e.share.conns - c1 >= 2)
    finally:
        e.close()


@case
def update_malicious_manifest():
    e = Env()
    try:
        e.upload()
        outside = os.path.join(e.dir, "outside")
        os.makedirs(outside)
        sentinel = os.path.join(outside, "keep.txt")
        with open(sentinel, "w") as f:
            f.write("k")
        good = e.share.manifest
        ent = {"sha256": "b" * 64, "size": 10}
        for bad in ["../x.json.gz", "hands/a.json.gz", "/tmp/BTN_BB_20bb.json.gz", "C:\\x", "BTN_BB_20bb.json.gz.tmp",
                    "turn/../../x.json.gz"]:
            files = dict(good["files"])
            files[bad] = ent
            e.share.raw_manifest = json.dumps({**good, "files": files}).encode()
            before = tree_state(e.dir)
            s = e.updater().cycle()
            after = tree_state(e.dir)
            # .update-tmp/lock 만 새로 생길 수 있다
            diff = {k for k in set(before[0]) | set(after[0]) if before[0].get(k) != after[0].get(k)}
            diff = {k for k in diff if not k.startswith(os.path.join("dst", G.TMP_NAME)) and k != "dst"}
            diff = {k for k in diff if not k.startswith("base") or k == "base"}
            test("악성 매니페스트(%s): 전체 거부, 대상 밖·안 쓰기 없음" % bad, s == "bad_manifest" and e.share.obj_gets() == 0
                 and not [d for d in diff if d != "base"] and open(sentinel).read() == "k", "%s %s" % (s, sorted(diff)[:3]))
        # 심볼릭 링크 탈출
        e.share.raw_manifest = None
        try:
            os.symlink(outside, os.path.join(e.dst, "turn"), target_is_directory=True)
        except OSError:  # Windows: 개발자 모드나 관리자 권한 없이는 심볼릭 링크를 만들 수 없다
            results.append(("SKIP", "심볼릭 링크로 대상 밖을 가리키는 경로", "심볼릭 링크 생성 권한 없음"))
        else:
            s = e.updater().cycle()
            test("심볼릭 링크로 대상 밖을 가리키는 경로: 거부, 대상 밖 쓰기 없음", s == "bad_manifest" and os.listdir(outside) == ["keep.txt"], s)
            os.unlink(os.path.join(e.dst, "turn"))
        for name, raw in {"format 모름": {**good, "format": 99}}.items():
            e.share.raw_manifest = json.dumps(raw).encode()
            test("매니페스트 %s: 무시" % name, e.updater().cycle() == "format_unknown" and snap(e.dst) == {})
        e.share.raw_manifest = b"{not json"
        test("매니페스트 JSON 깨짐: bad_manifest", e.updater().cycle() == "bad_manifest")
        e.share.raw_manifest = None
        e.updater().cycle()
        e.share.manifest = {**e.share.manifest, "version": 0}
        e.share.raw_manifest = None
        test("서버 버전이 낮음: server_older, 파일 불변", e.updater().cycle() in ("server_older", "unchanged"))
        # 경로 조작 URL (계약 모의)
        api = S.Api(e.up_cfg()["url"], RD_TOKEN)
        codes = [api.req("GET", p)[0] for p in (C.API + "/objects/../../x", C.API + "/objects/%2e%2e/%2e%2e/x",
                                                C.PATH_OBJECTS + "A" * 64, C.PATH_OBJECTS + "g" * 64)]
        test("계약 모의: 경로 조작 URL/대문자 해시 → 404", codes == [404] * 4, str(codes))
    finally:
        e.close()


@case
def update_share_violation():
    e = Env()
    try:
        e.upload()
        e.updater().cycle()
        write_lib(e.src, v=1, names=NAMES[:6])
        e.upload()
        new = {n: file_bytes(e.src, n) for n in NAMES}
        victim = os.path.normpath(os.path.join(e.dst, NAMES[4]))
        real = G._replace
        n = {"c": 0}

        def sharing(limit):
            def f(src, dst):
                if os.path.normpath(dst) == victim and n["c"] < limit:
                    n["c"] += 1
                    raise PermissionError(13, "sharing violation (injected)")
                return real(src, dst)
            return f

        with patched(G, "_replace", sharing(3)):
            s = e.updater().cycle()
        test("공유 위반 3회 후 성공: 재시도로 완료", s == "updated" and n["c"] == 3 and file_bytes(e.dst, NAMES[4]) == new[NAMES[4]]
             and applied(e) == 2, s)
        write_lib(e.src, v=2, names=NAMES[:6])
        e.upload()
        new2 = {n_: file_bytes(e.src, n_) for n_ in NAMES}
        old_victim = file_bytes(e.dst, NAMES[4])
        n["c"] = 0
        with patched(G, "_replace", sharing(10 ** 6)):
            u = e.updater()
            s = u.cycle()
        test("공유 위반 지속: partial, 버전 불변, 8회 재시도 후 보류", s == "partial" and applied(e) == 2 and n["c"] == 8, "시도 %d" % n["c"])
        test("공유 위반 지속: 보류 파일은 옛 내용 그대로·온전, 나머지는 새 내용",
             file_bytes(e.dst, NAMES[4]) == old_victim and all(file_bytes(e.dst, x) == new2[x] for x in NAMES[:6] if x != NAMES[4]))
        gets = e.share.obj_gets()
        s = e.updater().cycle()
        test("다음 사이클에 완료(보류분만)", s == "updated" and applied(e) == 3 and file_bytes(e.dst, NAMES[4]) == new2[NAMES[4]]
             and e.share.obj_gets() - gets <= 1)
    finally:
        e.close()


@case
def update_toast_and_reader_independence():
    e = Env()
    try:
        e.upload()
        e.updater().cycle()
        test("첫 대량 설치(63개)는 토스트 호출", len(e.done) == 1)
        src = source("gto_update.py")
        test("갱신기: 로컬 파일 삭제 함수는 스테이징 정리뿐(unlink 대상은 .update-tmp)", "rmtree" not in src)
        test("갱신기: solverlib 는 lib_dir 미지정일 때만 지연 import", src.count("import solverlib") == 1
             and "def target_dir" in src[:src.index("import solverlib")].rsplit("\n    def ", 1)[1] + "def target_dir")
    finally:
        e.close()


# =====================================================================================================
# 정적 확인: gitignore / hidden-import / 훅 / 금지 파일 불변
# =====================================================================================================
@case
def static_checks():
    for name in ("gto_update.json", "gto_update_state.json", ".update-tmp/x", "gto_dist_a.json", "t.pem", "t.key", "t.crt"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=HERE)
        test("gitignore: " + name, r.returncode == 0)
    readme = source("README.md")
    build = next(l for l in readme.splitlines() if l.startswith("pyinstaller") and "reader.py" in l)
    test("README: CardReader 빌드 명령에 hidden-import gto_update, gto_dist_common",
         "--hidden-import gto_update" in build and "--hidden-import gto_dist_common" in build)
    review = next(l for l in readme.splitlines() if l.startswith("pyinstaller") and "review_app.py" in l)
    test("README: 복기 도구 명령에는 넣지 않음", "gto_update" not in review)
    test("README: 주소는 자리표시자(실제 IP 없음)", not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", readme.replace("127.0.0.1", "")))
    rs = source("reader.py")
    tree = ast.parse(rs)
    app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "App")
    init = next(n for n in app.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    idx = next(i for i, n in enumerate(init.body) if "after" in ast.dump(n) and "tick" in ast.dump(n)
               and isinstance(n, ast.Expr))
    prev = ast.dump(init.body[idx - 1]) + ast.dump(init.body[idx - 2]) + ast.dump(init.body[idx - 3])
    test("reader 훅: __init__ 의 root.after(100, tick) 바로 앞에서 gto_update.start(BASE, ...)",
         "gto_update" in prev and "start" in prev and "BASE" in prev and "gto_updater" in prev)
    close = next(n for n in app.body if isinstance(n, ast.FunctionDef) and n.name == "close")
    test("reader 훅: close() 의 closed=True 직후 gto_updater.stop()", "closed" in ast.dump(close.body[0])
         and "stop" in ast.dump(close.body[1]) and "gto_updater" in ast.dump(close.body[1]))
    test("reader: gto_update 는 _optional 로 불러옴(없어도 앱 동작)", '_optional("gto_update")' in rs)
    try:
        import py_compile
        py_compile.compile(os.path.join(HERE, "reader.py"), cfile=os.path.join(TMP, "r.pyc"), doraise=True)
        ok = True
    except Exception:
        ok = False
    test("reader.py py_compile (numpy·tkinter 없어 실행은 미검증)", ok)
    for unit, key in (("gto-dist-sync.service", "ExecStart=/usr/bin/python3 %h/.local/share/gto-dist/app/gto_dist_sync.py"),
                      ("gto-dist-sync.timer", "OnUnitActiveSec=15min")):
        with open(os.path.join(HERE, "deploy", unit), encoding="utf-8") as f:
            t = f.read()
        test("deploy/%s 존재, 비밀·실제 IP 없음" % unit, key in t and not re.search(r"token|\d+\.\d+\.\d+\.\d+", t, re.I))
    p = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "solverlib.py", "build_gto_lib.py", "preflop.py"], cwd=HERE)
    test("solverlib.py / build_gto_lib.py / preflop.py 변경 없음(레인지 해시 보존)", p.returncode == 0)


# =====================================================================================================
# 실제 shareboxd 통합 (바이너리 경로를 환경변수로 받음. 없으면 SKIP)
# =====================================================================================================
def free_port():
    return closed_port()


class Real:
    """임시 데이터 폴더의 실제 shareboxd. 토큰은 변수에만 둔다(로그/파일/출력 금지)."""

    def __init__(self, binary):
        self.binary = binary
        self.data = os.path.join(TMP, "sharebox-data")
        os.makedirs(self.data)
        self.env = {**os.environ, "SHAREBOX_DATA": self.data, "HOME": os.path.join(TMP, "home"),
                    "USERPROFILE": os.path.join(TMP, "home")}
        self.hp, self.tp = free_port(), free_port()
        assert self.hp not in (8737, 1213) and self.tp not in (8737, 1213) and self.hp != self.tp
        self.proc = None
        self.up = self.sb("token", "add", "t-up", "space:gto:write")
        self.rd = self.sb("token", "add", "t-rd", "space:gto:read")
        self.us = self.sb("token", "add", "t-user", "user")
        self.pin = self.sb("cert-pin")
        self.start()

    def sb(self, *a):
        r = subprocess.run([self.binary, *a], env=self.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, "shareboxd %s 실패" % a[0]
        return r.stdout.strip()

    def start(self):
        self.proc = subprocess.Popen([self.binary, "serve", "-listen", "127.0.0.1:%d" % self.hp, "-tls-listen",
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

    def http(self, method, path, token=None, body=None, tls=False, headers=None):
        c = C.PinnedConnection("127.0.0.1", self.tp, self.pin) if tls else \
            http.client.HTTPConnection("127.0.0.1", self.hp, timeout=30)
        h = dict(headers or {})
        if token:
            h["Authorization"] = "Bearer " + token
        try:
            c.request(method, path, body=body, headers=h)
        except (BrokenPipeError, ConnectionResetError):      # 서버가 본문을 다 받기 전에 거부하고 끊은 경우
            pass
        r = c.getresponse()
        data = r.read()
        out = (r.status, data, {k.title(): v for k, v in r.getheaders()})
        c.close()
        return out

    def obj_path(self, sha):
        return os.path.join(self.data, "spaces", "gto", "objects", sha[:2], sha)

    def up_cfg(self, src, **kw):
        c = {"url": "http://127.0.0.1:%d" % self.hp, "token": self.up, "lib_dir": src}
        c.update(kw)
        return c

    def updater(self, base, dst, **kw):
        cfg = {"urls": ["https://127.0.0.1:%d" % self.tp], "token": self.rd, "cert_sha256": self.pin, "lib_dir": dst,
               "max_bytes_per_sec": 0}
        cfg.update(kw)
        errs = []
        u = G.Updater(base, cfg, on_error=lambda *a: errs.append(a), first_delay=0)
        u.errs = errs
        u.seen = []
        orig = u._request

        def spy(path, headers=None):
            r = orig(path, headers)
            u.seen.append((path, r.status))
            return r

        u._request = spy
        return u

    def cli(self, cfg, tag):
        p = os.path.join(TMP, "cli-%s.json" % tag)
        with open(p, "w") as f:
            json.dump(cfg, f)
        os.chmod(p, 0o600)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "gto_dist_sync.py"), "--config", p],
                           capture_output=True, text=True, env=env, timeout=300)
        os.unlink(p)
        return r


def real_shareboxd_tests(binary):
    """임시 SHAREBOX_DATA/HOME/포트에서만 실행. 실제 서비스(1213, 8737)와 홈 데이터는 쓰지 않는다."""
    R = Real(binary)
    try:
        real_flow(R)
        real_permissions(R)
        real_paths_and_validation(R)
        real_gc_race(R)
        real_idle_and_stop(R)
    finally:
        R.stop()


def real_flow(R):
    src, dst, base = (os.path.join(TMP, "real-" + x) for x in ("src", "dst", "base"))
    for d in (src, dst, base):
        os.makedirs(d)
    write_lib(src)
    before = tree_state(src)
    stats = {}
    rc = S.run(R.up_cfg(src), stats)
    test("[실서버] 업로드 후 게시: 버전 1, 63개 PUT, 원본 폴더 불변", rc == 0 and stats["version"] == 1 and stats["put"] == 63
         and tree_state(src) == before)
    stats = {}
    rc = S.run(R.up_cfg(src), stats)
    test("[실서버] 동일 내용 재동기화: exit 0, 게시 호출 없음(버전 1 유지), PUT 0회",
         rc == 0 and "version" not in stats and stats["put"] == 0
         and json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])["version"] == 1)
    st, data, hd = R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)
    test("[실서버] 매니페스트 GET: Content-Length와 ETag \"v1\", 304", st == 200 and hd.get("Content-Length") == str(len(data))
         and hd.get("Etag") == '"v1"' and R.http("GET", C.PATH_MANIFEST, R.rd, tls=True,
                                                 headers={"If-None-Match": '"v1"'})[0] == 304)
    pc = C.PinnedConnection("127.0.0.1", R.tp, R.pin)
    pc.connect()
    ver = pc.sock.version()
    pc.close()
    test("[실서버] 지문 일치: TLS 1.3 연결", ver == "TLSv1.3", ver)
    bad = R.updater(base, dst, cert_sha256="0" * 64)
    test("[실서버] 지문 불일치: pin_failed, 대상 폴더에 데이터 없음", bad.cycle() == "pin_failed" and bad.seen == []
         and not [n for n in os.listdir(dst) if n != G.TMP_NAME])
    u = R.updater(base, dst)
    s1 = u.cycle()
    test("[실서버] 빈 대상 첫 갱신: 63개 전부 설치, 내용 동일", s1 == "updated" and
         {k: v[0] for k, v in snap(dst).items()} == {k: v[0] for k, v in snap(src).items()}, s1)
    n_obj = sum(1 for p, c in u.seen if p.startswith(C.PATH_OBJECTS) and c == 200)
    u.seen.clear()
    s2 = u.cycle()
    test("[실서버] 매니페스트 불변: 304, 객체 GET 0회", s2 == "unchanged" and u.seen == [(C.PATH_MANIFEST, 304)], str(u.seen))
    write_lib(src, v=1, names=[NAMES[9]])
    S.run(R.up_cfg(src), stats)
    before_dst = snap(dst)
    u.seen.clear()
    s3 = u.cycle()
    gets = [p for p, c in u.seen if p.startswith(C.PATH_OBJECTS) and c == 200]
    after_dst = snap(dst)
    test("[실서버] 1개 변경 갱신: 객체 GET 정확히 1회(첫 설치는 %d회), PUT 1회" % n_obj,
         s3 == "updated" and len(gets) == 1 and stats["put"] == 1 and stats["version"] == 2)
    test("[실서버] 1개 변경 갱신: 나머지 62개 mtime 불변",
         [k for k in after_dst if after_dst[k] != before_dst[k]] == [os.path.normpath(NAMES[9])])
    # 손상 수신: 서버 데이터 폴더의 객체를 같은 길이로 변조(임시 폴더이므로 가능)
    old = snap(dst)
    write_lib(src, v=2, names=NAMES[10:13])
    S.run(R.up_cfg(src), stats)
    st, data, _ = R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)
    sha = json.loads(data)["files"][NAMES[11]]["sha256"]
    with open(R.obj_path(sha), "rb") as f:
        good = f.read()
    with open(R.obj_path(sha), "wb") as f:
        f.write(b"z" * len(good))
    u2 = R.updater(base, dst)
    s4 = u2.cycle()
    test("[실서버] 손상 파일 수신 거부: hash_mismatch, 기존 파일 동일, applied_version 불변",
         s4 == "hash_mismatch" and snap(dst) == old and json.load(open(os.path.join(base, G.STATE_NAME)))["applied_version"] == 2, s4)
    with open(R.obj_path(sha), "wb") as f:
        f.write(good)
    # 설치 중단
    cnt = {"n": 0}
    real_replace = G._replace

    def flaky(a, b):
        cnt["n"] += 1
        if cnt["n"] == 2:
            raise OSError(28, "injected")
        return real_replace(a, b)

    new = {n: file_bytes(src, n) for n in NAMES[10:13]}
    oldb = {n: file_bytes(dst, n) for n in NAMES[10:13]}
    with patched(G, "_replace", flaky):
        s5 = R.updater(base, dst).cycle()
    test("[실서버] 설치 중 예외: error, 각 파일은 옛것 또는 새것으로 온전, 버전 불변",
         s5 == "error" and not all_old_or_new(dst, oldb, new) and json.load(open(os.path.join(base, G.STATE_NAME)))["applied_version"] == 2, s5)
    test("[실서버] 예외 제거 후 다음 사이클에 마무리", R.updater(base, dst).cycle() == "updated"
         and all(file_bytes(dst, n) == new[n] for n in NAMES[10:13]))
    # 객체 삭제(정리와 겹침 흉내): stale_manifest
    write_lib(src, v=3, names=[NAMES[20]])
    S.run(R.up_cfg(src), stats)
    st, data, _ = R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)
    os.unlink(R.obj_path(json.loads(data)["files"][NAMES[20]]["sha256"]))
    keep = snap(dst)
    u3 = R.updater(base, dst)
    s6 = u3.cycle()
    test("[실서버] 서버에서 객체가 사라짐(404): stale_manifest, 오류 통지 없음, 기존 파일 유지", s6 == "stale_manifest" and not u3.errs
         and snap(dst) == keep)
    stats = {}
    S.run(R.up_cfg(src), stats)     # 매니페스트가 같아도 업로더가 사라진 객체를 다시 올려 복구
    test("[실서버] 다음 주기에 업로더가 사라진 객체를 재업로드(PUT 1회)하고 갱신 성공", stats["put"] == 1
         and R.updater(base, dst).cycle() == "updated")

    # 업로드 실패: lib 불변, exit 1
    st0 = tree_state(src)
    for name, cfg_kw, mut in (("토큰 오류", {"token": "wrong"}, None), ("서버 주소 닫힘", {"url": "http://127.0.0.1:%d" % closed_port()}, None)):
        r = R.cli(R.up_cfg(src, **cfg_kw), "f")
        test("[실서버] 업로드 실패(%s): exit 1, lib 불변, 토큰 미출력" % name, r.returncode == 1 and tree_state(src) == st0
             and R.up not in r.stdout + r.stderr and "wrong" not in r.stderr)


def real_permissions(R):
    st, data, _ = R.http("POST", "/api/texts", R.us, json.dumps({"content": "synthetic-private-text"}).encode(),
                         headers={"Content-Type": "application/json"})
    item = json.loads(data).get("id") if st in (200, 201) else None
    test("[실서버] (준비) user 토큰으로 사용자 항목 생성", item is not None, str(st))
    probes = [("GET", "/api/items", None), ("GET", "/api/items/%s" % item, None), ("GET", "/api/items/%s/content" % item, None),
              ("PATCH", "/api/items/%s" % item, b'{"pinned":true}'), ("DELETE", "/api/items/%s" % item, None),
              ("POST", "/api/files", b"x"), ("POST", "/api/texts", b'{"content":"x"}'), ("GET", "/api/me", None),
              ("GET", "/api/events", None)]
    for who, tok in (("읽기", R.rd), ("쓰기", R.up)):
        codes = []
        for tls in (False, True):
            for m, p, b in probes:
                codes.append(R.http(m, p, tok, b, tls=tls, headers={"Content-Type": "application/json"})[0])
        test("[실서버·15,16] %s 토큰: 기존 API %d종 × HTTP/HTTPS → 전부 403" % (who, len(probes)), set(codes) == {403}, str(sorted(set(codes))))
    st, data, _ = R.http("GET", "/api/items/%s/content" % item, R.us)
    test("[실서버·15,16] 시도 후에도 사용자 항목은 그대로(삭제·변경 없음)", st == 200 and b"synthetic-private-text" in data, str(st))
    sha = next(iter(json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])["files"].values()))["sha256"]
    test("[실서버·16] 쓰기 토큰: 객체 GET 403, 읽기 토큰: 객체 PUT/missing/게시 403",
         R.http("GET", C.PATH_OBJECTS + sha, R.up)[0] == 403
         and R.http("PUT", C.PATH_OBJECTS + sha, R.rd, b"x")[0] == 403
         and R.http("POST", C.PATH_MISSING, R.rd, b'{"sha256":[]}')[0] == 403
         and R.http("POST", C.PATH_MANIFEST, R.rd, b"{}")[0] == 403)
    test("[실서버] 쓰기 토큰도 매니페스트 GET 가능, 토큰 없음 401, 틀린 토큰 401",
         R.http("GET", C.PATH_MANIFEST, R.up)[0] == 200 and R.http("GET", C.PATH_MANIFEST)[0] == 401
         and R.http("GET", C.PATH_MANIFEST, "wrong")[0] == 401)
    skip("[실서버] 공간 API에 user/agent 토큰·세션 거부", "sharebox Go 테스트(space_test.go) 몫으로 표시 — 여기서는 user 토큰이 공간 API에서 거부되는지만 확인")
    test("[실서버] user 토큰으로 공간 API → 403", R.http("GET", C.PATH_MANIFEST, R.us)[0] == 403)
    R.sb("token", "revoke", "t-rd")
    test("[실서버·17] 읽기 토큰 폐기 후 다음 요청 401", R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[0] == 401)
    R.rd = R.sb("token", "add", "t-rd2", "space:gto:read")


def real_paths_and_validation(R):
    sha = next(iter(json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])["files"].values()))["sha256"]
    probes = ["/api/spaces/gto/objects/../../x", "/api/spaces/gto/objects/../manifest", "/api/spaces/gto/objects/%2e%2e/%2e%2e/x",
              "/api/spaces/gto/objects/..%2f..%2fx", "/api/spaces/gto/objects/" + sha.upper(), "/api/spaces/gto/objects/" + "g" * 64,
              "/api/spaces/gto/objects/%s/../%s" % (sha, sha)]
    rows = []
    for p in probes:
        c = C.PinnedConnection("127.0.0.1", R.tp, R.pin)
        c.request("GET", p, headers={"Authorization": "Bearer " + R.rd})
        r = c.getresponse()
        body = r.read()
        rows.append((p.split("objects/", 1)[-1][:40], r.status, r.getheader("Location"), len(body)))
        c.close()
    leak = [x for x in rows if x[1] == 200]
    test("[실서버] 경로 조작 URL %d종: 200(내용 노출) 없음, 상태 %s" % (len(rows), sorted({x[1] for x in rows})), not leak, str(rows))
    redirects = [x for x in rows if x[1] in (301, 302, 307, 308)]
    # 클라이언트는 리다이렉트를 따라가지 않는다: Updater._request 는 첫 응답을 그대로 돌려준다
    u = R.updater(os.path.join(TMP, "real-base"), os.path.join(TMP, "real-dst"))
    u._connect()
    r = u._request("/api/spaces/gto/objects/../../x")
    r.read()
    test("[실서버] 클라이언트는 리다이렉트를 따라가지 않음: 첫 응답 %d 을 그대로 돌려줌(추가 요청 0, urllib 미사용)" % r.status,
         r.status in (307, 404) and len(u.seen) == 1 and "urllib.request" not in source("gto_update.py")
         and "Location" not in source("gto_update.py"), "리다이렉트 응답 %d건" % len(redirects))
    u._close()
    st = R.http("GET", "/api/spaces/gto/objects/../../x", R.rd, tls=True)
    test("[실서버] 리다이렉트 응답 본문에 객체·매니페스트 내용 없음", b'"files"' not in st[1] and b"synthetic" not in st[1])
    # 게시 경로 조작: 422, 현재 매니페스트 불변
    cur = R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1]
    ent = {"sha256": sha, "size": 1}
    codes = []
    for bad in ["../x", "hands/a.json.gz", "/abs/BTN_BB_20bb.json.gz", "C:\\x", "BTN_BB_20bb.json.gz.tmp", "turn/../../x.json.gz"]:
        body = json.dumps({"base_version": json.loads(cur)["version"], "files": {bad: ent}, "allow_shrink": True}).encode()
        codes.append(R.http("POST", C.PATH_MANIFEST, R.up, body)[0])
    test("[실서버] 게시 경로 조작 6종 → 전부 422, 현재 매니페스트 불변", set(codes) == {422} and R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1] == cur, str(codes))
    mf = json.loads(cur)
    code = lambda **kw: R.http("POST", C.PATH_MANIFEST, R.up, json.dumps({"base_version": mf["version"], "files": mf["files"], "allow_shrink": False, **kw}).encode())[0]
    test("[실서버] 낡은 base_version → 409, 축소 → 409, 동일 파일 집합 → changed:false",
         code(base_version=mf["version"] - 1) == 409 and
         R.http("POST", C.PATH_MANIFEST, R.up, json.dumps({"base_version": mf["version"], "files": dict(list(mf["files"].items())[:5])}).encode())[0] == 409
         and b'"changed":false' in R.http("POST", C.PATH_MANIFEST, R.up, json.dumps({"base_version": mf["version"], "files": mf["files"]}).encode())[1])

    # PUT 검증
    good = make_obj("lib", 77)
    gsha = C.sha256_hex(good)

    def put(data, sha=None, extra=None):
        return R.http("PUT", C.PATH_OBJECTS + (sha or C.sha256_hex(data)), R.up, data, headers=extra)[0]

    chunked = http.client.HTTPConnection("127.0.0.1", R.hp, timeout=30)
    chunked.putrequest("PUT", C.PATH_OBJECTS + gsha)
    chunked.putheader("Authorization", "Bearer " + R.up)
    chunked.putheader("Transfer-Encoding", "chunked")
    chunked.endheaders()
    chunked.send(b"0\r\n\r\n")
    c411 = chunked.getresponse().status
    chunked.close()
    big = b"\0" * (C.MAX_OBJECT + 1)
    nick = gzip.compress(json.dumps({"players": [{"nick": "synthetic"}]}).encode(), mtime=0)
    r = {"해시 불일치": put(good, "0" * 64), "gzip 아님": put(b"plain"), "잘린 gzip": put(good[:-8]), "닉네임 JSON": put(nick),
         "길이 없음": c411, "크기 초과": put(big)}
    want = {"해시 불일치": 422, "gzip 아님": 422, "잘린 gzip": 422, "닉네임 JSON": 422, "길이 없음": 411, "크기 초과": 413}
    test("[실서버] PUT 거부: " + ", ".join("%s %d" % kv for kv in r.items()), r == want)
    test("[실서버] 정상 PUT 201 → 같은 해시 재PUT 200, 객체 mtime 불변", put(good) == 201 and
         (lambda m0: (time.sleep(1.1), put(good) == 200, os.stat(R.obj_path(gsha)).st_mtime_ns == m0)[1:])(os.stat(R.obj_path(gsha)).st_mtime_ns) == (True, True))
    # Go/Python 판정 비교
    rows = []
    for name, data, go_ok in py_verdict_variants():
        st = put(data)
        try:
            C.validate_object(data)
            py = True
        except C.ValidationError:
            py = False
        rows.append((name, "Go " + ("통과" if st in (200, 201) else "거부%d" % st), "Python " + ("통과" if py else "거부")))
        test("[실서버·판정] %s: Go %s, 업로더 검증 %s" % (name, "통과" if go_ok else "거부", "통과" if py else "거부"),
             (st in (200, 201)) == go_ok and (go_ok or not py), str(rows[-1]))
    old_only = {"UTF-8 BOM", "UTF-16 JSON", "UTF-32 JSON", "gzip 뒤 0바이트 패딩"}
    test("[실서버·판정] Go가 거부하는 4종은 업로더가 게시 전에 차단(Python 단독 통과 없음)",
         all(_py_rejects(d) for n, d, g in py_verdict_variants() if n in old_only))
    # 이런 파일이 lib 에 있을 때 업로더: exit 1, lib 불변, 게시 변화 없음
    for name, data, go_ok in py_verdict_variants():
        if _py_rejects(data) is False:
            continue          # 업로더가 통과시키는 파일은 정상 업로드이므로 제외
        if "정수" in name:    # 위 PUT 으로 서버에 이미 저장된 객체는 업로더가 검증하지 않으므로 서버에 없는 변종을 쓴다
            data = bigint_obj(4500)
        d = os.path.join(TMP, "badlib-" + re.sub(r"\W", "", name))
        write_lib(d)
        with open(os.path.join(d, NAMES[6]), "wb") as f:
            f.write(data)
        st0, ver0 = tree_state(d), json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])["version"]
        # 이 폴더는 다른 내용이라 게시가 바뀔 수 있으므로 base 서버 상태가 아니라 업로더 종료 상태만 본다
        r = R.cli(R.up_cfg(d), "bad")
        ver1 = json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])["version"]
        test("[실서버·판정] 업로드 거부 파일(%s) 포함 lib: exit 1, lib 불변, 게시 버전 불변" % name,
             r.returncode == 1 and tree_state(d) == st0 and ver0 == ver1 and "invalid_object" in r.stderr, r.stderr.strip()[-90:])
    print("판정표:", rows)


def real_gc_race(R):
    """48시간 넘은 미참조 객체가 missing 조회 뒤 서버 정리와 겹쳐 사라지는 경우(missing_object)."""
    src = os.path.join(TMP, "gc-src")
    shutil.rmtree(src, ignore_errors=True)
    write_lib(src)         # 현재 서버 상태와 같은 63개 파일 집합(축소 거부를 피한다)
    S.run(R.up_cfg(src), {})
    old = time.time() - 3 * 86400
    out = {}
    for round_no, (fire, label) in enumerate(((1, "1회 겹침"), (99, "계속 겹침"))):
        X_rel = NAMES[30 + round_no]
        x_bytes = file_bytes(src, X_rel)
        x_sha = C.sha256_hex(x_bytes)
        for k in range(1, 6):                            # X 를 현재+최근 4개 매니페스트에서 몰아낸다
            write_lib(src, v=100 * (round_no + 1) + k, names=[X_rel])
            S.run(R.up_cfg(src), {})
        os.utime(R.obj_path(x_sha), (old, old))          # 임시 폴더의 객체 mtime 을 3일 전으로
        out[label + "-사전"] = os.path.exists(R.obj_path(x_sha))
        with open(os.path.join(src, *X_rel.split("/")), "wb") as f:      # 러너가 옛 내용으로 되돌림
            f.write(x_bytes)
        fired = {"n": 0}
        orig = S.Api.json

        def hooked(self, method, path, obj, fire=fire, fired=fired, orig=orig, x_sha=x_sha):
            r = orig(self, method, path, obj)
            if path == C.PATH_MISSING and fired["n"] < fire:
                fired["n"] += 1
                if os.path.exists(R.obj_path(x_sha)):
                    os.utime(R.obj_path(x_sha), (old, old))
                # 서버 정리는 게시 때 실행되므로 다른 게시를 끼워 넣어 유발한다(실제로는 매시간 Maintain 이 같은 일을 한다)
                h = json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])
                files = dict(h["files"])
                alt = make_obj("turn", 999, 1000 * round_no + fired["n"])
                R.http("PUT", C.PATH_OBJECTS + C.sha256_hex(alt), R.up, alt)
                files[NAMES[40]] = {"sha256": C.sha256_hex(alt), "size": len(alt)}
                R.http("POST", C.PATH_MANIFEST, R.up, json.dumps({"base_version": h["version"], "files": files}).encode())
            return r

        gone = {}
        st = {}
        with patched(S.Api, "json", hooked):
            try:
                S.run(R.up_cfg(src), st)
                out[label] = "ok"
            except S.Fail as e:
                out[label] = "fail:" + str(e)[:30]
        out[label + "-삭제됨"] = fired["n"] >= 1
        if label == "계속 겹침":
            r = R.cli(R.up_cfg(src), "gc")           # 겹침 없는 다음 타이머 실행
            cur = json.loads(R.http("GET", C.PATH_MANIFEST, R.rd, tls=True)[1])
            out["다음주기"] = (r.returncode, cur["files"][X_rel]["sha256"] == x_sha, os.path.exists(R.obj_path(x_sha)))
    test("[실서버·GC] 사전 조건: 48시간 지난 미참조 객체 X 가 서버에 존재", out["1회 겹침-사전"] and out["계속 겹침-사전"])
    test("[실서버·GC] missing_object 가 1회 겹치면 같은 실행 안에서 재시도로 복구", out["1회 겹침"] == "ok", str(out))
    test("[실서버·GC] 계속 겹치면 Fail(exit 1)로 끝남", out["계속 겹침"].startswith("fail:"), str(out))
    test("[실서버·GC] 다음 주기(겹침 없음): exit 0, X 가 현재 매니페스트에 있고 객체 존재", out["다음주기"] == (0, True, True), str(out))


def real_idle_and_stop(R):
    base, dst = os.path.join(TMP, "idle-base"), os.path.join(TMP, "idle-dst")
    os.makedirs(base)
    os.makedirs(dst)
    u = R.updater(base, dst)
    u._connect()
    u._request(C.PATH_MANIFEST).read()
    time.sleep(125)                                      # sharebox IdleTimeout 2분을 넘긴다
    r = u._request(C.PATH_MANIFEST)
    r.read()
    test("[실서버] 유휴 2분 초과 후 연결 끊김 → 재연결로 성공(200)", r.status == 200)
    u._close()
    # 서버 중지
    s0 = u.cycle()
    before = snap(dst)
    R.stop()
    t0 = time.monotonic()
    u2 = R.updater(base, dst)
    s = u2.cycle()
    test("[실서버] 서버 중지: unreachable, 기존 파일 유지, 오류 통지 없음", s == "unreachable" and snap(dst) == before and not u2.errs, s0)
    u2.start()
    test("[실서버] 서버 중지 상태에서 start() 즉시 반환", time.monotonic() - t0 < 5 and u2.thread.daemon)
    u2.stop()
    r = R.cli(R.up_cfg(os.path.join(TMP, "real-src")), "down")
    test("[실서버] 서버 중지 중 업로더: exit 1, lib 불변", r.returncode == 1)


def snapshot_shas(d):
    return {C.sha256_hex(file_bytes(d, n)) for n in NAMES}


binary = os.environ.get("GTO_TEST_SHAREBOXD")
if not binary:
    skip("[실서버] 업로드/수신/지문/304/토큰 권한(설계 10절 1~5, 15~17)", "GTO_TEST_SHAREBOXD 환경변수 없음 (shareboxd 바이너리 경로)")
elif not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
    skip("[실서버] 통합 시험", "GTO_TEST_SHAREBOXD 경로가 실행 파일이 아님")
elif not HAVE_TLS:
    skip("[실서버] 통합 시험", "openssl 없음")
else:
    case(lambda: real_shareboxd_tests(binary))

skip("토큰 권한 15~17(기존 API 전 경로 403, 쓰기 토큰의 객체 GET 403, 폐기 후 401)을 가짜 서버로 검증",
     "서버 측 인증 코드(sharebox)의 속성이라 가짜 서버로는 의미 없음 — 실서버 통합 시험으로만 가능")
skip("서버 측 PUT 검증의 Go 판정 일치(같은 픽스처 양쪽 비교)", "Go 구현(gto-space 브랜치) 필요 — 통합 시험에서 확인")
skip("정리(GC) 정책, 40GiB 한도 507, 매니페스트 4개 유지", "서버 측 기능 — sharebox 쪽 Go 테스트 담당")
skip("Windows 실제 공유 위반 동작, 외부망(LTE) 접속", "Linux 테스트로 불가 — 수동 확인 항목")

# --- 마무리 -----------------------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
failed = 0
for st, name, info in results:
    failed += st == "FAIL"
    print(f"[{st}] {name}" + (f"  — {info}" if info else ""))
npass = sum(1 for s, *_ in results if s == "PASS")
print(f"\n{npass}/{len(results)} passed, {failed} failed, {sum(1 for s, *_ in results if s == 'SKIP')} skipped")
assert failed == 0, f"{failed} failed"
