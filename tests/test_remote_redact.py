"""Redaction before anything leaves the Mac (#412).

Secret-shaped strings are built at runtime so no real-looking token is
committed (and push protection has nothing to flag).
"""

import json
import time

import pytest

from agent_hq.remote.redact import MASK, redact_text, redact_value

GH_TOKEN = "ghp_" + "a1B2" * 9
GH_PAT = "github_pat_" + "a1B2" * 12
ANTHROPIC = "sk-" + "ant-api03-" + "a1B2c3D4" * 5
SLACK = "xox" + "b-1234567890-" + "aBcDeF" * 4
AWS = "AKIA" + "ABCD1234EFGH5678"
GOOGLE = "AIza" + "a1B2c3D4" * 4
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3In0" + "." + "abcDEF123456_-"
HEX40 = "0123456789abcdef" * 2 + "01234567"
PEM_LABEL = "RSA " + "PRIVATE KEY"
PEM = f"-----BEGIN {PEM_LABEL}-----\nfakebody\nmorekeymaterialhere\n-----END {PEM_LABEL}-----"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"push failed with {GH_TOKEN} today", GH_TOKEN),
        (f"pat {GH_PAT}", GH_PAT),
        (f"key {ANTHROPIC}", ANTHROPIC),
        (f"slack {SLACK}", SLACK),
        (f"aws {AWS}", AWS),
        (f"google {GOOGLE}", GOOGLE),
        (f"jwt {JWT}", JWT),
        (f"opaque {HEX40} here", HEX40),
    ],
)
def test_known_and_opaque_token_shapes_are_masked(text, secret):
    result = redact_text(text)
    assert secret not in result
    assert MASK in result


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abc123def", "abc123def"),
        ("authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ('"Authorization": "Bearer abc123def"', "abc123def"),
        ("Cookie: sid=abc; other=def", "sid=abc"),
        ("Set-Cookie: sid=abc; Path=/", "sid=abc"),
        ("got Bearer abc123def back", "abc123def"),
    ],
)
def test_auth_and_cookie_headers_are_masked(text, secret):
    assert secret not in redact_text(text)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("token=abc123", "abc123"),
        ("api_key: abc123", "abc123"),
        ("API key: abc123", "abc123"),
        ("apikey=abc123", "abc123"),
        ("client_secret=abc123", "abc123"),
        ("password: hunter2", "hunter2"),
        ("passwd=hunter2", "hunter2"),
        ("private_key=abc123", "abc123"),
        ("credentials: abc123", "abc123"),
        ('{"token":"abc def"}', "abc def"),
        ("{'token': 'abc def'}", "abc def"),
        ("**Anthropic API key:** `abc def`", "abc def"),
        ("ACCESS_TOKEN=abc123", "abc123"),
    ],
)
def test_sensitive_name_value_pairs_are_masked(text, secret):
    result = redact_text(text)
    assert secret not in result
    assert MASK in result


def test_name_value_keeps_the_name_for_context():
    assert redact_text("token=abc123 rest") == f"token={MASK} rest"


def test_count_shaped_token_field_is_not_a_secret():
    assert redact_text("input_tokens: 2000") == "input_tokens: 2000"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://u:p@x.dev/a?t=1#f", f"https://x.dev/a?{MASK}"),
        ("see https://x.dev/a?t=1 now", f"see https://x.dev/a?{MASK} now"),
        ("https://x.dev/a#frag", f"https://x.dev/a?{MASK}"),
        ("postgres://u:p@db.local/app", "postgres://db.local/app"),
        ("https://x.dev/plain/path", "https://x.dev/plain/path"),
        ("x.dev/a?t=1", f"x.dev/a?{MASK}"),
    ],
)
def test_url_query_fragment_and_userinfo_are_stripped(text, expected):
    assert redact_text(text) == expected


def test_url_query_with_brackets_is_gone_entirely():
    assert "secretpart" not in redact_text("https://x.dev/a?a=(b)secretpart")


def test_pem_block_is_masked_whole():
    result = redact_text(f"before\n{PEM}\nafter")
    assert "keymaterial" not in result
    assert result == f"before\n{MASK}\nafter"


def test_truncated_pem_block_masks_to_the_end():
    head = PEM.split("-----END")[0]
    assert "keymaterial" not in redact_text(head)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("account 123456789012", "account ****9012"),
        ("12345678", "****5678"),
        ("1234-5678-9012", "****9012"),
        ("1234 5678 9012 3456", "****3456"),
        ("acct12345678", "acct****5678"),
        ("acct_12345678", "acct_****5678"),
        ("1762262659.123", "****2659.123"),
    ],
)
def test_account_numbers_keep_only_the_last_four(text, expected):
    assert redact_text(text) == expected


def test_home_directory_is_shortened():
    assert redact_text("saved to /Users/dev/x/y.md") == "saved to ~/x/y.md"


@pytest.mark.parametrize(
    "text",
    [
        "$500.00",
        "10,089.33",
        "Checking to Brokerage 4821 (410.25) and Checking to CD 7305 (500.00)",
        "Paycheck (Acme Corp)",
        "2026-11-04T13:24:19.885Z",
        "2026-11-04 13:24:19,233",
        "Tue Nov  3 17:38:47 CST 2026",
        "exit 15",
        "#412",
        "ben-ralston/agent-hq#412",
        "feature-412-remote-health-errors",
        "connection=0c601022-0d4b-413d-9c0f-b4fdd64180d0",
        "ip=198.41.192.107",
        "mail:thread:1a1a41294348f546",
        "log:widgets:error:add-note-failed-writeverificationerror-4bfafce7",
        "[substack-progress-sync] done: 3 chunk(s), 120 processed, 0 created",
        "update_note_content failed: EditLockTimeoutError",
        "writeverificationerrorwriteverificationerrorwriteverificationerror",
    ],
)
def test_useful_content_survives(text):
    assert redact_text(text) == text


def test_ansi_and_control_characters_are_stripped_but_newlines_stay():
    assert redact_text("\x1b[31mred\x1b[0m\x07 a\nb\tc\x1b]0;title\x07") == "red a\nb\tc"


def test_terminal_escape_cannot_hide_a_secret_from_the_rules():
    assert "abc123" not in redact_text("to\x1b[0mken=abc123")


def test_redact_value_masks_sensitive_keys_at_any_depth():
    result = redact_value({"outer": [{"api_key": "plain", "Cookie": {"nested": "x"}}]})
    assert result == {"outer": [{"api_key": MASK, "Cookie": MASK}]}


def test_redact_value_runs_text_rules_on_keys_and_values():
    result = redact_value({f"k {GH_TOKEN}": f"v {GH_TOKEN}"})
    assert GH_TOKEN not in str(result)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (123456789, "****6789"),
        (1234567890.5, "****7890.5"),
        (4821, 4821),
        (500.25, 500.25),
        (True, True),
        (None, None),
    ],
)
def test_redact_value_checks_numbers_as_strings(value, expected):
    assert redact_value(value) == expected


def test_redact_value_keeps_none_under_a_sensitive_key():
    assert redact_value({"token": None}) == {"token": None}


def test_redact_value_handles_tuples_sets_and_other_objects():
    class Odd:
        def __str__(self):
            return f"odd {GH_TOKEN}"

    result = redact_value({"a": (GH_TOKEN,), "b": {GH_TOKEN}, "c": Odd()})
    assert GH_TOKEN not in str(result)
    assert isinstance(result["a"], list)


@pytest.mark.parametrize(
    "line",
    [
        "a-b_c/d" * 10_000 + " token" * 10_000 + "x" * 65_536,
        "token" * 13_000,
        "a-" * 32_000,
        "a." * 32_000,
        "1-" * 32_000,
        "1." * 32_000,
        "?" * 65_536,
        "token=[" * 9_000,
        "token=[a] " * 6_500,
    ],
)
def test_a_huge_unbroken_line_redacts_in_bounded_time(line):
    # Log lines reach 64 KiB. A regex that backtracks per position would hang.
    started = time.monotonic()
    redact_text(line)
    assert time.monotonic() - started < 1.0


# --- review round 1 ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password=123456",
        "token=1234567",
        "token: 123456",
        "secret=9876",
        "api_key: 1234",
        "password=123456789012",
    ],
)
def test_numeric_values_under_a_secret_name_are_fully_masked(text):
    result = redact_text(text)
    assert result.endswith(MASK)
    assert not any(c.isdigit() for c in result)


@pytest.mark.parametrize(
    "text",
    ["input_tokens: 2000", "max_tokens=4096", "total_tokens: 12", "token_count: 3", '"tokens": 5'],
)
def test_count_fields_stay_readable(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"input_tokens": 2000}, {"input_tokens": 2000}),
        ({"token_count": 3}, {"token_count": 3}),
        ({"token": 2000}, {"token": MASK}),
        ({"input_tokens": "abc"}, {"input_tokens": MASK}),
    ],
)
def test_count_keys_keep_only_integer_values(value, expected):
    assert redact_value(value) == expected


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"password":"abc\\"secretpart"}', "secretpart"),
        ('token: "ab\\"secretpart"', "secretpart"),
        ('{\\"token\\": \\"secretpart\\"}', "secretpart"),
        ('{\\"api_key\\":\\"secretpart\\"}', "secretpart"),
        ("password='ab\\'secretpart'", "secretpart"),
        ('password="unterminated secretpart', "secretpart"),
    ],
)
def test_escaped_quotes_do_not_end_the_mask_early(text, secret):
    assert secret not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "GET /oauth/callback?code=secretpart&state=xyz HTTP/1.1",
        "127.0.0.1:9894/callback?code=secretpart",
        "example.com:8443/a?code=secretpart",
        "localhost:8000/x?code=secretpart&a=b",
    ],
)
def test_query_strings_go_from_relative_and_host_port_urls(text):
    result = redact_text(text)
    assert "secretpart" not in result
    assert f"?{MASK}" in result


def test_relative_url_keeps_its_path_and_what_follows():
    assert redact_text("GET /cb?code=x&y=z HTTP/1.1") == f"GET /cb?{MASK} HTTP/1.1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("acct-1234-5678-9012", "acct-****9012"),
        ("1234-5678-9012-checking", "****9012-checking"),
        ("1234.5678.9012", "****9012"),
        ("1234-5678", "****5678"),
        ("SSN 123-45-6789", "SSN ****6789"),
        ("Brokerage Z12-345678", "Brokerage ****5678"),
        ("1234\t5678\t9012", "****9012"),
        ("1234\xa05678\xa09012", "****9012"),
    ],
)
def test_more_account_number_shapes_keep_only_the_last_four(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CD12345678", "CD****5678"),
        ("AC1234567890", "AC****7890"),
        ("fa123456789", "fa****6789"),
        ("acc123456789012", "acc****9012"),
        ("12345678ab", "****5678ab"),
    ],
)
def test_a_hex_letter_prefix_does_not_spare_an_account_number(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-04",
        "11-04-2026",
        "2026.11.04",
        "2026-11-04-412-remote-health-errors-design.md",
        "2026-11-03T17:38-06:00",
        "198.41.192.107",
        "abcd1234567890ef",
        "0c601022-4130-9000-1234-b4fdd64180d0",
        "Brokerage 4821 CD 7305",
        "4821 7305",
        "1.2.3",
    ],
)
def test_dates_ips_and_ids_are_not_account_numbers(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "--token secretpart",
        "--password secretpart",
        "--api-key secretpart",
        "-password secretpart",
        "--auth secretpart",
        "run --token=secretpart now",
    ],
)
def test_cli_flag_values_are_masked(text):
    assert "secretpart" not in redact_text(text)


def test_a_cli_flag_keeps_the_next_flag():
    assert redact_text("--token abc --verbose") == f"--token {MASK} --verbose"


@pytest.mark.parametrize(
    "text", ["token => secretpart", "token := secretpart", "token == secretpart"]
)
def test_multi_character_separators_mask_the_value(text):
    result = redact_text(text)
    assert "secretpart" not in result
    assert result.endswith(MASK)


def test_lone_surrogates_are_stripped_so_the_output_can_be_encoded():
    result = redact_text("ok \ud83d then \udc00 end")
    assert result == "ok  then  end"
    result.encode("utf-8")


def test_lone_surrogates_in_keys_are_stripped():
    json.dumps(redact_value({"k\ud83d": "v\udc00"}), ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize(
    "text",
    [
        "password=abc,secretpart",
        "password=abc;secretpart",
        "token=abc)secretpart",
        "password: correct horse secretpart",
        "Passphrase: correct horse secretpart",
        "Cookies: a=b; sid=secretpart",
    ],
)
def test_a_value_is_masked_past_punctuation_and_spaces(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"token":123,"password":"my secretpart phrase"}',
        '{"token":abc,"password":"my secretpart phrase"}',
        "{'token':123,'password':'my secretpart phrase'}",
        '{\\"token\\":123,\\"password\\":\\"my secretpart phrase\\"}',
        '{"a":{"token":123},"password":"my secretpart phrase"}',
        '{"session":abc,"api_key":"my secretpart phrase"}',
    ],
)
def test_an_unquoted_value_stops_before_the_next_quoted_field(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "password=my secretpart",
        "password=ab&secretpart",
        "passphrase=my secretpart",
        "pwd = my secretpart",
    ],
)
def test_an_unquoted_password_goes_to_the_end_of_the_line(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    ["--session=secretpart", "--auth=secretpart", "--sid=secretpart", "-sig=secretpart"],
)
def test_short_flag_names_with_an_equals_sign_are_masked(text):
    result = redact_text(text)
    assert "secretpart" not in result
    assert result.endswith(f"={MASK}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password: "x", amount: 5', f"password: {MASK}, amount: 5"),
        ('password:  "x y", amount: 5', f"password:  {MASK}, amount: 5"),
        ("password = 'x', payee: Brokerage", f"password = {MASK}, payee: Brokerage"),
    ],
)
def test_a_quoted_password_leaves_the_rest_of_the_line(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '{"tokens":["a","secretpart"]}',
        '{"credentials":{"u":"a","key":"secretpart"}}',
        '{"token":[1,2,"secretpart"]}',
        "token: {a: b, c: secretpart}",
        '{"secret":{"nested":{"x":"y","z":"secretpart"}}}',
    ],
)
def test_a_structured_value_under_a_secret_name_is_masked_whole(text):
    assert "secretpart" not in redact_text(text)


def test_an_unquoted_value_only_stops_at_a_real_next_field():
    assert "secretpart" not in redact_text('token=abc,"secretpart')


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("redis://:p/ss@h:6379", "redis://h:6379"),
        ("https://user:123/secret@h/", "https://h/"),
        ("https://user:a@b/c@h/", "https://h/"),
        ("https://:token@h.dev/a", "https://h.dev/a"),
    ],
)
def test_more_url_credential_shapes_are_removed(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "password: 'it's secretpart y'",
        'password: "a"b secretpart',
        "password: \\abc secretpart",
        "password = 'x' secretpart",
    ],
)
def test_a_value_that_only_starts_with_a_quote_goes_to_the_end_of_the_line(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "--session=-secretpart",
        '--auth="a secretpart"',
        "--session='a secretpart'",
        '--auth "a secretpart"',
        "--sid `a secretpart`",
        '--session="a \\" secretpart"',
    ],
)
def test_flag_values_are_masked_whole(text):
    assert "secretpart" not in redact_text(text)


def test_a_quoted_flag_value_leaves_what_follows():
    assert redact_text('--auth "a b" --verbose') == f"--auth {MASK} --verbose"


@pytest.mark.parametrize(
    "text",
    [
        "http://localhost:3000/,postgres://u:secretpart@db/",
        "https://first.last:123/secretpart@h/",
        "https://x.dev:8443/secretpart@h/",
    ],
)
def test_only_an_at_sign_starting_a_path_segment_is_spared(text):
    assert "secretpart" not in redact_text(text)


def test_a_mask_does_not_hide_the_next_field():
    assert redact_text("token=[redacted] api_key=secretpart") == f"token={MASK} api_key={MASK}"
    pem = "-----BEGIN " + "PRIVATE KEY-----\nbody\n-----END " + "PRIVATE KEY-----"
    assert "secretpart" not in redact_text(f"token={pem} api_key=secretpart")


def test_a_longer_value_starting_with_a_mask_is_still_masked():
    assert "secretpart" not in redact_text("token=[redacted]secretpart")


@pytest.mark.parametrize(
    "text",
    [
        '"token": [\n  "a",\n  "secretpart"\n]',
        '{"credentials": {\n  "u": "a",\n  "key": "secretpart"\n}\n}',
        '{"token":["]","secretpart"]}',
        "{'token':['}','secretpart']}",
        'tokens = {\n  "a": [1, {"b": "secretpart"}]\n}',
    ],
)
def test_a_structured_value_is_masked_through_its_closing_bracket(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("token: [a, b] amount: 5", f"token: {MASK} amount: 5"),
        ('{"token":[1,2],"amount":5}', f'{{"token":{MASK},"amount":5}}'),
        ('"token": {\n "a": 1\n}\nnext line', f'"token": {MASK}\nnext line'),
    ],
)
def test_text_after_the_closing_bracket_stays(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    ['"token": [\n "a",\n "secretpart"', "token: {a: it's, b: secretpart}", 'token: ["secretpart'],
)
def test_a_bracket_that_never_closes_masks_to_the_end(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        'token=x,"api key":"abc secretpart"',
        'token=x,"private key":"abc secretpart"',
        '--token "abc"secretpart',
        "--token 'abc'secretpart",
        'token="abc"secretpart',
        "password='abc'secretpart",
        '"cookies": [\n  {"name":"x","value":"secretpart"}\n]',
        '"authorization": {\n  "value":"secretpart"\n}',
        "Secret key: secretpart",
        '"my secret key": "secretpart"',
        "postgres://u:pa'secretpart@host/db",
        "postgres://u:pa<secretpart@host/db",
        "https://first.last:12345/secretpart@host",
    ],
)
def test_round_four_shapes_are_masked(text):
    assert "secretpart" not in redact_text(text)


def test_a_quoted_value_stops_at_its_field_delimiter():
    assert redact_text('{"token":"abc","amount":5}') == f'{{"token":{MASK},"amount":5}}'


def test_postgres_password_with_a_quote_keeps_the_host():
    assert redact_text("postgres://u:pa'ss@host/db") == "postgres://host/db"


@pytest.mark.parametrize(
    "text",
    [
        '{\\"token\\":[\\"]\\", \\"secretpart\\"]}',
        "token: [`]`, secretpart]",
        "token: [a} secretpart",
        "token: {a] secretpart",
        'token: {"a":[1}, "b":"secretpart"}',
    ],
)
def test_escaped_quotes_backticks_and_mismatched_brackets_do_not_end_the_value(text):
    assert "secretpart" not in redact_text(text)


def test_a_mismatched_closer_masks_to_the_end():
    assert redact_text("token: [a} rest") == f"token: {MASK}"


def test_a_password_line_stops_at_the_line_end():
    assert redact_text("password=a b\nnext line") == f"password={MASK}\nnext line"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://user:p/ss@host.dev/a", "https://host.dev/a"),
        ("https://user:pa#ss@host.dev/", "https://host.dev/"),
        ("https://user:pa?ss@host.dev/", "https://host.dev/"),
        ("postgres://u:p/w@db.local/app", "postgres://db.local/app"),
    ],
)
def test_url_credentials_with_unencoded_delimiters_are_removed(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://medium.com/@user/post",
        "https://x.dev:8443/@scope/pkg",
        "https://x.dev:8443/a",
        "git@github.com:ben-ralston/agent-hq.git",
    ],
)
def test_at_signs_in_paths_are_not_credentials(text):
    assert redact_text(text) == text


def test_a_count_before_another_field_stays_readable():
    assert redact_text('{"input_tokens":2000,"password":"a b"}') == (
        f'{{"input_tokens":2000,"password":{MASK}}}'
    )


def test_a_header_mask_is_not_masked_again():
    assert redact_text("Authorization: Bearer abc") == f"Authorization: {MASK}"
    assert redact_text("password: a b") == f"password: {MASK}"


@pytest.mark.parametrize(
    "text",
    [
        "passphrase=secretpart",
        "auth=secretpart",
        "session=secretpart",
        "session_id=secretpart",
        "sid=secretpart",
        "sig=secretpart",
        "signature=secretpart",
        "access_key=secretpart",
    ],
)
def test_more_secret_names_are_masked(text):
    assert "secretpart" not in redact_text(text)


@pytest.mark.parametrize(
    "text", ["author: Sam", "inside: x", "design: y", "reason=z", "assign=q", "obsession: r"]
)
def test_short_secret_names_only_match_whole_words(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("key", ["auth", "session", "sid", "sig", "signature", "passphrase"])
def test_more_secret_key_names_are_masked(key):
    assert redact_value({key: "x"}) == {key: MASK}


@pytest.mark.parametrize("key", ["author", "inside", "design", "reason"])
def test_short_key_names_only_match_whole_keys(key):
    assert redact_value({key: "x"}) == {key: "x"}
