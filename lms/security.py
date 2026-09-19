"""Proxy identity policy used by the database-backed login limiter."""

from ipaddress import ip_address, ip_network

from django.conf import settings


def client_ip(request):
    peer = request.META.get("REMOTE_ADDR", "")
    try:
        address = ip_address(peer)
        trusted = any(address in ip_network(network) for network in settings.TRUSTED_PROXY_IPS)
        if trusted:
            # A single address, not an attacker-controlled comma-separated chain.
            return str(ip_address(request.META.get("HTTP_X_REAL_IP", peer)))
        return str(address)
    except ValueError:
        return None
