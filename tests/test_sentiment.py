"""Tests for sentiment analysis."""

import pytest

from ossuary.sentiment.analyzer import SentimentAnalyzer


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
