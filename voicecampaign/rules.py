"""Pure CSV/phone/language/consent rules — no DB, no network."""
import csv
import io
import os
import re

REQUIRED_COLUMNS = ('expert_name', 'phone_number', 'language', 'consent_status')
MAX_ROWS = 5000
DEFAULT_LANGUAGES = ['Hindi', 'English', 'Tamil', 'Telugu', 'Malayalam', 'Kannada', 'Bengali', 'Marathi']

_ALIASES = {
    'hi': 'hindi', 'en': 'english', 'ta': 'tamil', 'te': 'telugu',
    'ml': 'malayalam', 'kn': 'kannada', 'bn': 'bengali', 'mr': 'marathi',
    'bangla': 'bengali',
}
_YES = {'yes', 'y', 'true', '1', 'consented', 'granted', 'opt-in', 'optin', 'opted in'}

# Why a row can't be called, in the order they are checked.
FLAG_LABELS = {
    'ready': 'Ready',
    'missing_language': 'Language missing',
    'invalid_phone': 'Invalid phone number',
    'no_consent': 'No consent',
    'duplicate': 'Duplicate number in this campaign',
    'no_recording': 'Recording not uploaded',
}


class CsvError(ValueError):
    pass


def norm_language(raw):
    """Canonical key for a language ('  Hindi ', 'HI' -> 'hindi'); None if blank."""
    key = re.sub(r'\s+', ' ', (raw or '').strip()).casefold()
    if not key:
        return None
    return _ALIASES.get(key, key)


def display_language(raw):
    return re.sub(r'\s+', ' ', (raw or '').strip()).title()


def normalize_phone(raw, default_cc=None):
    """Returns E.164 (+919876543210) or None if it isn't a plausible number."""
    cc = default_cc or os.environ.get('DEFAULT_COUNTRY_CODE', '91')
    s = re.sub(r'[\s\-().]', '', raw or '')
    if not s:
        return None
    if s.startswith('00'):
        s = '+' + s[2:]
    if s.startswith('+'):
        digits = s[1:]
        if digits.isdigit() and 8 <= len(digits) <= 15 and digits[0] != '0':
            return '+' + digits
        return None
    if not s.isdigit():
        return None
    if cc == '91':
        if len(s) == 10 and s[0] in '6789':
            return '+91' + s
        if len(s) == 11 and s[0] == '0' and s[1] in '6789':
            return '+91' + s[1:]
        if len(s) == 12 and s.startswith('91') and s[2] in '6789':
            return '+' + s
        return None
    if 8 <= len(s) <= 12:
        return '+' + cc + s.lstrip('0')
    return None


def has_consent(raw):
    return (raw or '').strip().casefold() in _YES


def parse_csv(data: bytes, default_cc=None):
    """Parses and classifies an upload. Returns a list of dicts ready to insert.
    Raises CsvError for an unusable file (not for individual bad rows — those
    are kept and flagged so they show up in the preview)."""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise CsvError('The file is not UTF-8 text. Export the sheet as "CSV UTF-8".')
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvError('The CSV is empty.')
    fieldmap = {(f or '').strip().casefold(): f for f in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldmap]
    if missing:
        raise CsvError('Missing column(s): ' + ', '.join(missing) +
                       '. Expected: ' + ', '.join(REQUIRED_COLUMNS) + '.')

    rows, seen = [], set()
    for line_no, rec in enumerate(reader, start=2):
        get = lambda c: (rec.get(fieldmap[c]) or '').strip()
        if not any((v or '').strip() for v in rec.values() if isinstance(v, str)):
            continue  # fully blank line
        if len(rows) >= MAX_ROWS:
            raise CsvError(f'Too many rows (limit {MAX_ROWS}). Split the file.')
        name, phone_raw = get('expert_name'), get('phone_number')
        lang_raw, consent_raw = get('language'), get('consent_status')
        lang_key = norm_language(lang_raw)
        phone = normalize_phone(phone_raw, default_cc)

        if not lang_key:
            flag = 'missing_language'
        elif not phone:
            flag = 'invalid_phone'
        elif not has_consent(consent_raw):
            flag = 'no_consent'
        elif phone in seen:
            flag = 'duplicate'
        else:
            flag = 'ok'
            seen.add(phone)
        rows.append({
            'line_no': line_no, 'expert_name': name, 'phone_raw': phone_raw,
            'phone_e164': phone, 'language_raw': lang_raw, 'language_key': lang_key,
            'consent_raw': consent_raw, 'eligibility': flag,
        })
    if not rows:
        raise CsvError('The CSV has a header but no rows.')
    return rows
