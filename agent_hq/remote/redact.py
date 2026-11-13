"""Redaction of log text before it leaves the Mac (#412).

The remote tools return full log detail, with tokens, API keys, cookies, URL
query strings and account numbers beyond the last four digits removed here
first. Everything else in a log line is returned as written.

This is pattern matching, so it errs towards over-masking. What can still pass:
a secret in an unknown format with no sensitive name beside it, when it is
shorter than 32 characters, all letters, or split into short hyphenated parts.

Used only by the remote tools; the web tabs show logs unredacted to the owner.

Log lines reach 64 KiB with no spaces, so every pattern anchors on a lookbehind
and bounds its name lengths: a long word costs one scan, not one per position.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[redacted]"

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-9;?]*[ -/]*[@-~]?|\x1b[@-_]?")
# Everything below space except tab and newline, DEL, C1 controls, the bidi
# overrides that can make text read differently from what it says, and lone
# surrogates, which can't be encoded and would fail the whole result. Built from
# code points so the source holds no invisible characters.
_CONTROL = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    f"{chr(0x202A)}-{chr(0x202E)}{chr(0x2066)}-{chr(0x2069)}{chr(0xD800)}-{chr(0xDFFF)}]"
)

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*KEY[A-Z0-9 ]*-----.*?(?:-----END [A-Z0-9 ]*-----|\Z)", re.DOTALL
)
_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|cookies?|set-cookie)\\?[\"']?\s*[:=]\s*[^\n]*",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(bearer)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_URL = re.compile(r"(?<![a-zA-Z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s\"'<>?#]+(?:[?#]\S*)?")
# Userinfo whose password holds an unencoded "/", "?", "#" or "@", which _URL would stop
# at: everything up to the last "@" of the URL goes. _strip_userinfo spares a host and
# port before an "@" in the path, so "x.dev:8443/@scope" stays.
_URL_USERINFO = re.compile(
    r"(?<![a-zA-Z0-9+.-])([a-zA-Z][a-zA-Z0-9+.-]{0,31}://)"
    r"([^\s/?#@:\"'<>]{0,256}):([^\s\"]{0,256})@"
)
# A port, then a path ending at the "@", so the "@" starts a path segment.
_PORT_THEN_PATH = re.compile(r"\d{1,5}/(?:[^:]*/)?")
# A query string on anything else: a relative path, host:port, a bare domain.
_QUERY = re.compile(r"\?[^\s=&?]{1,64}=\S*")
_BARE_URL_QUERY = re.compile(
    r"(?<![\w./-])((?:[A-Za-z0-9-]{1,63}\.){1,16}[A-Za-z]{2,}(?:/[^\s\"'<>?#]*)?)\?\S+"
)

_SENSITIVE = (
    r"token|secret[-_ ]?key|secret|passw(?:or)?d|passphrase|pwd|api[-_ ]?key|access[-_ ]?key"
    r"|cookies?|authorization|credentials?|private[-_ ]?key|signature"
)
# Too short to match inside other words: "author", "inside", "design".
_WHOLE_NAME = r"auth|session(?:[-_]?id)?|sid|sig"
_SENSITIVE_KEY = re.compile(rf"{_SENSITIVE}|^(?:{_WHOLE_NAME})$", re.IGNORECASE)
_COUNT_NAME = re.compile(r"(?:tokens|token[-_]?count)\W*$", re.IGNORECASE)
_NAME = rf"(?:[\w.-]{{0,64}}(?:{_SENSITIVE})[\w.-]{{0,64}}|(?:{_WHOLE_NAME}))"
_QUOTED = r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`[^`\n]*`"
_ESCAPED_QUOTED = r"\\\"(?:(?!\\\")[^\n])*\\\""
# A password may hold spaces or "&", so everything to the end of the line goes, unless the
# value is properly quoted: its closing quote must be followed by a delimiter.
_PASSWORD_LINE = re.compile(
    r"(?<![\w.-])([\w.-]{0,64}(?:passw(?:or)?d|passphrase|pwd)[\w.-]{0,64})"
    rf"(\s*[:=])(?!\s*(?:{_QUOTED}|{_ESCAPED_QUOTED})\s*(?:[,;}}\])]|$))(\s*)[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)
_NAME_VALUE = re.compile(
    rf"(?<![\w.-])(?P<name>\\?[\"'`]?{_NAME}\\?[\"'`]?)"
    r"(?P<sep>\s*(?:=>|:=|==|[:=])\*{0,2}\s*)"
    # A quoted value, plus any word joined to its closing quote ("abc"def is one word).
    r"(?P<val>(?:\\\"(?:(?!\\\")[^\n])*(?:\\\")?"
    r"|\"(?:[^\"\\\n]|\\.)*\"?"
    r"|'(?:[^'\\\n]|\\.)*'?"
    r"|`[^`\n]*`?)[^\s,;&}\])]*"
    # An unquoted value ends at the next quoted field, or it would swallow that
    # field's name and leave the rest of its value behind.
    r"|(?:(?!,\s*\\?[\"'`][\w. -]{1,64}\\?[\"'`]\s*[:=])[^\s&])+)",
    re.IGNORECASE,
)
# A list or object under a secret name; _mask_structured finds where it ends.
_STRUCTURED = re.compile(
    rf"(?<![\w.-])(\\?[\"'`]?{_NAME}\\?[\"'`]?\s*(?:=>|:=|==|[:=])\*{{0,2}}\s*)"
    r"[\[{](?!redacted\])",
    re.IGNORECASE,
)
_CLOSER_FOR = {"[": "]", "{": "}"}
_QUOTE_CHARS = "\"'`"
_ESCAPED_QUOTES = ('\\"', "\\'", "\\`")
_ONLY_A_MASK = re.compile(r"\[redacted\][,;}\])]*")
_FLAG = re.compile(
    rf"(?<![\w-])(--?[\w-]{{0,64}}(?:{_SENSITIVE})[\w-]{{0,64}}|--?(?:{_WHOLE_NAME}))"
    # After whitespace, a leading "-" is the next flag; after "=" it is the value.
    r"(\s+(?!-)|=)"
    r"(?:(?:\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?|`[^`\n]*`?)\S*|\S+)",
    re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|xox[a-z]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)?)"
)
_OPAQUE_RUN = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{32,}(?![A-Za-z0-9+/_-])")
_SEGMENT_SEP = re.compile(r"[-_/]")

_SPACED_DIGITS = re.compile(r"(?<![\w.,-])\d{3,}(?:[ \t\xa0]\d{3,}){2,}(?![\w-])")
_HYPHENATED = re.compile(r"(?<![0-9A-Za-z-])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A segment of an account number: digits, optionally after one letter (Z12-345678).
_NUMBER_SEGMENT = re.compile(r"[A-Za-z]?\d+")
_DOTTED_DIGITS = re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}(?!\w)")
_IP = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_DOTTED_DATE = re.compile(r"\d{4}\.\d{2}\.\d{2}")
_DIGIT_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z]*\d{8,}[0-9A-Za-z]*")
_LONG_DIGITS = re.compile(r"\d{8,}")
_HEX_SHAPED = re.compile(r"[0-9a-fA-F]+")
# The digits run to one end of the token, so it reads as prefix plus account number.
_PREFIXED_NUMBER = re.compile(r"[a-fA-F]*\d+|\d+[a-fA-F]*")
_HOME = re.compile(r"(?<![\w.-])/Users/[^/\s\"']+")

MIN_ACCOUNT_DIGITS = 8


def _digit_count(text: str) -> int:
    return sum(c.isdigit() for c in text)


def _last_four(digits: str) -> str:
    only = re.sub(r"\D", "", digits)
    return "****" + only[-4:]


def _strip_userinfo(match: re.Match) -> str:
    scheme, before_colon, after_colon = match.groups()
    looks_like_host = "." in before_colon or before_colon.lower() == "localhost"
    if looks_like_host and _PORT_THEN_PATH.fullmatch(after_colon):
        return match.group(0)
    return scheme


def _clean_url(match: re.Match) -> str:
    url = match.group(0)
    cut = min((i for i in (url.find("?"), url.find("#")) if i != -1), default=-1)
    base = url if cut == -1 else url[:cut]
    scheme, _, rest = base.partition("://")
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rpartition("@")[2]
    cleaned = f"{scheme}://{authority}{slash}{path}"
    return cleaned if cut == -1 else f"{cleaned}?{MASK}"


def _closing_bracket_end(text: str, start: int) -> int:
    """The index just past the bracket closing ``text[start]``, skipping quoted strings.

    Fails closed: with no closing bracket, a mismatched one, or a quote that never
    ends, it is the end of the text, so everything after the opening bracket gets masked.
    """
    closers: list[str] = []
    quote = None
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            pair = text[index : index + 2]
            if quote == pair:
                quote = None
            elif quote is None and pair in _ESCAPED_QUOTES:
                quote = pair
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in _QUOTE_CHARS:
            quote = char
        elif char in _CLOSER_FOR:
            closers.append(_CLOSER_FOR[char])
        elif char in "]}":
            if closers.pop() != char:
                return len(text)
            if not closers:
                return index + 1
        index += 1
    return len(text)


def _mask_structured(text: str) -> str:
    """Mask each list or object under a secret name, across lines: its nested fields
    can hold anything, whatever they are called."""
    out = []
    position = 0
    while match := _STRUCTURED.search(text, position):
        out += [text[position : match.start()], match.group(1), MASK]
        position = _closing_bracket_end(text, match.end() - 1)
    out.append(text[position:])
    return "".join(out)


def _mask_value(match: re.Match) -> str:
    value = match.group("val")
    if _ONLY_A_MASK.fullmatch(value):
        return match.group(0)  # an earlier rule's mask, not a secret
    if value.isdigit() and _COUNT_NAME.search(match.group("name")):
        return match.group(0)  # a count such as input_tokens: 2000
    return f"{match.group('name')}{match.group('sep')}{MASK}"


def _mask_opaque(match: re.Match) -> str:
    """Spare slugs, paths and UUIDs: only a long segment mixing letters and digits is opaque."""
    run = match.group(0)
    for segment in _SEGMENT_SEP.split(run):
        if (
            len(segment) > 12
            and any(c.isdigit() for c in segment)
            and any(c.isalpha() for c in segment)
        ):
            return MASK
    return run


def _is_date(segments: list[str]) -> bool:
    """Whether three digit segments read YYYY-MM-DD or MM-DD-YYYY."""
    sizes = [len(s) for s in segments]
    return all(s.isdigit() for s in segments) and sizes in ([4, 2, 2], [2, 2, 4])


def _mask_number_run(run: list[str]) -> list[str]:
    start = 3 if len(run) >= 3 and _is_date(run[:3]) else 0
    number = run[start:]
    if _digit_count("".join(number)) < MIN_ACCOUNT_DIGITS:
        return run
    return [*run[:start], _last_four("".join(number))]


def _mask_hyphenated(match: re.Match) -> str:
    """Mask a run of number segments in a hyphenated token, leaving its words."""
    token = match.group(0)
    if _UUID.fullmatch(token):
        return token
    out: list[str] = []
    run: list[str] = []
    for segment in token.split("-"):
        if _NUMBER_SEGMENT.fullmatch(segment):
            run.append(segment)
            continue
        out += _mask_number_run(run)
        out.append(segment)
        run = []
    out += _mask_number_run(run)
    return "-".join(out)


def _mask_dotted(match: re.Match) -> str:
    text = match.group(0)
    if (
        _digit_count(text) < MIN_ACCOUNT_DIGITS
        or _IP.fullmatch(text)
        or _DOTTED_DATE.fullmatch(text)
    ):
        return text
    return _last_four(text)


def _mask_digit_token(match: re.Match) -> str:
    token = match.group(0)
    if token.isdigit():
        return _last_four(token)
    if _HEX_SHAPED.fullmatch(token) and not _PREFIXED_NUMBER.fullmatch(token):
        return token  # a hex id such as a thread id, not an account number
    return _LONG_DIGITS.sub(lambda m: _last_four(m.group(0)), token)


def redact_text(text: str) -> str:
    """``text`` without terminal escapes, secrets or full account numbers."""
    text = _ANSI.sub("", text)
    text = _CONTROL.sub("", text)
    text = _PEM.sub(MASK, text)
    # Before the header rule, which would hide a multi-line "cookies": [ list from it.
    text = _mask_structured(text)
    text = _HEADER.sub(lambda m: f"{m.group(1)}: {MASK}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _URL_USERINFO.sub(_strip_userinfo, text)
    text = _URL.sub(_clean_url, text)
    text = _QUERY.sub(f"?{MASK}", text)
    text = _BARE_URL_QUERY.sub(lambda m: f"{m.group(1)}?{MASK}", text)
    text = _PASSWORD_LINE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)
    text = _NAME_VALUE.sub(_mask_value, text)
    text = _FLAG.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _KNOWN_TOKEN.sub(MASK, text)
    text = _OPAQUE_RUN.sub(_mask_opaque, text)
    text = _SPACED_DIGITS.sub(lambda m: _last_four(m.group(0)), text)
    text = _HYPHENATED.sub(_mask_hyphenated, text)
    text = _DOTTED_DIGITS.sub(_mask_dotted, text)
    text = _DIGIT_TOKEN.sub(_mask_digit_token, text)
    return _HOME.sub("~", text)


def _redact_item(key: str, item: Any) -> Any:
    if item is None:
        return None
    if isinstance(item, int) and not isinstance(item, bool) and _COUNT_NAME.search(key):
        return redact_value(item)
    if _SENSITIVE_KEY.search(key):
        return MASK
    return redact_value(item)


def redact_value(value: Any) -> Any:
    """A copy of ``value`` with every key and leaf redacted, at any depth."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float)):
        as_text = str(value)
        redacted = redact_text(as_text)
        return value if redacted == as_text else redacted
    if isinstance(value, dict):
        return {redact_text(str(key)): _redact_item(str(key), item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item) for item in value]
    return redact_text(str(value))
