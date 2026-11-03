#!/usr/bin/env python3
"""Render an isolated, household-specific deployment; never contact devices."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

SKILL = Path(__file__).resolve().parents[1]


def validate(raw):
    cfg = dict(raw)
    for key in ('service_ip', 'fan_ip', 'sensor_ip', 'ha_ip'):
        value = str(ipaddress.IPv4Address(cfg[key]))
        if value in ('0.0.0.0', '255.255.255.255') or ipaddress.ip_address(value).is_multicast:
            raise ValueError('a specific unicast IPv4 address is required')
        cfg[key] = value
    if len({cfg[k] for k in ('service_ip', 'fan_ip', 'sensor_ip', 'ha_ip')}) != 4:
        raise ValueError('service, fan, sensor and HA require distinct IPv4 addresses')
    mac = re.sub('[:-]', '', cfg['fan_mac']).lower()
    if not re.fullmatch('[0-9a-f]{12}', mac):
        raise ValueError('invalid fan MAC')
    cfg['fan_mac'] = mac
    if type(cfg['device_id']) is not int or not 1 <= cfg['device_id'] <= 2147483647:
        raise ValueError('device_id must be a positive observed integer')
    if not isinstance(cfg['cloud_ips'], list) or not cfg['cloud_ips']:
        raise ValueError('at least one observed old cloud address is required')
    cfg['cloud_ips'] = [str(ipaddress.IPv4Address(x)) for x in cfg['cloud_ips']]
    if not re.fullmatch('[A-Za-z0-9_.:-]{1,15}', cfg['lan_bridge']):
        raise ValueError('invalid LAN bridge name')
    if not re.fullmatch('[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', cfg['router_ssh']):
        raise ValueError('router_ssh must be user@host without shell options')
    for key in ('http_port','mqtt_port','admin_port','api_port'):
        if type(cfg[key]) is not int or not 1024 <= cfg[key] <= 65535:
            raise ValueError('ports must be integers between 1024 and 65535')
    if len({cfg[k] for k in ('http_port','mqtt_port','admin_port','api_port')}) != 4:
        raise ValueError('ports must be distinct')
    if type(cfg['nat_priority']) is not int or not -300 <= cfg['nat_priority'] <= -101:
        raise ValueError('NAT priority must precede ordinary destination NAT')
    return cfg


def render(config, destination):
    cfg = validate(config)
    dest = Path(destination).absolute()
    if dest.exists():
        raise FileExistsError('destination exists; use a new directory to avoid overwriting secrets')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='.airmx-render-', dir=dest.parent))
    old = {'service_ip':'192.0.2.1','fan_ip':'192.0.2.20','sensor_ip':'192.0.2.21','ha_ip':'192.0.2.30'}
    ports = {'http_port':25880,'mqtt_port':25884,'admin_port':25886,'api_port':25887}
    def replace(text):
        # Simultaneous replacement prevents cascading when target values overlap.
        mapping = {old[k]:cfg[k] for k in old}
        mapping.update({str(v):str(cfg[k]) for k,v in ports.items()})
        pattern = '|'.join(re.escape(x) for x in sorted(mapping,key=len,reverse=True))
        return re.sub(pattern,lambda m:mapping[m.group()],text)
    def write(path, text):
        p=temp/path
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(text)
        p.chmod(0o600)
    try:
        server=replace((SKILL/'scripts/recovery_server.py').read_text())
        for key,value in {'FAN_MAC':cfg['fan_mac'],'DEVICE_ID':cfg['device_id']}.items():
            server,count=re.subn(r'^'+key+r' = .*$',key+' = '+repr(value),server,flags=re.M)
            if count!=1:raise ValueError('unexpected server identity template')
        compile(server,'recovery_server.py','exec')
        write(Path('router/recovery_server.py'),server)
        for name in ('airmx-recovery.init','airmx-redirect.init','apply-rules.sh'):
            write(Path('router')/name,replace((SKILL/'assets'/name).read_text()))
        nft = ('table ip airmx_recovery {\n    chain prerouting {\n'
               '        type nat hook prerouting priority '+str(cfg['nat_priority'])+'; policy accept;\n')
        sources=', '.join((cfg['fan_ip'],cfg['sensor_ip']))
        targets=', '.join(cfg['cloud_ips'])
        for cloud_port,local_port in [(80,cfg['http_port']),(1883,cfg['mqtt_port'])]:
            nft += ('        iifname "'+cfg['lan_bridge']+'" ip saddr { '+sources+' } ip daddr { '+targets+' } tcp dport '
                    +str(cloud_port)+' counter redirect to :'+str(local_port)+'\n')
        nft += '    }\n}\n'
        write(Path('router/recovery-rules.nft'),nft)
        write(Path('ha/controller.py'),replace((SKILL/'scripts/controller.py').read_text()))
        write(Path('ha/airmx.yaml'),replace((SKILL/'assets/ha-package.yaml').read_text()))
        gateway=replace((SKILL/'scripts/pairing_gateway.py').read_text())
        gateway=gateway.replace('root@'+cfg['service_ip'],cfg['router_ssh'])
        write(Path('desktop/pairing_gateway.py'),gateway)
        shutil.copytree(SKILL/'assets/pairing',temp/'desktop/pairing',ignore=shutil.ignore_patterns('*.test.mjs'))
        write(Path('config.local.json'),json.dumps(cfg,indent=2)+'\n')
        temp.replace(dest)
    except BaseException:
        shutil.rmtree(temp,ignore_errors=True)
        raise
    return dest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    try:
        output=render(json.loads(args.config.read_text()),args.output)
    except (ValueError,KeyError,TypeError,OSError):
        p.exit(1,'Rendering failed: validate required fields and use a new output directory.\n')
    print('Rendered private deployment directory: '+str(output))
