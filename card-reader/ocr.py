"""화면의 팟/스택 숫자 읽기 (Windows 내장 OCR, 한국어).

게임 표기 예: '194.6BB', '0BB', '84만 7500', '13만'.
작은 글씨는 오인식이 잦아서 전처리 3가지로 읽은 뒤 다수결로 고른다.
"만"은 '안/한/인/민'처럼 다른 한글로 읽히는 경우가 많아, 화면 전체의 표기 단위(BB / 만)를
기준으로 애매한 글자를 해석한다.
"""
import asyncio
import re
from collections import Counter

import cv2

try:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
except ImportError:  # OCR 미지원 환경
    OcrEngine = None

TARGET_HEIGHT = 60  # 이 높이로 키워서 읽으면 작은 글씨 인식률이 크게 오름
HANGUL = re.compile(r"[가-힣]")


def available():
    return OcrEngine is not None and _engine() is not None


_ENGINE = []


def _engine():
    if not _ENGINE:
        engine = None
        if OcrEngine is not None:
            engine = OcrEngine.try_create_from_language(Language("ko")) or \
                OcrEngine.try_create_from_user_profile_languages()
        _ENGINE.append(engine)
    return _ENGINE[0]


def _bitmap(img):
    bgra = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA if img.ndim == 2 else cv2.COLOR_BGR2BGRA)
    h, w = bgra.shape[:2]
    writer = DataWriter()
    writer.write_bytes(bytes(bgra.tobytes()))
    return SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h,
                                                  BitmapAlphaMode.PREMULTIPLIED)


def _variants(img):
    scale = TARGET_HEIGHT / max(1, img.shape[0])
    big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binary = cv2.copyMakeBorder(255 - binary, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    return [big, 255 - gray, binary]


async def _read_all(crops):
    engine = _engine()
    out = {}
    for key, img in crops.items():
        texts = []
        for v in _variants(img):
            result = await engine.recognize_async(_bitmap(v))
            texts.append(" ".join(line.text for line in result.lines))
        out[key] = texts
    return out


def read_texts(crops):
    """{key: 이미지} → {key: [전처리별 OCR 문자열 3개]}. 백그라운드 스레드에서 호출."""
    return asyncio.run(_read_all(crops))


# --- 문자열 → 숫자 -------------------------------------------------------------
def _clean(text):
    t = text.replace(" ", "").replace(",", "")
    t = re.sub(r"[Oo](?=[\d.]*B)|(?<=\d)[Oo]|[Oo](?=\d)", "0", t)  # 0을 O로 읽은 경우
    return t


def candidates(text):
    """OCR 문자열 하나 → (값, 단위, 정확히 읽힘 여부). 해석 불가면 None."""
    t = _clean(text)
    m = re.search(r"(\d+(?:\.\d+)?)BB", t)
    if m:
        return float(m.group(1)), "BB", True
    m = re.search(r"(\d+)만(\d{1,4})?", t)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2) or 0), "만", True
    m = re.search(r"(\d+(?:\.\d+)?)([^\d.]*)(\d{1,4})?", t)
    if not m:
        return None
    number, junk, tail = m.group(1), m.group(2), m.group(3)
    if not junk:
        return float(number), None, True
    # 애매한 글자: 한글이면 '만'을, 아니면 'BB'를 잘못 읽은 것으로 추정 (단위 힌트로 최종 결정)
    guess = "만" if HANGUL.search(junk) and "." not in number else "BB"
    return (float(number), tail, guess), "?", False


def resolve(texts, unit_hint=None):
    """전처리별 문자열들 → (값, 단위). 다수결 (정확히 읽힌 값 가중치 2)."""
    votes = Counter()
    exact = set()
    for text in texts:
        c = candidates(text)
        if c is None:
            continue
        value, unit, is_exact = c
        if not is_exact:
            number, tail, guess = value
            unit = unit_hint or guess
            if unit == "만":
                value = int(number) * 10000 + int(tail or 0)
            else:
                value = number
        key = (value, unit)
        votes[key] += 2 if is_exact else 1
        if is_exact:
            exact.add(key)
    if not votes:
        return None
    return max(votes, key=lambda k: (votes[k], k in exact))


def unit_hint(all_texts):
    """화면 전체에서 정확히 읽힌 단위의 다수 (BB / 만)."""
    units = Counter()
    for texts in all_texts:
        for text in texts:
            c = candidates(text)
            if c and c[2] and c[1] in ("BB", "만"):
                units[c[1]] += 1
    return units.most_common(1)[0][0] if units else None


def format_value(value, unit):
    if unit == "BB":
        return f"{value:g}BB"
    if unit == "만":
        man, rest = divmod(int(value), 10000)
        return f"{man}만" + (f" {rest}" if rest else "") if man else f"{rest}"
    return f"{value:g}"
