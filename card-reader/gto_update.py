"""card-reader 의 GTO 데이터 백그라운드 갱신기.

BASE/gto_update.json 이 있고 enabled 일 때만 동작한다. 파일 단위 원자 교체만 하고 로컬 파일을 삭제하지 않는다.
공개 함수: start(base, on_done, on_error) -> Updater | None
"""
import hashlib
import http.client
import json
import os
import shutil
import sys
import threading
import time
import uuid

try:
    import fcntl
except ImportError:      # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

import gto_dist_common as C

CONFIG_NAME = "gto_update.json"
STATE_NAME = "gto_update_state.json"
TMP_NAME = ".update-tmp"
CHUNK = 256 * 1024
_replace = os.replace          # 테스트에서 PermissionError 주입용
_sleep = time.sleep
MIN_TOAST = 50                  # 설치 개수가 이 이상일 때만 on_done 호출
RETRY_SEC = 300


class Stopped(Exception):
    pass


class CycleFail(Exception):
    def __init__(self, status, detail=""):
        super().__init__(status)
        self.status, self.detail = status, detail


def server_marker():
    """서버 PC 표식 (업로더 설정). 있으면 러너의 원본 폴더를 덮어쓰지 않도록 시작을 거부한다."""
    return os.path.join(os.path.expanduser("~"), ".config", "gto-dist", "sync.json")


def load_config(base):
    try:
        with open(os.path.join(base, CONFIG_NAME), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    urls = cfg.get("urls")
    if not isinstance(urls, list) or not urls:
        return None
    for u in urls:
        if not isinstance(u, str) or not u.startswith("https://"):
            return None
        try:
            C.parse_https(u)
        except ValueError:
            return None
    if not isinstance(cfg.get("token"), str) or not cfg["token"]:
        return None
    fp = cfg.get("cert_sha256")
    if not isinstance(fp, str) or not C.SHA_RE.fullmatch(fp.lower()):
        return None
    return cfg


def start(base, on_done=None, on_error=None):
    """조건이 안 맞으면 None. 그 밖에는 데몬 스레드를 띄우고 즉시 반환."""
    cfg = load_config(base)
    if cfg is None:
        return None
    if os.path.exists(server_marker()):      # 서버 PC: 러너의 원본 폴더를 덮어쓰면 안 된다
        return None
    u = Updater(base, cfg, on_done, on_error)
    u.start()
    return u


class Updater:
    def __init__(self, base, cfg, on_done=None, on_error=None, first_delay=15):
        self.base, self.cfg = base, cfg
        self.on_done = on_done or (lambda m: None)
        self.on_error = on_error or (lambda *a: None)
        self.first_delay = first_delay
        self.stop_ev = threading.Event()
        self.thread = None
        self.etag = None
        self.last_status = None
        self.conn = None
        self.last_url = None
        self.rate = int(cfg.get("max_bytes_per_sec") or 0)
        self.interval = max(60, int(cfg.get("check_interval_sec") or 1800))

    # --- 수명 ------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self.run, name="gto-update", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        try:
            if sys.platform == "win32":
                import ctypes
                k = ctypes.windll.kernel32
                k.SetThreadPriority(k.GetCurrentThread(), -1)
        except Exception:
            pass
        delay = self.first_delay
        while not self.stop_ev.wait(delay):
            status = self.cycle()
            delay = RETRY_SEC if status in ("partial", "stale_manifest") else self.interval

    def cycle(self):
        try:
            status = self._cycle()
        except Stopped:
            status = "stopped"
        except CycleFail as e:
            status = e.status
            if status not in ("unreachable", "locked", "stale_manifest", "no_manifest"):
                self._err(status)
        except Exception as e:
            status = "error"
            self._err(type(e).__name__)
        finally:
            self._close()
        self.last_status = status
        return status

    def _err(self, what):
        try:
            self.on_error("gto_update", what)
        except Exception:
            pass

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    # --- 경로/상태 ----------------------------------------------------------------
    def target_dir(self):
        t = self.cfg.get("lib_dir")
        if not t:
            import solverlib
            t = solverlib.lib_dirs()[0]
        return os.path.abspath(os.path.expanduser(t))

    def load_state(self, target):
        try:
            with open(os.path.join(self.base, STATE_NAME), encoding="utf-8") as f:
                st = json.load(f)
            if st.get("target") == target and isinstance(st.get("files"), dict):
                return st
        except (OSError, ValueError, AttributeError):
            pass
        return {"applied_version": 0, "target": target, "files": {}}

    def save_state(self, st):
        p = os.path.join(self.base, STATE_NAME)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, p)

    # --- 네트워크 -----------------------------------------------------------------
    def _connect(self):
        """마지막 성공 URL, 그다음 설정 순서로 시도해 첫 연결에서 멈춘다. 지문은 연결 직후(요청 전) 확인된다."""
        urls = list(self.cfg["urls"])
        if self.last_url in urls:
            urls.remove(self.last_url)
            urls.insert(0, self.last_url)
        pin_failed = False
        for url in urls:
            host, port = C.parse_https(url)
            conn = C.PinnedConnection(host, port, self.cfg["cert_sha256"], timeout=30, connect_timeout=5)
            try:
                conn.connect()
            except Exception as e:
                if "fingerprint" in str(e):
                    pin_failed = True
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            self.conn = conn
            self.last_url = url
            return
        raise CycleFail("pin_failed" if pin_failed else "unreachable")      # 성공이 없을 때만 판정

    def _request(self, path, headers=None):
        """GET. 서버가 유휴 연결을 끊었으면(RemoteDisconnected 등) 1회 재연결해서 다시 보낸다."""
        h = {"Authorization": "Bearer " + self.cfg["token"]}
        h.update(headers or {})
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("GET", path, headers=h)
                return self.conn.getresponse()
            except CycleFail:
                raise
            except (OSError, http.client.HTTPException):
                self._close()
                if attempt:
                    raise CycleFail("unreachable")

    def _throttle(self, nbytes, t0):
        if self.rate > 0:
            wait = nbytes / self.rate - (time.monotonic() - t0)
            if wait > 0:
                if self.stop_ev.wait(wait):
                    raise Stopped()

    def fetch_manifest(self, use_etag):
        self._connect()
        h = {"If-None-Match": self.etag} if (use_etag and self.etag) else {}
        r = self._request(C.PATH_MANIFEST, h)
        if r.status == 304:
            r.read()
            return None
        if r.status == 404:
            r.read()
            raise CycleFail("no_manifest")
        if r.status == 401 or r.status == 403:
            r.read()
            raise CycleFail("unauthorized")
        if r.status != 200:
            r.read()
            raise CycleFail("manifest_http_%d" % r.status)
        cl = r.getheader("Content-Length")
        if cl is None or not cl.isdigit() or int(cl) > C.MAX_MANIFEST:
            raise CycleFail("manifest_too_large")
        body = r.read(int(cl))
        return r.getheader("ETag"), body

    def download(self, sha, size, part):
        """객체를 part 로 받아 해시/크기를 검증. 실패 시 CycleFail."""
        r = self._request(C.PATH_OBJECTS + sha)
        if r.status == 404:        # 서버 정리와 겹친 정상 상황: 오류 없이 나중에 다시
            r.read()
            raise CycleFail("stale_manifest")
        if r.status != 200:
            r.read()
            raise CycleFail("download_http_%d" % r.status)
        h = hashlib.sha256()
        got = 0
        t0 = time.monotonic()
        with open(part, "wb") as f:
            while True:
                if self.stop_ev.is_set():
                    raise Stopped()
                b = r.read(CHUNK)
                if not b:
                    break
                got += len(b)
                if got > size:
                    self._close()
                    raise CycleFail("hash_mismatch", "oversize")
                h.update(b)
                f.write(b)
                self._throttle(got, t0)
            f.flush()
            os.fsync(f.fileno())
        if got != size or h.hexdigest() != sha:
            raise CycleFail("hash_mismatch")

    # --- 잠금 ---------------------------------------------------------------------
    def _lock(self, tmpdir):
        f = open(os.path.join(tmpdir, "lock"), "a+")
        try:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            raise CycleFail("locked")
        return f

    # --- 한 사이클 ----------------------------------------------------------------
    def _cycle(self):
        target = self.target_dir()
        tmpdir = os.path.join(target, TMP_NAME)
        os.makedirs(tmpdir, exist_ok=True)
        lockf = self._lock(tmpdir)
        try:
            return self._locked_cycle(target, tmpdir)
        finally:
            lockf.close()

    def _locked_cycle(self, target, tmpdir):
        state = self.load_state(target)
        first = self.last_status is None
        res = self.fetch_manifest(use_etag=not first)
        if res is None:
            return "unchanged"
        etag, body = res
        try:
            m = json.loads(body)
        except ValueError:
            raise CycleFail("bad_manifest")
        if not isinstance(m, dict) or m.get("format") != C.FORMAT:
            return "format_unknown"
        ver, files = m.get("version"), m.get("files")
        if type(ver) is not int:
            raise CycleFail("bad_manifest")
        try:
            C.validate_manifest_files(files)
        except C.ValidationError as e:
            raise CycleFail("bad_manifest", e.code)
        rt = os.path.realpath(target)
        for rel in files:
            real = os.path.realpath(os.path.join(target, *rel.split("/")))
            if not real.startswith(rt + os.sep):
                raise CycleFail("bad_manifest", "escape")
        if ver < state.get("applied_version", 0):
            return "server_older"

        # 필요한 파일 계산
        need = {}
        sfiles = state["files"]
        for rel, ent in files.items():
            p = os.path.join(target, *rel.split("/"))
            try:
                st = os.stat(p)
            except OSError:
                need[rel] = ent
                continue
            if st.st_size != ent["size"]:
                need[rel] = ent
                continue
            s = sfiles.get(rel)
            if s and s.get("size") == st.st_size and s.get("mtime_ns") == st.st_mtime_ns:
                sha = s.get("sha256")
            else:
                sha = self._hash_file(p)
                _sleep(0.01)
            if sha != ent["sha256"]:
                need[rel] = ent
            else:
                sfiles[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}

        if not need:
            state["applied_version"] = ver
            self.save_state(state)
            self.etag = etag
            self._cleanup(tmpdir)
            return "unchanged"

        total = sum({e["sha256"]: e["size"] for e in need.values()}.values())
        if shutil.disk_usage(target).free < total * 1.1 + 100 * 1024 * 1024:
            raise CycleFail("no_space")

        # 해시 계산이 길어지면 서버가 유휴 연결을 끊었을 수 있으므로 새로 연결한다
        self._close()
        self._connect()

        # 스테이징
        for n in os.listdir(tmpdir):
            if n.endswith(".part"):
                try:
                    os.unlink(os.path.join(tmpdir, n))
                except OSError:
                    pass
        staged = {}
        for sha, size in {e["sha256"]: e["size"] for e in need.values()}.items():
            ok = os.path.join(tmpdir, sha + ".ok")
            if os.path.exists(ok):
                if os.path.getsize(ok) == size and self._hash_file(ok) == sha:
                    staged[sha] = ok
                    continue
                os.unlink(ok)
            part = os.path.join(tmpdir, sha + ".part")
            for attempt in (0, 1):
                try:
                    self.download(sha, size, part)
                    break
                except CycleFail as e:
                    try:
                        os.unlink(part)
                    except OSError:
                        pass
                    if e.status != "hash_mismatch" or attempt:
                        raise
                    self._close()
                    self._connect()
            os.replace(part, ok)
            staged[sha] = ok

        # 설치: 턴 파일 먼저, 루트 파일 나중
        uses = {}
        for ent in need.values():
            uses[ent["sha256"]] = uses.get(ent["sha256"], 0) + 1
        installed, held = 0, 0
        try:
            for rel in sorted(need, key=lambda r: (not r.startswith("turn/"), r)):
                ent = need[rel]
                sha = ent["sha256"]
                dest = os.path.join(target, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                uses[sha] -= 1
                if uses[sha] == 0:
                    src = staged[sha]
                else:
                    src = os.path.join(tmpdir, uuid.uuid4().hex + ".inst")
                    shutil.copyfile(staged[sha], src)
                if self._install(src, dest):
                    st = os.stat(dest)
                    sfiles[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}
                    installed += 1
                else:
                    held += 1
                    if uses[sha] != 0 and os.path.exists(src):
                        os.unlink(src)
        except BaseException:
            try:
                self.save_state(state)       # 설치한 파일은 온전하다. 버전은 올리지 않는다
            except Exception:
                pass
            raise
        if held:
            self.save_state(state)
            return "partial"
        state["applied_version"] = ver
        self.save_state(state)
        self.etag = etag
        self._cleanup(tmpdir)
        if installed >= MIN_TOAST:
            try:
                self.on_done("GTO 데이터 갱신: %d개" % installed)
            except Exception:
                pass
        return "updated"

    def _install(self, src, dest):
        for i in range(8):
            try:
                _replace(src, dest)
                return True
            except PermissionError:
                if self._wait(0.25):
                    raise Stopped()
        return False

    def _wait(self, sec):
        if _sleep is not time.sleep:       # 테스트에서 대체
            _sleep(sec)
            return False
        return self.stop_ev.wait(sec)

    def _hash_file(self, p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()

    def _cleanup(self, tmpdir):
        try:
            for n in os.listdir(tmpdir):
                if n != "lock":
                    try:
                        os.unlink(os.path.join(tmpdir, n))
                    except OSError:
                        pass
        except OSError:
            pass
