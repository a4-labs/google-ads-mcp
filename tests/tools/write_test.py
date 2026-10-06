"""Tests for the write tools (safety switches, preview/confirm flow, guards)."""

import os
import unittest
from unittest import mock

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient

import ads_mcp.tools.write as write

CID = "4379790242"


def _real_client():
    # Builds a client without network access, just to get real proto types.
    return GoogleAdsClient(
        credentials=mock.Mock(), developer_token="x", use_proto_plus=True
    )


class _FakeService:
    def __init__(self):
        self.calls = []

    def mutate(self, customer_id, mutate_operations, validate_only):
        self.calls.append((customer_id, list(mutate_operations), validate_only))
        response = mock.Mock()
        response.mutate_operation_responses = []
        return response

    def search_stream(self, customer_id, query):
        batch = mock.Mock()
        row = mock.Mock()
        row.shared_set.resource_name = f"customers/{CID}/sharedSets/999"
        batch.results = [row]
        return [batch]


class WriteToolsTest(unittest.TestCase):
    def setUp(self):
        write._PENDING.clear()
        self.env = mock.patch.dict(
            os.environ,
            {"ADS_WRITE_ENABLED": "true", "ADS_WRITE_ALLOWED_CUSTOMERS": CID},
        )
        self.env.start()
        self.client = _real_client()
        self.service = _FakeService()
        self.client.get_service = mock.Mock(return_value=self.service)
        self.patch_client = mock.patch.object(
            write.utils, "get_googleads_client", return_value=self.client
        )
        self.patch_client.start()

    def tearDown(self):
        self.patch_client.stop()
        self.env.stop()

    def _neg(self, **kw):
        return write.negatives_add(
            customer_id=CID,
            level="account",
            keywords=[{"text": "logowanie", "match_type": "PHRASE"}],
            **kw,
        )

    def test_write_disabled(self):
        with mock.patch.dict(os.environ, {"ADS_WRITE_ENABLED": "false"}):
            with self.assertRaises(ToolError):
                self._neg()

    def test_customer_not_allowed(self):
        with self.assertRaises(ToolError):
            write.negatives_add(
                customer_id="1112223333",
                level="account",
                keywords=[{"text": "x", "match_type": "PHRASE"}],
            )

    def test_preview_uses_validate_only_and_returns_id(self):
        result = self._neg()
        self.assertEqual(result["mode"], "preview")
        self.assertTrue(result["confirmation_id"])
        self.assertTrue(self.service.calls[-1][2])  # validate_only=True

    def test_confirm_requires_matching_preview(self):
        preview = self._neg()
        with self.assertRaises(ToolError):
            write.negatives_add(
                customer_id=CID,
                level="account",
                keywords=[{"text": "inne", "match_type": "PHRASE"}],
                confirm=True,
                confirmation_id=preview["confirmation_id"],
            )

    def test_confirm_executes_once(self):
        preview = self._neg()
        result = self._neg(confirm=True, confirmation_id=preview["confirmation_id"])
        self.assertEqual(result["mode"], "executed")
        self.assertFalse(self.service.calls[-1][2])  # validate_only=False
        with self.assertRaises(ToolError):
            self._neg(confirm=True, confirmation_id=preview["confirmation_id"])

    def test_text_length_limit(self):
        with self.assertRaises(ToolError):
            write.rsa_create(
                customer_id=CID,
                ad_group_id=1,
                final_url="https://a4academy.pl/",
                headlines=["x" * 31, "b", "c"],
                descriptions=["d1", "d2"],
            )

    def test_guard_blocks_campaign_remove(self):
        with self.assertRaises(ToolError):
            write.raw_mutate(
                customer_id=CID,
                operations=[
                    {"campaign_operation": {"remove": f"customers/{CID}/campaigns/1"}}
                ],
                description="test",
            )

    def test_guard_blocks_status_removed(self):
        with self.assertRaises(ToolError):
            write.raw_mutate(
                customer_id=CID,
                operations=[
                    {
                        "campaign_operation": {
                            "update": {
                                "resource_name": f"customers/{CID}/campaigns/1",
                                "status": "REMOVED",
                            },
                            "update_mask": "status",
                        }
                    }
                ],
                description="test",
            )

    def test_status_set_builds_update(self):
        result = write.status_set(
            customer_id=CID,
            resource_names=[f"customers/{CID}/campaigns/123"],
            status="PAUSED",
        )
        self.assertEqual(result["mode"], "preview")
        op = self.service.calls[-1][1][0]
        self.assertIn("status", list(op.campaign_operation.update_mask.paths))


if __name__ == "__main__":
    unittest.main()
