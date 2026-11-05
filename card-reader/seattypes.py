"""좌석별 상대 '타입' (게임 '유저 정보' 팝업의 LAG/LP/TAG/TP) — 메모리 전용, 이번 테이블/토너먼트 동안만.

    types = seattypes.SeatTypes()
    types.observe(names, state, t)          # 매 프레임: 닉네임(names.NameReader 확정값)·빈자리로 좌석 주인이 바뀌었는지
    key = types.apply_popup(info, names, t)  # 사용자가 연 팝업(userinfo.PopupWatcher 결과) → 그 좌석에 타입 저장
    types.prompt(state, live, t)             # 지금 눌러 달라고 할 좌석 하나 (내가 쉬는 동안만) 또는 None
    types.for_log()                          # handlog.Tracker.update(types=...) 용 {seat: {type, type_src, type_stats}}
    types.profile(key, base)                 # handread / 화면용 프로필 (base 통계 + 팝업 통계 + style)
    types.reset()                            # 새 [자동 지정] / 테이블 이동·합치기

왜: 토너먼트에서는 같은 상대를 다시 만나는 일이 드물어 사람별 누적은 의미가 없다. 대신 게임이 그 사람의 긴 기록으로 매긴
타입을 좌석별로 기억해 상대 읽기에 쓴다. 닉네임·타입 모두 디스크에 쓰지 않는다 (핸드 기록에는 좌석별 타입만 handlog 가 씀).

좌석 초기화 (그 좌석 타입을 버림)
  - 빈자리로 EMPTY_READS 프레임 연속 + EMPTY_SECS 초 넘게 보임 (떠남 → 다음에 앉는 사람은 새 사람) — 가장 믿을 만한 신호.
    승리 애니메이션은 좌석을 몇 초 가리고, 쇼다운 글자·휴식 안내는 여러 좌석을 한꺼번에 가림(OVERLAY_SEATS) → 세지 않음
  - 확정된 닉네임이 기억과 다른 사람으로 CHANGE_READS 프레임 연속, 그리고 CHANGE_SECS 초 넘게 읽힘. OCR 은 잘 틀리므로
    잘린 이름의 접두사 / 글자 유사도 SAME_RATIO 이상 / 글자 지문(names.fp_similarity)이 같으면 같은 사람으로 보고,
    글자 지문이 '다르다'고 확인되지 않았거나 이름이 반쯤 닮았으면(LIKE_RATIO) CHANGE_SECS_WEAK 초를 봐야 함
  - TABLE_MOVE_SEATS 자리 이상이 TABLE_MOVE_WINDOW 초 안에 바뀜 = 테이블 이동/합치기 → 모든 좌석 초기화
  - reset(): 새 [자동 지정] 세션 (앱이 track_new_session 에서 부름)
팝업 → 좌석 (OCR 이 틀려도 안내대로 누른 것은 저장되게, 애매하면 다른 좌석으로 옮기지 않게)
  0. 같은 팝업 이름을 이미 저장한 좌석 하나 → 그 좌석
  1. 팝업 닉네임과 정확히 같은 이름(또는 4글자 이상 잘린 이름의 접두사)인 좌석이 하나 → 그 좌석
  2. 안내 중인 좌석(PROMPT_RECENT 초 안에 보여 줌)의 이름을 못 읽었거나 반쯤이라도 닮았으면(LIKE_RATIO) → 그 좌석
     (다른 좌석 이름이 매우 닮았으면(STRONG_RATIO) 사용자가 그쪽을 누른 것 → 3으로)
  3. 같은 사람(유사도 SAME_RATIO)인 좌석이 하나 → 그 좌석. 단 이미 다른 사람의 팝업 타입이 있는 좌석이면 옮기지 않음
  그 밖(모호/다른 좌석)이면 저장하지 않음.
안내 (prompt) — 앱은 아무것도 클릭하지 않는다. 사용자에게 어느 캐릭터를 눌러 달라고 큰 글씨로 한 자리씩만.
  - 내가 쉴 때만: 내 차례/예약(preaction)이 아니고, 폴드했거나 이번 핸드에 카드가 없을 때 (차례 시간 ~9.8초를 뺏지 않게)
  - 타입을 모르는 상대만 (한 번 읽으면 그 좌석이 초기화되기 전까지 다시 묻지 않음)
  - 우선순위: 아직 핸드에 남은 상대 → 이번 핸드에 자발적으로 칩을 넣은(루즈/액티브) 상대 → 나보다 뒤에 행동하는 상대 → 자리 순
  - 문구: 닉네임이 확실할 때만(NameReader 확정 + 여러 전처리가 같은 글자 + NAME_STABLE 프레임 그대로) '닉네임 님을 눌러 주세요',
    아니면 화면 위치로 '오른쪽 위 사람을 눌러 주세요'. 아래 줄에는 늘 화면 위치를 먼저 (포지션 이름보다 알아보기 쉬움)
  - 한 번 고른 좌석은 눌러서 타입을 읽거나 그 좌석이 초기화(떠남/주인 바뀜/테이블 이동)될 때까지 계속 그 사람만 안내
    (시간이 지나도, 우선순위가 바뀌어도, 내 차례로 잠깐 안 보였다 다시 나와도 바꾸지 않음 → 눌러야 할 사람이 눌리기 전에 바뀌지 않게)
"""
import difflib

STYLES_KO = {"LAG": "루즈-어그레시브", "LP": "루즈-패시브", "TAG": "타이트-어그레시브", "TP": "타이트-패시브"}
LOOSE = ("LAG", "LP")
CHANGE_READS = 3           # 다른 닉네임이 이만큼 연속으로 확정돼 읽히고
CHANGE_SECS = 10.0         # 이만큼 넘게 계속되면 좌석 주인이 바뀐 것 (쇼다운 족보·빛 고리 몇 초로는 안 바뀌게)
CHANGE_SECS_WEAK = 30.0    # 글자 지문으로 '다른 글자'가 확인되지 않았거나 이름이 반쯤 닮았으면 이만큼
EMPTY_READS = 2            # 빈자리가 이만큼 연속이고
EMPTY_SECS = 6.0           # 이만큼 넘게 계속되면 떠난 것 (승리 애니메이션이 좌석을 몇 초 가림)
OVERLAY_SEATS = 3          # 앉아 있던 상대가 이만큼 (그리고 절반 넘게) 한꺼번에 빈자리로 보이면 덮는 화면 (쇼다운 글자 등)
OVERLAY_MAX = 15.0         # 덮는 화면이 이보다 오래면 진짜 빈자리로 받아들임
SAME_RATIO = 0.75          # 닉네임 글자 유사도가 이 이상이면 같은 사람 (OCR 오타)
LIKE_RATIO = 0.5           # 이 이상이면 '반쯤 닮음' (안내한 좌석 팝업 매칭 / 주인 바뀜 판단을 늦춤)
STRONG_RATIO = 0.85        # 안내 좌석이 아닌 다른 좌석으로 팝업을 보낼 만큼 닮음
FP_SAME = 0.70             # 글자 지문 유사도가 이 이상이면 같은 글자 (names.FP_SAME 과 같게)
FP_DIFF = 0.50             # 이 아래면 확실히 다른 글자
PROMPT_RECENT = 120.0      # 안내를 보여 준 지 이 초 안에 열린 팝업은 안내한 좌석 쪽으로 봄
NAME_STABLE = 2            # 안내에 닉네임을 쓰려면 같은 이름이 이만큼 연속 프레임 그대로여야 함
TABLE_MOVE_SEATS = 3       # 이 수 이상의 좌석이 짧은 시간에 바뀌면 테이블 이동/합치기
TABLE_MOVE_WINDOW = 20.0
NAME_SHOW_MAX = 8   # 안내에 보일 닉네임 최대 글자 수 (넘으면 … 로 줄임)
# 포스트플랍 행동 순서 (뒤일수록 나중에 행동 = 포지션이 좋음)
POST_ORDER = ("SB", "BB", "UTG", "U1", "U2", "U3", "MP", "LJ", "HJ", "CO", "BTN")
SLOT_KO = {"BL": "왼쪽 아래", "L": "왼쪽", "UL": "왼쪽 위", "TL": "위 왼쪽", "TC": "위 가운데", "TR": "위 오른쪽",
           "UR": "오른쪽 위", "R": "오른쪽", "BR": "오른쪽 아래"}
VOLUNTARY = {"open", "limp", "call", "3bet", "call_3bet", "4bet", "jam", "call_jam", "bet", "raise", "checkraise", "allin"}
STAT_KEYS = ("vpip", "pfr", "ats", "threebet")


def type_ko(style):
    return STYLES_KO.get(style)


def _norm(name):
    return "".join(str(name or "").split()).rstrip(".…‥").lower()


def _parts(a):
    if isinstance(a, tuple):
        return _norm(a[0]), bool(a[1])
    return _norm(a), False


def name_ratio(a, b):
    """닉네임 둘 (문자열 또는 (name, truncated)) 의 글자 유사도 0..1 (잘린 이름은 접두사 길이만큼 비교)."""
    (na, ta), (nb, tb) = _parts(a), _parts(b)
    if not na or not nb:
        return 0.0
    if ta and len(nb) > len(na):
        nb = nb[:len(na)]
    if tb and len(na) > len(nb):
        na = na[:len(nb)]
    return difflib.SequenceMatcher(None, na, nb).ratio()


def exact_person(a, b):
    """같은 글자 (잘린 이름이면 4글자 이상 접두사가 같음) — 팝업 매칭에서 '확실'."""
    (na, ta), (nb, tb) = _parts(a), _parts(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return (ta and len(na) >= 4 and nb.startswith(na)) or (tb and len(nb) >= 4 and na.startswith(nb))


def _fp_sim(a, b):
    """글자 지문 유사도 (둘 중 하나라도 없으면 None)."""
    if a is None or b is None:
        return None
    try:
        import names
        return names.fp_similarity(a, b)
    except Exception:
        return None


def same_person(a, b):
    """닉네임 둘 (문자열 또는 (name, truncated)) 이 같은 사람으로 볼 만한가 (잘린 이름 접두사, OCR 오타 허용)."""
    ta = bool(a[1]) if isinstance(a, tuple) else False
    tb = bool(b[1]) if isinstance(b, tuple) else False
    na, nb = _norm(a[0] if isinstance(a, tuple) else a), _norm(b[0] if isinstance(b, tuple) else b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if (ta and len(na) >= 2 and nb.startswith(na)) or (tb and len(nb) >= 2 and na.startswith(nb)):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= SAME_RATIO


class _Seat:
    __slots__ = ("name", "diff", "empty", "style", "stats", "src", "t", "popup_name", "fp", "diff_t0", "diff_weak", "hits")

    def __init__(self):
        self.name = None        # 메모리에서만: 지금 이 자리 사람의 닉네임 (name, truncated)
        self.diff = 0           # 다른 사람 닉네임이 연속으로 읽힌 프레임 수
        self.empty = 0          # 빈자리로 보인 연속 프레임 수
        self.style = None       # 'LAG'/'LP'/'TAG'/'TP' (팝업) 또는 None
        self.stats = None       # 팝업 통계 {vpip, pfr, ats, threebet, tab}
        self.src = None         # 'popup' 이면 팝업을 읽었음 (style 을 못 읽었어도 다시 묻지 않음)
        self.t = None
        self.popup_name = None  # 메모리에서만: 팝업에서 읽은 전체 닉네임 (좌석 매칭용)
        self.fp = None          # 메모리에서만: 이 사람 닉네임 글자 지문 (names.NameReader.fingerprints)
        self.diff_t0 = None     # 다른 이름이 처음 읽힌 시각
        self.diff_weak = False  # 이번 '다른 이름'이 약한 증거인지 (지문으로 확인 안 됨 / 반쯤 닮음)
        self.hits = 0           # 같은 이름(name 그대로)이 연속으로 읽힌 프레임 수 (안내에 이름을 쓸지)

    def known(self):
        return self.src is not None


class SeatTypes:
    def __init__(self, log=None):
        # log(title, ctx=None): 예외 기록용 콜백 (앱은 log_once 를 넘김). 기본은 아무것도 안 함.
        self.log = log or (lambda *a, **k: None)
        self.reset()

    def reset(self):
        """모든 좌석 초기화 (새 [자동 지정] / 테이블 이동)."""
        self.seats = {}
        self.prompt_seat = None
        self.prompt_t = None         # 안내를 마지막으로 보여 준 시각
        self.empty_seen = {}         # 좌석 → (빈자리로 보인 연속 프레임 수, 처음 비어 보인 시각)
        self.last_occ = set()        # 덮는 화면이 아닌 마지막 프레임에 앉아 있던 상대 좌석
        self.overlay_t0 = None
        self.recent_resets = []      # (t, seat) — 테이블 이동 감지
        self.resets = 0              # 초기화 횟수 (좌석 + 전체; 앱이 바뀌었는지 볼 때)
        self.table_moves = 0

    def _drop(self, key, t):
        if key in self.seats:
            del self.seats[key]
            self.resets += 1
            if self.prompt_seat == key:
                self.prompt_seat = None
            if t is not None:
                self.recent_resets = [(tt, k) for tt, k in self.recent_resets if t - tt <= TABLE_MOVE_WINDOW]
                self.recent_resets.append((t, key))
            return True
        return False

    # --- 매 프레임 ------------------------------------------------------------------------------------
    def observe(self, names, state, t=None, fps=None):
        """names {seat: (name, truncated)} (확정만) + tablestate 결과 (+ fps {seat: 닉네임 글자 지문}, 메모리 전용)
        → 이번에 초기화한 좌석 목록. 테이블 이동으로 보이면 전체를 초기화하고 ['*'] 반환."""
        dropped = []
        try:
            if not isinstance(state, dict) or state.get("layout_ok") is False or not state.get("seats"):
                return dropped
            names, fps = names or {}, fps or {}
            seats = [s for s in state["seats"] if isinstance(s, dict) and s.get("key") and s.get("key") != "hero"]
            occ = {s["key"] for s in seats if s.get("occupied")}
            if self._overlay(occ, t):
                return dropped  # 쇼다운·휴식 화면이 테이블을 덮음: 빈자리로 세지 않음 (이름 판단도 보류)
            for s in seats:
                k = s["key"]
                rec = self.seats.get(k)
                if k not in occ:
                    n, t0 = self.empty_seen.get(k, (0, t))
                    n += 1
                    self.empty_seen[k] = (n, t0)
                    # 승리 애니메이션 등이 좌석을 몇 초 가림 → EMPTY_READS 프레임 + EMPTY_SECS 초 넘게 비어야 떠난 것
                    gone = n >= EMPTY_READS and (t is None or t0 is None or t - t0 >= EMPTY_SECS)
                    if rec is not None:
                        rec.empty = n
                        if gone and self._drop(k, t):
                            dropped.append(k)
                    elif gone and self.prompt_seat == k:
                        self.prompt_seat = None  # 이름을 기억하기 전에 떠난 자리: 다음 사람에게 안내가 넘어가지 않게
                    continue
                self.empty_seen.pop(k, None)
                nm, fp = names.get(k), fps.get(k)
                if rec is None:
                    if nm and nm[0]:
                        rec = self.seats[k] = _Seat()
                        rec.name, rec.fp, rec.hits = (nm[0], bool(nm[1])), fp, 1
                    continue
                rec.empty = 0
                if not nm or not nm[0]:
                    continue  # 이번 프레임에 확정된 이름 없음: 판단 보류
                nm = (nm[0], bool(nm[1]))
                sim = _fp_sim(rec.fp, fp)
                named = rec.name is not None and self._same(rec, nm)
                if rec.name is None or named or (sim is not None and sim >= FP_SAME):
                    if rec.name is None or (named and rec.name[1] and not nm[1]):
                        rec.name, rec.hits = nm, 0     # 잘리지 않은 이름이 더 정확
                    rec.hits = rec.hits + 1 if nm == rec.name else 0
                    if rec.fp is None:
                        rec.fp = fp
                    rec.diff, rec.diff_t0 = 0, None
                    continue
                # 다른 이름: 글자 지문이 확실히 다르지 않거나 반쯤 닮았으면 약한 증거 → 더 오래 봐야 함
                rec.hits = 0
                weak = sim is None or sim >= FP_DIFF or self._like(rec, nm)
                if rec.diff == 0:
                    rec.diff_t0, rec.diff_weak = t, weak
                rec.diff += 1
                rec.diff_weak = rec.diff_weak or weak
                secs = CHANGE_SECS_WEAK if rec.diff_weak else CHANGE_SECS
                long_enough = t is None or rec.diff_t0 is None or t - rec.diff_t0 >= secs
                if rec.diff >= CHANGE_READS and long_enough and self._drop(k, t):
                    dropped.append(k)
                    new = self.seats[k] = _Seat()
                    new.name, new.fp, new.hits = nm, fp, 1
            if t is not None:
                recent = {k for tt, k in self.recent_resets if t - tt <= TABLE_MOVE_WINDOW}
                if len(recent) >= TABLE_MOVE_SEATS:
                    self.reset()
                    self.table_moves += 1
                    self.resets += 1
                    return ["*"]
        except Exception:
            pass
        return dropped

    def _overlay(self, occ, t):
        """이번 프레임이 '테이블을 덮는 화면'인가: 직전까지 앉아 있던 상대 OVERLAY_SEATS 명 이상이 한꺼번에 빈자리로 보임
        (쇼다운 글자, 휴식 안내). OVERLAY_MAX 초 넘게 계속되면 진짜로 비었다고 보고 받아들임."""
        gone = self.last_occ - occ
        if len(gone) >= OVERLAY_SEATS and len(gone) * 2 > len(self.last_occ):
            if self.overlay_t0 is None:
                self.overlay_t0 = t
            if t is None or self.overlay_t0 is None or t - self.overlay_t0 < OVERLAY_MAX:
                return True
        self.overlay_t0 = None
        self.last_occ = set(occ)
        return False

    @staticmethod
    def _same(rec, nm):
        return same_person(rec.name, nm) or (rec.popup_name is not None and same_person(rec.popup_name, nm))

    @staticmethod
    def _like(rec, nm):
        return max(name_ratio(rec.name, nm) if rec.name else 0.0,
                   name_ratio(rec.popup_name, nm) if rec.popup_name else 0.0) >= LIKE_RATIO

    # --- 팝업 ----------------------------------------------------------------------------------------
    def apply_popup(self, info, names=None, t=None):
        """userinfo 팝업 결과 → 저장한 좌석 키 또는 None (어느 좌석인지 모름 / 내 정보). 이름은 메모리에서만 씀."""
        try:
            if not isinstance(info, dict) or not info.get("name"):
                return None
            pname = info["name"]
            names = names or {}
            if names.get("hero") and same_person(names["hero"], pname):
                return None  # 내 캐릭터
            seen = {}   # 좌석 → 이번 프레임 이름, 없으면 기억한 이름, 그것도 없으면 None
            for k in ({k for k in names if k != "hero"} | set(self.seats)):
                nm = names.get(k)
                if not (nm and nm[0]):
                    rec = self.seats.get(k)
                    nm = rec.name if rec is not None else None
                seen[k] = (nm[0], bool(nm[1])) if nm and nm[0] else None
            again = [k for k, rec in self.seats.items() if rec.popup_name and _norm(rec.popup_name) == _norm(pname)]
            exact = [k for k, nm in seen.items() if nm and exact_person(nm, pname)]
            key = None
            if len(again) == 1:
                key = again[0]            # 0. 같은 사람 팝업을 다시 열었음
            elif len(exact) == 1:
                key = exact[0]            # 1. 정확히 같은 이름
            elif not exact:
                ps = self.prompt_seat
                recent = ps is not None and (t is None or self.prompt_t is None or t - self.prompt_t <= PROMPT_RECENT)
                strong = [k for k, nm in seen.items() if k != ps and nm and name_ratio(nm, pname) >= STRONG_RATIO]
                if recent and not strong:
                    pn = seen.get(ps)
                    if pn is None or name_ratio(pn, pname) >= LIKE_RATIO or same_person(pn, pname):
                        key = ps          # 2. 안내한 좌석: 이름을 못 읽었거나 닮음 → 안내대로 눌렀다고 봄
                if key is None:
                    fuzzy = [k for k, nm in seen.items() if nm and same_person(nm, pname)]
                    if len(fuzzy) == 1:
                        rec = self.seats.get(fuzzy[0])
                        taken = rec is not None and rec.known() and rec.popup_name and not same_person(rec.popup_name, pname)
                        if not taken:
                            key = fuzzy[0]  # 3. 닮은 좌석 하나 (다른 사람 타입이 이미 있는 좌석으로는 옮기지 않음)
            if key is None:
                return None
            rec = self.seats.get(key)
            if rec is None:
                rec = self.seats[key] = _Seat()
                nm = names.get(key)
                rec.name = (nm[0], bool(nm[1])) if nm and nm[0] else None
            rec.style = info.get("style") if info.get("style") in STYLES_KO else None
            rec.stats = {k: info.get(k) for k in STAT_KEYS if info.get(k) is not None}
            if info.get("tab"):
                rec.stats["tab"] = info["tab"]
            rec.src, rec.t, rec.popup_name = "popup", t, pname
            rec.diff, rec.diff_t0 = 0, None
            if self.prompt_seat == key:
                self.prompt_seat = None
            return key
        except Exception:
            return None

    # --- 조회 ----------------------------------------------------------------------------------------
    def style(self, key):
        rec = self.seats.get(key)
        return rec.style if rec is not None else None

    def known(self, key):
        rec = self.seats.get(key)
        return rec is not None and rec.known()

    def full_names(self, names):
        """names 사본에서 잘린 닉네임을 그 좌석 팝업의 전체 닉네임으로 바꿈 (접두사가 맞을 때만, 메모리 전용).
        handlog 의 pid 가 잘린 이름 좌석에도 붙을 수 있게."""
        out = dict(names or {})
        for k, nm in out.items():
            rec = self.seats.get(k)
            if nm and nm[0] and nm[1] and rec is not None and rec.popup_name and same_person(nm, rec.popup_name):
                out[k] = (rec.popup_name, False)
        return out

    def for_log(self):
        """{seat: {type, type_src, type_stats}} — 팝업을 읽은 좌석만 (handlog.Tracker.update(types=))."""
        out = {}
        for k, rec in self.seats.items():
            if rec.known():
                out[k] = {"type": rec.style, "type_src": rec.src,
                          "type_stats": {x: rec.stats[x] for x in STAT_KEYS if x in (rec.stats or {})} or None}
        return out

    def profile(self, key, base=None):
        """handread / 화면 / 결정 기록용 프로필: base(관측 통계, profiles.summarize 모양) + 팝업 통계(사이트 사전분포) + style.
        팝업을 안 읽은 좌석이면 base 그대로 (없으면 None)."""
        rec = self.seats.get(key)
        if rec is None or not rec.known():
            return base
        try:
            import profiles
            site = {x: rec.stats.get(x) for x in STAT_KEYS} if rec.stats else None
            if site and all(v is None for v in site.values()):
                site = None
            if site is not None:
                site["style"] = rec.style
                site["tab"] = rec.stats.get("tab")
                counts = (base or {}).get("counts") or {}
                out = profiles.summarize(counts, (base or {}).get("shown") or (), site)
            else:
                out = dict(base or profiles.summarize({}))
            out["style"] = rec.style
            out["type_src"] = rec.src
            return out
        except Exception:
            try:
                self.log("seattypes.profile", {"key": key})
            except Exception:
                pass
            # summarize 를 건너뛰어도 팝업(사이트) 통계는 버리지 않는다: 같은 키로 직접 복사
            out = dict(base or {})
            st = rec.stats or {}
            for x in STAT_KEYS:
                if st.get(x) is not None:
                    out[x] = st.get(x)
            if any(st.get(x) is not None for x in STAT_KEYS):
                out["tab"] = st.get("tab")
                out["source"], out["reliable"] = "site", True
                out["site"] = {**{x: st.get(x) for x in STAT_KEYS}, "style": rec.style, "tab": st.get("tab")}
            out["style"], out["type_src"] = rec.style, rec.src
            out["site_fallback"] = True
            return out

    # --- 안내 ----------------------------------------------------------------------------------------
    @staticmethod
    def idle(state):
        """내가 쉬는 중인가: 내 차례·예약이 아니고, 폴드했거나 이번 핸드에 카드가 없음."""
        if not isinstance(state, dict) or state.get("layout_ok") is False or not state.get("seats"):
            return False
        if state.get("to_act") or state.get("preaction"):
            return False
        return bool(state.get("hero_folded") or not state.get("hero_in_hand") or state.get("no_hand"))

    def candidates(self, state, live=None):
        """눌러 달라고 할 좌석 후보 (우선순위 순) [(key, pos, slot)]."""
        live = live or {}
        hero_pos = state.get("hero_pos")
        hi = POST_ORDER.index(hero_pos) if hero_pos in POST_ORDER else None
        rows = []
        for s in state.get("seats") or []:
            k = s.get("key")
            if not k or k == "hero" or not s.get("occupied") or self.known(k):
                continue
            in_hand = bool(s.get("in_hand") and not s.get("folded"))
            acts = live.get(k) or {}
            active = any((a.get("a") if isinstance(a, dict) else a) in VOLUNTARY
                         for st in ("preflop", "flop", "turn", "river") for a in (acts.get(st) or []))
            pos = s.get("pos")
            after = hi is not None and pos in POST_ORDER and POST_ORDER.index(pos) > hi
            rows.append(((not in_hand, not active, not after, k), k, pos, s.get("slot")))
        rows.sort(key=lambda r: r[0])
        return [(k, pos, slot) for _, k, pos, slot in rows]

    def shown_name(self, k, names=None, sure=None):
        """안내에 보여 줄 화면 닉네임 (메모리 전용 — 파일·로그·기록에 쓰지 않음). 확실하지 않으면 None.
        확실함: NameReader 가 확정했고 여러 전처리가 같은 글자로 읽었으며(sure 에 있음), 이 좌석에서 NAME_STABLE 프레임
        그대로 (observe). 잘렸거나 짧게 줄인 이름은 끝에 '…'."""
        if not sure or k not in sure:
            return None
        rec = self.seats.get(k)
        nm = (names or {}).get(k)
        if rec is None or rec.name is None or rec.hits < NAME_STABLE or not (nm and nm[0]):
            return None
        if (nm[0], bool(nm[1])) != rec.name:
            return None
        base = str(nm[0]).strip().rstrip(".…‥").strip()
        if len(base) < 2:
            return None
        cut = bool(nm[1])
        if len(base) > NAME_SHOW_MAX:
            base, cut = base[:NAME_SHOW_MAX], True
        return base + ("…" if cut else "")

    def prompt(self, state, live=None, t=None, names=None, sure=None):
        """지금 보여 줄 안내 {seat, pos, slot, text, sub} 또는 None (한 번에 한 좌석).
        닉네임이 확실하면 '테스트닉… 님을 눌러 주세요', 아니면 화면 위치로 '오른쪽 위 사람을 눌러 주세요'.
        아래 줄은 늘 '(오른쪽 위 · CO · 타입 확인)' — 화면 위치를 먼저."""
        try:
            if not self.idle(state):
                return None  # 안 보여 줄 뿐, 고른 좌석은 기억 (쉬는 때 다시 같은 사람)
            cands = self.candidates(state, live)
            if not cands:
                self.prompt_seat = None
                return None
            keys = [c[0] for c in cands]
            if self.prompt_seat not in keys:  # 처음 / 안내한 좌석이 해결됨·떠남 → 지금 1순위. 시간으로는 바꾸지 않음
                self.prompt_seat = keys[0]
            self.prompt_t = t
            k, pos, slot = next(c for c in cands if c[0] == self.prompt_seat)
            where = SLOT_KO.get(slot)
            sub = "(" + " · ".join(x for x in (where, pos, "타입 확인") if x) + ")"
            nm = self.shown_name(k, names, sure)
            if nm:
                text = f"{nm} 님을 눌러 주세요"
            elif where:
                text = f"{where} 사람을 눌러 주세요"
            elif pos:
                text = f"{pos} 자리 사람을 눌러 주세요"
            else:
                text = "상대 캐릭터를 눌러 주세요"
            return {"seat": k, "pos": pos, "slot": slot, "text": text, "sub": sub}
        except Exception:
            return None
