"""Test-environment shim — NOT used in Docker production runtime.

The Dockerfile copies only app/ so this directory is never present in
deployed images. It exists here so CI test environments that do not have
the real google-cloud packages installed can still collect and run tests
without TransportError or ImportError failures.
"""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
