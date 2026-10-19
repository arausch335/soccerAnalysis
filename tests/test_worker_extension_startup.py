from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from collection.worker import prepare_extension


class WorkerExtensionStartupTests(unittest.TestCase):
    def test_connection_code_starts_before_collection_without_waiting_or_requesting_browser(self):
        bridge=Mock();jobs=[SimpleNamespace(collection_types=('combined',))]
        with patch('config.TRANSPORT',{'sofascore_mode':'extension','browser':{'manual_wait_seconds':120}}),patch('providers.extensionBridge.acquire_bridge',return_value=(bridge,False)):
            prepare_extension(Path('/test-data'),jobs,False)
        bridge.start.assert_called_once();bridge.request.assert_not_called();bridge.connected.wait.assert_not_called()

    def test_monitor_or_unassigned_worker_does_not_start_extension(self):
        with patch('providers.extensionBridge.acquire_bridge') as acquire:
            prepare_extension(Path('/data'),[],False)
            prepare_extension(Path('/data'),[SimpleNamespace(collection_types=('combined',))],True)
        acquire.assert_not_called()

    def test_early_connection_is_reused_by_later_season_scopes(self):
        from providers.extensionBridge import extension_scope,acquire_bridge
        bridge=Mock();jobs=[SimpleNamespace(collection_types=('combined',))]
        with patch('providers.extensionBridge.ExtensionBridge',return_value=bridge) as constructor:
            with extension_scope():
                prepare_extension(Path('/data'),jobs,False)
                with extension_scope():
                    reused,owns=acquire_bridge()
                    self.assertIs(reused,bridge);self.assertFalse(owns)
            constructor.assert_called_once();bridge.close.assert_called_once()
