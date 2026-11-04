"""scrub_names.py 테스트 (가짜 기록 폴더). 실행: python test_scrub_names.py"""
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import scrub_names as sn

PID = re.compile(r"p_[0-9a-f]{8}")
NAMES = ("홍길동", "Alpha77", "User84", "Charlie")


def hand(hid, session, seats):
    return {"id": hid, "session": session, "confidence": "ok", "start": 1.0, "board": [], "seats": seats,
            "actions": {"preflop": [{"seat": "seat_BL", "act": "raise", "to": 2.5}]}, "notes": []}


def make_base():
    base = Path(tempfile.mkdtemp())
    h = base / "hands"
    h.mkdir()
    recs = [
        hand("S1-0001", "S1", {"seat_BL": {"name": "홍길동", "truncated": False, "pos": "UTG", "stats": {"hands": 1}},
                               "seat_TC": {"name": None, "truncated": True, "placeholder": "seat-local:seat_TC", "pos": "CO"},
                               "seat_UR": {"name": "Alpha77", "truncated": False, "pos": "BTN"},
                               "hero": {"name": None, "truncated": False, "hero": True, "pos": "BB"}}),
        hand("S1-0002", "S1", {"seat_UL": {"name": "홍길동", "truncated": False, "pos": "SB"},   # 자리를 옮김
                               "seat_UR": {"name": "Alpha77", "truncated": False, "pos": "BTN"}}),
        hand("S2-0001", "S2", {"seat_BL": {"name": "홍길동", "truncated": False, "pos": "UTG"}}),
    ]
    lines = [json.dumps(r, ensure_ascii=False) for r in recs] + ["{not json", ""]
    (h / "2026-11-01.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    decs = [{"type": "decision", "id": "S1-0001/1", "hand_id": "S1-0001", "day": "2026-11-01", "hand_name": "원페어",
             "seats": {"seat_BL": {"pos": "UTG", "name": "홍길동", "acts": {}, "profile": {"vpip": 30}},
                       "seat_TC": {"pos": "CO", "name": None, "placeholder": "seat-local:seat_TC"},
                       "hero": {"pos": "BB", "hero": True}},
             "bettor": {"seat": "seat_BL", "pos": "UTG", "note": "Charlie"}},
            {"type": "resolution", "id": "S1-0001/1", "hero_actual": {"action": "call"}},
            {"type": "decision", "id": "x/1", "hand_id": None, "day": "2026-11-02", "seats": {"seat_R": {"name": "Charlie"}}}]
    (h / "decisions_2026-11-01.jsonl").write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in decs),
                                                  encoding="utf-8")
    (h / "read_scores.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    (base / "debug.log").write_text("error near Alpha77\n", encoding="utf-8")
    db = sqlite3.connect(str(base / "profiles.db"))
    db.executescript("CREATE TABLE players (name TEXT PRIMARY KEY, hands INTEGER); INSERT INTO players VALUES ('홍길동', 3);"
                     "CREATE TABLE observed (hand_id TEXT PRIMARY KEY, t REAL);")
    db.commit()
    db.close()
    return base


def snapshot(base):
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}


def quiet(*a):
    pass


def test_refuses_while_running():
    base = make_base()
    try:
        before = snapshot(base)
        real = sn.running_apps
        sn.running_apps = lambda: 1
        try:
            r = sn.run(base, out=quiet)
        finally:
            sn.running_apps = real
        assert r == {"refused": "running"} and snapshot(base) == before
        sn.running_apps = lambda: None
        try:
            assert sn.run(base, out=quiet) == {"refused": "unknown"} and snapshot(base) == before
        finally:
            sn.running_apps = real
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_dry_run_changes_nothing():
    base = make_base()
    try:
        before = snapshot(base)
        r = sn.run(base, dry_run=True, check_running=False, out=quiet)
        assert snapshot(base) == before, "미리 보기는 파일을 안 바꿈"
        c = r["jsonl"]
        assert c["files"] == 2 and c["files_changed"] == 2 and c["bad_lines"] == 1, c
        assert c["pid_assigned"] == 7 and c["pid_none"] == 2 and c["sessions"] == 3, c   # S1, S2, day:2026-11-02
        assert c["stray_values"] == 1, c     # bettor.note == 'Charlie'
        assert r["profiles_db"]["profiles.db"]["tables"] == {"players": 1, "observed": 0}
        assert not r["profiles_db"]["profiles.db"].get("deleted")
        assert r["other_files_name_mentions"] == {"read_scores.json": 0, "debug.log": 1}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_real_run():
    base = make_base()
    try:
        out = []
        r = sn.run(base, check_running=False, out=out.append)
        report = "\n".join(out)
        assert not any(n in report for n in NAMES), "보고에 닉네임을 찍지 않음"
        h = base / "hands"
        text = (h / "2026-11-01.jsonl").read_text(encoding="utf-8")
        dtext = (h / "decisions_2026-11-01.jsonl").read_text(encoding="utf-8")
        for n in NAMES:
            assert n not in text and n not in dtext, n
        assert not (base / "profiles.db").exists() and r["profiles_db"]["profiles.db"]["deleted"]
        assert not list(base.rglob(".*.scrub.tmp")), "임시 파일 남지 않음"
        lines = text.split("\n")
        assert len(lines) == 6 and lines[3] == "{not json" and lines[4] == "", "줄 수·JSON 아닌 줄 그대로"
        r1, r2, r3 = (json.loads(x) for x in lines[:3])
        for rec in (r1, r2, r3):
            for k, s in rec["seats"].items():
                assert not set(s) & set(sn.NAME_FIELDS), s
        p = r1["seats"]["seat_BL"]["pid"]
        assert PID.fullmatch(p) and r1["seats"]["seat_UR"]["pid"] != p
        assert r1["seats"]["seat_TC"]["pid"] is None, "잘린 이름은 pid 없음"
        assert "pid" not in r1["seats"]["hero"]
        assert r2["seats"]["seat_UL"]["pid"] == p, "같은 세션: 자리를 옮겨도 같은 pid"
        assert r2["seats"]["seat_UR"]["pid"] == r1["seats"]["seat_UR"]["pid"]
        assert r3["seats"]["seat_BL"]["pid"] != p, "다른 세션: 연결 안 됨"
        assert r1["seats"]["seat_BL"]["stats"] == {"hands": 1} and r1["actions"]["preflop"][0]["to"] == 2.5, "나머지 그대로"
        decs = [json.loads(x) for x in dtext.splitlines()]
        assert decs[0]["seats"]["seat_BL"]["pid"] == p, "핸드 기록과 결정 기록이 같은 세션이면 같은 pid"
        assert decs[0]["seats"]["seat_TC"]["pid"] is None and decs[0]["hand_name"] == "원페어"
        assert decs[0]["bettor"]["note"] is None and decs[1] == {"type": "resolution", "id": "S1-0001/1",
                                                                  "hero_actual": {"action": "call"}}
        assert PID.fullmatch(decs[2]["seats"]["seat_R"]["pid"])
        # 두 번 돌려도 그대로 (이미 pid 있음)
        before = snapshot(base)
        r2nd = sn.run(base, check_running=False, out=quiet)
        assert r2nd["jsonl"]["files_changed"] == 0 and snapshot(base) == before
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_missing_folders():
    base = Path(tempfile.mkdtemp())
    try:
        r = sn.run(base, check_running=False, out=quiet)
        assert r["jsonl"]["files"] == 0 and r["profiles_db"] == {} and r["recordings_folders"] == 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as exc:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL", fn.__name__, repr(exc))
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
