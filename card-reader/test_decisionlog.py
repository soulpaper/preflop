"""decisionlog 단위 테스트 (진짜 handlog.Tracker 로 만든 핸드 사용). 실행: python test_decisionlog.py"""
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import decisionlog as dl
import handlog
from test_handlog import Table, finish


def tmp():
    return Path(tempfile.mkdtemp(prefix="declog_"))


def make_ctx(**kw):
    ctx = {"hero": ["Ah", "Kd"], "board": ["Ah", "7c", "2d"], "street": "flop", "players": 6, "opponents_in_hand": 2,
           "hero_pos": "BB", "villain_pos": None, "situation": None, "stack_bb": 50.0, "my_stack": 47.5, "pot": 8.0,
           "pot_total": 11.0, "call": 3.0, "to_act": True, "can_check": False, "equity": 0.62, "equity_basis": "range",
           "outs": 0, "hand_name": "원페어", "hero_ip": False, "aggressor": False, "flop_line": None, "looseness": 0.1,
           "facing_allin": False, "ante": False}
    ctx.update(kw)
    return ctx


def make_advice(**kw):
    a = {"action": "콜", "size_bb": None, "size_text": "", "reason": "에쿼티 충분",
         "detail": ["팟오즈 25%", "GTO 참고(비슷한 플랍 A72r): 콜 70%"],
         "plan": [], "confidence": "medium", "color_key": "call"}
    a.update(kw)
    return a


def play_to_flop_bet(names=None, tag="s9"):
    """UTG 오픈, BTN·BB(나) 콜, 플랍에서 UTG 벳 3, BTN 폴드 -> 내 차례."""
    tb = Table(handlog.Tracker(tag), names=names)
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    tb.step(fold=["MP", "CO"])
    tb.step(bets={"BTN": 2.5}, bubble={"BTN": "call"})
    tb.step(fold=["SB"])
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
    tb.step(board=["Ah", "7c", "2d"], collect=True, bubble={})
    tb.step(bets={"UTG": 3.0}, bubble={"UTG": "raise"})
    tb.step(fold=["BTN"], bubble={"BTN": "fold"})
    return tb


def finish_call_to_showdown(tb):
    tb.step(bets={"BB": 3.0}, bubble={"BB": "call"})                       # 내가 콜
    tb.step(board=["Ah", "7c", "2d", "9h"], collect=True, bubble={})
    tb.step()
    tb.step(board=["Ah", "7c", "2d", "9h", "2c"])
    tb.step()
    return finish(tb)


def lines(d):
    out = []
    for f in sorted((Path(d) / "hands").glob("decisions_*.jsonl")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


# --- 텍스처 ---------------------------------------------------------------------------------------
def test_texture():
    t = dl.board_texture(["Ks", "7d", "2c"])
    assert t["suit"] == "rainbow" and not t["paired"] and t["high"] == "K" and t["label"] == "dry", t
    assert t["connected"] == 0 and t["straight_windows"] == 0 and t["broadway"] == 1
    t = dl.board_texture(["9h", "8h", "6d"])
    assert t["suit"] == "two_tone" and t["label"] == "wet" and t["straight_windows"] >= 2, t
    t = dl.board_texture(["7h", "8h", "9h"])
    assert t["suit"] == "monotone" and t["wet"] >= 0.85, t
    t = dl.board_texture(["As", "Ks", "Qs"])
    assert t["suit"] == "monotone" and t["wet"] >= 0.6 and t["broadway"] == 3, t
    t = dl.board_texture(["7h", "7d", "2c"])
    assert t["paired"] and not t["trips"] and t["bucket"] == "dry+paired", t
    assert dl.board_texture(["7h", "7d", "7c"])["trips"]
    t = dl.board_texture(["Ac", "2d", "7c"])
    assert t["suit"] == "two_tone" and t["high"] == "A" and t["label"] == "dry", t
    t = dl.board_texture(["Ah", "Kh", "7h", "2h"])
    assert t["suit"] == "monotone" and t["n"] == 4, t
    t = dl.board_texture(["Ah", "Kh", "7h", "2c"])
    assert t["suit"] == "three_flush" and t["flush_max"] == 3, t
    t = dl.board_texture(["Ah", "Kh", "7h", "2h", "9d"])
    assert t["n"] == 5 and t["suit"] == "four_flush", t
    assert dl.board_texture([]) == {} and dl.board_texture(["Ah"]) == {}
    assert dl.board_texture(["Ah", "xx", "Kd", "Qs"])["n"] == 3


# --- 기록 -----------------------------------------------------------------------------------------
def test_record_fields():
    d = tmp()
    try:
        tb = play_to_flop_bet()
        log = dl.DecisionLog(d)
        profs = {"seat_BL": {"vpip": 28.0, "pfr": 20.0, "af": 2.1, "fold_to_bet": 40.0, "hands": 55, "source": "observed",
                             "shown": [["As", "Ks"]], "counts": {"hands": 55}}}
        reads = {"order": ["seat_BL"], "lines": {"seat_BL": ["UTG: 예상 상위 15%", "보드 기준: 탑페어 30%"]},
                 "width": {"seat_BL": 15.2}, "ranges": {"seat_BL": {"AhKh": 0.5}}}
        did = log.record(make_ctx(players_left=40, my_rank=12, stage="bubble"), make_advice(), reads=reads, state=tb.tr,
                         profiles=profs, t=1000.0)
        assert did and did.startswith(tb.tr.hand.id), did
        ds = lines(d)
        assert len(ds) == 1 and ds[0]["type"] == "decision"
        r = ds[0]
        assert r["hand_id"] == tb.tr.hand.id and r["street"] == "flop" and r["hero"] == ["Ah", "Kd"]
        assert r["board"] == ["Ah", "7c", "2d"] and r["tex"]["bucket"] == "dry" and r["tex"]["high"] == "A"
        assert r["call"] == 3.0 and r["pot"] == 8.0 and r["call_pct_pot"] == 0.375
        assert r["spr"] == round(47.5 / 11.0, 2) and r["opponents"] == 2 and r["hero_pos"] == "BB"
        assert r["equity"] == 0.62 and r["equity_basis"] == "range" and r["looseness"] == 0.1
        assert r["bettor"]["pos"] == "UTG" and r["bettor"]["act"] == "bet" and r["bettor"]["pct"] == 0.38, r["bettor"]
        assert r["n_aggr"] == 1 and r["hero_acts_before"] == 0
        s = r["seats"]["seat_BL"]
        assert s["pos"] == "UTG" and s["name"] == "P_seat_BL"
        assert s["acts"]["preflop"][0]["act"] == "raise" and s["acts"]["preflop"][0]["to"] == 2.5
        assert s["acts"]["flop"] == [{"act": "bet", "to": 3.0, "pct": 0.38}], s["acts"]["flop"]
        assert s["profile"] == {"vpip": 28.0, "pfr": 20.0, "af": 2.1, "fold_to_bet": 40.0, "hands": 55,
                                "source": "observed"}
        assert r["seats"]["seat_UR"]["folded"] == "flop" and r["seats"]["hero"]["hero"] is True
        assert "name" not in r["seats"]["hero"]
        assert r["reads"]["seat_BL"]["width"] == 15.2 and "예상 상위 15%" in r["reads"]["seat_BL"]["line"]
        a = r["advice"]
        assert a["action"] == "call" and a["action_ko"] == "콜" and a["confidence"] == "medium"
        assert a["gto_mode"] == "secondary" and "GTO 참고" in a["gto_line"]
        assert r["tourney"] == {"players_left": 40, "my_rank": 12, "stage": "bubble"}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_record_without_state():
    d = tmp()
    try:
        log = dl.DecisionLog(d)
        did = log.record(make_ctx(board=[], street="preflop", pot=1.5, pot_total=None, call=2.5),
                         make_advice(action="폴드"), hand_id="h1", t=5.0)
        r = lines(d)[0]
        assert did and r["hand_id"] == "h1" and "tex" not in r or r["tex"] == {}
        assert "seats" not in r and r["advice"]["action"] == "fold"
        assert log.record("junk", None) is None and log.errors == 0
        assert log.record({}, None, t=6.0)   # 빈 ctx 도 예외 없이
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_truncated_names():
    d = tmp()
    try:
        names = {"seat_BL": ("User84…", True), "seat_UL": ("홍길동", False)}
        tb = play_to_flop_bet(names=names, tag="s10")
        log = dl.DecisionLog(d)
        log.record(make_ctx(), make_advice(), state=tb.tr, t=1.0)
        text = "".join(f.read_text(encoding="utf-8") for f in (Path(d) / "hands").glob("decisions_*.jsonl"))
        assert "User84" not in text, "잘린 닉네임이 파일에 쓰임"
        r = lines(d)[0]
        s = r["seats"]["seat_BL"]
        assert s.get("name") is None and s["placeholder"] == "seat-local:seat_BL"
        assert r["seats"]["seat_UL"]["name"] == "홍길동" and "홍길동" in text
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dedupe_and_revisions():
    d = tmp()
    try:
        tb = play_to_flop_bet(tag="s11")
        log = dl.DecisionLog(d)
        a = log.record(make_ctx(equity=0.60), make_advice(), state=tb.tr, t=1.0)
        b = log.record(make_ctx(equity=0.63), make_advice(), state=tb.tr, t=2.0)    # 에쿼티만 달라짐: 같은 결정
        assert a == b and len(lines(d)) == 1
        c = log.record(make_ctx(), make_advice(action="폴드"), state=tb.tr, t=3.0)  # 추천이 바뀜: 같은 차례의 새 개정
        assert c != a and len(lines(d)) == 2
        rs = lines(d)
        assert rs[1]["turn"] == rs[0]["turn"] and rs[1]["rev"] == 1 and rs[0]["rev"] == 0
        ft = dl.final_turns(dl.load(d))
        assert len(ft) == 1 and ft[0]["id"] == c
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- 연결 -----------------------------------------------------------------------------------------
def test_link_hand():
    d = tmp()
    try:
        tb = play_to_flop_bet(tag="s12")
        log = dl.DecisionLog(d)
        log.record(make_ctx(), make_advice(), state=tb.tr, t=time.time())
        rec = finish_call_to_showdown(tb)
        assert rec["confidence"] == "ok", rec["issues"]
        rec["seats"]["seat_BL"]["shown"] = ["Qs", "Qd"]     # 상대 패가 공개됐다고 가정 (AA 22 대 QQ 22)
        assert log.link_hand(rec) == 1
        assert log.link_hand(rec) == 0, "같은 핸드를 두 번 연결하지 않음"
        res = dl.load(d)[0]["resolution"]
        ha = res["hero_actual"]
        assert ha["action"] == "call" and ha["to"] == 3.0 and ha["added"] == 3.0, ha
        assert res["followed"] is True and res["srcs"] == ["tracker"]
        oc = res["outcome"]
        assert oc["final_board"] == ["Ah", "7c", "2d", "9h", "2c"] and oc["showdown"] is True
        assert oc["hero_won"] is True and oc["shown"] == {"UTG": ["Qs", "Qd"]}, oc
        assert oc["hero_delta_bb"] == 8.5 and oc["delta_src"] == "showdown_approx", oc   # 상대들이 낸 합계 (UTG 5.5 + BTN 2.5 + SB 0.5)
        assert res["bettor_continued"] is False and res["bettor_next"] == ["check"], res
        assert any(x["street"] == "turn" and x["pos"] == "UTG" for x in res["later"])
        # 패를 모르면 승패 미상
        d2 = tmp()
        try:
            tb2 = play_to_flop_bet(tag="s13")
            log2 = dl.DecisionLog(d2)
            log2.record(make_ctx(), make_advice(action="레이즈", size_bb=9.0), state=tb2.tr, t=time.time())
            rec2 = finish_call_to_showdown(tb2)
            log2.link_hand(rec2)
            r2 = dl.load(d2)[0]["resolution"]
            assert r2["followed"] is False and "hero_won" not in r2["outcome"], r2
        finally:
            shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_link_fold_and_manual():
    d = tmp()
    try:
        tb = play_to_flop_bet(tag="s14")
        log = dl.DecisionLog(d)
        did = log.record(make_ctx(), make_advice(action="폴드"), state=tb.tr, t=time.time())
        tb.step(fold=["BB"])
        rec = finish(tb)
        assert log.link_hand(rec) == 1
        res = dl.load(d)[0]["resolution"]
        assert res["hero_actual"]["action"] == "fold" and res["followed"] is True, res
        assert res["outcome"]["hero_won"] is False and res["outcome"]["hero_delta_bb"] == -2.5, res["outcome"]
        assert res["responses"] == [] and res["outcome"]["hero_folded"] == "flop"
        # 수동 확정이 나중 줄이라 이김, id 모르는 결정도 예외 없이
        assert log.hero_action(did, "call", 3.0)
        r = dl.load(d)[0]["resolution"]
        assert r["hero_actual"]["action"] == "call" and r["followed"] is False and r["srcs"] == ["tracker", "manual"]
        assert log.hero_action("nope", "check") and log.errors == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_link_check_then_face_bet():
    """내 체크가 추적기에 안 보이다가 스트리트 끝에 추정으로 들어가는 경우도 결정마다 맞는 행동에 연결."""
    d = tmp()
    try:
        tb = Table(handlog.Tracker("s15"))
        tb.step()
        tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
        tb.step(fold=["MP", "CO", "BTN", "SB"])
        tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
        tb.step(board=["Ah", "7c", "2d"], collect=True, bubble={})
        log = dl.DecisionLog(d)
        d1 = log.record(make_ctx(call=0.0, can_check=True, pot=5.5, pot_total=5.5, opponents_in_hand=1),
                        make_advice(action="체크"), state=tb.tr, t=time.time())
        tb.step(bets={"UTG": 3.0}, bubble={"UTG": "raise"})
        d2 = log.record(make_ctx(call=3.0, pot=5.5, opponents_in_hand=1), make_advice(action="콜"), state=tb.tr,
                        t=time.time() + 1)
        assert d1 != d2
        tb.step(bets={"BB": 3.0}, bubble={"BB": "call"})
        tb.step(board=["Ah", "7c", "2d", "9h"], collect=True, bubble={})
        tb.step()
        rec = finish(tb)
        assert log.link_hand(rec) == 2
        recs = {r["id"]: r for r in dl.load(d)}
        assert recs[d1]["resolution"]["hero_actual"]["action"] == "check", recs[d1]["resolution"]
        assert recs[d2]["resolution"]["hero_actual"]["action"] == "call", recs[d2]["resolution"]
        assert recs[d1]["resolution"]["followed"] is True and recs[d2]["resolution"]["followed"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_link_after_restart():
    """앱을 다시 켜서 메모리에 결정이 없어도 파일에서 찾아 연결."""
    d = tmp()
    try:
        tb = play_to_flop_bet(tag="s16")
        dl.DecisionLog(d).record(make_ctx(), make_advice(), state=tb.tr, t=time.time())
        rec = finish_call_to_showdown(tb)
        assert dl.DecisionLog(d).link_hand(rec) == 1
        assert dl.load(d)[0]["resolution"]["hero_actual"]["action"] == "call"
        assert dl.DecisionLog(d).link_hand({"id": "other", "actions": {}, "seats": {}}) == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- 안전 -----------------------------------------------------------------------------------------
def test_crash_safety():
    d = tmp()
    try:
        log = dl.DecisionLog(d)
        log.record(make_ctx(), make_advice(), hand_id="h1", t=time.time())
        f = next((Path(d) / "hands").glob("decisions_*.jsonl"))
        with open(f, "a", encoding="utf-8") as fh:           # 쓰다 죽음: 줄바꿈 없는 반쪽 줄
            fh.write('{"type":"decision","id":"h2/1","hand_id":"h2","str')
        assert len(dl.load(d)) == 1
        log2 = dl.DecisionLog(d)                              # 재시작 후에도 이어 쓸 수 있고 앞 데이터는 그대로
        assert log2.record(make_ctx(call=4.0), make_advice(), hand_id="h3", t=time.time())
        recs = dl.load(d)
        assert [r["hand_id"] for r in recs] == ["h1", "h3"], recs
        # 쓸 수 없는 위치: 예외 없이 None, 오류 콜백
        errs = []
        bad = dl.DecisionLog(f, on_error=errs.append)          # hands 가 파일 아래라 폴더를 못 만듦
        assert bad.record(make_ctx(), make_advice(), hand_id="x") is None and bad.errors == 1 and errs
        assert bad.link_hand({"id": "x"}) == 0 and bad.load() == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_threads():
    d = tmp()
    try:
        log = dl.DecisionLog(d)
        ids, errs = [], []

        def work(n):
            try:
                for i in range(50):
                    ids.append(log.record(make_ctx(pot_total=10.0 + i), make_advice(), hand_id=f"h{n}", t=time.time()))
                    if i % 10 == 9:
                        log.link_hand({"id": f"h{n}", "actions": {}, "seats": {"hero": {"hero": True}}, "start": 0,
                                       "end": 0})
            except Exception as exc:
                errs.append(exc)
        th = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        [t.start() for t in th]
        [t.join() for t in th]
        assert not errs and log.errors == 0, (errs, log.last_error)
        assert len(set(ids)) == 200 and None not in ids
        ls = lines(d)
        assert sum(1 for x in ls if x["type"] == "decision") == 200
        assert len(dl.load(d)) == 200
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_timing():
    d = tmp()
    try:
        tb = play_to_flop_bet(tag="s17")
        log = dl.DecisionLog(d)
        reads = {"order": ["seat_BL"], "lines": {"seat_BL": ["a", "b"]}, "width": {"seat_BL": 12.0}}
        log.record(make_ctx(), make_advice(), reads=reads, state=tb.tr, t=1.0)   # 워밍업
        t0 = time.perf_counter()
        n = 200
        for i in range(n):
            log.record(make_ctx(pot_total=11.0 + i), make_advice(), reads=reads, state=tb.tr, t=2.0 + i)
        ms = (time.perf_counter() - t0) * 1000 / n
        print(f"    record: {ms:.2f} ms")
        assert ms < 2.0, ms
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_summarize():
    d = tmp()
    try:
        tb = play_to_flop_bet(tag="s18")
        log = dl.DecisionLog(d)
        log.record(make_ctx(), make_advice(), state=tb.tr, t=time.time())
        rec = finish_call_to_showdown(tb)
        rec["seats"]["seat_BL"]["shown"] = ["Qs", "Qd"]
        log.link_hand(rec)
        s = dl.summarize(dl.load(d, days=1))
        assert s["n_turns"] == 1 and s["n_resolved"] == 1 and s["follow_rate"] == 1.0, s
        assert s["follow_by_advice"]["call"]["actual"] == {"call": 1}
        assert s["avg_equity_by_advice"]["call"] == {"n": 1, "equity": 0.62}
        k = "flop|<=1/3|dry"
        f = s["facing"][k]
        assert f["n"] == 1 and f["bettor_continue_rate"] == 0.0 and f["win_rate_known"] == 1.0, s["facing"]
        assert s["called_realized"] == {"n": 1, "avg_equity": 0.62, "known": 1, "win_rate": 1.0}
        assert dl.summarize([])["n_turns"] == 0
        assert dl.load(d, days=["1999-01-01"]) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as exc:
            import traceback
            failed += 1
            print("FAIL", fn.__name__, repr(exc))
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
