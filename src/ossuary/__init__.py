"""
Ossuary - OSS Supply Chain Risk Scoring

Where abandoned packages come to rest.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("ossuary-risk")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
