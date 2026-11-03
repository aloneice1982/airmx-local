# 搭建：从同一内网到设备与 HA

## 准备与边界

完整自动生成模板面向：执行电脑在内网、可 SSH 的 OpenWrt 旁路网关运行 Python 服务、已有 HA。需要 Python 3.10+、路由器的 nftables/fw4、flock、SSH；可选 BLE 配网需要带蓝牙的桌面 Chrome/Edge。HA 无需重新安装。NAS/普通 Linux 可承载服务，但跨主机 DNAT、来源地址保留和 ACL 需自行适配，不能直接照搬本页的 router redirect。

先核对物理风机和检测仪：DHCP 租约、机身标签、实体按键与状态交叉确认。不要只根据 OUI、设备名或“某个 IP 会上传 MQTT”判断设备类型。为两台设备、网关和 HA 建 DHCP 保留。

短时定向观察，命令中的值替换为自己的实测值：

```sh
tcpdump -ni br-lan 'host FAN_IP or host SENSOR_IP'
```

记录旧域名、实际旧目的 IP/端口、风机 MAC、MQTT 主题末尾设备 ID、固件和状态字段。抓包可能含身份和密钥，只留在受限目录，不上传。观察为 TLS/HTTPS/8883 且无法确认明文协议时，停止套用此模板；不做全网 DNS 劫持。

## 填写配置并生成

仓库根目录：

```sh
cp skills/airmx-local-recovery/assets/config.example.json config.local.json
# 用编辑器填写自己的全部观测值。
python3 skills/airmx-local-recovery/scripts/render_project.py \
  --config config.local.json --output deploy
```

| 字段 | 含义 |
|---|---|
| service_ip | 本地服务所在路由器 LAN IPv4 |
| fan_ip / sensor_ip / ha_ip | 风机、检测仪、HA 固定来源地址 |
| fan_mac | 风机实际 MAC；不要反转字节 |
| device_id | 已有设备实际 topic / 注册 ID，不能复用本项目示例 |
| cloud_ips | 两台设备当前实际连接的旧服务器 IPv4 列表 |
| lan_bridge | 收到这两台设备报文的 LAN 网桥 |
| nat_priority | 精确 NAT 的 hook 优先级，需早于旁路代理；-110 仅为起点 |
| http_port / mqtt_port | 本地注册 HTTP / 窄范围 MQTT 服务端口 |
| admin_port | 仅 127.0.0.1 监听的管理端口 |
| api_port | 仅指定 LAN 地址监听、HA 白名单认证的 API |
| router_ssh | 执行电脑连接网关的 user@host，用于 BLE readiness 检查 |

示例是虚构身份和文档地址，不可直接部署。生成器拒绝覆盖已有目录；更新时生成新目录，人工比较后再替换运行文件。身份未知时先观察，不随机生成真实设备身份。渲染结果属于用户私人配置，已在 .gitignore 排除，不应提交到公开仓库。

## 路由器安装

先记录当前服务与防火墙配置并备份。以下从电脑上传，由用户用自己的已授权目标设置变量；默认 SSH 22，非标准端口自行给 ssh/scp 增加选项。

```sh
AIRMX_ROUTER='root@your-router'
ssh "$AIRMX_ROUTER" 'mkdir -p /root/airmx-recovery /etc/airmx-recovery; chmod 700 /root/airmx-recovery /etc/airmx-recovery'
scp -O deploy/router/recovery_server.py "$AIRMX_ROUTER":/root/airmx-recovery/recovery_server.py
scp -O deploy/router/airmx-recovery.init "$AIRMX_ROUTER":/etc/init.d/airmx-recovery
scp -O deploy/router/airmx-redirect.init "$AIRMX_ROUTER":/etc/init.d/airmx-redirect
scp -O deploy/router/apply-rules.sh deploy/router/recovery-rules.nft "$AIRMX_ROUTER":/etc/airmx-recovery/
```

在路由器上执行（先安装缺少的 python3 / flock，依本机包管理器）：

```sh
mkdir -p /root/airmx-recovery/data
chmod 700 /root/airmx-recovery/data
# 仅首次创建 token；再次部署必须保留旧 token 和 device.json。
python3 - <<'PY'
import os, secrets
path='/root/airmx-recovery/data/api-token'
try:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
except FileExistsError:
    print('Token already exists; preserved')
else:
    with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    print('Token created; do not print it in logs')
PY
chmod 755 /etc/init.d/airmx-recovery /etc/init.d/airmx-redirect /etc/airmx-recovery/apply-rules.sh
/etc/init.d/airmx-recovery start
/etc/init.d/airmx-recovery status
python3 /root/airmx-recovery/recovery_server.py status --admin-port YOUR_ADMIN_PORT
```

`YOUR_ADMIN_PORT` 为配置值。服务应先正常启动，然后执行一次规则测试：

```sh
/etc/init.d/airmx-redirect start
nft list table ip airmx_recovery
```

只接管来源为风机/检测仪、目的为观测旧 IP、端口 80/1883 的流量。若设备使用其他端口或不同 NAT 拓扑，应先改模板和测试；不要增加通配规则。`redirect` 会把流量送到路由器本机，不会自动送到 NAS。

待设备重连，查询状态应见风机与检测仪分别在线。若 fan status 尚为空，可只请求一次状态（不是调速命令）：

```sh
python3 /root/airmx-recovery/recovery_server.py request-status --admin-port YOUR_ADMIN_PORT
```

等实际新状态验签，检查 received_at、power、mode、cadr。不要在状态缺失时反复下发控制。

## 身份与 BLE（可选）

已有 Wi-Fi 通常先尝试重连：服务会把 CONNECT 密码视作候选，仅在签名校验成功后保存。若仍未登记，检查实际 genId 路径和 key 来源，再考虑 BLE；不要先清空现有身份。

在执行电脑开两个终端：

```sh
ssh -N -L 25886:127.0.0.1:YOUR_ADMIN_PORT root@your-router
python3 deploy/desktop/pairing_gateway.py --backend-port 25886
```

通过 `http://localhost:25890/` 打开页面。readiness 同时检查管理接口和目标 nft 表；服务未就绪时页面禁用配网。SSH 需现有密钥，网关需可执行 nft。

确认机型后，常见 AIRMX PRO 进入配网方式为 AI + 风量增加键按约 3 秒。用户主动选择蓝牙设备，再输入 Wi-Fi；页面只在内存使用凭据，不写 localStorage / URL / 日志。协议 UUID 来自社区实现，其他固件需重新核对。

完成后分别确认 registered、风机连接、签名状态、检测仪新数据。网页收到 ID 不等于注册成功。

## 持久化：服务与规则都要恢复

初次验证通过后，在路由器执行：

```sh
/etc/init.d/airmx-recovery enable
/etc/init.d/airmx-redirect enable
uci set firewall.airmx_recovery=include
uci set firewall.airmx_recovery.type=script
uci set firewall.airmx_recovery.path=/etc/airmx-recovery/apply-rules.sh
uci set firewall.airmx_recovery.fw4_compatible=1
uci set firewall.airmx_recovery.enabled=1
uci commit firewall
fw4 check
fw4 reload
nft list table ip airmx_recovery
ls -l /etc/rc.d/*airmx*
```

修改 firewall 前保留备份和其他自定义表的快照；检查 nft 原子事务、重复运行不增添重复规则、fw4 重载后仍有表，其他服务规则未被改变。先前实测 -110 早于旁路代理 -105；每个家庭应查实际 hook 顺序。不要使用 `flush ruleset`。

这验证防火墙重载恢复，不代表已实测整机重启。若用户计划重启，另行记录服务、规则、身份数据和设备重连的重启后验收。

## 下一步

按 [HA 与 AI](ha-and-ai.md) 安装现有 HA，先只读/观察，再现场验收一次控制。API token 通过受限文件传输并放 secrets.yaml；不要贴到对话或公开 issue。原生 AI 要确认真实检测仪数据已转发给风机，不能仅以 HA 有读数推断。
