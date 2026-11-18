"""The languages whose negation the verification filter reads, as data.

One `Language` per language: its function words, negation cues, and the phrases that limit or
widen a negation's scope. `kittum.judge.verify` merges every entry of `LANGUAGES` into its word
sets and patterns, so supporting a language is one entry here, with tests in its language. A
criterion wording in a language without an entry cannot have its polarity verified, so an
exclusion on it stays a proposal the filter marks unclear.

Regex fragments are alternatives without groups of their own around them; they are matched case
insensitively. `enrolment` and `sections` are matched on accent-folded text, so they are written
in ASCII.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Language:
    stopwords: str
    """Words too common to be a key term of a condition."""
    negation: str
    """Words that negate what follows them."""
    function_words: str
    """Further function words: a quote needs one to count as this language."""
    short_words: str
    """Short content words that are not function words."""
    clause_breaks: str
    """Words that end the clause between a negation cue and the term it would negate."""
    scope_cues: tuple[str, ...]
    """Phrases that turn a sentence's meaning around or make it two-sided ("with or without")."""
    qualifiers: tuple[str, ...]
    """Words that limit a negation to a time, a frequency, or an age."""
    coordination: tuple[str, ...]
    """Words that bring in or compare another population."""
    negating_prefixes: tuple[str, ...]
    """Compound prefixes that negate the word they join ("non-smokers")."""
    enrolment: tuple[str, ...]
    """Phrases stating who was enrolled or included."""
    sections: tuple[str, ...]
    """Abstract section labels, before a colon."""
    equivalents: dict[str, str] = field(default_factory=dict)
    """Spellings that mean the same term, beyond one inflection, mapped to the English one."""


ENGLISH = Language(
    stopwords="""a an and are as at be been by for from had has have in into is it its of on or
    that the their them these this those to was were which who whom with within must should
    least years year more than less any all other such only also both either each per""",
    negation="""no not without never none neither nor nobody nothing absence absent lack lacking
    naive untreated excluding except cannot didn doesn don isn aren wasn weren hasn haven hadn""",
    function_words="""after before during but if then there over under up out who whose what
    when where while upon via versus vs did do does done being can could may might will would""",
    short_words="""aged age old men mg kg ml day days week year arm arms dose rate risk cell lung
    type stage line oral iv alt""",
    clause_breaks=", ; : . ! ? and or but whereas while",
    scope_cues=(
        "excluded",
        "exclude",
        "excluding",
        "exclusion",
        "ineligible",
        "not eligible",
        "were not included",
        "was not included",
        "not enrolled",
        "with or without",
        "with and without",
        "regardless of",
        "irrespective of",
        "whether or not",
        "independent of",
        "independently of",
        "not only",
        "not just",
    ),
    qualifiers=(
        "before",
        "after",
        "since",
        "until",
        "till",
        "during",
        "ever",
        "every",
        "each",
        "daily",
        "weekly",
        "monthly",
        "yearly",
        "annually",
        "nightly",
        "regularly",
        "occasionally",
        "usually",
        "always",
        "often",
        "frequently",
        "sometimes",
        "rarely",
        "habitually",
        "heavily",
        "previously",
        "formerly",
        "lifetime",
        "history",
        "past",
        "anymore",
        "longer",
        "yet",
        r"prior\s+to",
        r"at\s+least",
        r"at\s+most",
        r"more\s+than",
        r"less\s+than",
        r"fewer\s+than",
        "per",
        "age",
        "aged",
        "ages",
        "old",
        "older",
        "younger",
        r"\d+(?:[.,]\d+)?\s*-?\s*(?:years?|yrs?|months?|weeks?|wks?|days?|hours?|hrs?|minutes?"
        r"|mins?)",
    ),
    coordination=(
        "and",
        "or",
        r"as\s+well\s+as",
        "plus",
        "including",
        "but",
        "except",
        "excluding",
        "versus",
        "vs",
        "compared",
        "comparison",
        "than",
        "whereas",
        "while",
        "whilst",
        "unlike",
        r"together\s+with",
        r"along\s+with",
        "matched",
    ),
    negating_prefixes=("non",),
    enrolment=(
        r"we\s+(?:enrolled|included|recruited|randomi[sz]ed|studied)",
        r"(?:were|was)\s+(?:enrolled|included|recruited|randomi[sz]ed|eligible)",
        r"inclusion\s+criteri\w*",
    ),
    sections=(
        "background",
        "introduction",
        "objectives?",
        "aims?",
        "purpose",
        "methods?",
        "design",
        "setting",
        "participants",
        "interventions?",
        "measurements",
        "outcomes?",
        r"main\s+outcome\s+measures?",
        "results?",
        "findings",
        "conclusions?",
        "discussion",
        "interpretation",
    ),
    equivalents={"chemotherapies": "chemotherapy"},
)

GERMAN = Language(
    stopwords="""der die das und oder mit von zu im in ist sind ein eine einer eines den dem des
    für auf bei als aus mindestens nach wie""",
    negation="""kein keine keiner keinen keinem keines nicht ohne nie niemals weder
    ausgenommen""",
    function_words="""wurden wurde werden worden waren war hatten hatte haben hat vor über unter
    durch gegen bis seit sowie aber zur zum vom beim am""",
    short_words="jahr tag tage",
    clause_breaks="und oder aber sowie während",
    scope_cues=("ausgeschlossen", "ausschluss", "mit oder ohne", "unabhängig von", "nicht nur"),
    qualifiers=(
        "vor",
        "nach",
        "seit",
        "bis",
        "während",
        "jemals",
        "je",
        "jede[nmrs]?",
        "täglich",
        "wöchentlich",
        "monatlich",
        "jährlich",
        "regelmäßig",
        "gelegentlich",
        "früher",
        "zuvor",
        "bisher",
        "mindestens",
        "höchstens",
        "alter",
        "alt",
        "älter",
        "jünger",
        "jahre?n?",
        "monate?n?",
        "wochen?",
        "tage?n?",
    ),
    coordination=(
        "und",
        "oder",
        "sowie",
        "aber",
        "außer",
        "einschließlich",
        "gegenüber",
        "verglichen",
        "sondern",
    ),
    negating_prefixes=("nicht", "kein"),
    enrolment=(
        r"einschlusskriteri\w*",
        r"eingeschlossen\s+wurden",
        r"wurden\s+(?:\w+\s+)?eingeschlossen",
        r"wir\s+schlossen",
    ),
    sections=("hintergrund", "methoden", "ergebnisse", "schlussfolgerungen?"),
    equivalents={"chemotherapie": "chemotherapy"},
)

LANGUAGES: dict[str, Language] = {"en": ENGLISH, "de": GERMAN}
"""Language code (the protocol's, without a region) to its entry."""
