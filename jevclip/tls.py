"""TLS certificate handling for source and frozen builds."""

import os


def ensure_ca_bundle():
    """Return a CA bundle path and expose it through SSL_CERT_FILE.

    PyInstaller builds, especially on macOS, can run without a usable system
    certificate store. `certifi` is a package dependency and PyInstaller's hook
    bundles its `cacert.pem`; point Python's TLS stack at that bundle unless the
    user already supplied a valid `SSL_CERT_FILE`.
    """
    configured = os.environ.get("SSL_CERT_FILE")
    if configured and os.path.isfile(configured):
        return configured

    try:
        import certifi
    except Exception:
        return configured or None

    path = certifi.where()
    if path and os.path.isfile(path):
        os.environ["SSL_CERT_FILE"] = path
        return path
    return configured or None
