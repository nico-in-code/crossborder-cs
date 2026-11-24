from __future__ import annotations

import pytest


@pytest.mark.integration
async def test_login_me_logout_flow(client, seeded, settings):
    assert (await client.get("/api/auth/me")).status_code == 401

    response = await client.post(
        "/api/auth/login",
        json={"email": "cs.a@example.com", "password": settings.seed_password},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["employee"]["email"] == "cs.a@example.com"
    assert body["employee"]["role"] == "agent"
    assert body["employee"]["refund_authority"] is True
    assert body["csrf_token"]

    cookie = response.headers.get("set-cookie", "")
    assert "cs_session=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower().replace(" ", "")

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["employee"]["display_name"] == "客服 小林"
    assert me.json()["csrf_token"] == body["csrf_token"]

    no_csrf = await client.post("/api/auth/logout")
    assert no_csrf.status_code == 403

    logout = await client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": body["csrf_token"]}
    )
    assert logout.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.integration
async def test_login_rejects_bad_password(client, seeded, settings):
    response = await client.post(
        "/api/auth/login",
        json={"email": "cs.a@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert "cs_session" not in response.headers.get("set-cookie", "")

    form = await client.post(
        "/login",
        data={"email": "cs.a@example.com", "password": "wrong-password"},
    )
    assert form.status_code == 401
    assert "邮箱或密码不正确" in form.text