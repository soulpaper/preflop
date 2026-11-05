"""card-reader 핸드/결정 기록을 로컬 sharebox 기록 공간으로 올리는 독립 업로더.

사용: python records_sync.py --base <폴더> --once   (--loop, --check-only, --dry-run, --backfill FROM [--to TO], --reset-disabled)
<base>/hands/ 한 폴더에서 허용 목록 이름(YYYY-MM-DD.jsonl, decisions_YYYY-MM-DD.jsonl)의 일반 파일만 연다.
설정 <base>/records_upload.json, 상태 <base>/records_state.json (본문·값·토큰 없음).
종료 코드: 0 성공, 1 일부 파일 오류, 2 거부, 3 연결 불가.
"""
import argparse
import ast
import collections
import datetime
import errno
import hashlib
import http.client
import json
import os
import re
import stat as S
import sys
import time
import unicodedata

try:
    import fcntl
except ImportError:      # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

import gto_dist_common as C
import records_check as RC

CONFIG_NAME = "records_upload.json"
STATE_NAME = "records_state.json"
LOCK_NAME = ".records-sync.lock"
API = "/api/spaces/records"
REPARSE = 0x400       # FILE_ATTRIBUTE_REPARSE_POINT
_open = open          # 테스트에서 감시


class Refuse(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


class Block(Exception):
    def __init__(self, rule, path="", line=0, file=""):
        super().__init__(rule)
        self.rule, self.path, self.line, self.file = rule, path, line, file


class ConnFail(Exception):
    pass


class FileError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def norm_name(name):
    return "".join(unicodedata.normalize("NFC", str(name or "")).split()).lower()


def load_config(base):
    """설정 검사. 문제가 있으면 Refuse. 경고 목록도 돌려준다."""
    path = os.path.join(base, CONFIG_NAME)
    try:
        if os.name == "nt":
            # Windows: 표준 라이브러리로 ACL 을 읽지 않는다. README 의 icacls 절차를 따른다.
            with _open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = RC.read_private_json(path)
    except RC.ConfigPerms as e:
        raise Refuse("config_perms", "chmod 600 %s ; chmod 700 %s (%s)" % (path, os.path.dirname(path), e))
    except (OSError, ValueError):
        raise Refuse("no_config")
    if not isinstance(cfg, dict):
        raise Refuse("bad_config")
    if cfg.get("enabled") is not True:
        raise Refuse("disabled_by_config")
    urls = cfg.get("urls")
    if not isinstance(urls, list) or not urls:
        raise Refuse("bad_config", "urls")
    for u in urls:
        if not isinstance(u, str) or not u.startswith("https://"):
            raise Refuse("bad_config", "urls")
        try:
            C.parse_https(u)
        except ValueError:
            raise Refuse("bad_config", "urls")
    if not isinstance(cfg.get("token"), str) or not cfg["token"]:
        raise Refuse("bad_config", "token")
    fp = cfg.get("cert_sha256")
    if not isinstance(fp, str) or not C.SHA_RE.fullmatch(fp.lower()):
        raise Refuse("bad_config", "cert_sha256")
    iv = cfg.get("interval_sec", 900)
    if type(iv) is not int or iv < 60:
        raise Refuse("bad_config", "interval_sec")
    wd = cfg.get("window_days", 7)
    if type(wd) is not int or not 1 <= wd <= 30:
        raise Refuse("bad_config", "window_days")
    al = cfg.get("allow_shrink", [])
    if not isinstance(al, list) or any(not isinstance(x, str) or RC.file_kind(x) is None for x in al):
        raise Refuse("bad_config", "allow_shrink")
    cfg = dict(cfg, interval_sec=iv, window_days=wd, allow_shrink=al)
    return cfg


def save_state_atomic(path, st):
    """같은 폴더 .tmp 에 써서 fsync 뒤 교체한다. 실패하면 .tmp 를 지운다."""
    tmp = path + ".tmp"
    try:
        with _open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _lock(base):
    f = _open(os.path.join(base, LOCK_NAME), "a+")
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        raise Refuse("busy")
    return f


# --- 본문 만들기 ---------------------------------------------------------------

def _dump(v):
    """결정적 직렬화: 키 정렬, 숫자는 원본 글자 그대로."""
    if isinstance(v, RC.Obj):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _dump(v[k]) for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_dump(e) for e in v) + "]"
    if isinstance(v, (RC.IntLit, RC.FloatLit)):
        return str(v)
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    return json.dumps(v, ensure_ascii=False)


def _strings(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)
    elif type(v) is str:
        yield v


def build_body(name, raw, extra_names=None, stats=None):
    """원본 바이트 -> (본문 바이트 또는 None, 줄 수, 뺀 줄 수). 차단이면 Block.
    stats(dict)가 있으면 필터가 버린 키 수 "keys"와 경로별 Counter "paths"를 더한다 (값 없음)."""
    V = RC.FILTER_V
    if stats is not None:
        stats.setdefault("values", 0)
        stats.setdefault("vpaths", collections.Counter())
    kind = RC.file_kind(name)
    cut = raw.rfind(b"\n")
    raw = raw[:cut + 1]
    names = [n for n in (norm_name(x) for x in (extra_names or ())) if len(n) >= 4 and any(c.isalpha() for c in n)]
    out, dropped = [], 0
    if not raw:
        return None, 0, 0
    for i, ln in enumerate(raw[:-1].split(b"\n"), 1):
        if ln.endswith(b"\r"):
            ln = ln[:-1]
        if not ln:
            continue
        try:
            text = ln.decode("utf-8")
        except UnicodeDecodeError:
            dropped += 1
            continue
        if RC._too_deep(text):
            dropped += 1
            continue
        try:
            rec = RC.parse_line(text)
        except (ValueError, RecursionError):
            dropped += 1
            continue
        if not isinstance(rec, RC.Obj):
            dropped += 1
            continue
        dp = RC.dup_path(rec)
        if dp is not None:
            raise Block("dup_key", dp, i, name)
        try:
            rec, dk = RC.filter_record(kind, rec, stats["paths"] if stats is not None else None, stats)
        except RC.FilterBlock as fb:
            raise Block(fb.rule, fb.path, i, name)
        if stats is not None:
            stats["keys"] += dk
        try:
            data = _dump(rec).encode("utf-8")
        except UnicodeEncodeError:
            dropped += 1
            continue
        v = RC.check_line(kind, data, (V, V))
        if v.cls == "block":
            raise Block(v.rule, v.path, i, name)
        if v.cls == "format":
            dropped += 1
            continue
        if names:
            for s in _strings(rec):
                ns = norm_name(s)
                if any(n in ns for n in names):
                    raise Block("known_name", "", i, name)
        out.append(data + b"\n")
    if not out:
        return None, 0, dropped
    body = b"".join(out)
    v = RC.check_bytes(kind, body, (V, V))
    if v.cls == "too_large":
        raise FileError(v.rule)
    if not v.ok():
        raise FileError("bad_body_" + v.rule)
    return body, len(out), dropped



# --- 백필 전 점검 (명세 13.4) -------------------------------------------------------

_JOSA = ("이", "가", "은", "는", "을", "를", "에", "의", "로", "와", "과", "도", "만", "면")
_KO_RUN = re.compile(r"[가-힣]{2,8}")
_CARD_RE = re.compile(r"[2-9TJQKA][shdc]")
_POS_RE = re.compile(r"\b(?:UTG|U1|U2|U3|MP|LJ|HJ|CO|BTN|SB|BB)\b")
DISTINCT_CAP = 100000
AUDIT_V = 2           # 점검 방식 판 (명세 16.6): 규칙이 바뀌면 올린다. 사전 sha 는 관문에 쓰지 않는다
UNK_LINES_MAX = 10    # where 줄에 적는 줄 번호 수 (종류별)


def load_vocab(here=None):
    """같은 폴더의 앱 소스(test_*.py, records_*.py 제외)에서 문자열 상수의 한글 덩어리를 모은다 (ast, import 안 함)."""
    here = here or os.path.dirname(os.path.abspath(__file__))
    words = set()
    for n in sorted(os.listdir(here)):
        if not n.endswith(".py") or n.startswith("test_") or n.startswith("records_"):
            continue
        try:
            with open(os.path.join(here, n), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                words.update(re.findall(r"[가-힣]+", node.value))
    return words


_EN_TOKEN = re.compile(r"[A-Za-z0-9]+")
_EN_CHUNK = re.compile(r"[A-Za-z]+")
_EN_CARDS = re.compile(r"(?:[2-9TJQKA][shdc])+")
_EN_COMBO = re.compile(r"[2-9TJQKA]{2,5}[so]?")
_EN_POS = frozenset(["UTG", "U1", "U2", "U3", "MP", "LJ", "HJ", "CO", "BTN", "SB", "BB"])


def _en_words(text):
    """문자열 하나에서 [A-Za-z]{2,} 덩어리를 소문자로 모은다."""
    return {w.lower() for w in _EN_CHUNK.findall(text) if len(w) >= 2}


def load_vocab_en(here=None):
    """load_vocab 과 같은 파일 고르기. 문자열 상수의 영문 덩어리(2자 이상, 소문자)를 모은다. 독스트링과 맨 문자열 문은 뺀다.
    RC.ENUMS 의 값에서 뽑은 덩어리를 더한다 (명세 16.6)."""
    here = here or os.path.dirname(os.path.abspath(__file__))
    words = set()
    for n in sorted(os.listdir(here)):
        if not n.endswith(".py") or n.startswith("test_") or n.startswith("records_"):
            continue
        try:
            with open(os.path.join(here, n), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            continue
        skip = {id(node.value) for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                words |= _en_words(node.value)
    for vals in RC.ENUMS.values():
        for v in vals:
            words |= _en_words(v)
    return words


def _unknown_en(text, vocab_en):
    """자유 텍스트 문자열의 낯선 영숫자 토큰 식별자 집합 (명세 16.6). 값은 돌려주지 않는다."""
    out = set()
    for tok in _EN_TOKEN.findall(unicodedata.normalize("NFKC", text)):
        if not any(c.isalpha() for c in tok):
            continue
        if _EN_CARDS.fullmatch(tok) or _EN_COMBO.fullmatch(tok):
            continue
        if all(len(c) < 2 or c in _EN_POS or c.lower() in vocab_en for c in _EN_CHUNK.findall(tok)):
            continue
        out.add(hashlib.blake2b(tok.lower().encode("utf-8"), digest_size=8).digest())
    return out


def _unknown_ko(text, vocab):
    out = set()
    for c in _KO_RUN.findall(text):
        if c in vocab:
            continue
        if any(c.endswith(j) and len(c) - 1 >= 2 and c[:-1] in vocab for j in _JOSA):
            continue
        out.add(hashlib.blake2b(c.encode("utf-8"), digest_size=8).digest())
    return out


def _skeleton(text):
    s = _CARD_RE.sub("C", text)
    s = _POS_RE.sub("P", s)
    return re.sub(r"[0-9]+(?:\.[0-9]+)?", "0", s)


def _verdict_rank(v):
    return {"OK": 0, "CHECK": 1, "STOP": 2}[v]


# --- 업로더 --------------------------------------------------------------------

def _same_file(st, ident):
    """targets() 때의 ident=(dev, ino, size, mtime_ns) 와 열고 나서의 fstat 이 같은 파일인가.
    ino 가 양쪽 다 실제 값이면 (dev, ino) 로 비교한다. 한쪽이라도 0 이면(Windows 일부 환경) ino 는 믿지 않고
    대체 비교: 파일을 쓰는 중에 커지는 것은 정상이므로 크기는 줄지 않았고 mtime 은 되돌아가지 않았을 때만 같다고 본다."""
    dev, ino, size, mtime = ident
    if ino != 0 and st.st_ino != 0:
        return (st.st_dev, st.st_ino) == (dev, ino)
    return st.st_size >= size and st.st_mtime_ns >= mtime


class Uploader:
    def __init__(self, base, cfg, on_error=None, extra_names=None, today=None, require_audit=True):
        self.base, self.cfg = base, cfg
        self.require_audit = require_audit
        self._audit = None
        self.on_error = on_error or (lambda *a: None)
        self.extra_names = extra_names        # None, 목록, 또는 매 주기 부르는 함수 (메모리에만 둔다)
        self.today = today or datetime.date.today
        self.conn = None
        self.last_url = None
        self.files = {}       # name -> state entry
        self.errors = []
        self.idents = {}      # name -> (st_dev, st_ino, st_size, st_mtime_ns) (targets 때)
        self.keystats = {}    # name -> 필터가 버린 키 통계

    # 상태
    def _state_path(self):
        return os.path.join(self.base, STATE_NAME)

    def load_state(self):
        try:
            with _open(self._state_path(), encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, st):
        save_state_atomic(self._state_path(), st)

    def _err(self, what, *a):
        try:
            self.on_error("records_sync", what, *a)
        except Exception:
            pass

    # 대상 파일
    def hands_dir(self):
        d = os.path.join(self.base, "hands")
        try:
            st = os.lstat(d)
        except OSError:
            return None
        if S.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
            raise Refuse("hands_is_link")
        return d if S.S_ISDIR(st.st_mode) else None

    def targets(self, lo, hi):
        d = self.hands_dir()
        if d is None:
            return []
        out = []
        with os.scandir(d) as it:
            for e in it:
                if RC.file_kind(e.name) is None:
                    continue
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    # DirEntry.stat() 은 Windows 에서 st_ino/st_dev 가 0 이다: 식별용 값은 os.stat 으로 얻는다
                    est = os.stat(os.path.join(d, e.name), follow_symlinks=False)
                    if getattr(est, "st_file_attributes", 0) & REPARSE or not S.S_ISREG(est.st_mode):
                        continue
                except OSError:
                    continue
                dt = RC.file_date(e.name)
                if lo <= dt <= hi:
                    out.append(e.name)
                    self.idents[e.name] = (est.st_dev, est.st_ino, est.st_size, est.st_mtime_ns)
        return sorted(out)

    def check_scrubbed(self):
        for n in ("profiles.db", "profiles.db-wal", "profiles.db-shm", "profiles.db-journal"):
            if os.path.lexists(os.path.join(self.base, n)):
                raise Refuse("scrub_not_done", "scrub_names.py 를 먼저 실행하세요")

    def _names_now(self):
        src = self.extra_names
        if callable(src):
            src = src()
        return list(src or ())

    def _open_checked(self, p, ident):
        """열기 직전 재확인: 일반 파일이고 targets() 때의 (dev, ino)와 같을 때만 읽는다."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(p, flags)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise FileError("changed_during_open")
            raise
        try:
            st = os.fstat(fd)
            if not S.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
                raise FileError("changed_during_open")
            if ident is not None and not _same_file(st, ident):
                raise FileError("changed_during_open")
            with os.fdopen(fd, "rb") as f:
                fd = -1
                return f.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def _gate_ok(self, n, raw):
        """백필 관문 (명세 13.4): 점검 through 이하 날짜의 파일은 OK 이거나 받아들인 점검 기록이 있어야 한다."""
        if not self.require_audit:
            return True
        a = self._audit
        if not isinstance(a, dict):
            return False                                  # 점검 기록이 아예 없으면 모든 파일이 막힌다
        try:
            through = datetime.date.fromisoformat(a.get("through", ""))
        except (ValueError, TypeError):
            return False
        d = RC.file_date(n)
        if d is not None and d > through:
            return True
        ent = (a.get("files") or {}).get(n)
        if a.get("filter_v") != RC.FILTER_V or a.get("audit_v") != AUDIT_V or not isinstance(ent, dict):
            return False
        if ent.get("sha256") != hashlib.sha256(raw).hexdigest():
            return False
        return ent.get("verdict") == "OK" or ent.get("accepted") is True

    def make_bodies(self, names):
        res = {}
        d = os.path.join(self.base, "hands")
        extra = self._names_now()
        for n in names:
            try:
                raw = self._open_checked(os.path.join(d, n), self.idents.get(n))
            except FileError as e:
                self.files[n] = {"result": e.code}
                self.errors.append(n)
                continue
            except OSError:
                self.files[n] = {"result": "read_error"}
                self.errors.append(n)
                continue
            if not self._gate_ok(n, raw):
                self.files[n] = {"result": "audit_required"}
                self.errors.append(n)
                continue
            stats = {"keys": 0, "paths": collections.Counter()}
            try:
                body, lines, dropped = build_body(n, raw, extra, stats)
            except Block as b:
                b.file = n
                raise
            except FileError as e:
                self.files[n] = {"result": e.code}
                self.errors.append(n)
                continue
            top = sorted(stats["paths"].items(), key=lambda kv: (-kv[1], kv[0]))[:20]
            vtop = sorted(stats["vpaths"].items(), key=lambda kv: (-kv[1], kv[0]))[:20]
            extra_ent = {"dropped_keys": stats["keys"], "dropped_paths": dict(top),
                         "dropped_values": stats["values"], "dropped_value_paths": dict(vtop)}
            if body is None:
                self.files[n] = dict({"result": "empty", "dropped": dropped}, **extra_ent)
                continue
            self.keystats[n] = extra_ent
            res[n] = (body, lines, dropped)
        return res

    # 네트워크
    def _connect(self):
        urls = list(self.cfg["urls"])
        if self.last_url in urls:
            urls.remove(self.last_url)
            urls.insert(0, self.last_url)
        for url in urls:
            host, port = C.parse_https(url)
            conn = C.PinnedConnection(host, port, self.cfg["cert_sha256"], timeout=30, connect_timeout=5)
            try:
                conn.connect()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            self.conn, self.last_url = conn, url
            return
        raise ConnFail()

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _request(self, method, path, headers=None, body=None):
        """(상태, 헤더맵, 본문 바이트). 끊기면 1회 다시 연결해 다시 보낸다."""
        h = {"Authorization": "Bearer " + self.cfg["token"]}
        h.update(headers or {})
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request(method, path, body=body, headers=h)
                r = self.conn.getresponse()
                data = r.read(1 << 20)
                return r.status, r, data
            except ConnFail:
                raise
            except (OSError, http.client.HTTPException):
                self._close()
                if attempt:
                    raise ConnFail()

    def get_status(self):
        code, _, data = self._request("GET", API + "/status")
        if code in (401, 403):
            raise Refuse("auth")
        if code != 200:
            raise Refuse("status_http_%d" % code)
        try:
            st = json.loads(data)
            files = {e["file"]: e for e in st.get("files", [])}
            return bool(st.get("blocked")), files
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Refuse("bad_status")


    def audit(self, from_date=None, to_date=None, accept=False, as_json=False):
        """네트워크 없이 과거 파일을 점검한다. 출력에는 경로와 개수만 있다 (값 없음). 종료 코드 0 OK, 1 CHECK, 2 STOP."""
        try:
            lock = _lock(self.base)
        except Refuse:
            print("records_sync: busy", file=sys.stderr)
            return 2
        try:
            return self._audit_run(from_date, to_date, accept, as_json)
        finally:
            lock.close()

    def _audit_run(self, from_date, to_date, accept, as_json):
        through = to_date or (self.today() - datetime.timedelta(days=1))
        lo = from_date or datetime.date(1, 1, 1)
        names = self.targets(lo, through)
        vocab = load_vocab()
        vocab_sha = hashlib.sha256("\n".join(sorted(vocab)).encode("utf-8")).hexdigest()
        vocab_en = load_vocab_en()
        vocab_en_sha = hashlib.sha256("\n".join(sorted(vocab_en)).encode("utf-8")).hexdigest()
        profiles = any(os.path.lexists(os.path.join(self.base, n))
                       for n in ("profiles.db", "profiles.db-wal", "profiles.db-shm", "profiles.db-journal"))
        n_path, typ_of = collections.Counter(), {}
        distinct, over = collections.defaultdict(set), set()
        skel, unk = collections.defaultdict(set), collections.defaultdict(set)
        unk_en = collections.defaultdict(set)
        keyset = collections.defaultdict(set)
        fail_total = collections.Counter()
        blocked = collections.Counter()
        total_lines = unreadable = fmt_lines = 0
        per_file, entries = [], {}
        for n in names:
            kind = RC.file_kind(n)
            try:
                raw = self._open_checked(os.path.join(self.base, "hands", n), self.idents.get(n))
            except (FileError, OSError):
                unreadable += 1
                per_file.append({"file": n, "lines": 0, "blocked": 0, "fail": 0, "unk_ko": 0,
                                 "unk_en": 0, "where": {}, "verdict": "CHECK"})
                continue
            fblocked = ffail = 0
            funk, funk_en = set(), set()
            where = {"unk_ko": [], "unk_en": []}
            flines = 0
            cut = raw.rfind(b"\n")
            raw2 = raw[:cut + 1]
            for lineno, ln in enumerate(raw2[:-1].split(b"\n") if raw2 else [], 1):
                if ln.endswith(b"\r"):
                    ln = ln[:-1]
                if not ln:
                    continue
                flines += 1
                try:
                    rec = RC.parse_line(ln.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    unreadable += 1
                    continue
                if not isinstance(rec, RC.Obj) or RC._too_deep(ln.decode("utf-8")):
                    unreadable += 1
                    continue
                dp = RC.dup_path(rec)
                if dp is not None:
                    blocked[("dup_key", dp)] += 1
                    fblocked += 1
                    continue
                vs = {"values": 0, "vpaths": collections.Counter()}
                try:
                    rec, _ = RC.filter_record(kind, rec, None, vs)
                except RC.FilterBlock as fb:
                    blocked[(fb.rule, fb.path)] += 1
                    fblocked += 1
                    continue
                for p, c in vs["vpaths"].items():
                    fail_total[p] += c
                ffail += vs["values"]
                try:
                    data = _dump(rec).encode("utf-8")
                except UnicodeEncodeError:
                    unreadable += 1
                    continue
                v = RC.check_line(kind, data, (RC.FILTER_V, RC.FILTER_V))
                if v.cls == "block":
                    blocked[(v.rule, v.path)] += 1
                    fblocked += 1
                    continue
                if v.cls == "format":
                    fmt_lines += 1
                    continue
                total_lines += 1

                def leaf(val, typ, segs):
                    if val is None or isinstance(val, (RC.Obj, list)):
                        return False
                    p = ".".join(RC._trim(segs))
                    n_path[p] += 1
                    typ_of[p] = typ
                    sval = val if type(val) is str else str(val)
                    if p not in over:
                        d = distinct[p]
                        d.add(hashlib.blake2b(sval.encode("utf-8"), digest_size=8).digest())
                        if len(d) > DISTINCT_CAP:
                            over.add(p)
                            distinct[p] = set()
                    if RC.TYPES.get(typ, {}).get("text") and type(val) is str:
                        skel[p].add(hashlib.blake2b(_skeleton(val).encode("utf-8"), digest_size=8).digest())
                        u = _unknown_ko(val, vocab)
                        unk[p] |= u
                        e = _unknown_en(val, vocab_en)
                        unk_en[p] |= e
                        for kind_, ids, seen in (("unk_ko", u, funk), ("unk_en", e, funk_en)):
                            if ids - seen and len(where[kind_]) < UNK_LINES_MAX and lineno not in where[kind_]:
                                where[kind_].append(lineno)
                            seen.update(ids)
                    return False
                RC._scan(rec, RC.schema_for(kind, rec), [], "top", lambda k, s, ok: False, None, leaf,
                         lambda k, ctx: keyset[ctx].add(hashlib.blake2b(k.encode("utf-8"), digest_size=8).digest())
                         if ctx in ("seat", "posseat", "q") else None, True)
            verdict = "STOP" if (fblocked or profiles) else ("CHECK" if (ffail or funk or funk_en) else "OK")
            per_file.append({"file": n, "lines": flines, "blocked": fblocked, "fail": ffail, "unk_ko": len(funk),
                             "unk_en": len(funk_en), "where": where, "verdict": verdict})
            entries[n] = {"sha256": hashlib.sha256(raw).hexdigest(), "verdict": verdict,
                          "accepted": bool(accept and verdict == "CHECK")}
        if profiles:
            blocked[("profiles_db", "-")] += 1
        overall = "OK"
        for f in per_file:
            if _verdict_rank(f["verdict"]) > _verdict_rank(overall):
                overall = f["verdict"]
        if profiles:
            overall = "STOP"
        paths = []
        for p in sorted(set(n_path) | set(fail_total)):
            row = {"path": p, "type": typ_of.get(p, "-"), "n": n_path.get(p, 0),
                   "distinct": ">%d" % DISTINCT_CAP if p in over else len(distinct.get(p, ())), "fail": fail_total.get(p, 0)}
            if RC.TYPES.get(typ_of.get(p), {}).get("text"):
                row["skel"], row["unk_ko"], row["unk_en"] = len(skel[p]), len(unk[p]), len(unk_en[p])
            paths.append(row)
        guide = {"OK": "값 형식과 낱말 점검을 통과했습니다. 이제 --backfill <시작> --to <끝> 으로 올릴 수 있습니다.",
                 "CHECK": "자유 텍스트 칸에 앱 문구에 없는 한글 낱말(unk_ko)이나 영문·숫자 낱말(unk_en)이 있거나, 형식이 틀려 버려질 값(fail)이 있습니다. "
                          "where 줄의 파일과 줄 번호를 직접 열어 확인하세요. 확인한 내용을 메시지나 문서에 옮기지 마세요. "
                          "닉네임이 아니면 --audit --accept-check 로 다시 실행하고, 닉네임이면 그 날짜를 백필에서 빼세요"
                          "(CHECK 파일은 받아들이기 전까지 올라가지 않습니다).",
                 "STOP": "업로드를 멈출 내용이 있습니다(위 blocked 줄). profiles.db 가 있으면 scrub_names.py 를 먼저 "
                         "실행하세요. 그 밖이면 해당 날짜를 백필 범위에서 빼고 홀덤연구소에 경로와 규칙 이름만 알리세요."}[overall]
        head = {"base": os.path.basename(os.path.abspath(self.base)), "files": len(names), "lines": total_lines,
                "unreadable": unreadable, "filter_v": RC.FILTER_V, "audit_v": AUDIT_V, "vocab": len(vocab),
                "vocab_en": len(vocab_en)}
        kc = {k: len(v) for k, v in sorted(keyset.items())}
        brows = [{"rule": r, "path": p, "lines": c} for (r, p), c in sorted(blocked.items())]
        if as_json:
            print(json.dumps({"head": head, "paths": paths, "keys": kc, "blocked": brows, "files": per_file,
                              "verdict": overall, "guide": guide}, ensure_ascii=False, sort_keys=True))
        else:
            print("records audit  base=%(base)s  files=%(files)d lines=%(lines)d unreadable=%(unreadable)d "
                  "filter_v=%(filter_v)d audit_v=%(audit_v)d vocab=%(vocab)d vocab_en=%(vocab_en)d" % head)
            print("path\ttype\tn\tdistinct\tfail\textra")
            for r in paths:
                extra = "skel=%d unk_ko=%d unk_en=%d" % (r["skel"], r["unk_ko"], r["unk_en"]) if "skel" in r else ""
                print("%s\t%s\t%d\t%s\t%d\t%s" % (r["path"], r["type"], r["n"], r["distinct"], r["fail"], extra))
            for k, c in kc.items():
                print("keys\t%s\t%d" % (k, c))
            for r in brows:
                print("blocked\t%s\t%s\t%d" % (r["rule"], r["path"], r["lines"]))
            print("file\tlines\tblocked\tfail\tunk_ko\tunk_en\tverdict")
            for f in per_file:
                print("%s\t%d\t%d\t%d\t%d\t%d\t%s" % (f["file"], f["lines"], f["blocked"], f["fail"], f["unk_ko"],
                                                       f["unk_en"], f["verdict"]))
            for f in per_file:
                for k in ("unk_ko", "unk_en"):
                    if f["where"].get(k):
                        print("where\t%s\t%s\t%s" % (f["file"], k, ",".join(str(x) for x in f["where"][k])))
            print("verdict: %s" % overall)
            print(guide)
        # 상태 기록 (값 없음)
        st = self.load_state()
        old = st.get("audit") if isinstance(st.get("audit"), dict) else {}
        same = old.get("filter_v") == RC.FILTER_V and old.get("audit_v") == AUDIT_V
        files = dict(old.get("files") or {}) if same else {}
        files.update(entries)
        th = through.isoformat()
        if same and old.get("through", "") > th:
            th = old["through"]
        st["audit"] = {"filter_v": RC.FILTER_V, "audit_v": AUDIT_V, "vocab_sha": vocab_sha, "vocab_en_sha": vocab_en_sha,
                       "at": int(time.time()), "through": th, "files": files}
        try:
            self.save_state(st)
        except OSError:
            pass
        return {"OK": 0, "CHECK": 1, "STOP": 2}[overall]

    # 한 주기
    def cycle(self, backfill=None, dry_run=False, check_only=False):
        """종료 코드를 돌려준다. backfill=(from_date, to_date)."""
        code = 0
        try:
            lock = _lock(self.base)
        except Refuse:
            return self._finish(2, "busy", save=False)
        try:
            return self._cycle(backfill, dry_run, check_only)
        finally:
            lock.close()

    def _cycle(self, backfill, dry_run, check_only):
        st = self.load_state()
        self._audit = st.get("audit")
        try:
            if st.get("disabled") is True:
                raise Refuse("disabled")
            self.cfg_ok()
            self.check_scrubbed()
            if backfill:
                lo, hi = backfill
            else:
                t = self.today()
                lo, hi = t - datetime.timedelta(days=self.cfg["window_days"]), t + datetime.timedelta(days=1)
            names = self.targets(lo, hi)
            try:
                bodies = self.make_bodies(names)
            except Block as b:
                st.update(disabled=True, disabled_rule=b.rule, disabled_file=b.file, disabled_line=b.line,
                          disabled_path=b.path)
                self._err("blocked", b.rule, b.file, b.line, b.path)
                raise Refuse("blocked_rule", b.rule)
            if check_only:
                print("files=%d ok=%d errors=%d" % (len(names), len(bodies), len(self.errors)))
                for n in sorted(self.files):
                    e = self.files[n]
                    if e.get("dropped_keys") is not None:
                        print("%s dropped_keys=%d dropped_paths=%s" % (
                            n, e["dropped_keys"], json.dumps(e.get("dropped_paths", {}), ensure_ascii=False, sort_keys=True)))
                for n in sorted(self.keystats):
                    if n not in self.files:
                        k = self.keystats[n]
                        print("%s dropped_keys=%d dropped_paths=%s" % (
                            n, k["dropped_keys"], json.dumps(k["dropped_paths"], ensure_ascii=False, sort_keys=True)))
                return 1 if self.errors else 0
            if not bodies:
                return self._finish(1 if self.errors else 0, "nothing", st)
            try:
                blocked, remote = self.get_status()
                if blocked:
                    raise Refuse("blocked")
                if dry_run:
                    for n, (body, lines, _) in sorted(bodies.items()):
                        print("%s %s lines=%d size=%d" % (n, self._decide(n, body, lines, remote)[0], lines, len(body)))
                    return 0
                return self._upload(bodies, remote, st)
            finally:
                self._close()
        except Refuse as r:
            if r.code in ("blocked_rule",):
                pass
            return self._finish(2, r.code, st)
        except ConnFail:
            return self._finish(3, "unreachable", st)

    def cfg_ok(self):
        pass

    def _decide(self, n, body, lines, remote):
        e = remote.get(n)
        sha = hashlib.sha256(body).hexdigest()
        if e is None:
            return "new", {"If-None-Match": "*"}, sha
        if e.get("sha256") == sha:
            return "same", None, sha
        if lines >= e.get("lines", 0) and len(body) >= e.get("size", 0):
            return "update", {"If-Match": '"%s"' % e["sha256"]}, sha
        fv = e.get("fv")
        if type(fv) is int and fv < RC.FILTER_V:
            return "shrink_filter", {"If-Match": '"%s"' % e["sha256"]}, sha     # 필터 판이 바뀌어 본문이 줆 (12.6)
        if n in self.cfg["allow_shrink"]:
            return "shrink_allowed", {"If-Match": '"%s"' % e["sha256"]}, sha
        return "shrink_local", None, sha

    def _put(self, n, body, hdrs, sha, shrink):
        h = {"Content-Type": "application/x-ndjson", "Content-Length": str(len(body)),
             "X-Content-SHA256": sha}
        h.update(hdrs)
        path = API + "/files/" + n + ("?allow_shrink=1" if shrink else "")
        code, _, data = self._request("PUT", path, h, body)
        try:
            j = json.loads(data) if data else {}
        except ValueError:
            j = {}
        return code, j if isinstance(j, dict) else {}

    def _upload(self, bodies, remote, st):
        worst = 1 if self.errors else 0
        for n in sorted(bodies):
            body, lines, dropped = bodies[n]
            ent = {"size": len(body), "lines": lines, "dropped": dropped}
            ent.update(self.keystats.get(n, {}))
            self.files[n] = ent
            for attempt in (0, 1):
                action, hdrs, sha = self._decide(n, body, lines, remote)
                ent["sha256"] = sha
                if action == "same":
                    ent["result"] = "unchanged"
                    break
                if action == "shrink_local":
                    ent["result"] = "shrink_local"
                    worst = 1
                    break
                code, j = self._put(n, body, hdrs, sha, action in ("shrink_allowed", "shrink_filter"))
                if code in (200, 201):
                    ent["result"] = "unchanged" if j.get("unchanged") else (
                        "shrink_filter" if action == "shrink_filter" else "uploaded")
                    break
                if code == 412 and attempt == 0:
                    blocked, remote = self.get_status()
                    if blocked:
                        raise Refuse("blocked")
                    continue
                if code == 412:
                    ent["result"] = "precondition_failed"
                elif code == 409:
                    ent["result"] = "shrink"
                elif code in (413, 422) and j.get("error") == "nickname_field":
                    st.update(disabled=True, disabled_rule=str(j.get("rule", "")), disabled_file=n,
                              disabled_line=j.get("line", 0), disabled_path="")
                    self._err("server_blocked", n)
                    raise Refuse("nickname_field")
                elif code == 422 and j.get("rule") == "filter_version":
                    ent["result"] = "filter_version"
                    print("records_sync: 서버와 업로더의 필터 판이 다릅니다 (filter_version)", file=sys.stderr)
                elif code in (413, 422):
                    ent["result"] = str(j.get("error", "rejected")) + (":" + str(j["rule"]) if "rule" in j else "")
                elif code == 423:
                    raise Refuse("blocked")
                elif code in (401, 403):
                    raise Refuse("auth")
                elif code == 507:
                    ent["result"] = "store_full"
                    self._finish(1, "store_full", st)
                    return 1
                else:
                    ent["result"] = "http_%d" % code
                worst = 1
                self.errors.append(n)
                break
        return self._finish(worst, "ok" if not worst else "file_errors", st)

    def _finish(self, code, result, st=None, save=True):
        if save:
            st = st if st is not None else self.load_state()
            st.update(last_run=int(time.time()), last_result=result, last_code=code,
                      name_check="known_names" if self.extra_names else "shape_only",
                      files=self.files if self.files else st.get("files", {}))
            try:
                self.save_state(st)
            except OSError:
                pass
        if code:
            print("records_sync: %s (code %d)" % (result, code), file=sys.stderr)
        self.last_result = result
        return code


def parse_date(s):
    return datetime.date.fromisoformat(s)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--check-only", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--backfill", metavar="FROM")
    g.add_argument("--reset-disabled", action="store_true")
    g.add_argument("--audit", action="store_true")
    ap.add_argument("--from", dest="from_")
    ap.add_argument("--to")
    ap.add_argument("--accept-check", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    base = os.path.abspath(a.base)
    if a.reset_disabled:
        try:
            lock = _lock(base)
        except Refuse:
            print("records_sync: busy", file=sys.stderr)
            return 2
        try:
            p = os.path.join(base, STATE_NAME)
            try:
                with _open(p, encoding="utf-8") as f:
                    st = json.load(f)
            except OSError:
                print("상태 파일이 없습니다")
                return 0
            except ValueError:
                st = {}
            if not isinstance(st, dict):
                st = {}
            for k in [k for k in st if k.startswith("disabled")]:
                del st[k]
            save_state_atomic(p, st)
        finally:
            lock.close()
        print("disabled 해제")
        return 0
    if a.audit:
        try:
            fd = parse_date(a.from_) if a.from_ else None
            td = parse_date(a.to) if a.to else None
        except ValueError:
            print("records_sync: bad date", file=sys.stderr)
            return 2
        return Uploader(base, {}, extra_names=None).audit(fd, td, a.accept_check, a.json)
    try:
        cfg = load_config(base)
    except Refuse as r:
        print("records_sync: %s %s" % (r.code, r.detail), file=sys.stderr)
        return 2
    if "backfill_from" in cfg:
        print("records_sync: backfill_from 설정은 무시합니다 (--backfill 사용)", file=sys.stderr)
    up = Uploader(base, cfg, extra_names=None)      # 독립 실행은 이름 목록이 없다: 모양 검사만 (name_check=shape_only)
    bf = None
    if a.backfill:
        try:
            lo = parse_date(a.backfill)
            hi = parse_date(a.to) if a.to else datetime.date.today()
        except ValueError:
            print("records_sync: bad date", file=sys.stderr)
            return 2
        bf = (lo, hi)
    if a.loop:
        try:
            while True:
                up.files, up.errors, up.idents, up.keystats = {}, [], {}, {}
                up.cycle()
                time.sleep(cfg["interval_sec"])
        except KeyboardInterrupt:
            return 0
    return up.cycle(backfill=bf, dry_run=a.dry_run, check_only=a.check_only)


if __name__ == "__main__":
    sys.exit(main())
