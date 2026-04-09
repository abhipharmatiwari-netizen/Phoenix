"""Namespace-friendly google package shim for lightweight tests."""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
