import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('renderer',ROOT/'scripts/render_project.py')
renderer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class RenderTests(unittest.TestCase):
    def config(self):
        cfg=json.loads((ROOT/'config/config.example.json').read_text())
        cfg.update(service_ip='10.42.0.1',fan_ip='10.42.0.20',sensor_ip='10.42.0.21',
                   ha_ip='10.42.0.30',fan_mac='02:11:22:33:44:55',device_id=23456,
                   cloud_ips=['203.0.113.20','203.0.113.21'],http_port=25980,
                   mqtt_port=25984,admin_port=25986,api_port=25987,
                   router_ssh='admin@10.42.0.1')
        return cfg

    def test_custom_identity_topics_ports_and_security_remain_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=renderer.render(self.config(),Path(tmp)/'out')
            scope={'__name__':'rendered_test'}
            exec(compile((out/'router/recovery_server.py').read_text(),'rendered','exec'),scope)
            self.assertEqual(scope['DEVICE_ID'],23456)
            self.assertEqual(scope['FAN_MAC'],'021122334455')
            self.assertEqual(scope['FAN_IP'],'10.42.0.20')
            self.assertTrue(scope['COMMAND_TOPIC'].endswith('/23456'))
            package=(out/'ha/airmx.yaml').read_text()
            controller=(out/'ha/controller.py').read_text()
            self.assertIn('http://10.42.0.1:25987/api/status',package)
            self.assertIn('http://10.42.0.1:25987/api/',controller)
            gateway=(out/'desktop/pairing_gateway.py').read_text()
            self.assertIn('admin@10.42.0.1',gateway)
            self.assertNotIn('192.0.2.',gateway)
            nft=(out/'router/recovery-rules.nft').read_text()
            self.assertIn('10.42.0.20, 10.42.0.21',nft)
            self.assertIn('203.0.113.20, 203.0.113.21',nft)
            self.assertIn('redirect to :25984',nft)
            self.assertNotIn('flush ruleset',nft)
            self.assertTrue((out/'desktop/pairing/main.mjs').is_file())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';out.mkdir()
            key=out/'api-token';key.write_text('private-test-marker')
            with self.assertRaises(FileExistsError):renderer.render(self.config(),out)
            self.assertEqual(key.read_text(),'private-test-marker')

    def test_shell_injection_invalid_identity_and_duplicate_hosts_are_rejected(self):
        for key,value in [('lan_bridge','br-lan;touch /tmp/bad'),('router_ssh','-oProxyCommand=bad'),
                          ('device_id',True),('fan_mac','not-a-mac'),('fan_ip','10.42.0.21'),
                          ('api_port',25984),('service_ip','0.0.0.0')]:
            cfg=self.config();cfg[key]=value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):renderer.validate(cfg)


if __name__=='__main__':unittest.main()
