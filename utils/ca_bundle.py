# ============================================================
# File   : utils/ca_bundle.py
# Version: V1-CA-BUNDLE-CERTIFI-PLUS-WINDOWS-ROOT
# ------------------------------------------------------------
# certifi と Windows 証明書ストアを合成した CA バンドルを作る。
#
# 背景 (2026-09-01):
#   Discord webhook が
#     ssl.SSLCertVerificationError:
#     [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
#   で失敗していた。実測:
#     certifi のみ                 -> discord.com 検証失敗
#     certifi + Windows ROOT 合成  -> discord.com status 200
#   この PC はセキュリティソフトが TLS を傍受しており、差し替えられた
#   証明書の発行元は Windows 証明書ストアにはあるが certifi には無い。
#
# したがって必要なのは「検証を切ること」ではなく
# 「この PC が実際に信頼している CA を Python にも見せること」。
# verify=False は使わない (中間者を検出できなくなるため)。
# ============================================================

from __future__ import annotations

import base64
import logging
import os
import ssl
import tempfile
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

VERSION = "V1-CA-BUNDLE-CERTIFI-PLUS-WINDOWS-ROOT"

_LOCK = threading.RLock()
_bundle_path: Optional[str] = None
_built_at: float = 0.0

_PEM_HEADER = "-----BEGIN CERTIFICATE-----\n"
_PEM_FOOTER = "-----END CERTIFICATE-----\n"


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _ttl_sec() -> float:
    try:
        return max(60.0, float(os.getenv("CA_BUNDLE_REFRESH_SEC", "86400") or 86400))
    except Exception:
        return 86400.0


def _bundle_file() -> str:
    override = os.getenv("CA_BUNDLE_PATH")
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "autostock_ca_bundle.pem")


def _windows_store_pem() -> tuple[bytes, int]:
    """Windows 証明書ストアの CA を PEM 化する。"""
    if os.name != "nt":
        return b"", 0
    chunks: list[bytes] = []
    count = 0
    for store in ("ROOT", "CA"):
        try:
            certs = ssl.enum_certificates(store)
        except Exception:
            logger.debug("[CA BUNDLE] enum_certificates failed store=%s", store, exc_info=True)
            continue
        for cert, encoding, _trust in certs:
            if encoding != "x509_asn":
                continue
            try:
                b64 = base64.encodebytes(cert).decode("ascii")
                chunks.append((_PEM_HEADER + b64 + _PEM_FOOTER).encode("ascii"))
                count += 1
            except Exception:
                continue
    return b"".join(chunks), count


def build_bundle(force: bool = False) -> Optional[str]:
    """certifi + Windows ストアの合成バンドルを作り、そのパスを返す。"""
    global _bundle_path, _built_at

    with _LOCK:
        if (
            not force
            and _bundle_path
            and os.path.exists(_bundle_path)
            and (time.time() - _built_at) < _ttl_sec()
        ):
            return _bundle_path

        try:
            import certifi

            base = open(certifi.where(), "rb").read()
        except Exception:
            logger.exception("[CA BUNDLE] certifi を読めない")
            return None

        win_pem, win_count = _windows_store_pem()
        if not win_pem:
            # Windows ストアが読めない環境では certifi のみ。
            # 検証は維持されるので安全側。
            logger.warning("[CA BUNDLE] Windows 証明書ストアを読めなかった -> certifi のみ使用")

        path = _bundle_file()
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(base)
                if win_pem:
                    if not base.endswith(b"\n"):
                        fh.write(b"\n")
                    fh.write(win_pem)
            os.replace(tmp, path)
        except Exception:
            logger.exception("[CA BUNDLE] バンドル書き出しに失敗 path=%s", path)
            return None

        _bundle_path = path
        _built_at = time.time()
        logger.warning(
            "[CA BUNDLE] built path=%s certifi_bytes=%d windows_certs=%d version=%s "
            "(verify=False は使わない)",
            path, len(base), win_count, VERSION,
        )
        return path


def install() -> Optional[str]:
    """REQUESTS_CA_BUNDLE / SSL_CERT_FILE を合成バンドルへ向ける。

    既に明示設定されている場合は尊重して何もしない。
    """
    if not _env_bool("CA_BUNDLE_INSTALL_ENABLED", True):
        return None

    existing = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if existing and os.path.exists(existing) and not _env_bool("CA_BUNDLE_FORCE", False):
        logger.info("[CA BUNDLE] 既存の CA バンドル設定を尊重する path=%s", existing)
        return existing

    path = build_bundle()
    if not path:
        return None
    os.environ["REQUESTS_CA_BUNDLE"] = path
    os.environ["SSL_CERT_FILE"] = path
    logger.warning("[CA BUNDLE] installed REQUESTS_CA_BUNDLE/SSL_CERT_FILE -> %s", path)
    return path


def verify_target() -> str:
    """requests の verify= に渡す値。失敗しても True (=検証する) を返す。"""
    path = build_bundle()
    return path if path else True  # type: ignore[return-value]


__all__ = ["VERSION", "build_bundle", "install", "verify_target"]
