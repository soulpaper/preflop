"""이미 쌓인 기록에서 닉네임 지우기 (2026-11-05 결정: 닉네임을 디스크에 남기지 않음).

    python scrub_names.py --base <CardReader.exe 가 있는 폴더> --dry-run     # 무엇이 바뀔지 개수만 (파일은 그대로)
    python scrub_names.py --base <CardReader.exe 가 있는 폴더>               # 실제로 고쳐 씀

하는 일
  1. <base>/hands/*.jsonl (핸드 기록 YYYY-MM-DD.jsonl + 결정 기록 decisions_*.jsonl) 의 좌석마다
     닉네임 칸(name / truncated / placeholder / linked_name)을 지우고, 대신 세션 가명 pid 를 넣는다.
       pid = handlog.make_pid(세션 소금, 닉네임) = 'p_' + HMAC-SHA256 16진 앞 8글자 — 앱이 새로 쓰는 기록과 같은 모양.
       세션 소금은 이 실행 동안 '기록의 세션 id 하나마다' 새 난수 (메모리에만, 어디에도 쓰지 않고 버림) → 같은 세션 안에서는
       같은 사람이 같은 pid (핸드 기록과 결정 기록도 같은 세션이면 같은 pid), 세션이 다르면 연결되지 않고 닉네임을 되살릴 수 없다.
       잘린 닉네임('User84…' / truncated / placeholder)이나 닉네임이 없던 좌석은 pid = None. 이미 pid 가 있으면 그대로.
     좌석 칸 밖에서 값 전체가 그 파일들의 닉네임과 똑같은 문자열이 있으면 None 으로 바꾸고 개수를 센다 (보통 0).
     세션 id: 핸드 기록은 'session', 결정 기록은 hand_id 의 마지막 '-' 앞 (없으면 날짜).
     JSON 이 아닌 줄은 그대로 둔다 (개수만 보고).
  2. <base>/profiles.db (+ -wal / -shm / -journal) 를 통째로 지운다. 이 DB 의 표는 모두 닉네임이 열쇠
     (players 관측 통계, shown 공개 카드, fingerprints 닉네임 글자 지문, site_stats 팝업 통계) 이고, observed 는 그 통계를
     두 번 넣지 않으려는 핸드 id 목록일 뿐이라 남길 것이 없다. 새 앱은 이 파일을 열지도 만들지도 않는다 (통계는 메모리).
  3. 알림만: <base>/hands 의 다른 파일(read_scores.json 등)과 <base>/debug.log 에 닉네임 문자열이 있는지 개수를 센다
     (고치지 않음). recordings/ 의 녹화 이미지는 화면 그대로라 닉네임이 보인다 — 폴더 수만 알리고 손대지 않는다.

안전
  - CardReader.exe 가 실행 중이면 아무것도 하지 않고 끝낸다 (기록 파일을 동시에 쓰지 않게).
  - 파일마다 같은 폴더의 임시 파일에 다 쓴 뒤 os.replace 로 한 번에 바꾼다 (중간에 끊겨도 원본 아니면 새 파일).
  - 원본 백업은 만들지 않는다 (백업에 닉네임이 남으므로). 먼저 --dry-run 으로 개수를 확인할 것.
  - 출력에 닉네임을 찍지 않는다 (개수만).
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import handlog

NAME_FIELDS = ("name", "truncated", "placeholder", "linked_name")
APP_EXE = "CardReader.exe"
DB_FILES = ("profiles.db", "profiles.db-wal", "profiles.db-shm", "profiles.db-journal")
MIN_NAME_LEN = 4            # 이보다 짧은 '닉네임'(OCR 찌꺼기 'AKo' 등)은 좌석 밖 문자열 비교에 쓰지 않음 (카드 코드·포지션과 겹침)


def _checkable(name):
    """좌석 밖 비교에 쓸 만한 닉네임: MIN_NAME_LEN 이상이고 숫자/기호만은 아님 (핸드 id·금액과 겹침)."""
    return len(name) >= MIN_NAME_LEN and any(ch.isalpha() for ch in name)


def running_apps():
    """실행 중인 CardReader.exe 프로세스 수 (확인할 수 없으면 None)."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {APP_EXE}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    return sum(1 for line in out.splitlines() if line.lower().startswith(f'"{APP_EXE.lower()}"'))


def _session_of(rec):
    if rec.get("session"):
        return str(rec["session"])
    hid = rec.get("hand_id")
    if not hid and rec.get("type") is None and "seats" in rec:
        hid = rec.get("id")
    if hid:
        return str(hid).rsplit("-", 1)[0]
    return "day:" + str(rec.get("day") or "?")


class Scrubber:
    def __init__(self):
        self.salts = {}             # 세션 id -> 난수 (메모리에만)
        self.names = set()          # 이번 실행에서 본 닉네임 (메모리에만, 출력 안 함)
        self.c = {"files": 0, "files_changed": 0, "lines": 0, "records_changed": 0, "bad_lines": 0,
                  "name_fields_removed": 0, "seats_with_name": 0, "pid_assigned": 0, "pid_none": 0,
                  "stray_values": 0, "sessions": 0}

    def salt(self, session):
        if session not in self.salts:
            self.salts[session] = os.urandom(16)
            self.c["sessions"] += 1
        return self.salts[session]

    def collect(self, rec):
        for seat in (rec.get("seats") or {}).values() if isinstance(rec.get("seats"), dict) else ():
            if isinstance(seat, dict):
                for f in ("name", "linked_name"):
                    v = seat.get(f)
                    if isinstance(v, str) and v.strip():
                        self.names.add(v)

    def scrub_record(self, rec):
        """기록 하나 (제자리에서) → 바뀌었으면 True."""
        changed = False
        seats = rec.get("seats")
        if isinstance(seats, dict):
            for key, seat in seats.items():
                if not isinstance(seat, dict):
                    continue
                present = [f for f in NAME_FIELDS if f in seat]
                if not present:
                    continue
                name = seat.get("name")
                trunc = bool(seat.get("truncated")) or bool(seat.get("placeholder"))
                hero = key == "hero" or seat.get("hero") is True
                if isinstance(name, str) and name.strip():
                    self.c["seats_with_name"] += 1
                if not hero and "pid" not in seat:
                    pid = None
                    if isinstance(name, str) and name.strip() and not trunc:
                        pid = handlog.make_pid(self.salt(_session_of(rec)), name)
                    seat["pid"] = pid
                    self.c["pid_assigned" if pid else "pid_none"] += 1
                for f in present:
                    seat.pop(f, None)
                self.c["name_fields_removed"] += len(present)
                changed = True
        if self.names:
            changed = self._stray(rec) or changed
        return changed

    def _stray(self, obj):
        """좌석 칸 밖에서 값 전체가 닉네임인 문자열 → None."""
        changed = False
        items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
        for k, v in list(items):
            if isinstance(v, str):
                if v in self.names and _checkable(v):
                    obj[k] = None
                    self.c["stray_values"] += 1
                    changed = True
            elif isinstance(v, (dict, list)):
                changed = self._stray(v) or changed
        return changed

    @staticmethod
    def _parse(line):
        try:
            rec = json.loads(line)
            return rec if isinstance(rec, dict) else None
        except ValueError:
            return None

    def gather(self, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                rec = self._parse(line)
                if rec is not None:
                    self.collect(rec)

    def scrub_file(self, path, dry_run):
        self.c["files"] += 1
        out, changed_any = [], False
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            raw = f.read()
        for line in raw.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if not body.strip():
                out.append(line)
                continue
            self.c["lines"] += 1
            rec = self._parse(body)
            if rec is None:
                self.c["bad_lines"] += 1
                out.append(line)
                continue
            if self.scrub_record(rec):
                self.c["records_changed"] += 1
                changed_any = True
                out.append(json.dumps(rec, ensure_ascii=False) + line[len(body):])
            else:
                out.append(line)
        if changed_any:
            self.c["files_changed"] += 1
            if not dry_run:
                atomic_write(path, "".join(out))
        return changed_any


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.scrub.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def db_summary(path):
    """profiles.db 표별 줄 수 (읽기 전용). 못 열면 {'error': ...}."""
    try:
        db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        finally:
            db.close()
    except Exception as exc:
        return {"error": type(exc).__name__}


def count_mentions(path, names):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(text.count(n) for n in names if _checkable(n))


def run(base, dry_run=False, check_running=True, out=print):
    """→ 보고 dict. 실행 중이면 {'refused': ...}."""
    base = Path(base)
    if check_running:
        n = running_apps()
        if n is None:
            out(f"{APP_EXE} 실행 여부를 확인할 수 없어 멈춥니다 (tasklist 실패).")
            return {"refused": "unknown"}
        if n:
            out(f"{APP_EXE} 가 실행 중입니다 ({n}개). 앱을 끄고 다시 실행하세요. 아무것도 바꾸지 않았습니다.")
            return {"refused": "running"}
    hands = base / "hands"
    files = sorted(hands.glob("*.jsonl")) if hands.is_dir() else []
    sc = Scrubber()
    for p in files:
        sc.gather(p)
    for p in files:
        sc.scrub_file(p, dry_run)
    report = {"base": str(base), "dry_run": dry_run, "jsonl": dict(sc.c), "names_seen": len(sc.names)}
    # profiles.db
    db = {}
    for fn in DB_FILES:
        p = base / fn
        if p.exists():
            db[fn] = {"bytes": p.stat().st_size}
            if fn == "profiles.db":
                db[fn]["tables"] = db_summary(p)
            if not dry_run:
                p.unlink()
                db[fn]["deleted"] = True
    report["profiles_db"] = db
    # 알림만
    others = {}
    if hands.is_dir():
        for p in sorted(hands.iterdir()):
            if p.is_file() and p.suffix != ".jsonl" and not p.name.startswith("."):
                others[p.name] = count_mentions(p, sc.names)
    log = base / "debug.log"
    if log.exists():
        others["debug.log"] = count_mentions(log, sc.names)
    report["other_files_name_mentions"] = others
    rec_dir = base / "recordings"
    report["recordings_folders"] = sum(1 for p in rec_dir.iterdir() if p.is_dir()) if rec_dir.is_dir() else 0
    sc.salts.clear()
    sc.names.clear()
    _print(report, out)
    return report


def _print(r, out):
    c = r["jsonl"]
    out(("[미리 보기 — 파일은 그대로] " if r["dry_run"] else "") + f"폴더: {r['base']}")
    out(f"  JSONL {c['files']}개 중 {c['files_changed']}개 {'바뀔' if r['dry_run'] else '바꿈'}: 줄 {c['lines']}, "
        f"기록 {c['records_changed']}개, 닉네임 칸 {c['name_fields_removed']}개 삭제 (닉네임 있던 좌석 {c['seats_with_name']})")
    out(f"  pid 붙임 {c['pid_assigned']} · pid 없음(잘린/빈 이름) {c['pid_none']} · 세션 {c['sessions']}개 · "
        f"좌석 밖 닉네임 값 {c['stray_values']} · JSON 아닌 줄 {c['bad_lines']} (그대로 둠) · 서로 다른 닉네임 {r['names_seen']}명")
    if r["profiles_db"]:
        for fn, d in r["profiles_db"].items():
            out(f"  {fn}: {d['bytes']} bytes {d.get('tables', '')} → {'지움' if d.get('deleted') else '지울 예정'}")
    else:
        out("  profiles.db 없음")
    for fn, n in r["other_files_name_mentions"].items():
        out(f"  (알림) {fn}: 닉네임 문자열 {n}회" + (" — 손으로 확인/삭제 필요" if n else ""))
    if r["recordings_folders"]:
        out(f"  (알림) recordings/ 녹화 {r['recordings_folders']}개: 화면 이미지라 닉네임이 보임 (손대지 않음)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=str(Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent),
                    help="CardReader.exe 가 있는 폴더 (hands/, profiles.db 가 있는 곳). 기본: 이 파일의 폴더")
    ap.add_argument("--dry-run", action="store_true", help="개수만 보고, 아무것도 바꾸지 않음")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    r = run(a.base, dry_run=a.dry_run)
    return 2 if r.get("refused") else 0


if __name__ == "__main__":
    raise SystemExit(main())
