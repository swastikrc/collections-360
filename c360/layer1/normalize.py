"""Standardisation macros shared by the curated layer and identity resolution.

Every source system stores names, dates of birth, phones and postal codes in its own
format. These DuckDB macros turn each one into a single comparable form.
"""

MACROS = r"""
-- Letters only, lower case, accents stripped: "St-Jean" -> "stjean", "Aurélie" -> "aurelie"
CREATE OR REPLACE MACRO norm_name(s) AS
    nullif(regexp_replace(lower(strip_accents(trim(s))), '[^a-z]', '', 'g'), '');

-- Any date string in the formats seen across sources -> DATE.
-- Order matters for ambiguous slashes: ISO first, then the explicit per-system formats.
CREATE OR REPLACE MACRO parse_date_any(s) AS coalesce(
    try_strptime(trim(s), '%Y-%m-%d'),
    try_strptime(trim(s), '%Y/%m/%d'),
    try_strptime(trim(s), '%Y%m%d'),
    try_strptime(trim(s), '%d/%m/%Y'),
    try_strptime(trim(s), '%m/%d/%Y'),
    try_strptime(trim(s), '%d-%m-%Y'),
    try_strptime(trim(s), '%b %d %Y'),
    try_strptime(trim(s), '%d %b %Y'),
    try_strptime(trim(s), '%Y-%m-%d %H:%M:%S'),
    try_strptime(trim(s), '%Y-%m-%dT%H:%M:%S')
)::DATE;

CREATE OR REPLACE MACRO parse_ts_any(s) AS coalesce(
    try_strptime(trim(s), '%Y-%m-%d %H:%M:%S'),
    try_strptime(trim(s), '%Y-%m-%dT%H:%M:%S'),
    try_strptime(trim(s), '%Y-%m-%d %H:%M:%S.%f'),
    try_strptime(trim(s), '%Y-%m-%d'),
    try_strptime(trim(s), '%d/%m/%Y %H:%M'),
    try_strptime(trim(s), '%m/%d/%Y %H:%M')
);

-- Phone -> 10 digits (drops +1 country code). Masked card phones keep their 'x'.
CREATE OR REPLACE MACRO phone_raw_digits(s) AS nullif(regexp_replace(lower(s), '[^0-9x]', '', 'g'), '');
CREATE OR REPLACE MACRO phone_digits(s) AS CASE
    WHEN length(phone_raw_digits(s)) = 11 AND starts_with(phone_raw_digits(s), '1') THEN substr(phone_raw_digits(s), 2)
    ELSE phone_raw_digits(s) END;
CREATE OR REPLACE MACRO phone10(s) AS
    CASE WHEN regexp_full_match(phone_digits(s), '[0-9]{10}') THEN phone_digits(s) END;
-- Comparable key that also works for masked numbers such as 672993xxx02: first 6 + last 2
CREATE OR REPLACE MACRO phone_mask_key(s) AS
    CASE WHEN regexp_full_match(phone_digits(s), '[0-9]{10}|[0-9]{6}x+[0-9]{2}')
         THEN left(phone_digits(s), 6) || right(phone_digits(s), 2) END;

-- Canadian postal code -> "A1A1A1"
CREATE OR REPLACE MACRO postal6(s) AS
    CASE WHEN regexp_full_match(upper(regexp_replace(s, '[^A-Za-z0-9]', '', 'g')), '[A-Z][0-9][A-Z][0-9][A-Z][0-9]')
         THEN upper(regexp_replace(s, '[^A-Za-z0-9]', '', 'g')) END;

-- Province/territory -> 2-letter code. Unknown labels fall back to the postal code's first letter
CREATE OR REPLACE MACRO province_std(p, pc) AS coalesce(
    CASE upper(regexp_replace(trim(p), '[^A-Za-z]', '', 'g'))
        WHEN 'ON' THEN 'ON' WHEN 'ONT' THEN 'ON' WHEN 'ONTARIO' THEN 'ON'
        WHEN 'QC' THEN 'QC' WHEN 'PQ' THEN 'QC' WHEN 'QUE' THEN 'QC' WHEN 'QUEBEC' THEN 'QC'
        WHEN 'BC' THEN 'BC' WHEN 'BRITISHCOLUMBIA' THEN 'BC'
        WHEN 'AB' THEN 'AB' WHEN 'ALTA' THEN 'AB' WHEN 'ALBERTA' THEN 'AB'
        WHEN 'MB' THEN 'MB' WHEN 'MAN' THEN 'MB' WHEN 'MANITOBA' THEN 'MB'
        WHEN 'SK' THEN 'SK' WHEN 'SASK' THEN 'SK' WHEN 'SASKATCHEWAN' THEN 'SK'
        WHEN 'NS' THEN 'NS' WHEN 'NOVASCOTIA' THEN 'NS'
        WHEN 'NB' THEN 'NB' WHEN 'NEWBRUNSWICK' THEN 'NB'
        WHEN 'NL' THEN 'NL' WHEN 'NFLD' THEN 'NL' WHEN 'NEWFOUNDLAND' THEN 'NL'
        WHEN 'PE' THEN 'PE' WHEN 'PEI' THEN 'PE'
        WHEN 'NT' THEN 'NT' WHEN 'NWT' THEN 'NT' WHEN 'YT' THEN 'YT' WHEN 'YUKON' THEN 'YT' WHEN 'NU' THEN 'NU'
    END,
    CASE left(upper(trim(pc)), 1)
        WHEN 'A' THEN 'NL' WHEN 'B' THEN 'NS' WHEN 'C' THEN 'PE' WHEN 'E' THEN 'NB'
        WHEN 'G' THEN 'QC' WHEN 'H' THEN 'QC' WHEN 'J' THEN 'QC'
        WHEN 'K' THEN 'ON' WHEN 'L' THEN 'ON' WHEN 'M' THEN 'ON' WHEN 'N' THEN 'ON' WHEN 'P' THEN 'ON'
        WHEN 'R' THEN 'MB' WHEN 'S' THEN 'SK' WHEN 'T' THEN 'AB' WHEN 'V' THEN 'BC' WHEN 'Y' THEN 'YT'
    END);

-- Core-banking CIF: 10 digits with leading zeros (some extracts dropped them)
CREATE OR REPLACE MACRO cif10(s) AS
    CASE WHEN regexp_full_match(trim(s), '[0-9]{1,10}') THEN lpad(trim(s), 10, '0') END;

-- Delinquency bucket recomputed from days past due (the stored bucket drifts in ~4% of rows)
CREATE OR REPLACE MACRO dpd_bucket_from_dpd(d) AS CASE
    WHEN d IS NULL THEN NULL
    WHEN d <= 0 THEN 'current'
    WHEN d <= 30 THEN '1-30'
    WHEN d <= 60 THEN '31-60'
    WHEN d <= 90 THEN '61-90'
    WHEN d <= 120 THEN '91-120'
    WHEN d <= 150 THEN '121-150'
    WHEN d <= 180 THEN '151-180'
    ELSE '180+' END;

-- Fellegi-Sunter comparison levels used in identity resolution -----------------------
-- Surname: 3 exact, 2 very close (typo), 1 loosely similar, 0 different, -1 missing
CREATE OR REPLACE MACRO cmp_surname(a, b) AS CASE
    WHEN a IS NULL OR b IS NULL THEN -1
    WHEN a = b THEN 3
    WHEN jaro_winkler_similarity(a, b) >= 0.92 OR damerau_levenshtein(a, b) <= 1 THEN 2
    WHEN jaro_winkler_similarity(a, b) >= 0.80 OR contains(a, b) OR contains(b, a) THEN 1
    ELSE 0 END;

-- Given name. Sources often hold only an initial, so an initial agreeing is its own level.
-- 3 exact or nickname-prefix, 2 close, 1 initial agrees, 0 different, -1 missing
CREATE OR REPLACE MACRO cmp_given(a_full, a_init, b_full, b_init) AS CASE
    WHEN coalesce(a_full, a_init) IS NULL OR coalesce(b_full, b_init) IS NULL THEN -1
    WHEN a_full IS NOT NULL AND b_full IS NOT NULL AND
         (a_full = b_full OR (length(a_full) >= 3 AND length(b_full) >= 3 AND
          (starts_with(a_full, b_full) OR starts_with(b_full, a_full)))) THEN 3
    WHEN a_full IS NOT NULL AND b_full IS NOT NULL AND jaro_winkler_similarity(a_full, b_full) >= 0.88 THEN 2
    WHEN left(coalesce(a_full, a_init), 1) = left(coalesce(b_full, b_init), 1) THEN 1
    ELSE 0 END;

-- DOB: 3 exact, 2 day/month transposed, 1 one keying error (single digit), 0 different, -1 missing
CREATE OR REPLACE MACRO cmp_dob(a, b) AS CASE
    WHEN a IS NULL OR b IS NULL THEN -1
    WHEN a = b THEN 3
    WHEN year(a) = year(b) AND month(a) = day(b) AND day(a) = month(b) THEN 2
    WHEN levenshtein(strftime(a, '%Y%m%d'), strftime(b, '%Y%m%d')) <= 1 THEN 1
    ELSE 0 END;

-- Phone: 2 full number equal, 1 masked-compatible (first 6 + last 2), 0 different, -1 missing
CREATE OR REPLACE MACRO cmp_phone(a10, amask, b10, bmask) AS CASE
    WHEN amask IS NULL OR bmask IS NULL THEN -1
    WHEN a10 IS NOT NULL AND b10 IS NOT NULL AND a10 = b10 THEN 2
    WHEN amask = bmask THEN 1
    ELSE 0 END;

-- Postal: 2 exact, 1 same FSA, 0 different, -1 missing
CREATE OR REPLACE MACRO cmp_postal(a, b) AS CASE
    WHEN a IS NULL OR b IS NULL THEN -1
    WHEN a = b THEN 2
    WHEN left(a, 3) = left(b, 3) THEN 1
    ELSE 0 END;
"""


def install(con):
    for stmt in MACROS.split(";"):
        if stmt.strip():
            con.execute(stmt)
