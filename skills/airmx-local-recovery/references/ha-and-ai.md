# HA 接入、控制选择与优化

## 安装现有 HA

使用 `render_project.py` 生成的 `deploy/ha/airmx.yaml` 和 `controller.py`。安装前备份现有 `/config/packages/airmx.yaml`、控制器、state.json 和相关仪表盘。若已加载基础/其他同名 AIRMX package，只替换或合并一次，不能重复启用整套同名实体。

将文件放到：

```text
/config/packages/airmx.yaml
/config/airmx_smart/controller.py
```

`airmx_smart` 目录 0700、程序和 state 文件 0600，保证 HA Core 运行用户能读写。它由 Core 的 shell_command 调用，而不是 SSH 加载项另启的守护进程。核对 Core 中有 Python 与 PyYAML。

在已有 `configuration.yaml` 的 homeassistant 节点合并（不能覆盖原配置）：

```yaml
homeassistant:
  packages: !include_dir_named packages
```

已有 packages 配置则沿用，只把文件放到它实际包含的位置。将 WRT 受限 token 文件通过 SSH 安全传入 HA 的 secrets.yaml，条目形如：

```yaml
airmx_local_api: "Bearer YOUR_RANDOM_LOCAL_TOKEN"
```

这是占位说明，不能提交真实 token。模板 REST 请求和控制器共用该 secret。不要复制 `.storage/auth`、生成会话令牌或要求用户把登录凭据贴进对话。

使用 HA 开发者工具的 YAML 配置检查或已授权 API 检查；首次引入 shell_command/command_line 等组件后根据 HA 要求重启 Core。只更新 Python 文件时不需重启，也不要为更新重新清空 state.json。

## 实体与界面

模板提供 CO₂、PM2.5/PM10、温湿度、实际风量、已用滤芯和真实协议档位，以及 input_select.airmx_mode、input_number.airmx_desired_speed、sensor.airmx_smart_controller。首次安装核对实际 entity_id；已存在重复实体时可能有后缀，不应盲目复制其他家庭的卡片 ID。

先用 HA 的实体卡片显示这些值，再按手机/网页需要分区。界面应并列显示“所选控制方”和“实际设备档位”，不能把 helper 目标当成回报。模板本身不会自动创建 Mushroom 仪表盘。

可在默认实体 ID 对应时添加：

```yaml
type: entities
title: AIRMX 本地新风
entities:
  - input_select.airmx_mode
  - sensor.airmx_smart_controller
  - sensor.airmx_shi_ji_yun_xing_mo_shi
  - sensor.airmx_xin_feng_feng_liang
  - sensor.airmx_shi_nei_co2
  - sensor.airmx_shi_nei_pm2_5
  - input_number.airmx_desired_speed
```

## 首次控制顺序

1. 校对设备屏幕与新鲜空气读数，确认风机验签状态、当前电源、档位和风量。
2. 选择“观察学习”：只记录建议，不写控制；确认真实新样本能更新 slope / sample_ts。
3. 在用户可观察设备且风机开机时测试一次手动风量，核对请求后的签名回报与实际风量。下发成功不等于执行成功。
4. 决定用原生 AI 还是 HA 平稳智能。两者不能同时调速；HA 接管时发送手动协议档位是正常行为。

模式切换自动应用；“重新应用模式”可用于用户明确恢复人工接管后的 HA 调速。手动滑块只在所选手动模式生效，避免一次误触使 AI 退出。不要周期性强行重新应用模式来掩盖暂停。

## 原生 AI

原生 AI 依赖风机收到配套检测仪数据。服务只转发真实测量、原 topic/payload，且只给已订阅的风机。若 AI 按键两声但不启用，先看数据转发与订阅，再发一次模式命令并验证真实回报；不能仅因 HA 有空气值就断定联动完整。

## HA 平稳智能：当前策略

本地有界趋势控制，不是大模型推理，也不冒充原厂云 AI。示例工程参数：

| 项目 | 行为 |
|---|---|
| 参考浓度 | 中心 700 ppm、目标带 650–800 ppm；按家庭需求调整 |
| 风量下限 | 9% |
| 测量使用 | 只在 received_at 前进时计算普通调速；重复轮询去重 |
| 趋势 | 最近三个真实样本、至少 480 秒跨度估计斜率 |
| 预测 | 5 分钟轻量外推，权重限定在 0 / 0.25 / 0.5，按历史误差保守选择 |
| 普通上调 | 每个新样本最多 5 个百分点 |
| 普通下降 | 每个新样本最多 2 个百分点，允许最终 10→9 |
| 保持 | 目标带内回落且预测仍在带内时保持，避免过早收风 |
| 趋势修正 | 上升系数 0.25、最多加 10 点；下降系数 0.08、最多扣 3 点 |
| 基准学习 | 稳定目标带内向实际有效风量靠拢 5%，仍有上下界 |
| 高浓度 | 1000 / 1200 / 1500 ppm 的工程介入档，以及 PM2.5 35 / 75 的需求覆盖，详见源码 |
| 传感器过期 | 超过 15 分钟进入至少 25% 备用，保留更高现有风量 |
| 设备保护 | 风机离线、状态未验签或过期时拒绝写；人工接管/关机后暂停 |

这些阈值不是医学健康标准，也不是适合所有风道、房间、人数的最佳值。型号额定风量、噪声需求与实际浓度需重新评估。稳定区间的数字应由用户确认；不要因背景 CO₂ 低于 650 就故意减少通风至关机。

检测仪约五分钟才有一条新网络报文。HA REST 每 30 秒读取缓存、控制任务每分钟执行，不会把物理采样提高到每分钟。开始积累三个样本通常约十分钟，之后滚动处理，不是每次都固定再等十分钟。

## 从曲线定位波动

同时导出实际风量、CO₂、PM2.5、控制器属性、所选模式和真实档位。按真实 sample_ts 去重，再对照调速时间和 reason；HA 五分钟汇总图可能掩盖离散阶梯，不能只凭平滑线判断协议风量。

曾出现 CO₂ 约 740–780 时，负斜率让风量 24→19→14，浓度再次上升后又到 24。优化保留提前增风，减弱负趋势、放慢下降，并让稳定负荷基准向有效风量靠拢。不要用统一固定等待掩盖模型问题，也不能说斜率已经分离了人数变化和换气效果。

## 更新与效果验证

先运行离线回归；远端备份程序与学习状态，在现有 controller.lock 的 fcntl 锁内原子替换 Python 文件。通常保留当前 state.json；不重置人工接管标志，也不重新抢占控制。下一分钟原有调度自然读取新程序。

至少确认一个更新后的真实新样本已参与计算、控制器 error 为空、实际电源/风量未发生意外变化。固定历史 CO₂ 输入回放只能对比决策曲线，不能证明新风量下房间 CO₂ 的闭环结果。高占用、高浓度及断线恢复要分别记录真实验证；低负荷保持 9% 不代表全部验收。
