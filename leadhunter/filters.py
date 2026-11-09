"""Правила-фильтры в духе LeadScanner: +обязательно, -запрет, (а|б) любое из, "точная форма".
Словоформы приводятся к лемме (pymorphy3): «сайт» ловит «сайта», «сайты», «сайтов»."""
import re
from dataclasses import dataclass, field
from functools import lru_cache

import pymorphy3

_morph = pymorphy3.MorphAnalyzer()
TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.I)  # дефисы режем: «сайт-визитка» = «сайт» + «визитка»
TERM_RE = re.compile(r'([+-]?)(?:\(([^()]+)\)|"([^"]+)"|(\S+))')


@lru_cache(maxsize=50_000)
def lemma(word: str) -> str:
    word = word.lower().replace("ё", "е")
    return _morph.parse(word)[0].normal_form.replace("ё", "е")


def tokens(text: str) -> list[str]:
    return [t.lower().replace("ё", "е") for t in TOKEN_RE.findall(text)]


def lemmatize(text: str) -> list[str]:
    return [lemma(t) for t in tokens(text)]


@dataclass
class Term:
    words: list[str]  # леммы; одно слово или фраза
    exact: str | None = None  # "точная форма" — ищем как подстроку

    def matches(self, lemmas: list[str], toks: list[str], norm: str) -> bool:
        if self.exact is not None:
            return self.exact in norm
        if len(self.words) == 1:
            w = self.words[0]
            return any(l == w or (len(w) >= 5 and t.startswith(w)) for l, t in zip(lemmas, toks))
        n = len(self.words)
        return any(lemmas[i:i + n] == self.words for i in range(len(lemmas) - n + 1))


@dataclass
class Rule:
    expr: str
    required: list[list[Term]] = field(default_factory=list)  # каждый элемент — группа «любое из»
    forbidden: list[Term] = field(default_factory=list)
    anyof: list[Term] = field(default_factory=list)  # термы без знака: хотя бы один

    @property
    def is_block(self) -> bool:
        return not self.required and not self.anyof and bool(self.forbidden)

    def matches(self, text: str) -> bool:
        lem, toks, norm = lemmatize(text), tokens(text), " ".join(tokens(text))
        if any(t.matches(lem, toks, norm) for t in self.forbidden):
            return False
        if self.is_block:
            return False
        for group in self.required:
            if not any(t.matches(lem, toks, norm) for t in group):
                return False
        if self.anyof and not any(t.matches(lem, toks, norm) for t in self.anyof):
            return False
        return True

    def blocks(self, text: str) -> bool:
        if not self.is_block:
            return False
        lem, toks, norm = lemmatize(text), tokens(text), " ".join(tokens(text))
        return any(t.matches(lem, toks, norm) for t in self.forbidden)


def _term(raw: str) -> Term:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) > 2:
        return Term(words=[], exact=" ".join(tokens(raw[1:-1])))
    words = lemmatize(raw)
    if not words:
        raise ValueError(f"пустой терм: {raw!r}")
    return Term(words=words)


def parse_rule(expr: str) -> Rule:
    rule = Rule(expr=expr.strip())
    if not rule.expr:
        raise ValueError("пустое правило")
    for sign, group, quoted, single in TERM_RE.findall(rule.expr):
        if group:
            terms = [_term(x) for x in group.split("|") if x.strip()]
        elif quoted:
            terms = [Term(words=[], exact=" ".join(tokens(quoted)))]
        else:
            terms = [_term(single)]
        if sign == "+":
            rule.required.append(terms)
        elif sign == "-":
            rule.forbidden.extend(terms)
        else:
            rule.anyof.extend(terms)
    if not (rule.required or rule.forbidden or rule.anyof):
        raise ValueError("правило не содержит термов")
    return rule


_rule_cache: dict[str, Rule] = {}


def evaluate(text: str, exprs: list[str]) -> tuple[str | None, bool]:
    """(выражение подошедшего правила | None, заблокировано ли блок-правилом)."""
    matched = None
    for expr in exprs:
        rule = _rule_cache.get(expr)
        if rule is None:
            try:
                rule = parse_rule(expr)
            except ValueError:
                continue
            _rule_cache[expr] = rule
        if rule.blocks(text):
            return None, True
        if matched is None and rule.matches(text):
            matched = expr
    return matched, False
