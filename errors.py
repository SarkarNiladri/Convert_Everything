"""Shared exception type, kept in its own module so converter modules can import it
without importing each other."""


class ConversionError(Exception):
    """A user-facing conversion problem (bad input, unsupported pair, tool failure)."""
