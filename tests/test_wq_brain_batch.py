"""Tests for wq_brain_batch route and MCP tracking."""

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


class TestBatchSubmitValidation:
    async def test_requires_auth(self, client):
        resp = await client.post("/api/v1/wq-brain/batch-submit", json={
            "expression": "rank(close)",
            "tag": "test-agent",
        })
        assert resp.status_code in (401, 403)

    async def test_returns_503_when_not_configured(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "", "WQ_BRAIN_PASSWORD": ""}, clear=False):
            resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                "expression": "rank(close)",
                "tag": "test-agent",
            }, headers=auth_headers)
            assert resp.status_code == 503

    async def test_rejects_invalid_region(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                "expression": "rank(close)",
                "tag": "test-agent",
                "regions": ["INVALID"],
            }, headers=auth_headers)
            assert resp.status_code == 400
            assert "region" in resp.json()["detail"].lower()

    async def test_rejects_invalid_universe(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                "expression": "rank(close)",
                "tag": "test-agent",
                "universes": ["INVALID"],
            }, headers=auth_headers)
            assert resp.status_code == 400
            assert "universe" in resp.json()["detail"].lower()

    async def test_rejects_invalid_neutralization(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                "expression": "rank(close)",
                "tag": "test-agent",
                "neutralizations": ["BOGUS"],
            }, headers=auth_headers)
            assert resp.status_code == 400
            assert "neutralization" in resp.json()["detail"].lower()

    async def test_rejects_invalid_delay(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                "expression": "rank(close)",
                "tag": "test-agent",
                "delays": [5],
            }, headers=auth_headers)
            assert resp.status_code == 400
            assert "delay" in resp.json()["detail"].lower()

    async def test_rejects_too_many_combinations(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                "expression": "rank(close)",
                "tag": "test-agent",
                "regions": ["USA"],
                "delays": [0, 1],
                "universes": ["TOP3000", "TOP1000", "TOP500", "TOP200"],
                "neutralizations": ["MARKET", "SUBINDUSTRY", "INDUSTRY", "SECTOR", "NONE"],
            }, headers=auth_headers)
            assert resp.status_code == 400
            assert "组合数" in resp.json()["detail"]


class TestBatchSubmitCreatesTask:
    async def test_creates_task_with_defaults(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            with patch("quantgpt.routes.wq_brain_batch._run_batch_task"):
                resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                    "expression": "rank(close)",
                    "tag": "test-agent",
                }, headers=auth_headers)
                assert resp.status_code == 202
                data = resp.json()
                assert "task_id" in data
                assert data["status"] == "pending"
                assert data["total_combinations"] == 1

    async def test_creates_task_with_sweep(self, client, test_user, auth_headers):
        with patch.dict(os.environ, {"WQ_BRAIN_EMAIL": "a@b.com", "WQ_BRAIN_PASSWORD": "pw"}):
            with patch("quantgpt.routes.wq_brain_batch._run_batch_task"):
                resp = await client.post("/api/v1/wq-brain/batch-submit", json={
                    "expression": "rank(close/open)",
                    "tag": "test-agent",
                    "regions": ["USA"],
                    "delays": [0, 1],
                    "universes": ["TOP3000"],
                    "neutralizations": ["SUBINDUSTRY", "MARKET"],
                }, headers=auth_headers)
                assert resp.status_code == 202
                data = resp.json()
                assert data["total_combinations"] == 1 * 2 * 1 * 2


class TestBatchRequestModel:
    def test_defaults(self):
        from quantgpt.routes.wq_brain_batch import WQBrainBatchRequest
        req = WQBrainBatchRequest(expression="rank(close)", tag="test")
        assert req.regions == ["USA"]
        assert req.delays == [1]
        assert req.universes == ["TOP3000"]
        assert req.neutralizations == ["SUBINDUSTRY"]
        assert req.decay == 0
        assert req.truncation == 0.08
        assert req.auto_submit is False
        assert req.tag == "test"

    def test_custom_values(self):
        from quantgpt.routes.wq_brain_batch import WQBrainBatchRequest
        req = WQBrainBatchRequest(
            expression="ts_mean(close, 5)",
            tag="test-sweep",
            regions=["USA"],
            delays=[0, 1],
            decay=5,
            truncation=0.1,
            auto_submit=True,
        )
        assert req.regions == ["USA"]
        assert req.delays == [0, 1]
        assert req.decay == 5
        assert req.auto_submit is True
        assert req.tag == "test-sweep"


