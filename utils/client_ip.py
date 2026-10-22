"""Single ingress contract: nginx overwrites X-Real-IP with a validated address.

No application parsing of client-supplied forwarding chains. Only explicitly
configured transport peers may supply X-Real-IP and X-Forwarded-Proto.
"""
from ipaddress import ip_address, ip_network

from flask import has_request_context, request
from werkzeug.middleware.proxy_fix import ProxyFix


def parse_address(value):
    if not isinstance(value, str) or not value or '%' in value:
        return None
    try:
        address = ip_address(value.strip())
    except ValueError:
        return None
    return address.ipv4_mapped if getattr(address, 'ipv4_mapped', None) else address


def get_client_ip():
    """Return the middleware-normalized address, or empty outside a request."""
    if not has_request_context():
        return ''
    address = parse_address(request.remote_addr)
    return str(address) if address is not None else ''


class TrustedProxyMiddleware:
    def __init__(self, app, *, enabled=False, trusted_cidrs=()):
        if not isinstance(trusted_cidrs, (list, tuple)):
            raise ValueError('security.trusted_proxy_cidrs must be a list of CIDRs')
        self.networks = tuple(ip_network(cidr, strict=True) for cidr in trusted_cidrs)
        if any(network.prefixlen == 0 for network in self.networks):
            raise ValueError('Do not trust all addresses as proxy peers')
        self.enabled = enabled is True
        self.app = app
        # Host is already overwritten by nginx; never consume X-Forwarded-Host.
        self.proxy_app = ProxyFix(app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    def __call__(self, environ, start_response):
        peer = parse_address(environ.get('REMOTE_ADDR'))
        trusted = self.enabled and peer is not None and any(
            peer in network for network in self.networks
        )
        environ['simcricketx.transport_peer'] = str(peer) if peer else ''
        real_ip = parse_address(environ.get('HTTP_X_REAL_IP')) if trusted else None
        environ['REMOTE_ADDR'] = str(real_ip or peer) if (real_ip or peer) else ''
        proto = environ.get('HTTP_X_FORWARDED_PROTO') if trusted else None
        # Strip competing identity/URL headers even for trusted peers.
        for key in ('HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_FOR', 'HTTP_CF_CONNECTING_IP',
                    'HTTP_FORWARDED', 'HTTP_X_FORWARDED_HOST', 'HTTP_X_FORWARDED_PORT',
                    'HTTP_X_FORWARDED_PREFIX', 'HTTP_X_FORWARDED_PROTO'):
            environ.pop(key, None)
        if proto in ('http', 'https'):
            environ['HTTP_X_FORWARDED_PROTO'] = proto
            return self.proxy_app(environ, start_response)
        return self.app(environ, start_response)
