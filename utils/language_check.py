"""Deterministic English-only check for community posts and comments.

No model, no network, no third-party dependency. Two layers:

1. Script: letters outside the Latin script (Devanagari, Telugu, Arabic,
   CJK, ...) above a small share reject the text outright.
2. Vocabulary: Latin-script text (Hinglish, Spanish, ...) must be mostly made
   of words from a bundled English word list (SCOWL/ESDB size 50, see the
   licence header of ``language_data/en_words.txt``) plus a cricket/app
   allowlist.

Proper nouns (capitalised mid-sentence), short acronyms, numbers, URLs,
@mentions and code are left out of the count, so player names, error
messages and stack traces do not trip the check.

``langdetect`` / ``lingua`` were rejected: the first is unreliable on short
text and blind to Hinglish, the second costs too much RAM on the 1 GB box.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

WORDLIST_PATH = os.path.join(os.path.dirname(__file__), "language_data", "en_words.txt")

# Share of letters allowed outside the Latin script (accented names, a stray
# symbol) before the text is treated as another script.
MAX_NON_LATIN_SHARE = 0.10
# Share of counted words that must be recognised English.
MIN_ENGLISH_SHARE = 0.70
# Below this many counted words there is too little evidence to judge.
MIN_COUNTED_TOKENS = 3
# Hinglish markers: this many, and this share of counted words, is decisive.
MIN_MARKERS = 2
MIN_MARKER_SHARE = 0.15

# Cricket and app vocabulary the general word list lacks.
DOMAIN_WORDS = frozenset("""
    lbw dls odi odis powerplay powerplays wicketkeeper wicketkeepers keeper keepers
    googly googlies doosra yorker yorkers bouncer bouncers legspin legspinner
    legspinners offspin offspinner offspinners legbreak offbreak allrounder
    allrounders duckworth lewis stern runrate runrates nrr rpo motm mom dnb
    lista fc listaa superover superovers declaration declared follow-on
    scorecard scorecards leaderboard leaderboards playoff playoffs knockout
    knockouts roundrobin simcricketx scx simulator sim sims resimulate
    resimulated resim unplayable unbowled
    ui ux api apis url urls wifi ios android chrome safari firefox edge iphone
    ipad pc laptop dropdown dropdowns popup popups tooltip tooltips bugfix
    signup signups login logins logout username usernames darkmode checkbox
    checkboxes navbar sidebar homepage webpage localhost timestamp timestamps
    frontend backend dataset datasets config configs csv json xlsx pdf png jpg
    jpeg webp gif svg http https html css javascript js
""".split())

# Everyday informal English the dictionary does not carry.
INFORMAL_WORDS = frozenset("""
    dont doesnt didnt cant couldnt shouldnt wouldnt isnt arent wasnt werent
    havent hasnt hadnt wont im ive id youre youve youd theyre theyve thats
    whats theres heres lets its u ur pls plz thx thanks thankyou btw idk imo
    imho asap ok okay hi hey hello etc eg ie vs yeah yep nope gonna wanna
    gotta kinda sorta
""".split())

# Common romanised Hindi/Urdu words that are not English. A match counts as an
# unrecognised word even if the dictionary happens to carry a homograph.
HINGLISH_MARKERS = frozenset("""
    hai hain hoon hu nahi nahin nhi na kya kyu kyun kyon kyunki bhai yaar yar
    raha rahi rahe karo karna karta karte kar kiya kiye hua hui hue hogaya
    hogya gaya gayi gaye mera meri mere tera teri tere apna apni aur bhi toh
    kaise kaisa kab kuch sab abhi pe se ka ki ke mein mai main hum tum aap
    wala wali wale lekin chahiye bata batao samajh dekho accha acha achha
    theek thik ji haan han bahut bohot jaldi kyonki matlab chalo chal ho jata
    jati jate sahi galat kaam
""".split()) - {"main", "han", "na"}  # 'main' is English; keep the two short ones out too


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_MENTION_RE = re.compile(r"@\w+")
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*|\w+", re.UNICODE)
_SENTENCE_BREAK = set(".!?:;\n\"(")


@dataclass
class LanguageResult:
    ok: bool
    reason: str = ""                       # "" | "script" | "vocabulary"
    unrecognised: list[str] = field(default_factory=list)
    counted: int = 0
    english_share: float = 1.0

    def message(self) -> str:
        if self.ok:
            return ""
        if self.reason == "script":
            return "Posts must be written in English (English letters only)."
        words = ", ".join(self.unrecognised[:8])
        return f"Posts must be written in English. Not recognised: {words}."


@lru_cache(maxsize=1)
def _wordlist() -> frozenset[str]:
    words = set()
    with open(WORDLIST_PATH, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            w = line.strip()
            if w:
                words.add(w)
    return frozenset(words)


@lru_cache(maxsize=4096)
def _is_latin(ch: str) -> bool:
    try:
        return "LATIN" in unicodedata.name(ch)
    except ValueError:
        return False


def _strip_noise(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    for rx in (_CODE_FENCE_RE, _INLINE_CODE_RE, _URL_RE, _EMAIL_RE, _MENTION_RE):
        text = rx.sub(" . ", text)
    # Quoted lines (pasted error text, other people's words) are not judged.
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith(">"))


def _normalise(token: str) -> str:
    t = token.lower().strip("'")
    if t.endswith("'s"):
        t = t[:-2]
    return t


def _known(word: str, extra: frozenset[str]) -> bool:
    if word in HINGLISH_MARKERS:
        return False
    return (
        word in DOMAIN_WORDS
        or word in INFORMAL_WORDS
        or word in extra
        or word in _wordlist()
        or word.replace("'", "") in INFORMAL_WORDS
    )


def check_english(text: str, extra_words: Iterable[str] = ()) -> LanguageResult:
    """Judge whether ``text`` is English. ``extra_words`` are lowercase words
    to accept on top of the built-in lists (e.g. player-pool surnames)."""
    text = _strip_noise(text or "")

    letters = [ch for ch in text if ch.isalpha()]
    if letters:
        non_latin = sum(1 for ch in letters if not _is_latin(ch))
        if non_latin / len(letters) > MAX_NON_LATIN_SHARE:
            return LanguageResult(ok=False, reason="script")

    extra = frozenset(w.lower() for w in extra_words)

    # Collect tokens with whether they open a sentence.
    raw: list[tuple[str, bool]] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if any(c.isdigit() for c in tok) or "_" in tok:
            continue  # T20, v2.5, 2nd, snake_case identifiers
        prev = text[:m.start()].rstrip()
        raw.append((tok, not prev or prev[-1] in _SENTENCE_BREAK))

    alpha = [t for t, _ in raw]
    # If most words are Title Case or ALL CAPS, capitalisation carries no
    # proper-noun signal (and would otherwise let "Bhai Match Nahi Chal" skip
    # every word), so judge every word.
    capitalised = sum(1 for t in alpha if t[:1].isupper())
    trust_case = not alpha or capitalised / len(alpha) <= 0.5

    counted: list[str] = []
    for tok, sentence_start in raw:
        word = _normalise(tok)
        if not word:
            continue
        if trust_case:
            if tok.isupper() and 2 <= len(tok) <= 5 and word not in HINGLISH_MARKERS:
                continue  # acronym: LBW, DLS, API
            if tok[:1].isupper() and not sentence_start and word not in HINGLISH_MARKERS:
                continue  # proper noun: Kohli, Wankhede
        counted.append(word)

    if len(counted) < MIN_COUNTED_TOKENS:
        return LanguageResult(ok=True, counted=len(counted))

    unknown = [w for w in counted if not _known(w, extra)]
    share = 1 - len(unknown) / len(counted)
    markers = sum(1 for w in counted if w in HINGLISH_MARKERS)

    unique_unknown = list(dict.fromkeys(unknown))
    if markers >= MIN_MARKERS and markers / len(counted) >= MIN_MARKER_SHARE:
        return LanguageResult(False, "vocabulary", unique_unknown, len(counted), share)
    if share < MIN_ENGLISH_SHARE:
        return LanguageResult(False, "vocabulary", unique_unknown, len(counted), share)
    return LanguageResult(True, "", [], len(counted), share)
