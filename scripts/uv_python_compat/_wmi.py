"""Disable Python 3.12's WMI platform probe when the Windows WMI service hangs."""

raise ImportError("Use platform.py's non-WMI Windows fallback")
