"""Moment research agent CLI."""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "moment-cli"

try:
    __version__ = version(PACKAGE_NAME)
except PackageNotFoundError:
    # Source checkouts are importable before the distribution is installed.
    __version__ = "2.6.0"
