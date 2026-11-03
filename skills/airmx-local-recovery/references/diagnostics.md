# 排障与回退

先读取当前状态和时间戳，不把历史截图当现场数据。以下顺序尽量只读，保留现有授权与用户手动操作。

| 现象 | 优先检查 |
|---|---|
| HA 全部 unavailable | 服务进程/API 监听、HA 来源/Host/token、专用 NAT 表、服务自启链接 |
| 服务活着、设备未连 | DHCP 保留、旧目的 IP 是否变化、网桥、proxy hook 是否先截流、MQTT/HTTP 规则是否分别命中 |
| BLE 有 ID、未登记 | genId 是否真的发出、配置 MAC/ID、key 是否有有效签名支持；不先恢复出厂 |
| 原生 AI 两声拒绝 | 风机订阅、检测仪真实数据是否按原主题转发给风机；HA 有读数不是充分条件 |
| helper AI、设备手动 | 若所选 HA 平稳智能则正常；若原生 AI 则核对控制回报和是否误触手动滑块 |
| 控制器 paused | 设备是否关机或发生外部风量/模式变化；不能自动重新应用以抢回控制 |
| 只在重启后掉线 | service enable 与 redirect enable、fw4 include 分别检查；内存 nft 表不会自动持久化 |
| PM2.5/PM10 一样 | 对照原始检测仪字段；不虚构修正公式 |
| 滤芯快耗尽 | 字段是“已用”还是“剩余”，模板按已用解释；不要反向显示 |
| 只有百分比没有风量 | cadr 是百分比；额定最大风量仅用于估算，prm 单位未确认 |
| 风速反复尖峰 | 同时对照真实 CO₂/PM、斜率、预测、reason、模式；先区分正常需求与调节过敏 |

## 路由器只读核对

```sh
/etc/init.d/airmx-recovery status
/etc/init.d/airmx-recovery enabled
/etc/init.d/airmx-redirect enabled
nft list table ip airmx_recovery
python3 /root/airmx-recovery/recovery_server.py status --admin-port YOUR_ADMIN_PORT
```

如连接修复后 HA 仍无读数，使用 HA 的 REST reload 服务重新加载，或按其实际错误处理首次组件加载；不要直接覆盖 .storage 状态。REST availability 的 YAML schema 应以安装版本为准，不把 template sensor 的 availability_template 直接复制到不支持的 REST 配置。

## 可逆恢复

先备份已有 init / firewall 命名 include 和规则快照，再恢复进程、精确 NAT，核对真实回报。新增 include 不代表其他网络规则可随意覆盖；恢复时按命名部分撤销，不整体覆盖后来已有的防火墙修改。

撤销接管可停用、停止 airmx-redirect 并移除相应 fw4 include；这会让设备重新尝试原云，不会恢复原厂云。保留身份数据，避免为了网络问题清空 key。

HA 策略回退通常只恢复旧 controller.py，仍在控制器锁内原子替换并保留 0600 权限。不要用过时 state.json 覆盖已发生的人工接管、关机或控制确认。切回原生 AI 应通过正常模式选择并验证风机新回报。
