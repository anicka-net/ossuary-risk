"""Sentiment analysis for maintainer communications.

The analyzer has two layers:

1. **VADER general sentiment** — runs across every text we have
   (commits, issue bodies, comments) and feeds the ±10 sentiment
   factor as a community-mood signal.
2. **Rule-based frustration detection** — a curated set of regex
   templates plus literal phrases that target burnout / sabotage
   precursor language. Frustration evidence drives the +20 risk
   factor in the engine.

Two design choices that matter for thesis defensibility:

- **Author attribution.** Frustration is a signal about the
  *maintainer*, not random users complaining about the project. When
  the caller supplies the maintainer login(s) via
  ``maintainer_logins``, frustration scoring is restricted to text
  authored by those users (bot accounts are always excluded). VADER
  still scans everything for the broader mood signal. Without an
  ``author_login`` (or with ``maintainer_logins=None``), the analyzer
  falls back to scanning every text for frustration — preserving the
  pre-v6.2 behaviour and keeping the API backward-compatible.
- **Rule templates over literal keywords.** The flat keyword list
  used through v6.1 caught Marak Squires' Nov 2020 rant by exact
  phrase ("free work", "no longer support") but missed paraphrases.
  v6.2 replaces it with a small set of regex *templates* that
  capture verb stems and structural patterns ("tired of \\w+ing",
  "no longer (going to )?(support|maintain)") plus literal fallbacks
  for multi-word phrases that don't generalise cleanly. Both layers
  are 100% deterministic — same input, same output.

VADER scored Marak's actual sabotage rant at +0.676 (positive!) due
to words like "support" and "opportunity". The rule layer is the
backstop that keeps the obvious frustration cases visible.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)


# Rule-based frustration detection.
#
# Each rule is ``(label, regex_pattern)``. Templates come first because
# they capture broader paraphrases; literal multi-word phrases follow
# as fallbacks for the cases where templates over- or under-match.
# Patterns are matched case-insensitively against ``text.lower()`` so
# the patterns themselves can stay lowercase.
#
# When changing this list, run the corpus tests in
# tests/test_sentiment.py — paraphrases of Marak Squires' Nov 2020
# rant and a curated negatives set must continue to behave correctly.
FRUSTRATION_RULES: list[tuple[str, str]] = [
    # --- Templates (verb-stem and structural patterns) -------------
    ("tired_of_X",
        r"\btired of \w+(ing|s)?\b"),
    ("no_longer_support",
        r"\bno longer (going to |gonna |willing to )?"
        r"(support|maintain|fix|work on|contribute|develop)\w*\b"),
    ("stop_supporting",
        r"\b(going to )?stop(ping|ped)? "
        r"(support|maintain|fix|contribut|develop|work|"
        r"merg|review|releas|publish|answer|respond)\w*\b"),
    ("done_with_project",
        r"\bdone (with|maintaining|supporting|fixing|answering) "
        r"(this|the|my)\b"),
    # First-person guard: "Don't give up on the tests" must NOT fire,
    # but "I'm giving up on maintaining this" must.
    ("giving_up_on",
        r"\b(?:i(?:'m| am| have|'ve)?|we(?:'re| are|'ve)?)\s+"
        r"giv(e|en|ing) up (on )?"
        r"(this|the|maintaining|supporting|trying|fighting)\b"),
    # "free time" is mostly benign in dev contexts ("free time fun"),
    # so only "free work" / "free labor" / "free hours" fire here.
    ("free_labor",
        r"\b(my )?free (work|labor|hours)\b"),
    ("unpaid_X",
        r"\bunpaid \w+\b"),
    ("pay_developers",
        r"\bpay (me|us|developers|maintainers|the developers|the maintainers)\b"),
    ("corporate_exploitation",
        r"\b(corporate|company|companies|corporation|corporations|"
        r"fortune\s*500|big ?tech|google|amazon|microsoft|meta|facebook) "
        r"(exploit\w*|profit\w*|making millions|make millions|"
        r"makes millions|profiting)\w*\b"),
    ("work_for_free",
        r"\bwork(ing)? for free\b"),
    # "Why am I doing this for free?" / "Doing this for nothing"
    # "for free time" is benign ("I do this in my free time"), so a
    # negative lookahead drops the "free time …" follow-on.
    ("doing_for_nothing",
        r"\b(doing|working on|maintain(ing)?) (this|it|all of this) "
        r"for (free|nothing)(?!\s+time)\b"),
    ("why_am_i_doing",
        r"\bwhy am i (doing|maintaining|fixing|supporting|reviewing)\b"),
    ("donating_time",
        r"\bdonat(e|ing) (my )?(time|labor|work)\b"),
    ("abandoning_project",
        r"\babandon\w* (this|the) (project|repo|codebase|library|package)\b"),
    ("walking_away",
        r"\b(walking|walked) away (from )?(this|the project|open source|oss)?\b"),
    ("archiving_repo",
        r"\barchiv\w* (this|the) (repo|repository|project|package)\b"),
    ("delete_repo",
        r"\bdelet\w* (the|this|my) (repo|repository|project|package|account)\b"),
    ("break_on_purpose",
        r"\bbreak\w*\s+(this|it)\s+on purpose\b"),
    ("teach_corporations_lesson",
        r"\bteach (them|you|these (corporations|companies|users)) a lesson\b"),
    ("lost_interest",
        r"\blost (all )?(interest|motivation|enthusiasm|enjoyment|will|patience|hope)\b"),
    ("dont_enjoy_anymore",
        r"\b(don'?t enjoy|no longer enjoy|stopped enjoying) (this|it|maintaining|coding|oss|open source)?\b"),
    ("no_more_free",
        r"\bno more free \w+\b"),
    ("need_break_from",
        r"\bneed a break from (this|open source|oss|maintaining|coding|github)\b"),
    # Will-not / not-going-to refusals.
    ("will_not_be",
        r"\bwill not be (reviewing|fixing|maintaining|supporting|merging|"
        r"answering|releasing|publishing)\b"),
    ("dont_expect_more",
        r"\bdon'?t expect (any more|more|further) "
        r"(updates|releases|fixes|features|prs|reviews)\b"),
    # Handover / replacement-seeking signals.
    ("find_another_maintainer",
        r"\bfind (another|a new|someone else as) maintainer\b"),
    ("looking_for_maintainer",
        r"\blooking for (a )?(new )?maintainer\b"),
    # Funding-or-die and sponsor demands.
    # Sponsor / fund-or-die, with optional "on GitHub" / "on Patreon".
    ("sponsor_or_die",
        r"\b(sponsor (me|us)|fund (this|us|me|the project))"
        r"(\s+on\s+\w+)?\s+or\s+(this|the)\s+(project|repo|library)\s+"
        r"(will\s+)?(die|stop|end|be archived|shut down|go unmaintained)\b"),
    ("without_funding",
        r"\bwithout (funding|sponsorship|sponsors|donations|payment) "
        r"(i|we) (cannot|can'?t|won'?t) (continue|maintain|sustain|keep)\b"),
    # Allow an optional adjective ("six figure yearly contract").
    ("six_figure",
        r"\bsix(\s|-)?figure (\w+ )?(contract|salary|deal|sponsorship|"
        r"retainer|payment)\b"),
    ("no_time_to_maintain",
        r"\bdon'?t (really )?have (the )?time (to|for) "
        r"(maintain|fix|review|support|work on)\b"),
    ("handover_signal",
        r"\b(give|grant|hand(ing)? over) (you|someone|them|whoever|the next)\s*"
        r"(access|the keys|maintainer|publish access|push access|"
        r"commit access|ownership|the repo|the project)\b"),
    ("please_stop_opening",
        r"\b(please )?stop (opening|filing|submitting|making|posting) "
        r"(issues|tickets|prs|requests|bug reports)\b"),
    # Complaint-volume / entitled-users frustration.
    ("thankless",
        r"\bthankless\b"),
    ("nothing_but_complaints",
        r"\bnothing but (complaints|whining|demands|entitlement)\b"),
    ("entitled_users",
        r"\bentitled (users|developers|companies|consumers|clients)\b"),
    ("never_contribute_back",
        r"\bnever contribut\w+ back\b"),
    # Personal-state burnout (require first-person to avoid meta/neg).
    ("im_exhausted",
        r"\bi(?:'m| am)\s+(exhausted|drained|done|spent|burnt? out)\b"),
    ("i_quit",
        r"\bi (quit|resign|am quitting|am resigning|am out|'m out|'m quitting|'m resigning)\b"),
    ("no_bandwidth",
        r"\b(don'?t have|out of|running out of) (the )?"
        r"(bandwidth|capacity|energy|patience|time)\b"),
    ("cant_keep_doing",
        r"\bcan'?t keep (doing|maintaining|supporting|fixing|reviewing)\b"),
    ("transferring_ownership",
        r"\b(transfer(ring|red)?|hand(ing|ed)? over|passing on|giving up) "
        r"(ownership|the project|the repo|the keys|maintainer(ship)?)\b"),
    ("unmaintained_signal",
        r"\b(no longer (actively )?maintained|"
        r"(will be|is now|going) unmaintained|"
        r"this project is dead|this is dead)\b"),
    ("last_release",
        r"\b(my|the|this is the) last release\b"),
    ("never_paid",
        r"\bnever paid (a dime|a cent|me|us|anything|the maintainer)\b"),
    ("where_are_sponsors",
        r"\bwhere are (the|my|our) (sponsors|donations|donors|funders|sponsorships)\b"),
    ("feeling_unappreciated",
        r"\bfeel(ing|s)? (unappreciated|undervalued|abandoned|invisible|"
        r"taken for granted|exploited)\b"),
    ("no_energy_left",
        r"\b(no|zero) energy (left )?(for|to)\b"),
    ("taking_a_stand",
        r"\btaking a stand\b"),
    ("alone_for_years",
        r"\b(maintaining|working on|supporting) (this|it) alone\b"),

    # --- Literal multi-word phrases (fallbacks) --------------------
    ("not_getting_paid",
        r"\bnot getting (paid|compensated)\b"),
    ("not_getting_compensation",
        r"\bnot getting (any )?compensation\b"),
    ("burned_out",
        r"\b(burn(ed|t|ing) out|burnout)\b"),
    ("stepping_down",
        r"\bstepping down\b"),
    ("mass_resignation",
        r"\bmass resignation\b"),
    ("open_source_exploitation",
        r"\bopen source exploitation\b"),
    ("fortune_500",
        r"\bfortune\s*500\b"),
    ("fund_open_source",
        r"\bfund open source\b"),
    ("on_strike",
        r"\bon strike\b"),
    # "Boycott" / "resentment" are kept as literals but constrained to
    # verb / noun-phrase contexts to avoid the meta-usage false
    # positives ("boycott of legacy browser support",
    # "resentment-driven design").
    ("protest",
        r"\b(in protest|protest(ing|ed)? (against|the))\b|"
        r"\b(act|form|sign) of protest\b"),
    ("boycott",
        r"\b(call(ing)? for|in|launch(ing|ed)?|organi[sz](e|ing|ed)) "
        r"(a |an )?boycott\b|"
        r"\bboycott(ing|ed) (the|all|companies|corporations)\b"),
    ("resentment",
        r"\b(my|growing|building|deep) resent(ment|ful)\b|"
        r"\bfilled with resentment\b"),
    ("taken_advantage_of",
        r"\btaken advantage of\b"),
]


# Backward-compatible alias. Pre-v6.2 callers (and a couple of
# scripts) imported ``FRUSTRATION_KEYWORDS`` directly. The list now
# exposes the rule *labels* — semantically equivalent to "what kinds
# of frustration we look for" but no longer the literal substrings.
FRUSTRATION_KEYWORDS: list[str] = [label for label, _ in FRUSTRATION_RULES]


@dataclass
class SentimentResult:
    """Result of sentiment analysis for a single text."""

    text_hash: str
    compound_score: float  # -1 (negative) to +1 (positive)
    positive_score: float
    negative_score: float
    neutral_score: float
    frustration_detected: bool = False
    # Labels of the rules that fired (e.g. ``"free_labor"``,
    # ``"no_longer_support"``). Pre-v6.2 this was the literal matched
    # substring; the field name is kept for backward compatibility.
    frustration_keywords: list[str] = field(default_factory=list)


@dataclass
class AggregatedSentiment:
    """Aggregated sentiment analysis results."""

    total_analyzed: int = 0
    average_compound: float = 0.0
    average_positive: float = 0.0
    average_negative: float = 0.0
    frustration_count: int = 0
    frustration_evidence: list[str] = field(default_factory=list)
    most_negative_texts: list[tuple[str, float]] = field(default_factory=list)  # (text_preview, score)


def _is_bot_login(login: Optional[str]) -> bool:
    """Heuristic for GitHub bot accounts.

    GitHub appends ``[bot]`` to bot account logins (e.g.
    ``dependabot[bot]``). Some service accounts use the ``-bot``
    suffix as a convention. We exclude both from frustration scoring
    because bot-authored text isn't a maintainer-burnout signal.
    """
    if not login:
        return False
    lowered = login.lower()
    return "[bot]" in lowered or lowered.endswith("-bot")


class SentimentAnalyzer:
    """
    Sentiment analyzer for OSS maintainer communications.

    Uses VADER for general sentiment analysis and rule-based pattern
    matching for frustration detection. See module docstring for the
    two-layer design and the role of ``maintainer_logins``.
    """

    def __init__(self):
        """Initialize the sentiment analyzer."""
        self.vader = SentimentIntensityAnalyzer()
        self.frustration_rules: list[tuple[str, re.Pattern]] = [
            (label, re.compile(pattern, re.IGNORECASE))
            for label, pattern in FRUSTRATION_RULES
        ]

    @staticmethod
    def text_hash(text: str) -> str:
        """Generate hash for text deduplication."""
        return hashlib.sha256(text.encode()).hexdigest()

    def _detect_frustration(self, text: str) -> tuple[bool, list[str]]:
        """Detect frustration rule hits in text.

        Returns ``(detected, labels)`` where ``labels`` is the list of
        rule labels that matched, in declaration order (deterministic).
        """
        if not text:
            return False, []
        # The patterns themselves carry ``re.IGNORECASE`` already, so
        # there is no need to lower-case the input — but we keep doing
        # so for parity with the pre-v6.2 implementation, which means
        # any non-ASCII case-folding behaviour stays identical.
        text_lower = text.lower()
        found_labels = []
        for label, pattern in self.frustration_rules:
            if pattern.search(text_lower):
                found_labels.append(label)
        return bool(found_labels), found_labels

    def analyze_text(self, text: str) -> SentimentResult:
        """
        Analyze sentiment of a single text.

        Args:
            text: Text to analyze

        Returns:
            SentimentResult with scores
        """
        if not text or not text.strip():
            return SentimentResult(
                text_hash=self.text_hash(""),
                compound_score=0.0,
                positive_score=0.0,
                negative_score=0.0,
                neutral_score=1.0,
            )

        # VADER sentiment scores
        scores = self.vader.polarity_scores(text)

        # Frustration detection
        frustration_detected, labels = self._detect_frustration(text)

        return SentimentResult(
            text_hash=self.text_hash(text),
            compound_score=scores["compound"],
            positive_score=scores["pos"],
            negative_score=scores["neg"],
            neutral_score=scores["neu"],
            frustration_detected=frustration_detected,
            frustration_keywords=labels,
        )

    def analyze_texts(
        self,
        texts: list[str],
        source_type: str = "unknown",
    ) -> AggregatedSentiment:
        """Analyze multiple texts and aggregate results.

        Author-agnostic path — every text counts toward both VADER
        averages and frustration counts. Use ``analyze_authored_texts``
        when you have authorship metadata and want to restrict
        frustration scoring to the maintainer.
        """
        if not texts:
            return AggregatedSentiment()
        return self.analyze_authored_texts(
            [(None, t) for t in texts],
            maintainer_logins=None,
            source_type=source_type,
        )

    def analyze_authored_texts(
        self,
        authored_texts: list[tuple[Optional[str], str]],
        maintainer_logins: Optional[Iterable[str]] = None,
        source_type: str = "unknown",
    ) -> AggregatedSentiment:
        """Analyze texts with optional author-attribution filtering.

        Args:
            authored_texts: List of ``(author_login, text)`` tuples.
                ``author_login`` may be ``None`` when authorship isn't
                known.
            maintainer_logins: Iterable of GitHub logins considered
                maintainers. When non-empty, frustration is *only*
                counted for texts authored by one of these logins;
                bot-authored text is always excluded. ``None`` (the
                default) preserves the pre-v6.2 behaviour: frustration
                scoring runs across every text.
            source_type: Tag used in evidence strings (commit / issue /
                comment / etc).

        Returns:
            AggregatedSentiment. ``total_analyzed`` counts every
            non-empty text (the VADER denominator). ``frustration_count``
            only includes texts that passed the maintainer filter.
        """
        if not authored_texts:
            return AggregatedSentiment()

        # Normalise the maintainer set once. ``None`` → no filtering;
        # empty iterable → also no filtering (callers shouldn't have to
        # special-case "we have no maintainer username yet").
        if maintainer_logins:
            allowed = {
                login.lower()
                for login in maintainer_logins
                if login
            }
        else:
            allowed = None

        results = []
        frustration_evidence = []
        negative_texts = []

        for author_login, text in authored_texts:
            if not text or not text.strip():
                continue

            result = self.analyze_text(text)
            results.append(result)

            # Decide whether this text is allowed to *contribute* to
            # the frustration count. We always exclude bot-authored
            # text. When the caller provided a maintainer set, we
            # additionally require the author to be in it.
            if _is_bot_login(author_login):
                count_frustration = False
            elif allowed is None:
                count_frustration = True
            elif author_login and author_login.lower() in allowed:
                count_frustration = True
            else:
                count_frustration = False

            if count_frustration and result.frustration_detected:
                frustration_evidence.append(
                    f"[{source_type}] {author_login or 'unknown'}: "
                    f"matched rules {result.frustration_keywords}"
                )
            elif not count_frustration and result.frustration_detected:
                # Drop the per-result frustration flag so downstream
                # consumers don't accidentally re-count this hit (it
                # would otherwise still appear in ``frustration_keywords``
                # via ``analyze_text``). The aggregated counters are
                # what flow into the engine, but we keep the result
                # fields honest too.
                result.frustration_detected = False
                result.frustration_keywords = []

            if result.compound_score < -0.3:
                preview = text[:100] + "..." if len(text) > 100 else text
                negative_texts.append((preview, result.compound_score))

        if not results:
            return AggregatedSentiment()

        avg_compound = sum(r.compound_score for r in results) / len(results)
        avg_positive = sum(r.positive_score for r in results) / len(results)
        avg_negative = sum(r.negative_score for r in results) / len(results)
        frustration_count = sum(1 for r in results if r.frustration_detected)

        negative_texts.sort(key=lambda x: x[1])

        return AggregatedSentiment(
            total_analyzed=len(results),
            average_compound=avg_compound,
            average_positive=avg_positive,
            average_negative=avg_negative,
            frustration_count=frustration_count,
            frustration_evidence=frustration_evidence[:10],
            most_negative_texts=negative_texts[:5],
        )

    def analyze_commits(
        self,
        commit_messages: list[str],
    ) -> AggregatedSentiment:
        """Analyze sentiment of commit messages.

        Commits already imply maintainer authorship (someone with push
        access wrote them), so we don't bother with the
        ``maintainer_logins`` filter here.
        """
        return self.analyze_texts(commit_messages, source_type="commit")

    def analyze_issues(
        self,
        issues: list[dict],
        maintainer_logins: Optional[Iterable[str]] = None,
    ) -> AggregatedSentiment:
        """Analyze sentiment of issues and their comments.

        Args:
            issues: List of issue dicts. Each dict should carry
                ``title``, ``body``, ``comments`` (list of
                ``{body, author?, ...}`` dicts) and, ideally,
                ``author_login`` for the issue itself. When
                ``author_login`` is missing, the text is treated as
                authored by an unknown user — frustration scoring then
                only counts it if ``maintainer_logins`` is also
                ``None`` (no filtering).
            maintainer_logins: GitHub logins to treat as maintainers
                for frustration attribution. See
                ``analyze_authored_texts``.

        Returns:
            AggregatedSentiment for all issue content.
        """
        authored_texts: list[tuple[Optional[str], str]] = []

        for issue in issues:
            issue_author = issue.get("author_login") or issue.get("author")
            title = issue.get("title", "")
            body = issue.get("body", "")
            if title:
                authored_texts.append((issue_author, title))
            if body:
                authored_texts.append((issue_author, body))

            for comment in issue.get("comments", []):
                if isinstance(comment, dict):
                    comment_author = comment.get("author") or comment.get("author_login")
                    comment_body = comment.get("body", "")
                else:
                    comment_author = None
                    comment_body = str(comment)
                if comment_body:
                    authored_texts.append((comment_author, comment_body))

        return self.analyze_authored_texts(
            authored_texts,
            maintainer_logins=maintainer_logins,
            source_type="issue",
        )
