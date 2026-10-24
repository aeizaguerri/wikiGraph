"""Shared fixtures for real Postgres contract tests.

Tests own uniquely keyed rows and clean those rows explicitly; suite-wide table
truncation is intentionally absent so parallel or unrelated data is untouched.
"""
