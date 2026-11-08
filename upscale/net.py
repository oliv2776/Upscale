"""Garde-fous réseau pour les adresses fournies par l'utilisateur (téléchargement de modèles).

L'installation d'un modèle accepte une adresse arbitraire. Sans contrôle, l'API permettrait d'atteindre
des services internes (SSRF) : ce module n'autorise que http(s), refuse les adresses privées, loopback,
link-local et réservées, et revérifie **chaque redirection**.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from typing import Any


class UnsafeURLError(ValueError):
    """Adresse refusée par la politique réseau."""


def is_private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
            or address.is_multicast or address.is_unspecified)


def validate_url(url: str, *, allow_private: bool = False) -> str:
    """Valide une URL http(s) et résout son hôte ; lève :class:`UnsafeURLError` sinon."""
    text = (url or "").strip()
    if not text:
        raise UnsafeURLError("Adresse vide")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise UnsafeURLError(f"Adresse illisible : {exc}") from None
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURLError("Seules les adresses http:// et https:// sont acceptées")
    if not parsed.hostname:
        raise UnsafeURLError("Adresse sans nom d'hôte")
    if parsed.username or parsed.password:
        raise UnsafeURLError("Adresse contenant des identifiants refusée")
    if allow_private:
        return text
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURLError(f"Nom d'hôte introuvable : {parsed.hostname}") from None
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeURLError(f"Nom d'hôte introuvable : {parsed.hostname}")
    if any(is_private_address(address) for address in addresses):
        raise UnsafeURLError("Adresse privée ou locale refusée "
                             "(UPSCALE_ALLOW_PRIVATE_MODEL_URLS=1 pour l'autoriser)")
    return text


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Vérifie la nouvelle adresse avant de suivre une redirection."""

    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> Any:
        validate_url(newurl, allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, *, timeout: float, allow_private: bool = False,
             headers: dict[str, str] | None = None) -> Any:
    """Ouvre une URL après validation, y compris chaque redirection."""
    validate_url(url, allow_private=allow_private)
    opener = urllib.request.build_opener(_CheckedRedirect(allow_private))
    request = urllib.request.Request(url, headers=headers or {})
    return opener.open(request, timeout=timeout)
