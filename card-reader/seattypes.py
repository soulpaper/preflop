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
  - 확정된 닉네임이 기억과 다른 사람으로 CHANGE_READS 프레임 연속 읽힘 (잘린 이름의 접두사 / OCR 오타 정도 차이는 같은 사람)
  - 빈자리로 EMPTY_READS 프레임 연속 보임 (떠남 → 다음에 앉는 사람은 새 사람)
  - TABLE_MOVE_SEATS 자리 이상이 TABLE_MOVE_WINDOW 초 안에 바뀜 = 테이블 이동/합치기 → 모든 좌석 초기화
  - reset(): 새 [자동 지정] 세션 (앱이 track_new_session 에서 부름)
팝업 → 좌석: 팝업 닉네임과 같은 사람(위 규칙)인 좌석이 하나면 그 좌석. 없고 지금 눌러 달라고 한 좌석의 닉네임을 아직 못 읽었으면
  그 좌석 (사용자가 안내대로 눌렀다고 봄). 그 밖(모호/다른 좌석)이면 저장하지 않음.
안내 (prompt) — 앱은 아무것도 클릭하지 않는다. 사용자에게 어느 캐릭터를 눌러 달라고 큰 글씨로 한 자리씩만.
  - 내가 쉴 때만: 내 차례/예약(preaction)이 아니고, 폴드했거나 이번 핸드에 카드가 없을 때 (차례 시간 ~9.8초를 뺏지 않게)
  - 타입을 모르는 상대만 (한 번 읽으면 그 좌석이 초기화되기 전까지 다시 묻지 않음)
  - 우선순위: 아직 핸드에 남은 상대 → 이번 핸드에 자발적으로 칩을 넣은(루즈/액티브) 상대 → 나보다 뒤에 행동하는 상대 → 자리 순
  - 고른 좌석은 PROMPT_HOLD 초 동안 유지 (안내가 이리저리 바뀌지 않게)
"""
import difflib

STYLES_KO = {"LAG": "루즈-어그레시브", "LP": "루즈-패시브", "TAG": "타이트-어그레시브", "TP": "타이트-패시브"}
LOOSE = ("LAG", "LP")
CHANGE_READS = 3           # 다른 닉네임이 이만큼 연속으로 확정돼 읽히면 좌석 주인이 바뀐 것
EMPTY_READS = 2            # 빈자리가 이만큼 연속이면 떠난 것
SAME_RATIO = 0.75          # 닉네임 글자 유사도가 이 이상이면 같은 사람 (OCR 오타)
TABLE_MOVE_SEATS = 3       # 이 수 이상의 좌석이 짧은 시간에 바뀌면 테이블 이동/합치기
TABLE_MOVE_WINDOW = 20.0
NAME_SHOW_MAX = 8   # 안내에 보일 닉네임 최대 글자 수 (넘으면 … 로 줄임)
PROMPT_HOLD = 8.0          # 안내한 좌석을 바꾸지 않고 유지하는 시간 (초)
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
    __slots__ = ("name", "diff", "empty", "style", "stats", "src", "t", "popup_name")

    def __init__(self):
        self.name = None        # 메모리에서만: 지금 이 자리 사람의 닉네임 (name, truncated)
        self.diff = 0           # 다른 사람 닉네임이 연속으로 읽힌 프레임 수
        self.empty = 0          # 빈자리로 보인 연속 프레임 수
        self.style = None       # 'LAG'/'LP'/'TAG'/'TP' (팝업) 또는 None
        self.stats = None       # 팝업 통계 {vpip, pfr, ats, threebet, tab}
        self.src = None         # 'popup' 이면 팝업을 읽었음 (style 을 못 읽었어도 다시 묻지 않음)
        self.t = None
        self.popup_name = None  # 메모리에서만: 팝업에서 읽은 전체 닉네임 (좌석 매칭용)

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
        self.prompt_seat, self.prompt_t = None, None
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
    def observe(self, names, state, t=None):
        """names {seat: (name, truncated)} (확정만) + tablestate 결과 → 이번에 초기화한 좌석 목록.
        테이블 이동으로 보이면 전체를 초기화하고 ['*'] 반환."""
        dropped = []
        try:
            if not isinstance(state, dict) or state.get("layout_ok") is False or not state.get("seats"):
                return dropped
            names = names or {}
            for s in state["seats"]:
                k = s.get("key") if isinstance(s, dict) else None
                if not k or k == "hero":
                    continue
                rec = self.seats.get(k)
                if not s.get("occupied"):
                    if rec is not None:
                        rec.empty += 1
                        if rec.empty >= EMPTY_READS and self._drop(k, t):
                            dropped.append(k)
                    continue
                nm = names.get(k)
                if rec is None:
                    if nm and nm[0]:
                        rec = self.seats[k] = _Seat()
                        rec.name = (nm[0], bool(nm[1]))
                    continue
                rec.empty = 0
                if not nm or not nm[0]:
                    continue  # 이번 프레임에 확정된 이름 없음: 판단 보류
                nm = (nm[0], bool(nm[1]))
                if rec.name is None or self._same(rec, nm):
                    if rec.name is None or (rec.name[1] and not nm[1]):
                        rec.name = nm          # 잘리지 않은 이름이 더 정확
                    rec.diff = 0
                    continue
                rec.diff += 1
                if rec.diff >= CHANGE_READS and self._drop(k, t):
                    dropped.append(k)
                    new = self.seats[k] = _Seat()
                    new.name = nm
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

    @staticmethod
    def _same(rec, nm):
        return same_person(rec.name, nm) or (rec.popup_name is not None and same_person(rec.popup_name, nm))

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
            cands = set()
            for k, nm in names.items():
                if k != "hero" and nm and nm[0] and same_person(nm, pname):
                    cands.add(k)
            for k, rec in self.seats.items():
                if rec.name is not None and same_person(rec.name, pname) and not names.get(k):
                    cands.add(k)  # 이번 프레임 이름은 없지만 기억한 이름이 같음
            if len(cands) > 1:  # 정확히 같은 이름이 하나면 그것
                exact = {k for k in cands if _norm((names.get(k) or (self.seats[k].name if k in self.seats else None)
                                                    or ("",))[0]) == _norm(pname)}
                cands = exact if len(exact) == 1 else set()
            key = next(iter(cands)) if len(cands) == 1 else None
            if key is None and not cands and self.prompt_seat and not (names.get(self.prompt_seat) or (None,))[0]:
                key = self.prompt_seat  # 안내한 좌석의 이름을 못 읽음: 안내대로 눌렀다고 봄
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
            rec.diff = 0
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

    def shown_name(self, k, names=None):
        """안내에 보여 줄 화면 닉네임 (메모리 전용 — 파일·로그·기록에 쓰지 않음). 없으면 None.
        잘렸거나 짧게 줄인 이름은 끝에 '…'."""
        nm = (names or {}).get(k)
        if not (nm and nm[0]):
            rec = self.seats.get(k)
            nm = rec.name if rec is not None else None
        if not (nm and str(nm[0]).strip()):
            return None
        base = str(nm[0]).strip().rstrip(".…‥").strip()
        if not base:
            return None
        cut = bool(nm[1])
        if len(base) > NAME_SHOW_MAX:
            base, cut = base[:NAME_SHOW_MAX], True
        return base + ("…" if cut else "")

    def prompt(self, state, live=None, t=None, names=None):
        """지금 보여 줄 안내 {seat, pos, slot, text, sub} 또는 None (한 번에 한 좌석).
        이름을 알면 '테스트닉… 님을 눌러 주세요' + '(CO · 오른쪽 위 · 타입 확인)', 모르면 '자리' 문구."""
        try:
            if not self.idle(state):
                self.prompt_seat = None
                return None
            cands = self.candidates(state, live)
            if not cands:
                self.prompt_seat = None
                return None
            keys = [c[0] for c in cands]
            hold = (self.prompt_seat in keys and t is not None and self.prompt_t is not None
                    and t - self.prompt_t < PROMPT_HOLD)
            if not hold:  # 처음 / 안내한 좌석이 해결됨 / 유지 시간이 지남 → 지금 1순위 (같으면 시간만 새로)
                self.prompt_seat, self.prompt_t = keys[0], t
            k, pos, slot = next(c for c in cands if c[0] == self.prompt_seat)
            where = SLOT_KO.get(slot)
            nm = self.shown_name(k, names)
            if nm:
                sub = " · ".join(x for x in (pos, where, "타입 확인") if x)
                return {"seat": k, "pos": pos, "slot": slot, "text": f"{nm} 님을 눌러 주세요", "sub": f"({sub})"}
            who = f"{pos} 자리" if pos else (f"{where} 자리" if where else "상대")
            sub = "타입 확인" + (f" · {where}" if pos and where else "")
            return {"seat": k, "pos": pos, "slot": slot, "text": f"{who} 캐릭터를 눌러 주세요", "sub": f"({sub})"}
        except Exception:
            return None
