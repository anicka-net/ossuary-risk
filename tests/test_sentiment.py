"""Tests for sentiment analysis."""

import json
from pathlib import Path

import pytest

from ossuary.sentiment.analyzer import SentimentAnalyzer


_CORPUS_PATH = Path(__file__).parent / "fixtures" / "sentiment_corpus.jsonl"


def _load_corpus():
    """Load the sentiment corpus from the JSONL fixture.

    Each line is ``{"label": "positive"|"negative", "source": str,
    "text": str}``. ``source`` tags the rough provenance bucket
    (Marak rant variants, event-stream handover language, generic OSS
    burnout discourse, etc) so that a defender of the methodology can
    trace why each example is in the set.
    """
    entries = []
    for line in _CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


_CORPUS = _load_corpus()
_POSITIVES = [e for e in _CORPUS if e["label"] == "positive"]
_NEGATIVES = [e for e in _CORPUS if e["label"] == "negative"]


class TestSentimentAnalyzer:
    """Tests for SentimentAnalyzer."""

    def setup_method(self):
        self.analyzer = SentimentAnalyzer()

    def test_analyze_positive_text(self):
        result = self.analyzer.analyze_text("This is a great release with amazing features!")
        assert result.compound_score > 0

    def test_analyze_negative_text(self):
        result = self.analyzer.analyze_text("This is terrible and broken, awful experience.")
        assert result.compound_score < 0

    def test_analyze_neutral_text(self):
        result = self.analyzer.analyze_text("Update dependency version to 2.0.1")
        assert -0.3 <= result.compound_score <= 0.3

    def test_analyze_empty_text(self):
        result = self.analyzer.analyze_text("")
        assert result.compound_score == 0

    def test_frustration_detection_free_work(self):
        result = self.analyzer.analyze_text("I'm tired of doing free work for corporations")
        assert result.frustration_detected
        assert len(result.frustration_keywords) > 0

    def test_frustration_detection_burnout(self):
        result = self.analyzer.analyze_text("I'm burned out and stepping down from this project")
        assert result.frustration_detected

    def test_frustration_detection_unpaid(self):
        result = self.analyzer.analyze_text("This is unpaid work and I can't continue")
        assert result.frustration_detected

    def test_frustration_detection_corporate(self):
        result = self.analyzer.analyze_text("Fortune 500 companies profit from corporate exploitation")
        assert result.frustration_detected

    def test_no_frustration_in_normal_text(self):
        result = self.analyzer.analyze_text("Fix bug in parsing module, update tests")
        assert not result.frustration_detected
        assert len(result.frustration_keywords) == 0

    def test_text_hash_deterministic(self):
        h1 = SentimentAnalyzer.text_hash("hello world")
        h2 = SentimentAnalyzer.text_hash("hello world")
        assert h1 == h2

    def test_text_hash_differs_for_different_text(self):
        h1 = SentimentAnalyzer.text_hash("hello")
        h2 = SentimentAnalyzer.text_hash("world")
        assert h1 != h2


class TestAggregatedSentiment:
    """Tests for aggregated sentiment analysis."""

    def setup_method(self):
        self.analyzer = SentimentAnalyzer()

    def test_analyze_commits_basic(self):
        messages = [
            "Fix critical security vulnerability",
            "Add new feature for user management",
            "Update documentation",
        ]
        result = self.analyzer.analyze_commits(messages)
        assert result.total_analyzed == 3
        assert isinstance(result.average_compound, float)

    def test_analyze_commits_empty(self):
        result = self.analyzer.analyze_commits([])
        assert result.total_analyzed == 0
        assert result.average_compound == 0

    def test_analyze_commits_frustration_count(self):
        messages = [
            "Another fix for free",
            "I'm burned out and tired of unpaid work",
            "Normal commit message",
        ]
        result = self.analyzer.analyze_commits(messages)
        assert result.frustration_count >= 1

    def test_analyze_issues_basic(self):
        issues = [
            {"title": "Bug report", "body": "Something is broken", "comments": []},
            {"title": "Feature request", "body": "Please add dark mode", "comments": []},
        ]
        result = self.analyzer.analyze_issues(issues)
        assert result.total_analyzed > 0

    def test_analyze_issues_with_comments(self):
        issues = [
            {
                "title": "Issue title",
                "body": "Issue body",
                "comments": [
                    {"body": "This is a helpful comment"},
                    {"body": "I agree, thanks for the fix"},
                ],
            }
        ]
        result = self.analyzer.analyze_issues(issues)
        assert result.total_analyzed > 0

    def test_analyze_issues_empty(self):
        result = self.analyzer.analyze_issues([])
        assert result.total_analyzed == 0

    def test_most_negative_texts_limited(self):
        """Should only keep top 5 most negative texts."""
        messages = [f"This is terrible awful horrible bad message {i}" for i in range(10)]
        result = self.analyzer.analyze_commits(messages)
        assert len(result.most_negative_texts) <= 5


class TestFrustrationTemplates:
    """v6.2: regex templates should catch paraphrases the v6.1 flat
    keyword list missed."""

    def setup_method(self):
        self.analyzer = SentimentAnalyzer()

    def test_paraphrase_no_longer_maintain(self):
        # v6.1 only had "no longer support" as a literal — v6.2 should
        # also catch the verb variants.
        result = self.analyzer.analyze_text(
            "I am no longer going to maintain this package."
        )
        assert result.frustration_detected
        assert "no_longer_support" in result.frustration_keywords

    def test_paraphrase_tired_of_fixing(self):
        result = self.analyzer.analyze_text(
            "Honestly I'm tired of fixing other people's bugs."
        )
        assert result.frustration_detected
        assert "tired_of_X" in result.frustration_keywords

    def test_paraphrase_done_with_project(self):
        result = self.analyzer.analyze_text("I am done with this project.")
        assert result.frustration_detected
        assert "done_with_project" in result.frustration_keywords

    def test_paraphrase_giving_up_on(self):
        result = self.analyzer.analyze_text("I'm giving up on maintaining this.")
        assert result.frustration_detected
        assert "giving_up_on" in result.frustration_keywords

    def test_paraphrase_unpaid_labor(self):
        result = self.analyzer.analyze_text("This is unpaid labor at this point.")
        assert result.frustration_detected
        assert "unpaid_X" in result.frustration_keywords

    def test_paraphrase_pay_maintainers(self):
        result = self.analyzer.analyze_text("Pay maintainers or fork it.")
        assert result.frustration_detected
        assert "pay_developers" in result.frustration_keywords

    def test_paraphrase_company_makes_millions(self):
        result = self.analyzer.analyze_text(
            "Companies make millions off my code and contribute nothing."
        )
        assert result.frustration_detected
        assert "corporate_exploitation" in result.frustration_keywords

    def test_marak_rant_still_caught(self):
        # Regression: Marak Squires' actual Nov 2020 phrasing must
        # remain detected after the rule rewrite. "Fortune 500" alone
        # was dropped as a literal in v6.2.1 (it fired on neutral
        # marketing copy like "we support Fortune 500 companies");
        # the meaningful Marak signals — refusal-to-support and
        # free-labor — still fire and are sufficient.
        result = self.analyzer.analyze_text(
            "With all due respect, I am no longer going to support "
            "the Fortune 500 with my free work."
        )
        assert result.frustration_detected
        labels = set(result.frustration_keywords)
        assert "no_longer_support" in labels
        assert "free_labor" in labels

    def test_normal_dev_text_not_flagged(self):
        # Negative corpus: ordinary changelog/PR language must NOT
        # trip the templates. "tired of \w+ing" is the most aggressive
        # template — make sure it doesn't fire on neutral text.
        for text in [
            "Refactor parser to support nested expressions.",
            "Bump dependency to fix CVE-2025-1234.",
            "Add docs for the new sponsorship tier.",
            "We support Python 3.10+.",
            "Stop iterating once the buffer is full.",
        ]:
            result = self.analyzer.analyze_text(text)
            assert not result.frustration_detected, (
                f"False positive on neutral text: {text!r} → "
                f"{result.frustration_keywords}"
            )

    def test_determinism(self):
        # Same input must produce the same labels in the same order
        # across calls — list (not set) iteration order is part of the
        # determinism contract.
        text = (
            "I'm burned out, tired of fixing this, and giving up on "
            "maintaining the project."
        )
        first = self.analyzer.analyze_text(text)
        second = self.analyzer.analyze_text(text)
        assert first.frustration_keywords == second.frustration_keywords
        assert first.compound_score == second.compound_score


class TestAuthorAttribution:
    """v6.2: frustration scoring should be restricted to maintainer-
    authored text when the caller supplies a maintainer set."""

    def setup_method(self):
        self.analyzer = SentimentAnalyzer()
        self.frustration_text = (
            "I am no longer going to maintain this. Tired of fixing "
            "everyone else's bugs."
        )

    def _issue(self, author, body, comments=None):
        return {
            "title": "Whatever",
            "body": body,
            "author_login": author,
            "comments": comments or [],
        }

    def test_maintainer_authored_counted(self):
        issues = [self._issue("alice", self.frustration_text)]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins={"alice"}
        )
        assert result.frustration_count >= 1

    def test_user_authored_not_counted(self):
        # Same frustration body, but written by a random user.
        issues = [self._issue("random_user", self.frustration_text)]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins={"alice"}
        )
        assert result.frustration_count == 0
        # But VADER still scored the text — so total_analyzed reflects
        # that the community-mood signal wasn't dropped.
        assert result.total_analyzed >= 2  # title + body

    def test_no_maintainer_set_falls_back(self):
        # Backward compat: when maintainer_logins is None, the v6.1
        # behaviour (count every text) is preserved.
        issues = [self._issue("random_user", self.frustration_text)]
        result = self.analyzer.analyze_issues(issues)
        assert result.frustration_count >= 1

    def test_bot_author_always_excluded(self):
        # Even when no maintainer set is supplied, [bot] accounts
        # must not contribute frustration evidence.
        issues = [self._issue("dependabot[bot]", self.frustration_text)]
        result = self.analyzer.analyze_issues(issues)
        assert result.frustration_count == 0

    def test_bot_author_excluded_with_maintainer_set(self):
        # Defense in depth: even if a bot login somehow appeared in
        # the maintainer set, bot text still gets dropped.
        issues = [self._issue("dependabot[bot]", self.frustration_text)]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins={"dependabot[bot]"}
        )
        assert result.frustration_count == 0

    def test_comment_author_attribution(self):
        # Maintainer wrote the issue, but the frustrated comment is
        # from a random user — frustration must NOT be counted.
        issues = [
            {
                "title": "Bug report",
                "body": "Found a bug in parsing.",
                "author_login": "alice",
                "comments": [
                    {"author": "random_user", "body": self.frustration_text},
                ],
            }
        ]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins={"alice"}
        )
        assert result.frustration_count == 0

    def test_maintainer_comment_counted(self):
        # Inverse: maintainer's frustrated comment on a user's issue.
        issues = [
            {
                "title": "Bug report",
                "body": "Found a bug",
                "author_login": "random_user",
                "comments": [
                    {"author": "alice", "body": self.frustration_text},
                ],
            }
        ]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins={"alice"}
        )
        assert result.frustration_count >= 1

    def test_login_matching_is_case_insensitive(self):
        issues = [self._issue("Alice", self.frustration_text)]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins={"alice"}
        )
        assert result.frustration_count >= 1

    def test_empty_maintainer_set_falls_back(self):
        # Same as None: callers shouldn't have to special-case "we
        # don't know the maintainer yet".
        issues = [self._issue("random_user", self.frustration_text)]
        result = self.analyzer.analyze_issues(
            issues, maintainer_logins=set()
        )
        assert result.frustration_count >= 1


# --- Corpus-driven coverage tests -----------------------------------
# The corpus lives at tests/fixtures/sentiment_corpus.jsonl and
# tags each entry with a ``source`` bucket for thesis defensibility
# (Marak variants, event-stream handover language, generic burnout
# discourse, sabotage precursors, dev-code negatives). When adding
# or changing rules, update the corpus first and drive the rule
# changes from the coverage report.


class TestCorpusCoverage:
    """Bulk coverage checks against the committed corpus.

    These are fail-loud thresholds — they're not a replacement for
    the targeted tests above, but they catch regressions when a rule
    change silently drops coverage on a whole source bucket.
    """

    def setup_method(self):
        self.analyzer = SentimentAnalyzer()

    def test_corpus_is_non_trivial(self):
        # Guard against the file being accidentally truncated or lost.
        assert len(_POSITIVES) >= 40, (
            f"corpus has only {len(_POSITIVES)} positives; the "
            f"thresholds below assume a meaningful sample"
        )
        assert len(_NEGATIVES) >= 20, (
            f"corpus has only {len(_NEGATIVES)} negatives"
        )

    def test_positive_recall(self):
        missed = [
            e for e in _POSITIVES
            if not self.analyzer.analyze_text(e["text"]).frustration_detected
        ]
        recall = 1 - len(missed) / len(_POSITIVES)
        # v6.2 rules hit 100% on this committed corpus; the threshold
        # is set slightly below to allow small corpus additions
        # without immediately failing CI — but a significant drop
        # here means a regression.
        assert recall >= 0.95, (
            f"positive recall dropped to {recall:.1%} "
            f"(missed {len(missed)} / {len(_POSITIVES)}): "
            f"{[m['text'] for m in missed[:5]]}"
        )

    def test_negative_specificity(self):
        fp = [
            e for e in _NEGATIVES
            if self.analyzer.analyze_text(e["text"]).frustration_detected
        ]
        fp_rate = len(fp) / len(_NEGATIVES)
        assert fp_rate <= 0.05, (
            f"false-positive rate rose to {fp_rate:.1%} "
            f"(fired on {len(fp)} / {len(_NEGATIVES)}): "
            f"{[(e['text'], self.analyzer.analyze_text(e['text']).frustration_keywords) for e in fp[:5]]}"
        )

    @pytest.mark.parametrize(
        "bucket",
        ["marak_2020", "burnout_corpus", "funding_corpus",
         "sabotage_precursor"],
    )
    def test_per_bucket_recall(self, bucket):
        # Per-source recall so a rule change can't silently gut one
        # whole category while staying above the global 95% bar.
        entries = [e for e in _POSITIVES if e["source"] == bucket]
        if not entries:
            pytest.skip(f"bucket {bucket!r} not in corpus")
        missed = [
            e for e in entries
            if not self.analyzer.analyze_text(e["text"]).frustration_detected
        ]
        recall = 1 - len(missed) / len(entries)
        assert recall >= 0.8, (
            f"bucket {bucket!r} recall {recall:.1%} "
            f"(missed {[m['text'] for m in missed]})"
        )


class TestGovernanceLifecycleNotFrustration:
    """Regression guard for GPT 2026-04-19 review.

    Healthy OSS governance / deprecation / handover language and
    routine release-management asks must NOT trigger the +20
    frustration factor. The principle: emotional / personal exit
    signals fire frustration; clinical lifecycle announcements do
    not. Genuinely-frustrated handover ("I'm walking away from
    this", "I quit") still trips emotional rules.
    """

    def setup_method(self):
        self.analyzer = SentimentAnalyzer()

    @pytest.mark.parametrize("text", [
        # Succession / handover (orderly)
        "looking for a new maintainer for this package",
        "find another maintainer if you need faster reviews",
        "transfer ownership to the new team",
        "I'm transferring ownership of this repo to the new team",
        # Deprecation / EOL announcements
        "this project is no longer actively maintained",
        "this is the last release supporting Python 3.8",
        "this will be unmaintained going forward",
        # Operational / process language
        "we support Fortune 500 companies",
        "without funding we cannot continue the benchmark run",
        "out of bandwidth for this milestone",
        "need a break from coding this weekend",
        "please stop opening PRs against the release branch",
        "Please stop opening issues without a reproducer.",
    ])
    def test_governance_text_does_not_fire_frustration(self, text):
        result = self.analyzer.analyze_text(text)
        assert not result.frustration_detected, (
            f"governance/lifecycle text triggered frustration: "
            f"{text!r} → {result.frustration_keywords}"
        )

    @pytest.mark.parametrize("text", [
        # Genuinely-frustrated exit MUST still fire — these are the
        # signals the dropped/tightened rules used to share with
        # neutral lifecycle text. Their detection is now load-bearing
        # on the emotional rules instead.
        "I'm walking away from this project.",
        "I quit.",
        "I am sick and tired of doing this for nothing.",
        "I don't have the bandwidth anymore.",
        "After years of unpaid maintenance I'm done.",
        "I am no longer going to support the Fortune 500 with my free work.",
    ])
    def test_emotional_exit_still_fires(self, text):
        result = self.analyzer.analyze_text(text)
        assert result.frustration_detected, (
            f"emotional exit signal lost: {text!r}"
        )
