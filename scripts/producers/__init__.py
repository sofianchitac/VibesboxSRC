"""Metadata producers. Each producer owns one external source of truth."""

from .base import Producer, Sink

__all__ = ["Producer", "Sink"]
