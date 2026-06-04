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
