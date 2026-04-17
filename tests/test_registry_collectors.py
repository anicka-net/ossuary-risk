"""Tests for lightweight registry collectors."""

import asyncio
from unittest.mock import AsyncMock, Mock

from ossuary.collectors.registries import PackagistCollector


class TestPackagistCollector:
    def test_collect_picks_highest_stable_version_not_first_dict_key(self):
        async def run():
            collector = PackagistCollector()
            try:
                response = Mock()
                response.status_code = 200
                response.json.return_value = {
                    "package": {
                        "description": "console component",
                        "repository": "https://github.com/symfony/console",
                        "downloads": {"daily": 100},
                        "versions": {
                            "dev-main": {
                                "version": "dev-main",
                                "version_normalized": "9999999-dev",
                            },
                            "1.2.0": {
                                "version": "1.2.0",
                                "version_normalized": "1.2.0.0",
                            },
                            "1.10.0": {
                                "version": "1.10.0",
                                "version_normalized": "1.10.0.0",
                            },
                        },
                    }
                }
                collector.client.get = AsyncMock(return_value=response)

                data = await collector.collect("vendor/package")

                assert data.version == "1.10.0"
                assert data.weekly_downloads == 700
            finally:
                await collector.close()

        asyncio.run(run())

    def test_pick_latest_packagist_version_falls_back_when_no_normalized_versions(self):
        versions = {
            "dev-main": {"version": "dev-main"},
            "feature-x": {"version": "feature-x"},
        }

        assert PackagistCollector._pick_latest_packagist_version(versions) == "dev-main"
