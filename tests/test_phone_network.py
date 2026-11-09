import threading,unittest
from unittest.mock import patch
from phone_network import PhoneNetwork

class Tests(unittest.TestCase):
    def test_settings_recreation_retries_read_only_navigation(self):
        p=PhoneNetwork(None,threading.Event());value={'ssid':'test','mode':'none'}
        with patch.object(p,'open_proxy',side_effect=[ValueError('设置页未就绪'),None]) as opened,patch.object(p,'read_proxy_page',return_value=value),patch.object(p,'pause'):
            self.assertEqual(p.read_proxy('test'),value);self.assertEqual(opened.call_count,2)
    def test_changed_ssid_is_not_retried(self):
        p=PhoneNetwork(None,threading.Event())
        with patch.object(p,'open_proxy',side_effect=ValueError('当前已连接 Wi-Fi 与备份不一致')) as opened:
            with self.assertRaises(ValueError):p.read_proxy('test')
            self.assertEqual(opened.call_count,1)
    def test_cancel_is_not_retried(self):
        p=PhoneNetwork(None,threading.Event())
        with patch.object(p,'open_proxy',side_effect=KeyboardInterrupt) as opened:
            with self.assertRaises(KeyboardInterrupt):p.read_proxy('test')
            self.assertEqual(opened.call_count,1)

if __name__=='__main__':unittest.main()
