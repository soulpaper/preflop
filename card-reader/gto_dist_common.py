"""GTO 데이터 배포(sharebox 공간 API): 업로더/클라이언트가 공유하는 규칙. 표준 라이브러리만 사용."""
import gzip
import hashlib
import io
import json
import re

LIB_RE = r"[A-Z0-9]+_[A-Z0-9]+_\d{2,3}bb\.json\.gz"
TURN_RE = r"turn/[A-Z0-9]+_[A-Z0-9]+_\d{2,3}bb/(?:[2-9TJQKA][shdc]){3}\.json\.gz"
_LIB = re.compile(LIB_RE)
_TURN = re.compile(TURN_RE)
SHA_RE = re.compile(r"[0-9a-f]{64}")

LIB_KEYS = {"ante_total", "behind", "built", "flops", "ip", "oop", "open", "pot", "range_hash", "ranges",
            "scale", "solver", "spot", "stack", "tree", "tree_hash", "version"}
TURN_KEYS = {"board", "lines", "rh", "th"}
MAX_OBJECT = 16 * 1024 * 1024
MAX_INFLATED = 64 * 1024 * 1024
MAX_FILES = 100000
MAX_MANIFEST = 32 * 1024 * 1024
DEFAULT_PORT = 1213        # sharebox TLS 포트
FORMAT = 1

# sharebox 공간 API (Go 쪽과 계약만 공유한다)
API = "/api/spaces/gto"
PATH_MANIFEST = API + "/manifest"
PATH_MISSING = API + "/objects/missing"
PATH_OBJECTS = API + "/objects/"


class ValidationError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code


def valid_path(rel):
    """허용 경로인가. 반드시 fullmatch (.tmp, hands/, 절대 경로, ../ 는 전부 불허)."""
    return isinstance(rel, str) and bool(_LIB.fullmatch(rel) or _TURN.fullmatch(rel))


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def validate_manifest_files(files):
    """files: {rel: {"sha256","size"}}. 하나라도 잘못이면 ValidationError (전체 거부)."""
    if not isinstance(files, dict):
        raise ValidationError("bad_manifest", "files")
    if len(files) > MAX_FILES:
        raise ValidationError("bad_manifest", "too_many_files")
    for rel, ent in files.items():
        if not valid_path(rel):
            raise ValidationError("bad_path", repr(rel)[:80])
        if not isinstance(ent, dict) or set(ent) != {"sha256", "size"}:
            raise ValidationError("bad_manifest", "entry")
        sha, size = ent["sha256"], ent["size"]
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ValidationError("bad_hash", repr(rel)[:80])
        if type(size) is not int or size < 0 or size > MAX_OBJECT:
            raise ValidationError("bad_size", repr(rel)[:80])


def _reject_constant(name):
    raise ValueError("non-standard JSON constant " + name)     # NaN/Infinity 는 Go 쪽도 거부


def validate_object(data):
    """gzip + JSON 스키마 검증. 통과하면 파싱된 dict 를 돌려준다."""
    if len(data) > MAX_OBJECT:
        raise ValidationError("too_large")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            raw = g.read(MAX_INFLATED + 1)
    except Exception as e:
        raise ValidationError("bad_gzip", type(e).__name__)
    if len(raw) > MAX_INFLATED:
        raise ValidationError("bad_gzip", "inflated_too_large")
    try:
        obj = json.loads(raw, parse_constant=_reject_constant)
    except Exception:
        raise ValidationError("bad_schema", "json")
    del raw
    if not isinstance(obj, dict):
        raise ValidationError("bad_schema", "not_object")
    keys = set(obj)
    if keys == LIB_KEYS:
        if obj["version"] != 1 or type(obj["version"]) is not int or not isinstance(obj["spot"], str) \
                or type(obj["stack"]) is not int:
            raise ValidationError("bad_schema", "lib_types")
    elif keys == TURN_KEYS:
        if not isinstance(obj["board"], str):
            raise ValidationError("bad_schema", "turn_types")
    else:
        raise ValidationError("bad_schema", "keys")
    return obj


# --- TLS 지문 고정 클라이언트 (앱 갱신기가 사용) ---------------------------
import http.client  # noqa: E402
import ssl  # noqa: E402
import urllib.parse  # noqa: E402


class PinnedConnection(http.client.HTTPSConnection):
    """서버 인증서의 SHA-256 이 설정값과 다르면 요청과 토큰을 보내기 전에 연결을 끊는다."""

    def __init__(self, host, port, fingerprint, timeout=30, connect_timeout=5):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        super().__init__(host, port, timeout=connect_timeout, context=ctx)
        self._fp = fingerprint.lower()
        self._read_timeout = timeout

    def connect(self):
        super().connect()
        try:
            der = self.sock.getpeercert(True)
            ok = bool(der) and hmac_equal(hashlib.sha256(der).hexdigest(), self._fp)
        except Exception:
            ok = False
        if not ok:
            try:
                self.sock.close()
            finally:
                self.sock = None
            raise ssl.SSLError("fingerprint_mismatch")
        self.sock.settimeout(self._read_timeout)


def hmac_equal(a, b):
    import hmac
    return hmac.compare_digest(a, b)


def parse_https(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("https only")
    return u.hostname, u.port or DEFAULT_PORT
