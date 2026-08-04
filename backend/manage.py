#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def _warn_on_python_version_mismatch():
    """Audit fix M24: the pinned runtime.txt version and what's actually running
    locally can silently drift, and the mismatch has produced real, misleading test
    crashes before (an unrelated Django/Python interaction broke the test client's
    error-template rendering) — wasting real debugging time misattributing an
    environment issue to an application bug. Non-fatal by design: this must never
    block CI or a deploy target using a different (but still supported) version,
    it only surfaces the drift loudly instead of silently.
    """
    try:
        runtime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.txt")
        with open(runtime_path) as f:
            pinned = f.read().strip()  # e.g. "python-3.13.2"
        pinned_version = pinned.split("-", 1)[1] if "-" in pinned else pinned
        pinned_major_minor = tuple(int(p) for p in pinned_version.split(".")[:2])
        running_major_minor = sys.version_info[:2]
        if pinned_major_minor != running_major_minor:
            print(
                f"WARNING: runtime.txt pins Python {pinned_version}, but this process is "
                f"running {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} "
                f"— version-specific behavior differences are a real, previously-hit source of "
                f"misleading test failures. Recreate your venv with the pinned version if this "
                f"is unexpected.",
                file=sys.stderr,
            )
    except Exception:
        pass  # never let this check itself block startup


def main():
    """Run administrative tasks."""
    _warn_on_python_version_mismatch()
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
