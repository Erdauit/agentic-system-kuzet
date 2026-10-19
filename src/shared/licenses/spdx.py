"""Разбор и оценка SPDX-выражений лицензий (MIT OR Apache-2.0, (A AND B) WITH X …)."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    ALLOWED = "allowed"
    REVIEWED = "reviewed"  # вне белого списка, но прошло ручное ревью (policy.yaml: reviewed)
    DENIED = "denied"  # явно запрещено (AGPL, GPL, SSPL, RSAL)
    UNKNOWN = "unknown"  # не удалось определить или нет в белом списке — тоже блокирует


@dataclass(frozen=True)
class LicenseId:
    id: str


@dataclass(frozen=True)
class WithException:
    license: LicenseId
    exception: str


@dataclass(frozen=True)
class AllOf:
    items: tuple[Expr, ...]


@dataclass(frozen=True)
class AnyOf:
    items: tuple[Expr, ...]


Expr = LicenseId | WithException | AllOf | AnyOf


class SpdxParseError(ValueError):
    pass


_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.\-+:]+)")


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise SpdxParseError(f"не удалось разобрать SPDX-выражение: {text!r}")
        tokens.append(m.group(1))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str:
        tok = self.peek()
        if tok is None:
            raise SpdxParseError("неожиданный конец SPDX-выражения")
        self.pos += 1
        return tok

    def parse(self) -> Expr:
        expr = self.parse_or()
        if self.peek() is not None:
            raise SpdxParseError(f"лишний токен {self.peek()!r}")
        return expr

    def parse_or(self) -> Expr:
        items = [self.parse_and()]
        while (tok := self.peek()) is not None and tok.upper() == "OR":
            self.take()
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else AnyOf(tuple(items))

    def parse_and(self) -> Expr:
        items = [self.parse_with()]
        while (tok := self.peek()) is not None and tok.upper() == "AND":
            self.take()
            items.append(self.parse_with())
        return items[0] if len(items) == 1 else AllOf(tuple(items))

    def parse_with(self) -> Expr:
        atom = self.parse_atom()
        if (tok := self.peek()) is not None and tok.upper() == "WITH":
            self.take()
            exception = self.take()
            if not isinstance(atom, LicenseId):
                raise SpdxParseError("WITH применяется только к одиночной лицензии")
            return WithException(atom, exception)
        return atom

    def parse_atom(self) -> Expr:
        tok = self.take()
        if tok == "(":
            expr = self.parse_or()
            if self.take() != ")":
                raise SpdxParseError("ожидалась ')'")
            return expr
        if tok == ")" or tok.upper() in {"AND", "OR", "WITH"}:
            raise SpdxParseError(f"неожиданный токен {tok!r}")
        return LicenseId(tok)


def parse(text: str) -> Expr:
    return _Parser(_tokenize(text)).parse()


def render(expr: Expr) -> str:
    if isinstance(expr, LicenseId):
        return expr.id
    if isinstance(expr, WithException):
        return f"{expr.license.id} WITH {expr.exception}"
    joiner = " AND " if isinstance(expr, AllOf) else " OR "
    parts = [f"({render(i)})" if isinstance(i, AllOf | AnyOf) else render(i) for i in expr.items]
    return joiner.join(parts)


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]
    denied_patterns: tuple[str, ...]

    def canonical(self, license_id: str) -> str:
        """Каноническое написание id из белого списка (сравнение без учёта регистра)."""
        lowered = license_id.lower()
        for allowed in self.allowed:
            if allowed.lower() == lowered:
                return allowed
        return license_id

    def classify_id(self, license_id: str) -> Verdict:
        lid = self.canonical(license_id)
        if lid in self.allowed:
            return Verdict.ALLOWED
        if any(fnmatch.fnmatchcase(lid.upper(), p.upper()) for p in self.denied_patterns):
            return Verdict.DENIED
        return Verdict.UNKNOWN

    def evaluate(self, expr: Expr) -> Verdict:
        if isinstance(expr, LicenseId):
            return self.classify_id(expr.id)
        if isinstance(expr, WithException):
            # Исключение (например, LLVM-exception) только расширяет права базовой лицензии.
            return self.classify_id(expr.license.id)
        verdicts = [self.evaluate(i) for i in expr.items]
        if isinstance(expr, AllOf):
            return _all_of(verdicts)
        return _any_of(verdicts)


def _all_of(verdicts: Iterable[Verdict]) -> Verdict:
    vs = list(verdicts)
    if Verdict.DENIED in vs:
        return Verdict.DENIED
    if Verdict.UNKNOWN in vs:
        return Verdict.UNKNOWN
    return Verdict.ALLOWED


def _any_of(verdicts: Iterable[Verdict]) -> Verdict:
    vs = list(verdicts)
    if Verdict.ALLOWED in vs:
        return Verdict.ALLOWED
    if Verdict.UNKNOWN in vs:
        return Verdict.UNKNOWN
    return Verdict.DENIED
