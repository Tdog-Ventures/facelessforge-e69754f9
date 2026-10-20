from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET
import socket

import httpx
import pytest
from pydantic import ValidationError

from app import render, scraper, thumbnail_images
from app.routes import longform


@pytest.mark.asyncio
async def test_render_provider_id_cannot_escape_scene_directory(monkeypatch, tmp_path):
    paths = []
    async def download(url, target, **kw):
        paths.append(target.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'image')
        return True
    monkeypatch.setattr(render, '_download_to', download)
    result = await render._resolve_scene_visual(
        {'id': 'scene'}, [{'id': 'asset', 'scene_id': 'scene', 'asset_type': 'stock_image',
         'external_id': '../../../../outside', 'download_url': 'https://example.com/photo.png'}],
        {}, tmp_path, 0)
    assert paths and all(p.is_relative_to((tmp_path / 'scenes').resolve()) for p in paths)
    assert result[0].read_bytes() == b'image'


def test_mock_svg_text_cannot_create_active_elements():
    svg = thumbnail_images._mock_svg('seed', {'topic': '</text><script>alert(1)</script>'}, {'thumbnail_title_text': 'A & B < C'})
    root = ET.fromstring(svg)
    assert root.findall('.//{http://www.w3.org/2000/svg}script') == []
    assert 'A & B < C' in ''.join(root.itertext())


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://127.0.0.1/', 'http://169.254.169.254/latest/meta-data/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/'])
async def test_scraper_blocks_private_destinations_before_network(monkeypatch, url):
    reached = []
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: (reached.append(req), httpx.Response(200, text='<title>private</title>'))[1])
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    with pytest.raises(ValueError):
        await scraper.scrape_website(url)
    assert reached == []


@pytest.mark.asyncio
async def test_redirect_to_private_host_is_blocked(monkeypatch):
    seen = []
    original = httpx.AsyncClient
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    def respond(req):
        seen.append(req)
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(ValueError):
        await scraper.scrape_website('https://public.example/')
    assert len(seen) == 1
    assert seen[0].url.host == '93.184.216.34'
    assert seen[0].headers['host'] == 'public.example'
    assert seen[0].extensions['sni_hostname'] == 'public.example'


@pytest.mark.asyncio
async def test_public_site_uses_validated_ip_without_second_dns_lookup(monkeypatch):
    lookups = []
    original = httpx.AsyncClient
    def resolve(*args, **kwargs):
        lookups.append(args[0])
        address = '93.184.216.34' if len(lookups) == 1 else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text='<title>Public business</title><h1>Solar panels</h1>'))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    result = await scraper.scrape_website('https://public.example/')
    assert result['title'] == 'Public business'
    assert len(lookups) == 1


@pytest.mark.asyncio
async def test_render_download_rejects_private_url_without_creating_file(tmp_path):
    target = tmp_path / 'out.mp4'
    assert not await render._download_to('http://127.0.0.1/private', target, max_bytes=1024)
    assert not target.exists()


@pytest.mark.parametrize('name', ['../secret.txt', '/outside.txt', '..\\secret.txt'])
def test_spa_path_cannot_escape_public_root(tmp_path, name):
    from app.public_files import public_file
    from fastapi import HTTPException
    root = tmp_path / 'build'
    root.mkdir()
    (tmp_path / 'secret.txt').write_text('private')
    with pytest.raises(HTTPException) as error:
        public_file(root, name)
    assert error.value.status_code == 404


def test_spa_ordinary_asset_is_available(tmp_path):
    from app.public_files import public_file
    asset = tmp_path / 'bundle.js'
    asset.write_text('public')
    assert public_file(tmp_path, 'bundle.js') == asset.resolve()
