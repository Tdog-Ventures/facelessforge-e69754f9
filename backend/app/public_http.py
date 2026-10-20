"""Public HTTP fetches: validate every hop and connect to the validated IP.

Pinning the connection prevents a second DNS lookup from rebinding to a private
address. Host and TLS SNI retain the original hostname and certificate checks.
"""
import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager

import httpx


async def _public_target(url: httpx.URL) -> tuple[httpx.URL, str, str]:
    if url.scheme not in ('http', 'https') or not url.host or url.userinfo:
        raise ValueError('Only public HTTP(S) URLs without credentials are allowed')
    host = url.host
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                host, url.port or (443 if url.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError('Unable to resolve public host') from exc
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.ipv4_mapped is not None
                            for ip in addresses if isinstance(ip, ipaddress.IPv6Address)):
        raise ValueError('Destination is not a public address')
    if any(not ip.is_global or ip.is_multicast or ip.is_unspecified for ip in addresses):
        raise ValueError('Destination is not a public address')
    # Keep authority (including a nondefault port) on the HTTP Host header.
    authority = url.netloc.decode('ascii')
    return url.copy_with(host=str(addresses[0])), authority, host


@asynccontextmanager
async def public_stream(client: httpx.AsyncClient, url: str):
    current = httpx.URL(url)
    for hop in range(6):
        pinned, authority, hostname = await _public_target(current)
        request = client.build_request('GET', pinned, headers={'Host': authority},
                                       extensions={'sni_hostname': hostname})
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            if response.is_redirect:
                if hop == 5 or 'location' not in response.headers:
                    raise ValueError('Too many or invalid redirects')
                current = current.join(response.headers['location'])
                continue
            yield response
            return
        finally:
            await response.aclose()


async def public_text(url: str, *, timeout: float = 12.0, max_bytes: int = 2 * 1024 * 1024) -> str:
    async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                 headers={'User-Agent': 'FacelessForge Scraper'}) as client:
        async with public_stream(client, url) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise ValueError('Website response is too large')
                body.extend(chunk)
            return bytes(body).decode(response.encoding or 'utf-8', errors='replace')
