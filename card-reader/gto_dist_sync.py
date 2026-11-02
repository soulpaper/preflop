"""서버 PC 타이머 작업: gto_lib 을 읽기 전용으로 스캔해 로컬 sharebox 의 GTO 공간에 올리고 게시한다.
러너(build_gto_lib.py)와는 프로세스, 잠금, 쓰기를 공유하지 않는다. lib 폴더는 읽기만 한다.

    python3 gto_dist_sync.py --config ~/.config/gto-dist/sync.json
종료 코드: 0 성공(변경 없음 포함), 1 실패 (다음 타이머에 재시도).
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gto_dist_common as C  # noqa: E402


def log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


class Fail(Exception):
    pass


DEFAULT_LOCAL_PORT = 8737


def parse_loopback(url):
    """업로더는 루프백 HTTP 만 쓴다. 그 밖의 주소(원격, https)는 거부."""
    u = urllib.parse.urlsplit(url)
    host = u.hostname
    if u.scheme != "http" or not host:
        raise ValueError("loopback http only")
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = host == "localhost"
    if not ok:
        raise ValueError("loopback http only")
    return host, u.port or DEFAULT_LOCAL_PORT


class Api:
    def __init__(self, url, token):
        self.host, self.port = parse_loopback(url)
        self.token = token
        self.conn = None

    def req(self, method, path, body=None, headers=None):
        h = {"Authorization": "Bearer " + self.token}
        h.update(headers or {})
        if body is not None:
            h["Content-Length"] = str(len(body))
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
                self.conn.request(method, path, body=body, headers=h)
                r = self.conn.getresponse()
                data = r.read()
                if r.getheader("Connection", "").lower() == "close":
                    self.conn.close()
                    self.conn = None
                return r.status, data
            except (OSError, ConnectionError, http.client.HTTPException):
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                if attempt:
                    raise
        raise Fail("unreachable")

    def json(self, method, path, obj):
        return self.req(method, path, json.dumps(obj).encode(), {"Content-Type": "application/json"})


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def scan(lib_dir, cache):
    """허용 경로 파일만 {rel: {sha256,size}}. cache: {rel: [size, mtime_ns, sha]} (갱신됨)."""
    out, newcache = {}, {}
    for dp, dn, fn in os.walk(lib_dir):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, lib_dir).replace(os.sep, "/")
            if not C.valid_path(rel) or os.path.islink(full):
                continue
            try:
                st = os.stat(full)
                c = cache.get(rel)
                if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
                    sha = c[2]
                else:
                    sha = hash_file(full)
            except OSError:
                continue
            newcache[rel] = [st.st_size, st.st_mtime_ns, sha]
            out[rel] = {"sha256": sha, "size": st.st_size}
    return out, newcache


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        pass


def get_manifest(api):
    st, data = api.req("GET", C.PATH_MANIFEST)
    if st == 404:
        return 0, {}
    if st != 200:
        raise Fail("manifest_get_%d" % st)
    m = json.loads(data)
    return m["version"], m["files"]


def put_files(api, lib_dir, files, stats):
    """서버에 없는 객체만 올린다. 그사이 러너가 파일을 교체하면 files 를 새 해시로 갱신한다."""
    shas = sorted({e["sha256"] for e in files.values()})
    st, data = api.json("POST", C.PATH_MISSING, {"sha256": shas})
    if st != 200:
        raise Fail("missing_%d" % st)
    missing = set(json.loads(data)["missing"])
    for rel in sorted(files):
        ent = files[rel]
        if ent["sha256"] not in missing:
            continue
        try:
            with open(os.path.join(lib_dir, *rel.split("/")), "rb") as f:
                body = f.read(C.MAX_OBJECT + 1)
        except OSError:
            raise Fail("read_failed " + rel)
        if len(body) > C.MAX_OBJECT:
            raise Fail("too_large " + rel)
        sha = C.sha256_hex(body)
        if sha != ent["sha256"] or len(body) != ent["size"]:
            files[rel] = ent = {"sha256": sha, "size": len(body)}   # 그사이 러너가 교체함
        try:
            C.validate_object(body)       # 형식이 맞지 않는 파일은 프로세스 밖으로 나가지 않는다
        except C.ValidationError as e:
            raise Fail("invalid_object %s %s" % (rel, e.code))
        st, data = api.req("PUT", C.PATH_OBJECTS + sha, body, {"Content-Type": "application/octet-stream"})
        if st not in (200, 201):
            raise Fail("put_%d %s %s" % (st, rel, data[:80].decode("utf-8", "replace")))
        stats["put"] += 1
        missing.discard(sha)


def run(cfg, stats=None):
    stats = stats if stats is not None else {}
    stats.update(put=0, scanned=0)
    api = Api(cfg["url"], cfg["token"])
    lib_dir = cfg["lib_dir"]
    base, cur = get_manifest(api)
    cache_path = cfg.get("cache")
    cache = load_cache(cache_path) if cache_path else {}
    files, cache = scan(lib_dir, cache)
    stats["scanned"] = len(files)
    if cache_path:
        save_cache(cache_path, cache)
    if not files:
        raise Fail("scan_empty")
    if files == cur:
        # 게시할 것은 없지만 서버에 객체가 모두 있는지는 확인한다(요청 1회). 사라진 객체가 있으면 다시 올린다.
        put_files(api, lib_dir, files, stats)
        if files != cur:
            raise Fail("changed_during_sync")      # 그사이 러너가 교체함: 다음 타이머가 게시
        log("변경 없음 (버전 %d, 파일 %d, 재업로드 %d)" % (base, len(files), stats["put"]))
        return 0
    for attempt in (0, 1):
        put_files(api, lib_dir, files, stats)
        st, data = api.json("POST", C.PATH_MANIFEST, {"base_version": base, "files": files, "allow_shrink": False})
        # 48시간 넘은 미참조 객체는 missing 조회 뒤 서버 정리와 겹치면 사라질 수 있다.
        # 이 경우 한 번만 missing 부터 다시 한다(그래도 실패하면 exit 1, 다음 타이머가 복구).
        if st == 422 and b"missing_object" in data and attempt == 0:
            log("게시 중 객체가 정리됨(missing_object): 다시 올림")
            base, _ = get_manifest(api)
            continue
        break
    if st != 200:
        raise Fail("publish_%d %s" % (st, data[:80].decode("utf-8", "replace")))
    out = json.loads(data)
    stats["version"], stats["changed"] = out["version"], out["changed"]
    log("게시: 버전 %d, 파일 %d, 업로드 %d" % (out["version"], len(files), stats["put"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    a = ap.parse_args(argv)
    os.umask(0o077)
    try:
        with open(os.path.expanduser(a.config), encoding="utf-8") as f:
            cfg = json.load(f)
        return run(cfg)
    except Fail as e:
        log("실패: %s" % e)
    except Exception as e:
        log("실패: %s" % type(e).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
