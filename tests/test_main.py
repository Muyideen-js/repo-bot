import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_root_supports_head_uptime_checks():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.head("/")
    assert response.status_code == 204
    assert response.content == b""
