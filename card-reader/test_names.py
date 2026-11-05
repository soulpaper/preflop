"""names.py (좌석 닉네임 읽기) 의 OCR 없는 부분: 다수결 · 정리 · 족보 표시 거르기 · 좌석별 확정 규칙.
닉네임은 모두 합성 (실제 닉네임을 테스트에 쓰지 않음). 실행: python test_names.py"""
import sys

import numpy as np

import names


def test_clean_and_dots():
    assert names.clean("Test12...") == ("Test12", True)
    assert names.clean("Test12•") == ("Test12", True), "OCR 이 '…' 을 '•' 하나로 읽음"
    assert names.clean("Test12.") == ("Test12", True)
    assert names.clean(" 가나다라 ") == ("가나다라", False)
    assert names.clean("-가나다\\") == ("가나다", False)


def test_tally_needs_agreement():
    assert names.tally(["가나다", "가나다", "가나디", ""]) == ("가나다", 2)
    assert names.tally(["가나다", "가나디"]) == (None, 0), "한 표씩이면 섞어서 만들지 않음"
    assert names.tally(["가나다", "가나다", "가나디", "가나디"]) == (None, 0), "1등이 둘"
    assert names.tally(["Abc1", "Abc1", "Abc1", "x"]) == ("Abc1", 3)
    assert names.tally(["가나0", "가나0", "가EI다"]) == (None, 0), "한글+영문/숫자 섞임은 오독"
    assert names.tally(["가+나", "가+나"]) == (None, 0)
    assert names.vote(["Qwer", "Qwer", ""]) == "Qwer"


def test_hand_label():
    assert names.hand_label(["Q 스트레이트", "Q 스트레이트", "Q 스트e이트", ""])
    assert names.hand_label(["Q,J 투페어", "Q,J 투페어"])
    assert names.hand_label(["7 원페어", "7원페어"])
    assert not names.hand_label(["원페어좋아...", "원페어좋아.."]), "족보 낱말이 들어간 진짜 닉네임"
    assert not names.hand_label(["가나다", "가나다"])
    assert not names.hand_label(["", ""])


def _r(name, truncated=False, votes=3, fp=None):
    return {"name": name, "truncated": truncated, "votes": votes, "sure": bool(name) and votes >= names.SURE_VOTES,
            "fp": fp, "texts": []}


def test_confirm_and_hold():
    nr = names.NameReader()
    st = names._Seat()
    a, b = _r("가나다라"), _r("마바사아")
    for i in range(names.CONFIRM_READS - 1):
        nr._update("seat_X", st, a, sig=None)
        assert st.confirmed is None
    nr._update("seat_X", st, None, sig=None)                 # 못 읽은 프레임은 끊지 않음
    nr._update("seat_X", st, _r(None, votes=0), sig=None)
    nr._update("seat_X", st, a, sig=None)
    assert st.confirmed == ("가나다라", False) and st.sure
    nr.seats["seat_X"] = st
    assert nr.sure() == {"seat_X"} and nr.confirmed() == {"seat_X": ("가나다라", False)}
    # 다른 이름이 몇 번 읽혀도 (빛 고리·족보·오독) 확정은 그대로; CONFIRM_READS * REPLACE_FACTOR 번 연속이면 바뀜
    need = names.CONFIRM_READS * names.REPLACE_FACTOR
    for i in range(need - 1):
        nr._update("seat_X", st, b, sig=None)
        assert st.confirmed == ("가나다라", False), i
    nr._update("seat_X", st, a, sig=None)                    # 원래 이름이 다시 → 도전 끝
    for i in range(need - 1):
        nr._update("seat_X", st, b, sig=None)
    assert st.confirmed == ("가나다라", False)
    nr._update("seat_X", st, b, sig=None)
    assert st.confirmed == ("마바사아", False)


def test_sure_needs_strong_votes():
    nr = names.NameReader()
    st = names._Seat()
    weak = _r("가나다라", votes=2)
    for i in range(names.CONFIRM_READS):
        nr._update("k", st, weak, sig=None)
    assert st.confirmed == ("가나다라", False) and not st.sure, "2표로만 확정 → 안내에 이름을 쓰지 않음"
    nr._update("k", st, _r("가나다라", votes=4), sig=None)
    assert st.sure


def test_fingerprint_keeps_confirmed():
    rng = np.random.default_rng(3)
    fp = (rng.random((16, 50)) * 255).astype(np.uint8)
    nr = names.NameReader()
    st = names._Seat()
    for i in range(names.CONFIRM_READS):
        nr._update("k", st, _r("가나다라", fp=fp), sig=None)
    for i in range(20):   # 글자 모양이 같으면 OCR 이 다르게 읽어도 같은 이름
        nr._update("k", st, _r("가니다라", fp=fp), sig=None)
    assert st.confirmed == ("가나다라", False) and st.candidate is None
    assert names.fp_similarity(fp, fp) > 0.99 and nr.fingerprints() == {}
    nr.seats["k"] = st
    assert nr.fingerprints()["k"] is fp


def test_variants_shape():
    for v in names.OCR_VARIANTS:
        assert len(v) == 6 and v[3] in ("min", "val")
    img = np.zeros((60, 200, 3), np.uint8)
    img[20:34, 40:160] = (200, 200, 200)
    img[22:32, 90:96] = (60, 90, 130)    # 구리색 줄: 'min' 밝기에서는 글자가 아님
    ims = names._ocr_images(img, (40, 20, 160, 34), 14)
    assert len(ims) == len(names.OCR_VARIANTS) and all(i.ndim == 2 for i in ims)


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
