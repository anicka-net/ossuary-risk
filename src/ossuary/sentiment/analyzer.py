"""Sentiment analysis for maintainer communications."""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)

# Keywords indicating maintainer frustration/burnout
# These should be specific enough to avoid false positives on normal development discussions
FRUSTRATION_KEYWORDS = [
    # Direct economic frustration (high signal)
    "not getting paid",
    "unpaid work",
    "free labor",
    "work for free",
    "donating my time",
    "corporate exploitation",
    "open source exploitation",
    "mass resignation",
    # Burnout signals (moderate signal)
    "burned out",
    "burnout",
    "stepping down",
    "giving up on this",
    "abandoning this project",
    # Economic frustration (moderate signal)
    "fortune 500",
    "pay developers",
    "fund open source",
    "companies make millions",
    # Protest signals (high signal)
    "protest",
    "on strike",
    "boycott",
    # Explicit negative emotions (only strong ones)
    "resentment",
    "exploitation",
    "taken advantage of",
]


@dataclass
class SentimentResult:
    """Result of sentiment analysis for a single text."""

    text_hash: str
    compound_score: float  # -1 (negative) to +1 (positive)
    positive_score: float
    negative_score: float
    neutral_score: float
    frustration_detected: bool = False
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


class SentimentAnalyzer:
    """
    Sentiment analyzer for OSS maintainer communications.

    Uses VADER for general sentiment analysis and keyword matching
    for frustration detection.
    """

    def __init__(self):
        """Initialize the sentiment analyzer."""
        self.vader = SentimentIntensityAnalyzer()
        self.frustration_patterns = [re.compile(rf"\b{kw}\b", re.IGNORECASE) for kw in FRUSTRATION_KEYWORDS]

    @staticmethod
    def text_hash(text: str) -> str:
        """Generate hash for text deduplication."""
        return hashlib.sha256(text.encode()).hexdigest()

    def _detect_frustration(self, text: str) -> tuple[bool, list[str]]:
        """
        Detect frustration keywords in text.

        Args:
            text: Text to analyze

        Returns:
            Tuple of (detected, keywords_found)
        """
        text_lower = text.lower()
        found_keywords = []

        for i, pattern in enumerate(self.frustration_patterns):
            if pattern.search(text_lower):
                found_keywords.append(FRUSTRATION_KEYWORDS[i])

        return len(found_keywords) > 0, found_keywords

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
        frustration_detected, keywords = self._detect_frustration(text)

        return SentimentResult(
            text_hash=self.text_hash(text),
            compound_score=scores["compound"],
            positive_score=scores["pos"],
            negative_score=scores["neg"],
            neutral_score=scores["neu"],
            frustration_detected=frustration_detected,
            frustration_keywords=keywords,
        )

    def analyze_texts(self, texts: list[str], source_type: str = "unknown") -> AggregatedSentiment:
        """
        Analyze multiple texts and aggregate results.

        Args:
            texts: List of texts to analyze
            source_type: Type of source (commit, issue, comment) for reporting

        Returns:
            AggregatedSentiment with aggregated results
        """
        if not texts:
            return AggregatedSentiment()

        results = []
        frustration_evidence = []
        negative_texts = []

        for text in texts:
            if not text or not text.strip():
                continue

            result = self.analyze_text(text)
            results.append(result)

            if result.frustration_detected:
                preview = text[:100] + "..." if len(text) > 100 else text
                frustration_evidence.append(f"[{source_type}] Found keywords: {result.frustration_keywords}")

            if result.compound_score < -0.3:
                preview = text[:100] + "..." if len(text) > 100 else text
                negative_texts.append((preview, result.compound_score))

        if not results:
            return AggregatedSentiment()

        # Calculate averages
        avg_compound = sum(r.compound_score for r in results) / len(results)
        avg_positive = sum(r.positive_score for r in results) / len(results)
        avg_negative = sum(r.negative_score for r in results) / len(results)
        frustration_count = sum(1 for r in results if r.frustration_detected)

        # Sort negative texts by score
        negative_texts.sort(key=lambda x: x[1])

        return AggregatedSentiment(
            total_analyzed=len(results),
            average_compound=avg_compound,
            average_positive=avg_positive,
            average_negative=avg_negative,
            frustration_count=frustration_count,
            frustration_evidence=frustration_evidence[:10],  # Limit to 10 examples
            most_negative_texts=negative_texts[:5],  # Top 5 most negative
        )

    def analyze_commits(self, commit_messages: list[str]) -> AggregatedSentiment:
        """Analyze sentiment of commit messages."""
        return self.analyze_texts(commit_messages, source_type="commit")

    def analyze_issues(self, issues: list[dict]) -> AggregatedSentiment:
        """
        Analyze sentiment of issues and their comments.

        Args:
            issues: List of issue dicts with 'title', 'body', and 'comments' keys

        Returns:
            AggregatedSentiment for all issue content
        """
        texts = []

        for issue in issues:
            # Issue title and body
            title = issue.get("title", "")
            body = issue.get("body", "")
            if title:
                texts.append(title)
            if body:
                texts.append(body)

            # Comments
            comments = issue.get("comments", [])
            for comment in comments:
                comment_body = comment.get("body", "") if isinstance(comment, dict) else str(comment)
                if comment_body:
                    texts.append(comment_body)

        return self.analyze_texts(texts, source_type="issue")
