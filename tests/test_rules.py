import pytest

from voicecampaign import rules

HEADER = 'expert_name,phone_number,language,consent_status\n'


@pytest.mark.parametrize('raw,expected', [
    ('9876543210', '+919876543210'), ('98765 43210', '+919876543210'),
    ('09876543210', '+919876543210'), ('919876543210', '+919876543210'),
    ('+91-98765-43210', '+919876543210'), ('+14155550123', '+14155550123'),
    ('12345', None), ('5876543210', None), ('abcdefghij', None), ('', None),
])
def test_normalize_phone(raw, expected):
    assert rules.normalize_phone(raw, '91') == expected


def test_language_keys_and_aliases():
    assert rules.norm_language('  Hindi ') == 'hindi'
    assert rules.norm_language('TA') == 'tamil'
    assert rules.norm_language('Bangla') == 'bengali'
    assert rules.norm_language('') is None and rules.norm_language(None) is None


def test_consent():
    assert rules.has_consent('Yes') and rules.has_consent(' y ')
    assert not rules.has_consent('No') and not rules.has_consent('') and not rules.has_consent('maybe')


def test_parse_classifies_rows():
    csv_ = (HEADER + 'A,9876543210,Hindi,Yes\nB,9876543211,,Yes\nC,123,Tamil,Yes\n'
            'D,9876543213,Telugu,No\nE,9876543210,Hindi,Yes\nF,9876543215,Malayalam,yes\n\n')
    got = [r['eligibility'] for r in rules.parse_csv(csv_.encode())]
    assert got == ['ok', 'missing_language', 'invalid_phone', 'no_consent', 'duplicate', 'ok']


def test_non_consenting_row_does_not_block_later_consenting_duplicate():
    rows = rules.parse_csv((HEADER + 'A,9876543210,Hindi,No\nB,9876543210,Hindi,Yes\n').encode())
    assert [r['eligibility'] for r in rows] == ['no_consent', 'ok']


def test_headers_case_bom_and_missing_columns():
    rows = rules.parse_csv(('﻿Expert_Name , Phone_Number,LANGUAGE,Consent_Status\nA,9876543210,Hindi,Yes\n').encode())
    assert rows[0]['eligibility'] == 'ok'
    with pytest.raises(rules.CsvError, match='language'):
        rules.parse_csv(b'expert_name,phone_number,consent_status\nA,1,Yes\n')
    with pytest.raises(rules.CsvError):
        rules.parse_csv(HEADER.encode())
    with pytest.raises(rules.CsvError):
        rules.parse_csv(b'\xff\xfe\x00bad')
