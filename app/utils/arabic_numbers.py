# Legacy lists — used by num_to_arabic_words (0–999 grade scores in student records)
_ONES = [
    '', 'واحد', 'اثنان', 'ثلاثة', 'أربعة', 'خمسة', 'ستة', 'سبعة', 'ثمانية', 'تسعة',
    'عشرة', 'أحد عشر', 'اثنا عشر', 'ثلاثة عشر', 'أربعة عشر', 'خمسة عشر',
    'ستة عشر', 'سبعة عشر', 'ثمانية عشر', 'تسعة عشر',
]
_TENS = ['', '', 'عشرون', 'ثلاثون', 'أربعون', 'خمسون', 'ستون', 'سبعون', 'ثمانون', 'تسعون']
_HUNDREDS = [
    '', 'مئة', 'مئتان', 'ثلاثمئة', 'أربعمئة', 'خمسمئة',
    'ستمئة', 'سبعمئة', 'ثمانمئة', 'تسعمئة',
]


def num_to_arabic_words(value) -> str:
    """Convert an integer (0–999) to Arabic written form.

    Used by the student registration record PDF for grade scores.
    Returns empty string for empty / None / invalid input.
    """
    if value is None or value == '':
        return ''
    try:
        n = int(value)
    except (ValueError, TypeError):
        return ''
    if n < 0:
        return ''
    if n == 0:
        return 'صفر'
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] if o == 0 else f'{_ONES[o]} و{_TENS[t]}'
    if n < 1000:
        h, rem = divmod(n, 100)
        if rem == 0:
            return _HUNDREDS[h]
        return f'{_HUNDREDS[h]} و{num_to_arabic_words(rem)}'
    return ''


# ── Iraqi Dinar amount-to-words ───────────────────────────────────────────────
# Uses مائة spelling (more common in Iraqi formal documents).
# Handles 0 through 999,999,999,999 (hundreds of billions).

_IQD_ONES = [
    '', 'واحد', 'اثنان', 'ثلاثة', 'أربعة', 'خمسة', 'ستة', 'سبعة', 'ثمانية', 'تسعة',
    'عشرة', 'أحد عشر', 'اثنا عشر', 'ثلاثة عشر', 'أربعة عشر', 'خمسة عشر',
    'ستة عشر', 'سبعة عشر', 'ثمانية عشر', 'تسعة عشر',
]
_IQD_TENS = ['', '', 'عشرون', 'ثلاثون', 'أربعون', 'خمسون', 'ستون', 'سبعون', 'ثمانون', 'تسعون']
_IQD_HUNDREDS = [
    '', 'مائة', 'مائتان', 'ثلاثمائة', 'أربعمائة', 'خمسمائة',
    'ستمائة', 'سبعمائة', 'ثمانمائة', 'تسعمائة',
]


def _iqd_below1000(n: int) -> str:
    """Convert 1–999 to Arabic words (no currency). Returns '' for 0."""
    if n == 0:
        return ''
    if n < 20:
        return _IQD_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _IQD_TENS[t] if o == 0 else f'{_IQD_ONES[o]} و{_IQD_TENS[t]}'
    h, r = divmod(n, 100)
    hw = _IQD_HUNDREDS[h]
    return hw if r == 0 else f'{hw} و{_iqd_below1000(r)}'


def _iqd_group(count: int, has_lower: bool,
               singular: str, dual_acc: str, dual_nom: str,
               plural_3_10: str, plural_11plus: str) -> str:
    """Return Arabic text for count × one scale level (thousand / million / billion).

    dual_acc: dual construct form used when no smaller group follows (ألفا, مليونا)
    dual_nom: dual independent form used when smaller groups follow (ألفان, مليونان)
    """
    if count == 1:
        return singular
    if count == 2:
        return dual_nom if has_lower else dual_acc
    if count <= 10:
        return f'{_IQD_ONES[count]} {plural_3_10}'
    # 11+: count in words + singular scale word
    return f'{_iqd_below1000(count)} {plural_11plus}'


def _iqd_int_to_words(n: int) -> str:
    """Convert a positive integer to Arabic number words without a currency suffix."""
    parts = []

    billions  = n // 1_000_000_000;  n %= 1_000_000_000
    millions  = n // 1_000_000;      n %= 1_000_000
    thousands = n // 1_000;          n %= 1_000
    remainder = n

    has_lo_b = millions > 0 or thousands > 0 or remainder > 0
    has_lo_m = thousands > 0 or remainder > 0
    has_lo_t = remainder > 0

    if billions:
        parts.append(_iqd_group(billions, has_lo_b,
                                'مليار', 'مليارا', 'ملياران',
                                'مليارات', 'مليار'))
    if millions:
        parts.append(_iqd_group(millions, has_lo_m,
                                'مليون', 'مليونا', 'مليونان',
                                'ملايين', 'مليون'))
    if thousands:
        parts.append(_iqd_group(thousands, has_lo_t,
                                'ألف', 'ألفا', 'ألفان',
                                'آلاف', 'ألف'))
    if remainder:
        parts.append(_iqd_below1000(remainder))

    return ' و'.join(parts)


def amount_to_words_iqd(value) -> str:
    """Convert a non-negative integer amount to Iraqi Dinar written words.

    Returns '' for None, empty, negative, or non-numeric input.
    Decimal parts are truncated (no fils wording).

    Examples:
        5        → خمسة دنانير عراقية
        1000     → ألف دينار عراقي
        2000     → ألفا دينار عراقي
        2500000  → مليونان وخمسمائة ألف دينار عراقي
    """
    if value is None or value == '':
        return ''
    try:
        n = int(float(value))
    except (ValueError, TypeError):
        return ''
    if n < 0:
        return ''
    if n == 0:
        return 'صفر دينار عراقي'
    if n == 1:
        return 'دينار عراقي واحد'
    if n == 2:
        return 'ديناران عراقيان'
    words = _iqd_int_to_words(n)
    if 3 <= n <= 10:
        return f'{words} دنانير عراقية'
    return f'{words} دينار عراقي'
