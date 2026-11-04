"""seattypes.SeatTypes (좌석별 상대 타입, 메모리 전용) + 앱 연결(팝업 → 좌석, handread 좌석 프로필) 테스트.
실행: python test_seattypes.py"""
import sys
import types as _t

import seattypes as sty

NAMES = {"seat_BL": ("Alpha77", False), "seat_UL": ("Bravo", False), "seat_TC": ("User84", True),
         "seat_UR": ("Charlie", False), "seat_BR": ("Delta", False)}
KEYS = list(NAMES) + ["hero"]
POS = {"seat_BL": "SB", "seat_UL": "BB", "seat_TC": "UTG", "seat_UR": "CO", "seat_BR": "BTN", "hero": "MP"}


def state(occupied=None, in_hand=None, folded=None, hero_folded=False, hero_in_hand=True, to_act=False, preaction=False,
          hero_pos="MP"):
    occupied = set(KEYS) if occupied is None else set(occupied) | {"hero"}
    in_hand = set(KEYS) if in_hand is None else set(in_hand)
    folded = set(folded or ())
    seats = [{"key": k, "slot": k.replace("seat_", "") if k != "hero" else "HERO", "pos": POS[k], "occupied": k in occupied,
              "dealt": k in occupied, "in_hand": k in in_hand and k not in folded and k in occupied,
              "folded": k in folded} for k in KEYS]
    return {"layout_ok": True, "seats": seats, "hero_folded": hero_folded, "hero_in_hand": hero_in_hand,
            "to_act": to_act, "preaction": preaction, "hero_pos": hero_pos, "no_hand": False}


def popup(name, style="LP", vpip=48, pfr=6):
    return {"name": name, "style": style, "style_ko": sty.STYLES_KO.get(style), "vpip": vpip, "pfr": pfr, "ats": 10,
            "threebet": 2, "tab": "토너먼트"}


def test_same_person():
    assert sty.same_person(("User85", True), "User851112222")       # 잘린 이름 접두사
    assert not sty.same_person(("User84", True), "User851112222")
    assert sty.same_person("Charlie", ("Char1ie", False))           # OCR 오타 한 글자
    assert not sty.same_person("Alpha77", "Bravo")
    assert not sty.same_person(("", False), "x")


def test_popup_assign_and_profile():
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    k = st.apply_popup(popup("Charlie", "LAG", 55, 30), NAMES, 2.0)
    assert k == "seat_UR" and st.style("seat_UR") == "LAG" and st.known("seat_UR")
    assert st.apply_popup(popup("User849990001", "TP"), NAMES, 2.5) == "seat_TC", "잘린 이름 좌석에 전체 이름 팝업"
    full = st.full_names(NAMES)
    assert full["seat_TC"] == ("User849990001", False) and full["seat_BL"] == NAMES["seat_BL"]
    assert st.apply_popup(popup("Nobody99"), NAMES, 3.0) is None, "모르는 이름 + 안내 없음 → 저장 안 함"
    hero_names = dict(NAMES, hero=("MyName", False))
    assert st.apply_popup(popup("MyName"), hero_names, 3.0) is None, "내 캐릭터"
    log = st.for_log()
    assert log["seat_UR"] == {"type": "LAG", "type_src": "popup",
                              "type_stats": {"vpip": 55, "pfr": 30, "ats": 10, "threebet": 2}}, log
    assert "seat_BL" not in log
    p = st.profile("seat_UR", None)
    assert p["style"] == "LAG" and p["type_src"] == "popup" and p["source"] == "site" and p["reliable"] and p["vpip"] == 55
    base = {"counts": {"hands": 50, "vpip": 10, "pfr": 5}, "shown": [], "style": None}
    p2 = st.profile("seat_UR", base)
    assert abs(p2["vpip"] - (150 * 55 + 50 * 20) / 200) < 0.1 and p2["hands"] == 50, p2
    assert st.profile("seat_BL", base) is base, "타입 모르는 좌석은 그대로"
    assert st.profile("seat_BL", None) is None


def test_popup_falls_back_to_prompted_seat():
    st = sty.SeatTypes()
    names = {k: v for k, v in NAMES.items() if k != "seat_BR"}   # BTN 이름을 못 읽음
    st.observe(names, state(), 1.0)
    pr = st.prompt(state(hero_folded=True, hero_in_hand=False, in_hand={"seat_BR", "seat_UR"}), {}, 1.0)
    assert pr["seat"] in ("seat_BR", "seat_UR")
    st.prompt_seat = "seat_BR"
    assert st.apply_popup(popup("Zulu"), names, 2.0) == "seat_BR" and st.style("seat_BR") == "LP"
    st.prompt_seat = "seat_UL"   # 안내 좌석의 이름을 읽었는데 팝업 이름이 다르면 → 다른 사람을 눌렀음
    assert st.apply_popup(popup("Yankee"), names, 3.0) is None


def test_seat_reset_rules():
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    for k, nm in NAMES.items():
        st.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 0.5)
    assert all(st.known(k) for k in NAMES)
    # 1) 잘린 이름 · OCR 오타 정도는 같은 사람
    noisy = dict(NAMES, seat_UR=("Char1ie", False), seat_BL=("Alpha7", True))
    for i in range(5):
        assert st.observe(noisy, state(), 1.0 + i) == []
    assert st.known("seat_UR") and st.known("seat_BL")
    # 2) 다른 이름이 한두 번 → 유지, CHANGE_READS 번 연속 → 초기화
    changed = dict(NAMES, seat_UL=("Zebra", False))
    st.observe(changed, state(), 10.0)
    st.observe(NAMES, state(), 11.0)   # 끊김: 연속이 아님
    st.observe(changed, state(), 12.0)
    assert st.known("seat_UL")
    st.observe(changed, state(), 13.0)
    assert st.observe(changed, state(), 14.0) == ["seat_UL"] and not st.known("seat_UL")
    assert st.seats["seat_UL"].name == ("Zebra", False), "새 사람 이름으로 다시 시작"
    # 3) 이름 없는 프레임은 판단 보류
    st.observe({}, state(), 15.0)
    assert st.known("seat_BR")
    # 4) 빈자리 1프레임 → 유지, EMPTY_READS 연속 → 초기화 (다시 앉으면 새 사람)
    occ = set(KEYS) - {"seat_BR"}
    st.observe(NAMES, state(occupied=occ), 40.0)
    st.observe(NAMES, state(), 41.0)
    assert st.known("seat_BR")
    st.observe(NAMES, state(occupied=occ), 42.0)
    assert st.observe(NAMES, state(occupied=occ), 43.0) == ["seat_BR"] and not st.known("seat_BR")
    # 5) 화면이 보정과 어긋난 프레임은 무시
    bad = state(occupied=set())
    bad["layout_ok"] = False
    st.observe({}, bad, 44.0)
    st.observe({}, bad, 45.0)
    assert st.known("seat_UR")


def test_table_move_resets_all():
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    for k, nm in NAMES.items():
        st.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 0.5)
    st.prompt_seat = None
    new = {"seat_BL": ("Echo", False), "seat_UL": ("Foxtrot", False), "seat_TC": ("Golf", False),
           "seat_UR": ("Charlie", False), "seat_BR": ("Delta", False)}
    out = []
    for i in range(sty.CHANGE_READS):
        out = st.observe(new, state(), 100.0 + i)
    assert out == ["*"] and st.table_moves == 1, out
    assert not any(st.known(k) for k in NAMES), "테이블 이동: 이름이 그대로인 좌석(UR, BR)도 초기화"
    st.observe(new, state(), 200.0)
    st.apply_popup(popup("Echo"), new, 201.0)
    st.reset()   # 새 [자동 지정]
    assert not st.seats and st.prompt_seat is None


def test_prompt_idle_only():
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    assert st.prompt(state(), {}, 1.0) is None, "핸드 중(폴드 안 함)에는 묻지 않음"
    assert st.prompt(state(hero_folded=True, to_act=True), {}, 1.0) is None, "내 차례"
    assert st.prompt(state(hero_folded=True, preaction=True), {}, 1.0) is None, "예약 버튼(차례 직전)"
    assert st.prompt(state(hero_folded=True), {}, 1.0) is not None, "폴드 뒤"
    assert st.prompt(state(hero_in_hand=False), {}, 1.0) is not None, "핸드 사이"
    bad = state(hero_folded=True)
    bad["layout_ok"] = False
    assert st.prompt(bad, {}, 1.0) is None


def test_prompt_priority_and_once():
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    folded_hero = dict(hero_folded=True, hero_in_hand=False)
    # 남은 상대 먼저 (BL=SB, TC=UTG 만 남음), 그중 자발적으로 칩을 넣은 사람 먼저
    live = {"seat_TC": {"preflop": ["open"]}, "seat_BL": {"preflop": ["blind", "call"]}}
    s = state(in_hand={"seat_BL", "seat_TC"}, **folded_hero)
    c = st.candidates(s, live)
    assert [k for k, _, _ in c][:2] in (["seat_TC", "seat_BL"], ["seat_BL", "seat_TC"]) and len(c) == 5
    live2 = {"seat_TC": {"preflop": ["open"]}, "seat_BL": {"preflop": ["blind", "fold"]}}
    assert st.candidates(s, live2)[0][0] == "seat_TC", "루즈/액티브 먼저"
    # 둘 다 수동적이면 나보다 뒤에 행동하는 사람 먼저 (나 = MP: CO·BTN 이 뒤, SB·BB·UTG 는 앞)
    s2 = state(in_hand=set(), **folded_hero)
    order = [k for k, _, _ in st.candidates(s2, {})]
    assert set(order[:2]) == {"seat_UR", "seat_BR"}, order
    # 한 번에 한 좌석, 큰 글 안내 문구
    pr = st.prompt(s, live2, 10.0)
    assert pr["seat"] == "seat_TC" and pr["text"] == "UTG 자리 캐릭터를 눌러 주세요" and "타입 확인" in pr["sub"], pr
    assert "위 가운데" in pr["sub"]
    # 유지: 우선순위가 바뀌어도 PROMPT_HOLD 동안은 같은 좌석
    live3 = {"seat_BL": {"preflop": ["blind", "raise"]}}
    s3 = state(in_hand={"seat_BL"}, **folded_hero)
    assert st.prompt(s3, live3, 11.0)["seat"] == "seat_TC"
    assert st.prompt(s3, live3, 10.0 + sty.PROMPT_HOLD + 0.1)["seat"] == "seat_BL"
    # 읽은 좌석은 다시 묻지 않음
    st.apply_popup(popup("Alpha77"), NAMES, 30.0)
    pr = st.prompt(s3, live3, 31.0)
    assert pr["seat"] != "seat_BL" and not st.known(pr["seat"])
    for k, nm in NAMES.items():
        st.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 32.0)
    assert st.prompt(s3, live3, 40.0) is None, "모두 읽으면 안내 없음"
    # 좌석이 초기화되면 다시 물음
    st.observe(NAMES, state(occupied=set(KEYS) - {"seat_UL"}), 50.0)
    st.observe(NAMES, state(occupied=set(KEYS) - {"seat_UL"}), 51.0)
    st.observe(dict(NAMES, seat_UL=("Kilo", False)), state(**folded_hero), 52.0)
    assert st.prompt(state(**folded_hero), {}, 53.0)["seat"] == "seat_UL"


def test_prompt_without_position():
    st = sty.SeatTypes()
    s = state(hero_folded=True)
    for x in s["seats"]:
        x["pos"] = None
    s["hero_pos"] = None
    pr = st.prompt(s, {}, 1.0)
    assert pr["text"].endswith("자리 캐릭터를 눌러 주세요") and pr["pos"] is None, pr


# --- 앱 연결 (reader.App 메서드를 가짜 self 로) ------------------------------------------------------
def test_app_make_reads_passes_style_and_popup():
    import reader
    seen = {}

    def estimate(seats, board, players, dead=(), profiles=None, stack_bb=100, ante=False, looseness=0.0):
        seen["seats"], seen["profiles"] = seats, profiles
        return {k: {"range": {}, "top_hands": [], "width_pct": 20} for k in seats}
    hr = _t.SimpleNamespace(estimate=estimate, describe=lambda r: ["x"])
    fake = _t.SimpleNamespace(handread=hr, cfg={"inputs": {"ante": ""}}, looseness=lambda: 0.0, aggression=reader.App.aggression)
    live = {"seat_UR": {"pos": "CO", "preflop": ["open"], "flop": [], "turn": [], "river": []},
            "seat_BL": {"pos": "SB", "preflop": ["call"], "flop": [], "turn": [], "river": []}}
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    st.apply_popup(popup("Charlie", "LP"), NAMES, 1.0)
    profiles = {k: p for k in live for p in [st.profile(k, None)] if p is not None}
    reader.App.make_reads(fake, {"pos_players": 6}, ["Ah", "Kd"], [], live, profiles, ["seat_UR", "seat_BL"], ("k",))
    seats = seen["seats"]
    assert seats["seat_UR"]["profile"]["style"] == "LP" and seats["seat_UR"]["profile"]["type_src"] == "popup", seats
    assert "profile" not in seats["seat_BL"], "타입 모르는 좌석은 프로필 없음"
    assert "profile" not in live["seat_UR"], "원본 live 는 그대로"
    assert seen["profiles"]["seat_UR"]["style"] == "LP"
    # read_popup: 좌석에 저장하고 알림 (이름은 화면 알림에만, 파일 아님)
    info = popup("Bravo", "TAG")
    app = _t.SimpleNamespace(popup=_t.SimpleNamespace(update=lambda img: info), seat_types=st, cfg={},
                             track={"names": NAMES}, table_state=state(), pending_toast=None)
    reader.App.read_popup(app, None)
    assert st.style("seat_UL") == "TAG" and app.pending_toast.startswith("BB 자리 타입: 타이트-어그레시브"), app.pending_toast
    assert app.cfg["game_tab"] == "토너먼트"
    info2 = popup("Unknown123", "LAG")
    app.popup = _t.SimpleNamespace(update=lambda img: info2)
    reader.App.read_popup(app, None)
    assert "저장하지 않았습니다" in app.pending_toast


def test_synthetic_run_writes_no_names():
    # 앱과 같은 연결로 (SeatTypes → Tracker(types, full_names) → Store.observe(names_for) + DecisionLog) 한 테이블을 돌린 뒤
    # 쓴 파일 어디에도 닉네임이 없고, 좌석 타입·pid 는 남는다
    import json
    import shutil
    import tempfile
    from pathlib import Path
    import decisionlog as dl
    import handlog
    import profiles
    from test_handlog import Table, finish
    tmp = Path(tempfile.mkdtemp())
    try:
        store = profiles.Store(hands_dir=tmp / "hands")
        log = dl.DecisionLog(tmp)
        tr = handlog.Tracker("syn")
        tr.on_hand = lambda r: (store.observe(r, names=tr.names_for(r["id"])), log.link_hand(r))
        tps = sty.SeatTypes()
        names = {"seat_BL": ("Alpha77", False), "seat_UL": ("Bravo", False), "seat_TC": ("User84", True),
                 "seat_UR": ("Charlie", False), "seat_BR": ("Delta", False)}

        class T(Table):
            def step(self, **kw):
                snap = self.snap()
                tps.observe(self.names, snap, self.t)
                orig = self.tr.update

                def upd(state, hero, board, nm, t, shown=None):
                    return orig(state, hero, board, tps.full_names(nm), t, shown=shown, types=tps.for_log())
                self.tr.update = upd
                try:
                    return Table.step(self, **kw)
                finally:
                    self.tr.update = orig
        tb = T(tr, names=names)
        tb.step()
        tps.apply_popup(popup("User849990001", "LAG"), names, tb.t)
        tps.apply_popup(popup("Charlie", "TP"), names, tb.t)
        tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
        prof = {k: p for k in names for p in [tps.profile(k, store.get_for_table(names).get(k))] if p}
        log.record({"hero": ["Ah", "Kd"], "board": [], "street": "preflop", "call": 2.5, "pot": 1.5},
                   {"action": "콜"}, state=tr, profiles=prof, t=tb.t)
        tb.step(fold=["MP", "CO", "BTN", "SB"])
        tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
        tb.step(board=["Ah", "7c", "2d"], collect=True, bubble={})
        tb.step(fold=["UTG"], bubble={"UTG": "fold"})
        rec = finish(tb)
        text = "".join(f.read_text(encoding="utf-8") for f in tmp.rglob("*") if f.is_file())
        for nm in ("Alpha77", "Bravo", "User84", "Charlie", "Delta", "User849990001"):
            assert nm not in text, nm
        assert sorted(p.name for p in tmp.rglob("*") if p.is_file() and not p.name.endswith(".jsonl")) == []
        hands = [json.loads(x) for x in next((tmp / "hands").glob("[0-9]*.jsonl")).read_text(encoding="utf-8").splitlines()]
        tc = hands[0]["seats"]["seat_TC"]
        # 팝업 전 프레임에 잘린 이름으로 읽힌 핸드라 pid 는 없음 (보수적으로), 타입은 남음
        assert tc["type"] == "LAG" and tc["type_src"] == "popup" and tc["pid"] is None, tc
        assert hands[0]["seats"]["seat_UR"]["pid"].startswith("p_")
        assert hands[0]["seats"]["seat_UR"]["type"] == "TP" and hands[0]["seats"]["seat_BL"]["type"] is None
        decs = [json.loads(x) for x in next((tmp / "hands").glob("decisions_*.jsonl")).read_text(encoding="utf-8").splitlines()]
        d0 = decs[0]["seats"]
        assert d0["seat_TC"]["type"] == "LAG" and d0["seat_TC"]["profile"]["style"] == "LAG", d0["seat_TC"]
        assert rec["seats"]["seat_BL"]["pid"] == d0["seat_BL"]["pid"], "같은 세션: 핸드·결정 기록의 pid 가 같음"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
