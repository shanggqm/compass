"""Irreversible exit redaction, with separate model and storage policies."""

import json
import re
from collections import Counter
from datetime import date
from typing import Any

SECRETS = re.compile(
    r"(?P<assignment>\b(?i:api[_-]?key|access[_-]?token|secret[_-]?key|password)"
    r"[\"']?\s*[:=]\s*)"
    r"(?P<assigned>\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|"
    r"(?:Bearer\s+)?[^\s\"',;}]+)|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}|"
    r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}|"
    r"\b(?:postgres(?:ql)?|mysql|redis)://[^\s:@/]+:[^\s@/]+@[^\s\"'<>]+",
)
PHONE = re.compile(r"(?<![\w])(?:\+?86[ -]?)?1[3-9]\d{9}(?![\w])")
EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?![\w.-])"
)
IDENTITY = re.compile(r"(?<![\w])[1-9]\d{16}[\dXx](?![\w])")
BUSINESS_ID = re.compile(
    r"(?:order_?id|order_?no|sku(?:_?id)?|product_?id|tracking_?(?:id|no)|"
    r"订单号|订单编号|商品编号|运单号)[\"'\s:=：]*$",
    re.I,
)


def valid_identity(value: str) -> bool:
    try:
        date(int(value[6:10]), int(value[10:12]), int(value[12:14]))
    except ValueError:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check = sum(int(n) * w for n, w in zip(value[:17], weights, strict=True)) % 11
    return value[-1].upper() == "10X98765432"[check]


def redact(text: str, *, pii: bool = True) -> tuple[str, Counter]:
    hits: Counter = Counter()
    if not re.search(r"[A-Za-z0-9@]", text):
        return text, hits

    def mask(match, rule):
        hits[rule] += 1
        return f"[REDACTED:{rule.upper()}]"

    def mask_secret(match):
        assigned = match["assigned"]
        if assigned is None:
            return mask(match, "secret")
        quote = assigned[0] if assigned.startswith(('"', "'")) else ""
        value = assigned[1:-1] if quote else assigned
        if value == "[REDACTED:SECRET]":
            return match[0]
        return match["assignment"] + quote + mask(match, "secret") + quote

    text = SECRETS.sub(mask_secret, text)
    if pii:
        text = EMAIL.sub(lambda m: mask(m, "email"), text)
        for pattern, rule in ((IDENTITY, "identity_number"), (PHONE, "phone")):
            original = text

            def replace(match, rule=rule, original=original):
                if BUSINESS_ID.search(original[max(0, match.start() - 60) : match.start()]):
                    return match[0]
                if rule == "identity_number" and not valid_identity(match[0]):
                    return match[0]
                return mask(match, rule)

            text = pattern.sub(replace, text)
    return text, hits


def redact_structure(value: Any, *, pii=True, field="") -> tuple[Any, Counter]:
    hits: Counter = Counter()
    if re.fullmatch(r"api[_-]?key|access[_-]?token|password|secret(?:[_-]?key)?", field, re.I):
        if value == "[REDACTED:SECRET]":
            return value, hits
        if value:
            hits["secret"] += 1
        return "[REDACTED:SECRET]", hits
    if isinstance(value, list | tuple):
        result = []
        for item in value:
            clean, found = redact_structure(item, pii=pii, field=field)
            result.append(clean)
            hits.update(found)
        return result, hits
    if isinstance(value, dict):
        result = {}
        columns = value.get("columns")
        for key, item in value.items():
            clean_key, found = redact(str(key), pii=pii)
            hits.update(found)
            if (
                key == "rows"
                and isinstance(columns, list | tuple)
                and all(isinstance(c, str) for c in columns)
                and isinstance(item, list | tuple)
            ):
                rows = []
                for row in item:
                    if isinstance(row, list | tuple) and len(row) == len(columns):
                        clean_row = []
                        for column, cell in zip(columns, row, strict=True):
                            clean, found = redact_structure(cell, pii=pii, field=column)
                            clean_row.append(clean)
                            hits.update(found)
                        rows.append(clean_row)
                    else:
                        clean, found = redact_structure(row, pii=pii)
                        rows.append(clean)
                        hits.update(found)
                result[clean_key] = rows
                continue
            result[clean_key], found = redact_structure(item, pii=pii, field=str(key))
            hits.update(found)
        return result, hits
    if (
        pii
        and isinstance(value, int)
        and re.fullmatch(
            r"phone|mobile|tel|identity_number|id_card|身份证|手机号|电话", field, re.I
        )
    ):
        clean, found = redact(str(value))
        return (clean if found else value), found
    if isinstance(value, str):
        return redact_content(value, pii=pii and not bool(BUSINESS_ID.fullmatch(field)))
    return value, hits


def redact_content(text: str, *, pii=True) -> tuple[str, Counter]:
    """Retain structured tool records and column semantics across model/storage exits."""
    opening, ending, body = "", "", text
    if text.startswith('<untrusted_context source="') and text.endswith("\n</untrusted_context>"):
        split = text.find("\n")
        if split >= 0:
            opening, ending = text[: split + 1], "\n</untrusted_context>"
            body = text[split + 1 : -len(ending)]
    if body.lstrip().startswith(("{", "[")):
        try:
            structured = json.loads(body)
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(structured, dict | list):
                clean, hits = redact_structure(structured, pii=pii)
                if hits:
                    return opening + json.dumps(
                        clean, ensure_ascii=False, separators=(",", ":")
                    ) + ending, hits
                return text, hits
    return redact(text, pii=pii)


def storage_value(value: Any) -> Any:
    """Redact structured values while retaining business IDs and JSON syntax."""
    return redact_structure(value)[0]
