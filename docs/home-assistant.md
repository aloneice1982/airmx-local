# Home Assistant 接入与控制模式

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

## 启用平稳智能

接入及控制回报验证完成后，再阅读[平稳智能与参数调优](smart-control.md)。先用观察学习确认数据节奏，再选择 HA 平稳智能接管。
