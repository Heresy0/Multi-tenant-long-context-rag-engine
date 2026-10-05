"""Reviewed literal aliases with bounded negation/contradiction guards.

Opt-in per fact. This is still a proxy, not a semantic judge; do not globally
rewrite Chinese negations or accept any answer containing the word '不'.
"""
import re


_REVERSED = re.compile(r"(?:并非|不是|不能说|不能认为|不代表|并不意味着|否认).{0,4}$")
_NEGATED = re.compile(r"(?:不|未|没有|并非|不是|不能说|不能认为|错误说法(?:是)?)$")


def _occurrences(text, phrase):
    start = 0
    while phrase and (index := text.find(phrase, start)) >= 0:
        yield index
        start = index + len(phrase)


def negative_assertion_match(text, aliases, rule):
    if rule.get('polarity') != 'negative':
        raise ValueError('Unsupported fact assertion polarity')
    # Punctuation boundaries keep unrelated earlier negations from changing this clause.
    def prefix(index):
        return re.split(r'[。；;，,！？!?\n]|但是|然而|但|却|实际上', text[max(0, index - 24):index])[-1]

    accepted = any(not _REVERSED.search(prefix(index))
                   for alias in aliases for index in _occurrences(text, alias))
    contradictions = [phrase for phrase in rule.get('contradictions', [])
                      if any(not _NEGATED.search(prefix(index)) for index in _occurrences(text, phrase))]
    return accepted and not contradictions, contradictions
