---
name: airmx-local-recovery
description: Restore AIRMX PRO ventilation locally after cloud shutdown, connect its air monitor and Home Assistant, diagnose MQTT/BLE registration and tune CO2 trend-based airflow. Use for AIRMX recovery and its HA controller, not unrelated HVAC or water heaters.
---

# AIRMX 本地恢复与平稳智能

目标：在用户内网运行兼容 HTTP/MQTT 服务，验证真实设备状态后接入现有 HA，按需恢复原生 AI 或启用 HA 趋势控制。入口机器默认已在同一内网；不要求 Tailscale、NAS 或重装 HA。

## 先确定任务阶段

- 首次恢复：读 [搭建流程](references/setup.md) 和 [协议边界](references/protocol.md)。确认风机/检测仪身份、实测旧地址、固件和服务位置；不先恢复出厂。
- HA 接入或智能优化：读 [HA 与控制策略](references/ha-and-ai.md)。先区分所选 helper、真实设备回报和实际控制方。
- 失联、AI 拒绝或风量反复：读 [排障](references/diagnostics.md)；需要理解方案演进时读 [实践记录](references/experience.md)。

## 可执行资源

`scripts/render_project.py` 从用户自己的 JSON 配置生成独立部署目录，参数示例见 [assets/config.example.json](assets/config.example.json)。模板代码只含文档保留地址与虚构身份；必须生成后部署。不要把示例地址或主题 ID 直接用到设备。

脚本和模板包含恢复服务、受限 API、OpenWrt 自启/防火墙规则、BLE 配网页、HA package 与平稳控制器。渲染器不连接设备，不产生密钥，不远程写配置；生成目录保存用户身份，应留在其内网并保持忽略上传。

## 必须保留的运行约束

- 仅接管已确认设备与实测旧目标的明文 HTTP/MQTT；TLS 固件需要另行研究。首次只做定向观察和可逆测试。
- MQTT 候选 key 仅在状态验签成功后持久化；HTTP genId 登记是设备声明，registered 不代表已验签。任何控制均须另查有效签名状态。MQTT 来源白名单不是密码认证；日志只记脱敏元数据。
- 不打印凭据或完整认证报文，不读取 HA 的认证存储来冒用现有会话。使用用户提供的 SSH 或受限 API 授权；无权限时只准备离线配置并说明缺少哪项连接。
- 控制前核对电源、模式、连接和最新验签时间。下发成功不等于执行确认，检查请求后的新鲜真实状态。
- 原生 AI 和 HA 自动调速只能由一方控制。HA 接管时固件显示手动属于正常；关机和人工接管时暂停，不强制开机或抢回控制。
- 普通调速只消耗真实新样本。HA 30 秒轮询和每分钟调度不能被当作新测量。
- 示例策略为 9% 下限、650–800 ppm 目标带；这是实测家庭的工程起点，不是所有家庭的最佳值或健康标准。沿用用户已有偏好，变更需在当前授权范围内。
- 远端变更先备份，原子替换程序并保留学习状态；规则持久化与服务自启分别验证。不要为验证重启整台路由器，除非用户任务确实包含重启测试。

## 完成判据

分开记录已验证与待验证：身份登记、设备连接、签名状态、真实空气样本、HA 实体、控制回报、重载恢复。调速更新至少运行离线回归，并确认新程序处理了一个真实新样本；低负荷保持下限或固定历史输入回放都不能证明高占用闭环效果。交付配置位置、回退步骤、测量周期和真实限制。
