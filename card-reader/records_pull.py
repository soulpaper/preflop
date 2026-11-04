"""리눅스 수신기: 로컬 sharebox 기록 공간(루프백)에서 기록 파일을 받아 다시 검사하고 보관한다.

python3 records_pull.py --config <pull.json> [--recheck]
설정 {"url":"http://127.0.0.1:8737","token":"…","data_dir":"~/.local/share/holdem-records","work_dir":"<작업 폴더 절대 경로>","inbox":"…"}
data_dir/raw/<src>/<file>, hands/<file>(병합본, CARD_READER_DATA 용), quarantine/, pull_state.json, .pull.lock
종료 코드: 0 정상, 1 일부 오류·격리, 2 설정·안전장치 거부, 3 연결 불가.
records_check.py 와 같은 폴더에 있어야 한다 (표준 라이브러리만 사용).
"""
import argparse
import errno
import hashlib
import http.client
import json
import os
import stat
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone

import records_check as RC

API = "/api/spaces/records"
QUARANTINE_KEEP = 7 * 24 * 3600
# 저장 검사는 필터 판 RC.ACCEPT_MIN..RC.FILTER_V 를 받는다 (명세 12.2, 13.2-7)
# 한 번에 받아들이는 개수 상한 (명세 12.8 L3)
MAX_ENTRIES = 5000
MAX_SRCS = 8
MAX_GETS = 400
MAX_RUN_BYTES = 512 << 20
MAX_QUARANTINE = 500
MAX_REPORTED = 5000
MAX_STATUS_BYTES = 8 << 20


class Refuse(Exception):
    pass


class ConnFail(Exception):
    pass


def _chmod(p, mode):
    try:
        os.chmod(p, mode)
    except OSError:
        pass


def _mkdir(p):
    os.makedirs(p, mode=0o700, exist_ok=True)
    _chmod(p, 0o700)


def _real_dir(p):
    """p 가 없으면 만들고, 있으면 링크가 아닌 폴더여야 한다. 아니면 Refuse."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        _mkdir(p)
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refuse("data_dir_link")


def _read_regular(p):
    """O_NOFOLLOW 로 열어 일반 파일일 때만 읽는다. 링크나 일반 파일이 아니면 None."""
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        if e.errno == errno.ELOOP:
            return None
        raise
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        return f.read()


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, data):
    d = os.path.dirname(path)
    _mkdir(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".w-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


def load_config(path):
    try:
        cfg = RC.read_private_json(path)
    except RC.ConfigPerms as e:
        raise Refuse("config_perms: chmod 600 %s ; chmod 700 %s (%s)" % (path, os.path.dirname(os.path.abspath(path)), e))
    except (OSError, ValueError):
        raise Refuse("bad_config")
    if not isinstance(cfg, dict):
        raise Refuse("bad_config")
    u = urllib.parse.urlsplit(str(cfg.get("url", "")))
    if u.scheme != "http" or u.hostname != "127.0.0.1" or u.username or u.password:
        raise Refuse("url_not_loopback")
    try:
        u.port
    except ValueError:
        raise Refuse("bad_config")
    if not isinstance(cfg.get("token"), str) or not cfg["token"]:
        raise Refuse("bad_config")
    if not isinstance(cfg.get("data_dir"), str) or not cfg["data_dir"]:
        raise Refuse("bad_config")
    wd = cfg.get("work_dir")
    if type(wd) is not str or not wd or not os.path.isabs(os.path.expanduser(wd)):
        raise Refuse("bad_config: pull.json 에 work_dir(작업 폴더 절대 경로)를 넣으세요")
    if not os.path.isdir(os.path.realpath(os.path.expanduser(wd))):
        raise Refuse("work_dir_missing")
    return cfg, u


def check_data_dir(data_dir, work_dir):
    real = os.path.realpath(os.path.expanduser(data_dir))
    work = os.path.realpath(os.path.expanduser(work_dir))
    if real == work or real.startswith(work + os.sep) or work.startswith(real + os.sep):
        raise Refuse("data_dir_in_work")
    p = real
    while True:
        if os.path.lexists(os.path.join(p, ".git")):
            raise Refuse("data_dir_in_git")
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return real


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Puller:
    def __init__(self, cfg, url, data_dir):
        self.cfg, self.data = cfg, data_dir
        self.host, self.port = url.hostname, url.port or 80
        self.state_path = os.path.join(data_dir, "pull_state.json")
        self.state = {"files": {}, "reported": {}}
        self.problems = 0
        self.gets = 0
        self.content_gets = 0
        self.run_bytes = 0
        self.stale_304 = 0
        self.conn = None

    # --- 상태/보고 ---
    def load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict) and isinstance(st.get("files"), dict):
                st.setdefault("reported", {})
                self.state = st
        except (OSError, ValueError):
            pass

    def log(self, msg):
        """저널(stderr)에 한 줄. 개수와 이름만, 값과 내용은 쓰지 않는다."""
        sys.stderr.write("records_pull: %s\n" % msg)

    def save_state(self):
        rep = self.state.get("reported", {})
        if len(rep) > MAX_REPORTED:
            drop = sorted(rep, key=lambda k: (rep[k] if isinstance(rep[k], (int, float)) else 0, k))[:len(rep) - MAX_REPORTED]
            for k in drop:
                del rep[k]
            self.log("reported pruned %d (limit %d)" % (len(drop), MAX_REPORTED))
        write_atomic(self.state_path, json.dumps(self.state, sort_keys=True).encode())

    def report(self, event, src="-", file="-", rule="-", path="-", line=0, key=None):
        key = key or "|".join([event, src, file, str(self.state["files"].get(src + "/" + file, {}).get("sha256", ""))])
        if key in self.state["reported"]:
            return
        self.state["reported"][key] = int(time.time())
        inbox = self.cfg.get("inbox")
        if not inbox:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = "- [%s] [홀덤연구소/records_pull] %s %s %s %s %s line=%d\n" % (
            ts, event, src, file, rule or "-", path or "-", line)
        try:
            with open(os.path.expanduser(inbox), "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            sys.stderr.write(text)       # 수신함에 못 쓰면 저널(stderr)로

    # --- 네트워크 ---
    def request(self, path, headers=None, limit=None):
        h = {"Authorization": "Bearer " + self.cfg["token"]}
        h.update(headers or {})
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
                self.conn.request("GET", path, headers=h)
                r = self.conn.getresponse()
                data = r.read((limit or RC.MAX_BODY) + 1)
                self.gets += 1
                return r.status, r, data
            except (OSError, http.client.HTTPException):
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                if attempt:
                    raise ConnFail()

    # --- 격리 ---
    def quarantine(self, src, file, body, v):
        meta = {"at": int(time.time() * 1000), "src": src, "file": file,
                "sha256": hashlib.sha256(body).hexdigest(), "size": len(body), "lines": body.count(b"\n"),
                "line": v.line, "rule": v.rule, "path": v.path}
        name = "%s-%s-%s.json" % (utc_stamp(), src, file)
        write_atomic(os.path.join(self.data, "quarantine", name), json.dumps(meta).encode())
        self.report("quarantine", src, file, v.rule, v.path, v.line, key="q|%s|%s|%s" % (src, file, meta["sha256"]))
        self.problems += 1

    def clean_quarantine(self):
        d = os.path.join(self.data, "quarantine")
        try:
            ents = [e for e in os.scandir(d) if e.is_file(follow_symlinks=False)]
        except OSError:
            return
        now = time.time()
        left = []
        for e in ents:
            try:
                mt = e.stat(follow_symlinks=False).st_mtime
                if now - mt > QUARANTINE_KEEP:
                    os.unlink(e.path)
                else:
                    left.append((mt, e.name, e.path))
            except OSError:
                pass
        left.sort()
        extra = left[:max(0, len(left) - MAX_QUARANTINE)]
        for mt, n, p in extra:        # 오래된 것부터
            try:
                os.unlink(p)
            except OSError:
                pass
        if extra:
            self.log("quarantine pruned %d (limit %d)" % (len(extra), MAX_QUARANTINE))

    # --- raw 읽기 (링크는 건너뜀) ---
    def _raw_srcs(self):
        raw = os.path.join(self.data, "raw")
        try:
            with os.scandir(raw) as it:
                return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def _read_raw(self, src, p, file):
        try:
            data = _read_regular(p)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if data is None:
            self.report("symlink_skipped", src, file, key="symlink|%s|%s" % (src, file))
            self.problems += 1
        return data

    # --- 병합 ---
    def merge(self, file):
        raw = os.path.join(self.data, "raw")
        parts = []
        for s in self._raw_srcs():
            data = self._read_raw(s, os.path.join(raw, s, file), file)
            if data is not None:
                parts.append(data)
        out = os.path.join(self.data, "hands", file)
        if not parts:
            try:
                os.unlink(out)
            except OSError:
                pass
            return
        if len(parts) == 1:
            data = parts[0]
        else:
            seen, lines = set(), []
            for b in parts:
                for ln in b.split(b"\n")[:-1]:
                    if ln not in seen:
                        seen.add(ln)
                        lines.append(ln)
            data = b"".join(ln + b"\n" for ln in lines)
        write_atomic(out, data)

    # --- 한 번 받기 ---
    def pull(self):
        for d in ("raw", "hands", "quarantine"):
            _real_dir(os.path.join(self.data, d))
        self.load_state()
        code, _, data = self.request(API + "/status", limit=MAX_STATUS_BYTES)
        if code == 200 and len(data) > MAX_STATUS_BYTES:
            self.report("bad_status", key="bad_status_size")
            self.save_state()
            return 1
        if code != 200:
            self.report("status_http_%d" % code, key="status|%d" % code)
            self.save_state()
            return 1
        try:
            st = json.loads(data)
            entries = st.get("files", [])
            blocked = bool(st.get("blocked"))
        except (ValueError, AttributeError):
            self.report("bad_status", key="bad_status")
            self.save_state()
            return 1
        if blocked:
            self.report("blocked", key="blocked")
        else:
            self.state["reported"].pop("blocked", None)
        if not isinstance(entries, list):
            self.report("bad_status", key="bad_status")
            self.save_state()
            return 1
        day = datetime.now().strftime("%Y-%m-%d")
        def keyed(e):
            return isinstance(e, dict) and type(e.get("src")) is str and type(e.get("file")) is str
        good = sorted((e for e in entries if keyed(e)), key=lambda e: (e["src"], e["file"]))
        entries = good + [e for e in entries if not keyed(e)]
        if len(entries) > MAX_ENTRIES:
            self.report("too_many_entries", key="tme|" + day)
            self.log("too_many_entries %d (limit %d)" % (len(entries), MAX_ENTRIES))
            entries = entries[:MAX_ENTRIES]
        srcs = sorted({e["src"] for e in entries if keyed(e)})
        if len(srcs) > MAX_SRCS:
            self.report("too_many_srcs", key="tms|" + day)
            self.log("too_many_srcs %d (limit %d)" % (len(srcs), MAX_SRCS))
            keep = set(srcs[:MAX_SRCS])
            entries = [e for e in entries if not keyed(e) or e["src"] in keep]
        changed = set()
        for e in entries:
            if self.content_gets >= MAX_GETS or self.run_bytes >= MAX_RUN_BYTES:
                self.log("run limit reached gets=%d (limit %d) bytes=%d (limit %d); rest next run" % (
                    self.content_gets, MAX_GETS, self.run_bytes, MAX_RUN_BYTES))
                break
            try:
                src, file, sha = e["src"], e["file"], e["sha256"]
                size = e["size"]
            except (KeyError, TypeError):
                self.problems += 1
                continue
            if type(src) is not str or type(file) is not str or not RC.SRC_RE.fullmatch(src) \
                    or RC.file_kind(file) is None or type(sha) is not str or len(sha) != 64:
                self.report("bad_entry", key="bad_entry|%r" % (str(src)[:20],))
                self.problems += 1
                continue
            k = src + "/" + file
            old = self.state["files"].get(k)
            rawp = os.path.join(self.data, "raw", src, file)
            if old and old.get("sha256") == sha and os.path.isfile(rawp):
                continue
            hdr = {"If-None-Match": '"%s"' % old["sha256"]} if old and os.path.isfile(rawp) else {}
            self.content_gets += 1
            code, resp, body = self.request("%s/src/%s/files/%s" % (API, src, file), hdr)
            self.run_bytes += len(body or b"")
            if code == 304 and old:
                # 내용은 옛것이므로 상태를 바꾸지 않는다. 다음 실행에서 sha 가 달라 다시 받는다.
                self.stale_304 += 1
                self.report("stale_304", src, file, key="s304|%s|%s|%s" % (src, file, sha))
                continue
            if code != 200:
                self.problems += 1
                continue
            got = hashlib.sha256(body).hexdigest()
            if got != sha or got != resp.getheader("X-Content-SHA256") or len(body) > RC.MAX_BODY:
                self.report("hash_mismatch", src, file, key="hm|%s|%s|%s" % (src, file, sha))
                self.problems += 1
                continue
            v = RC.check_body(file, body, (RC.ACCEPT_MIN, RC.FILTER_V))
            if not v.ok():
                self.quarantine(src, file, body, v)
                continue
            prev_lines = old.get("lines", 0) if old else 0
            lines = body.count(b"\n")
            _real_dir(os.path.dirname(rawp))
            write_atomic(rawp, body)
            _chmod(os.path.dirname(rawp), 0o700)
            if lines < prev_lines:
                self.report("shrink_applied", src, file, key="shrink|%s|%s|%s" % (src, file, sha))
            if old and e.get("rewrites", 0) > old.get("rewrites", 0):
                self.report("rewrite", src, file, key="rewrite|%s|%s|%s" % (src, file, sha))
            self.state["files"][k] = {"sha256": sha, "size": size, "lines": lines, "rewrites": e.get("rewrites", 0),
                                      "updated": e.get("updated", 0), "pulled_at": int(time.time())}
            changed.add(file)
        for f in sorted(changed):
            self.merge(f)
        self.clean_quarantine()
        self.save_state()
        return 1 if self.problems else 0

    def recheck(self):
        self.load_state()
        raw = os.path.join(self.data, "raw")
        changed = set()
        for s in self._raw_srcs():
            sd = os.path.join(raw, s)
            try:
                with os.scandir(sd) as it:
                    fnames = sorted(e.name for e in it)
            except OSError:
                continue
            for f in fnames:
                if RC.file_kind(f) is None:
                    continue
                body = self._read_raw(s, os.path.join(sd, f), f)
                if body is None:
                    continue
                v = RC.check_body(f, body, (RC.ACCEPT_MIN, RC.FILTER_V))
                if not v.ok():
                    self.quarantine(s, f, body, v)
                    os.unlink(os.path.join(sd, f))
                    self.state["files"].pop(s + "/" + f, None)
                    changed.add(f)
        for f in sorted(changed):
            self.merge(f)
        self.save_state()
        return 1 if self.problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--recheck", action="store_true")
    a = ap.parse_args(argv)
    os.umask(0o077)
    try:
        cfg, url = load_config(a.config)
        data = check_data_dir(cfg["data_dir"], cfg["work_dir"])
    except Refuse as r:
        print("records_pull: %s" % r, file=sys.stderr)
        return 2
    try:
        _real_dir(data)
    except Refuse as r:
        print("records_pull: %s" % r, file=sys.stderr)
        return 2
    import fcntl      # 수신기는 리눅스 전용: Windows 에서 이 모듈을 불러오기만 해도 실패하지 않게 여기서 가져온다
    lockf = open(os.path.join(data, ".pull.lock"), "a+")
    _chmod(os.path.join(data, ".pull.lock"), 0o600)
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    p = Puller(cfg, url, data)
    try:
        return p.recheck() if a.recheck else p.pull()
    except ConnFail:
        print("records_pull: unreachable", file=sys.stderr)
        return 3
    except Refuse as r:
        print("records_pull: %s" % r, file=sys.stderr)
        return 2
    finally:
        lockf.close()


if __name__ == "__main__":
    sys.exit(main())
