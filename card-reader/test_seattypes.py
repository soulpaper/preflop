"""seattypes.SeatTypes (좌석별 상대 타입, 메모리 전용) + 앱 연결(팝업 → 좌석, handread 좌석 프로필) 테스트.
실행: python test_seattypes.py"""
import sys
import types as _t

import seattypes as sty

NAMES = {"seat_BL": ("Alpha77", False), "seat_UL": ("Bravo", False), "seat_TC": ("User0", True),
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
    assert sty.same_person(("User2", True), "User200000002")       # 잘린 이름 접두사
    assert not sty.same_person(("User0", True), "User200000002")
    assert sty.same_person("Charlie", ("Char1ie", False))           # OCR 오타 한 글자
    assert not sty.same_person("Alpha77", "Bravo")
    assert not sty.same_person(("", False), "x")


def test_popup_assign_and_profile():
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    k = st.apply_popup(popup("Charlie", "LAG", 55, 30), NAMES, 2.0)
    assert k == "seat_UR" and st.style("seat_UR") == "LAG" and st.known("seat_UR")
    assert st.apply_popup(popup("User000000001", "TP"), NAMES, 2.5) == "seat_TC", "잘린 이름 좌석에 전체 이름 팝업"
    full = st.full_names(NAMES)
    assert full["seat_TC"] == ("User000000001", False) and full["seat_BL"] == NAMES["seat_BL"]
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


def test_profile_fallback_keeps_site_stats():
    import profiles
    calls = []
    st = sty.SeatTypes(log=lambda *a, **k: calls.append(a))
    st.observe(NAMES, state(), 1.0)
    st.apply_popup(popup("Charlie", "LAG", 55, 30), NAMES, 2.0)
    orig = profiles.summarize

    def boom(*a, **k):
        raise RuntimeError("forced")
    profiles.summarize = boom
    try:
        p = st.profile("seat_UR", {"counts": {"hands": 5}, "shown": []})
    finally:
        profiles.summarize = orig
    assert p["vpip"] == 55 and p["pfr"] == 30 and p["ats"] == 10 and p["threebet"] == 2 and p["tab"] == "토너먼트", p
    assert p["style"] == "LAG" and p["type_src"] == "popup" and p["site_fallback"] is True and p["counts"] == {"hands": 5}
    assert len(calls) == 1, calls
    assert "site_fallback" not in st.profile("seat_UR", None), "정상 경로에는 표시 없음"


def test_popup_falls_back_to_prompted_seat():
    st = sty.SeatTypes()
    names = {k: v for k, v in NAMES.items() if k != "seat_BR"}   # BTN 이름을 못 읽음
    st.observe(names, state(), 1.0)
    pr = st.prompt(state(hero_folded=True, hero_in_hand=False, in_hand={"seat_BR", "seat_UR"}), {}, 1.0)
    assert pr["seat"] in ("seat_BR", "seat_UR")
    st.prompt_seat = "seat_BR"
    assert st.apply_popup(popup("Zulu"), names, 2.0) == "seat_BR" and st.style("seat_BR") == "LP"
    st.prompt_seat = "seat_UL"   # 안내 좌석의 이름을 읽었는데 팝업 이름이 다르면 → 다른 사람을 눌렀음
    assert st.apply_popup(popup("Yankee"), names, 30.0) is None   # 안내 20초 지남 -> 옛 규칙


def test_popup_prefers_prompted_seat():
    """팝업 이름과 화면 OCR 이름이 조금 달라도, 지금 안내한 좌석이면 그 좌석에 저장. 애매하면 다른 좌석으로 옮기지 않음."""
    fh = dict(hero_folded=True, hero_in_hand=False)
    # (a) 안내 좌석 OCR 이 반쯤 틀림 ('Xharlje' vs 'Charlie', 같은 사람 기준 미만) → 안내 좌석
    names = dict(NAMES, seat_UR=("Xharlje", False))
    st = sty.SeatTypes()
    st.observe(names, state(), 1.0)
    assert sty.LIKE_RATIO <= sty.name_ratio(names["seat_UR"], "Charlie") < sty.SAME_RATIO
    st.prompt_seat = "seat_UR"
    st.prompt(state(**fh), {}, 2.0)
    assert st.prompt_seat == "seat_UR"
    assert st.apply_popup(popup("Charlie", "TAG"), names, 3.0) == "seat_UR" and st.style("seat_UR") == "TAG"
    # (b) 안내가 없으면 같은 OCR 로는 어느 좌석인지 모름 → 저장 안 함
    st = sty.SeatTypes()
    st.observe(names, state(), 1.0)
    assert st.apply_popup(popup("Charlie"), names, 3.0) is None and not st.known("seat_UR")
    # (c) 안내한 지 오래됨 → 안내 좌석으로 보지 않음
    st = sty.SeatTypes()
    st.observe(names, state(), 1.0)
    st.prompt_seat = "seat_UR"
    st.prompt(state(**fh), {}, 2.0)
    assert st.apply_popup(popup("Charlie"), names, 2.0 + sty.PROMPT_RECENT + 1) is None
    # (d) 안내 좌석 이름을 못 읽었지만 다른 좌석 이름이 팝업과 매우 닮음 → 그 좌석 (사용자가 그쪽을 누름)
    names_d = {k: v for k, v in NAMES.items() if k != "seat_BR"}
    st = sty.SeatTypes()
    st.observe(names_d, state(), 1.0)
    st.prompt_seat = "seat_BR"
    st.prompt(state(**fh), {}, 2.0)
    assert st.apply_popup(popup("Charlie"), names_d, 30.0) == "seat_UR"   # 안내 20초 지남
    assert st.prompt_seat == "seat_BR" and not st.known("seat_BR")
    # (e) 정확히 같은 이름이 있는 좌석이 안내 좌석보다 우선
    st.prompt_seat = "seat_BR"
    assert st.apply_popup(popup("Bravo"), names_d, 31.0) == "seat_UL"
    # (f) 이미 다른 사람 타입이 있는 좌석으로는 약한 매칭으로 옮기지 않음
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    assert st.apply_popup(popup("Alphonse7", "TP"), NAMES, 2.0) is None   # 안내 없음 + 닮지 않음
    st.prompt_seat = "seat_BL"
    st.prompt(state(**fh), {}, 2.5)
    assert st.apply_popup(popup("Alphonse7", "TP"), NAMES, 3.0) == "seat_BL"   # 안내대로 (반쯤 닮음)
    st.prompt_seat = None
    assert sty.same_person(NAMES["seat_BL"], "Alpha78") and not sty.same_person("Alphonse7", "Alpha78")
    assert st.apply_popup(popup("Alpha78", "LAG"), NAMES, 4.0) is None
    assert st.style("seat_BL") == "TP", "알려진 타입을 약한 매칭으로 덮지 않음"
    # (g) 같은 팝업을 다시 열면 그 좌석 (화면 이름과 달라도)
    assert st.apply_popup(popup("Alphonse7", "LP"), NAMES, 5.0) == "seat_BL" and st.style("seat_BL") == "LP"


def test_owner_change_needs_evidence():
    """OCR 이름만 달라진 것은 약한 증거. 글자 지문이 같으면 주인 그대로, 확실히 다르면 CHANGE_SECS 뒤 초기화."""
    import numpy as np
    rng = np.random.default_rng(7)
    fp_a = (rng.random((16, 60)) * 255).astype(np.uint8)
    fp_b = (rng.random((16, 60)) * 255).astype(np.uint8)
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0, fps={"seat_UL": fp_a})
    st.apply_popup(popup("Bravo"), NAMES, 0.5)
    assert st.known("seat_UL")
    # 같은 지문, 다른 OCR 글자 → 오래 계속돼도 같은 사람
    misread = dict(NAMES, seat_UL=("Kxqzv", False))
    for i in range(80):
        assert st.observe(misread, state(), 1.0 + i, fps={"seat_UL": fp_a}) == []
    assert st.known("seat_UL")
    # 지문이 확실히 다름 + 이름도 다름 → CHANGE_SECS 뒤 초기화 (약한 증거보다 빠름)
    other = dict(NAMES, seat_UL=("Kilo99", False))
    assert st.observe(other, state(), 100.0, fps={"seat_UL": fp_b}) == []
    assert st.observe(other, state(), 101.0, fps={"seat_UL": fp_b}) == []
    assert st.observe(other, state(), 100.0 + sty.CHANGE_SECS - 1, fps={"seat_UL": fp_b}) == []
    assert st.known("seat_UL")
    assert st.observe(other, state(), 100.0 + sty.CHANGE_SECS, fps={"seat_UL": fp_b}) == ["seat_UL"]
    assert not st.known("seat_UL") and st.seats["seat_UL"].name == ("Kilo99", False)
    # 빈자리는 이름과 상관없이 확실한 신호 (EMPTY_READS 프레임)
    st.apply_popup(popup("Kilo99"), other, 120.0)
    occ = set(KEYS) - {"seat_UL"}
    st.observe(other, state(occupied=occ), 121.0)
    assert st.observe(other, state(occupied=occ), 122.0) == [], "승리 애니메이션 등 몇 초 가림"
    assert st.observe(other, state(occupied=occ), 121.0 + sty.EMPTY_SECS) == ["seat_UL"]


def test_overlay_is_not_empty_seats():
    """쇼다운 글자·휴식 안내가 테이블을 덮어 여러 좌석이 한꺼번에 빈자리로 보이면 떠난 것으로 세지 않음."""
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    for k, nm in NAMES.items():
        st.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 0.5)
    for i in range(10):   # 10초 동안 상대 모두 안 보임
        assert st.observe({}, state(occupied=set()), 1.0 + i) == []
    assert all(st.known(k) for k in NAMES) and st.table_moves == 0
    st.observe(NAMES, state(), 12.0)
    # 아주 오래 계속되면 (OVERLAY_MAX) 진짜 빈자리 → 테이블 이동으로 전체 초기화
    out = []
    for i in range(int(sty.OVERLAY_MAX + sty.EMPTY_SECS) + 3):
        out = st.observe({}, state(occupied=set()), 20.0 + i) or out
    assert out == ["*"] and not any(st.known(k) for k in NAMES), out


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
    # 2) 다른 이름이 한두 번 → 유지. CHANGE_READS 번 연속이어도 짧으면 유지 (쇼다운 족보·빛 고리·오독)
    #    글자 지문이 없으면 약한 증거 → CHANGE_SECS_WEAK 초 넘게 계속되어야 초기화
    changed = dict(NAMES, seat_UL=("Zebra", False))
    st.observe(changed, state(), 10.0)
    st.observe(NAMES, state(), 11.0)   # 끊김: 연속이 아님
    st.observe(changed, state(), 12.0)
    assert st.known("seat_UL")
    st.observe(changed, state(), 13.0)
    assert st.observe(changed, state(), 14.0) == [] and st.known("seat_UL")
    assert st.observe(changed, state(), 12.0 + sty.CHANGE_SECS_WEAK - 1) == [] and st.known("seat_UL")
    assert st.observe(changed, state(), 12.0 + sty.CHANGE_SECS_WEAK) == ["seat_UL"] and not st.known("seat_UL")
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
    assert st.observe(NAMES, state(occupied=occ), 43.0) == [] and st.known("seat_BR"), "짧게 비어 보임 (승리 애니메이션)"
    assert st.observe(NAMES, state(occupied=occ), 42.0 + sty.EMPTY_SECS) == ["seat_BR"] and not st.known("seat_BR")
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
    for i in range(int(sty.CHANGE_SECS_WEAK) + 2):
        out = st.observe(new, state(), 100.0 + i)
        if out:
            break
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
    st.observe(NAMES, state(), 1.0)                     # 같은 이름이 NAME_STABLE 프레임 그대로
    pr = st.prompt(s, live2, 10.0, NAMES, sure=set(NAMES))
    assert pr["seat"] == "seat_TC" and pr["text"] == "User0… 님을 눌러 주세요" and "타입 확인" in pr["sub"], pr  # 잘린 이름 + …
    assert pr["sub"].startswith("(위 가운데")
    # 유지: 우선순위가 바뀌어도, 시간이 오래 지나도 같은 좌석
    live3 = {"seat_BL": {"preflop": ["blind", "raise"]}}
    s3 = state(in_hand={"seat_BL"}, **folded_hero)
    assert st.prompt(s3, live3, 11.0)["seat"] == "seat_TC"
    assert st.prompt(s3, live3, 10.0 + 8.1)["seat"] == "seat_TC"
    assert st.prompt(s3, live3, 200.0)["seat"] == "seat_TC"
    # 읽은 좌석은 다시 묻지 않음 (TC 를 읽으면 다음 1순위 = BL)
    st.apply_popup(popup("User0999"), NAMES, 30.0)
    assert st.known("seat_TC")
    pr = st.prompt(s3, live3, 31.0)
    assert pr["seat"] == "seat_BL"
    st.apply_popup(popup("Alpha77"), NAMES, 31.5)
    pr = st.prompt(s3, live3, 32.0)
    assert pr["seat"] != "seat_BL" and not st.known(pr["seat"])
    st.prompt_shown_t = None   # 안내가 화면에 없는 때의 팝업
    for k, nm in NAMES.items():
        st.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 32.0)
    assert st.prompt(s3, live3, 40.0) is None, "모두 읽으면 안내 없음"
    # 좌석이 초기화되면 다시 물음
    st.observe(NAMES, state(occupied=set(KEYS) - {"seat_UL"}), 45.0)
    st.observe(NAMES, state(occupied=set(KEYS) - {"seat_UL"}), 51.0)
    st.observe(dict(NAMES, seat_UL=("Kilo", False)), state(**folded_hero), 52.0)
    assert st.prompt(state(**folded_hero), {}, 53.0)["seat"] == "seat_UL"


def _shown(nm, frames=2, sure=True):
    """observe 를 frames 번 한 뒤의 안내 (sure: NameReader 가 확실하다고 한 좌석인지)."""
    s = state(in_hand={"seat_TC"}, hero_folded=True, hero_in_hand=False)
    st = sty.SeatTypes()
    names = {"seat_TC": nm} if nm is not None else {}
    for i in range(frames):
        st.observe(names, s, 1.0 + i)
    return st.prompt(s, {}, 5.0, names, sure={"seat_TC"} if sure else set())


def test_prompt_shows_name():
    """확실한 이름이면 '누구를 누를지' (메모리 전용 화면 문구), 잘림/긴 이름은 …, 아니면 화면 위치 문구.
    아래 줄에는 늘 화면 위치 (먼저) · 포지션."""
    pr = _shown(("테스트닉네임", False))
    assert pr["seat"] == "seat_TC" and pr["text"] == "테스트닉네임 님을 눌러 주세요", pr
    assert pr["sub"] == "(위 가운데 · UTG · 타입 확인)", pr
    assert _shown(("테스트닉", True))["text"] == "테스트닉… 님을 눌러 주세요"   # 잘린 이름
    long_name = "가나다라마바사아자차카타"                                     # 너무 긴 이름은 줄임
    assert _shown((long_name, False))["text"] == long_name[:sty.NAME_SHOW_MAX] + "… 님을 눌러 주세요"
    where = "위 가운데 사람을 눌러 주세요"
    for nm in (None, ("", False), ("…", True), ("가", False)):                  # 이름 없음/한 글자 → 위치 문구
        pr = _shown(nm)
        assert pr["text"] == where and pr["sub"] == "(위 가운데 · UTG · 타입 확인)", pr
    # 확실하지 않으면 이름을 쓰지 않음: NameReader 가 sure 로 안 줌 / 한 프레임만 봄 / 화면 이름이 기억과 다름
    assert _shown(("테스트닉네임", False), sure=False)["text"] == where
    assert _shown(("테스트닉네임", False), frames=1)["text"] == where
    s = state(in_hand={"seat_TC"}, hero_folded=True, hero_in_hand=False)
    st = sty.SeatTypes()
    st.observe({"seat_TC": ("테스트닉네임", False)}, s, 1.0)
    st.observe({"seat_TC": ("테스트닉네임", False)}, s, 2.0)
    assert st.prompt(s, {}, 3.0, {"seat_TC": ("데스트닉네임", False)}, sure={"seat_TC"})["text"] == where
    assert st.prompt(s, {}, 3.0, None, sure={"seat_TC"})["text"] == where
    assert st.prompt(s, {}, 3.0)["text"] == where, "sure 를 안 넘기면 이름을 쓰지 않음"
    # 이름이 흔들리면 (다른 글자가 읽힘) 안정 카운트가 처음부터
    st.observe({"seat_TC": ("완전다른사람", False)}, s, 4.0)
    st.observe({"seat_TC": ("테스트닉네임", False)}, s, 5.0)
    assert st.prompt(s, {}, 6.0, {"seat_TC": ("테스트닉네임", False)}, sure={"seat_TC"})["text"] == where
    st.observe({"seat_TC": ("테스트닉네임", False)}, s, 6.0)
    assert st.prompt(s, {}, 7.0, {"seat_TC": ("테스트닉네임", False)}, sure={"seat_TC"})["text"] == "테스트닉네임 님을 눌러 주세요"


def test_prompt_text_never_reaches_disk():
    """안내 문구(닉네임 포함)는 화면 전용: 합성 실행 중 디스크에 쓰인 어떤 파일에도, 로그용 출력에도 없어야 함."""
    import json
    import os
    import re
    import tempfile
    nick = "테스트닉네임"
    s = state(in_hand={"seat_TC"}, hero_folded=True, hero_in_hand=False)
    with tempfile.TemporaryDirectory() as d:
        old = os.getcwd()
        os.chdir(d)
        try:
            st = sty.SeatTypes()
            st.observe({"seat_TC": (nick, False)}, s, 1.0)
            st.observe({"seat_TC": (nick, False)}, s, 2.0)
            pr = st.prompt(s, {}, 2.0, {"seat_TC": (nick, False)}, sure={"seat_TC"})
            assert nick in pr["text"]
            logged = json.dumps([st.for_log(), st.full_names({"seat_TC": (nick[:3], True)})], ensure_ascii=False)
            assert "님을 눌러" not in logged and "캐릭터를 눌러" not in logged, logged
            assert not os.listdir(d), "SeatTypes 는 파일을 만들지 않음"
        finally:
            os.chdir(old)
    # 정적 확인: reader.py 에서 안내 문구(prompt)는 화면 상태(self.track) 와 그리기에만 쓰이고 로그·저장 호출에 없음
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "reader.py"), encoding="utf-8").read().splitlines()
    for i, ln in enumerate(src, 1):
        if re.search(r"\bprompt\b", ln) and re.search(r"log|write|dump|save|upload|record|json", ln, re.I):
            assert "log_once" not in ln and "debug" not in ln.lower() and "dump" not in ln and "write" not in ln, (i, ln)


def test_prompt_sticky():
    """고른 좌석은 눌러서 해결되거나 그 좌석이 초기화될 때까지 바뀌지 않음 (시간 · 우선순위 · 내 차례와 무관)."""
    fh = dict(hero_folded=True, hero_in_hand=False)
    # (a) 8초 넘게, 우선순위가 바뀌어도 같은 좌석
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 0.0)
    live_a = {"seat_TC": {"preflop": ["open"]}}
    first = st.prompt(state(in_hand={"seat_TC", "seat_BL"}, **fh), live_a, 1.0)["seat"]
    assert first == "seat_TC"
    live_b = {"seat_BL": {"preflop": ["raise"]}, "seat_UR": {"preflop": ["raise"]}}
    s_b = state(in_hand={"seat_BL", "seat_UR"}, folded={"seat_TC"}, **fh)
    assert st.candidates(s_b, live_b)[0][0] != "seat_TC", "우선순위는 실제로 바뀜"
    for t in (5.0, 9.5, 60.0, 600.0):
        assert st.prompt(s_b, live_b, t)["seat"] == "seat_TC", t
    # (b) 내 차례/핸드 중이면 안 보이지만 기억은 유지 → 다시 쉬면 같은 사람
    assert st.prompt(state(to_act=True, **fh), live_b, 601.0) is None
    assert st.prompt(state(), live_b, 602.0) is None
    assert st.prompt_seat == "seat_TC"
    assert st.prompt(s_b, live_b, 603.0)["seat"] == "seat_TC"
    # (c) 팝업으로 해결되면 다음 후보
    assert st.apply_popup(popup("User0999"), NAMES, 604.0) == "seat_TC"
    nxt = st.prompt(s_b, live_b, 605.0)["seat"]
    assert nxt != "seat_TC" and not st.known(nxt)
    assert st.prompt(s_b, live_b, 700.0)["seat"] == nxt, "새로 고른 좌석도 유지"
    # 후보가 없으면 지움
    st2 = sty.SeatTypes()
    st2.observe(NAMES, state(), 0.0)
    st2.prompt(state(**fh), {}, 1.0)
    assert st2.prompt_seat
    st2.prompt_shown_t = None
    for k, nm in NAMES.items():
        st2.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 2.0)
    assert st2.prompt(state(**fh), {}, 3.0) is None and st2.prompt_seat is None
    # (d) 주인이 바뀌면 (같은 좌석 키에 새 사람) 안내 좌석을 버리고 다시 고름
    st3 = sty.SeatTypes()
    st3.observe(NAMES, state(), 0.0)
    pick = st3.prompt(state(**fh), {}, 1.0)["seat"]
    changed = dict(NAMES, **{pick: ("Newcomer", False)})
    for i in range(sty.CHANGE_READS):
        st3.observe(changed, state(**fh), 2.0 + i)
    assert st3.prompt_seat == pick, "다른 이름이 잠깐 읽힌 것으로는 안내를 바꾸지 않음"
    st3.observe(changed, state(**fh), 2.0 + sty.CHANGE_SECS_WEAK)
    assert st3.prompt_seat is None, "주인이 바뀌면 이전 안내 해제"
    # 떠난 자리 (EMPTY_READS 프레임 연속 빈자리) → 후보에서 빠지고 다른 사람으로
    st4 = sty.SeatTypes()
    st4.observe(NAMES, state(), 0.0)
    pick = st4.prompt(state(**fh), {}, 1.0)["seat"]
    occ = set(KEYS) - {pick}
    for i in range(sty.EMPTY_READS):
        st4.observe(NAMES, state(occupied=occ), 2.0 + i)
    assert st4.prompt_seat == pick, "몇 초 비어 보인 것으로는 안내를 바꾸지 않음"
    st4.observe(NAMES, state(occupied=occ), 2.0 + sty.EMPTY_SECS)
    assert st4.prompt_seat is None
    assert st4.prompt(state(occupied=occ, **fh), {}, 20.0)["seat"] != pick
    # 이름을 기억하기 전에 떠난 자리도 안내 해제
    st5 = sty.SeatTypes()
    st5.prompt_seat = "seat_UL"
    st5.observe({}, state(occupied=set(KEYS) - {"seat_UL"}), 1.0)
    assert st5.prompt_seat == "seat_UL"
    st5.observe({}, state(occupied=set(KEYS) - {"seat_UL"}), 1.0 + sty.EMPTY_SECS)
    assert st5.prompt_seat is None
    # 테이블 이동 (전체 초기화) / reset()
    st6 = sty.SeatTypes()
    st6.observe(NAMES, state(), 0.0)
    for k, nm in NAMES.items():
        st6.apply_popup(popup(nm[0] if not nm[1] else nm[0] + "999"), NAMES, 0.5)
    st6.prompt_seat = "seat_UR"
    new = {"seat_BL": ("Echo", False), "seat_UL": ("Foxtrot", False), "seat_TC": ("Golf", False),
           "seat_UR": ("Charlie", False), "seat_BR": ("Delta", False)}
    for i in range(sty.CHANGE_READS):
        st6.observe(new, state(), 100.0 + i)
    st6.observe(new, state(), 100.0 + sty.CHANGE_SECS_WEAK)
    assert st6.prompt_seat is None and st6.table_moves == 1
    st7 = sty.SeatTypes()
    st7.prompt_seat = "seat_BL"
    st7.reset()
    assert st7.prompt_seat is None


def test_prompt_without_position():
    st = sty.SeatTypes()
    s = state(hero_folded=True)
    for x in s["seats"]:
        x["pos"] = None
    s["hero_pos"] = None
    pr = st.prompt(s, {}, 1.0)
    assert pr["text"].endswith(" 사람을 눌러 주세요") and pr["pos"] is None, pr
    assert pr["sub"] == "(" + sty.SLOT_KO[pr["slot"]] + " · 타입 확인)", pr
    for x in s["seats"]:   # 위치도 포지션도 모름
        x["slot"] = None
    st2 = sty.SeatTypes()
    assert st2.prompt(s, {}, 1.0)["text"] == "상대 캐릭터를 눌러 주세요"


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
        names = {"seat_BL": ("Alpha77", False), "seat_UL": ("Bravo", False), "seat_TC": ("User0", True),
                 "seat_UR": ("Charlie", False), "seat_BR": ("Delta", False)}

        class T(Table):
            def step(self, **kw):
                snap = self.snap()
                tps.observe(self.names, snap, self.t)
                orig = self.tr.update

                def upd(state, hero, board, nm, t, shown=None, **kw):
                    return orig(state, hero, board, tps.full_names(nm), t, shown=shown, types=tps.for_log())
                self.tr.update = upd
                try:
                    return Table.step(self, **kw)
                finally:
                    self.tr.update = orig
        tb = T(tr, names=names)
        tb.step()
        tps.apply_popup(popup("User000000001", "LAG"), names, tb.t)
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
        for nm in ("Alpha77", "Bravo", "User0", "Charlie", "Delta", "User000000001"):
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


def test_popup_during_prompt_is_prompted_player():
    """안내 중에 연 팝업은 이름이 전혀 달라도 안내한 그 사람. 이미 아는 팝업 이름 재오픈 / 20초 지남 / 내 팝업은 예외."""
    fh = dict(hero_folded=True, hero_in_hand=False)
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    pr = st.prompt(state(**fh), {}, 2.0)
    seat = pr["seat"]
    assert st.apply_popup(popup("Qqqqzzzz", "TAG"), NAMES, 3.0) == seat and st.style(seat) == "TAG"
    # 이미 아는 팝업 이름을 다시 열면 그 좌석 그대로, 안내는 유지
    pr2 = st.prompt(state(**fh), {}, 4.0)
    assert pr2 is not None and pr2["seat"] != seat
    assert st.apply_popup(popup("Qqqqzzzz", "LP"), NAMES, 5.0) == seat and st.style(seat) == "LP"
    assert st.prompt_seat == pr2["seat"]
    # 안내를 보여 준 지 20초 넘음 -> 옛 규칙 (이름이 다르면 저장 안 함 / 같으면 정확히 매칭)
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    st.prompt(state(**fh), {}, 2.0)
    late = 2.0 + sty.PROMPT_CLICK_WINDOW + 1
    assert st.apply_popup(popup("Qqqqzzzz"), NAMES, late) is None
    assert st.apply_popup(popup("Bravo"), NAMES, late) == "seat_UL"
    # 안내를 한 번도 안 보였으면 (prompt_seat 만 있음) 옛 규칙
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    st.prompt_seat = "seat_UR"
    assert st.apply_popup(popup("Qqqqzzzz"), NAMES, 2.0) is None
    # 내 팝업은 안내 중이어도 None
    st = sty.SeatTypes()
    st.observe(NAMES, state(), 1.0)
    st.prompt(state(**fh), {}, 2.0)
    assert st.apply_popup(popup("MyHero"), dict(NAMES, hero=("MyHero", True)), 3.0) is None


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
