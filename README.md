# AIRMX Local

**AIRMX 新风机本地恢复与智能控制。**

在原厂云服务不可用后，让 AIRMX PRO 和配套空气检测仪继续在局域网工作，并接入已有的 Home Assistant。本项目提供本地通信服务、BLE 配网页、部署配置和 CO₂ 趋势调速程序，从设备恢复一直覆盖到后续智能控制优化。

## 能做什么

- 接收设备原有的 HTTP/MQTT 通信，恢复设备登记和经过签名验证的风机状态。
- 将配套检测仪的真实数据转发给风机，支持恢复原生 AI 联动。
- 在 HA 中查看空气质量、实际风量、设备档位和滤芯使用比例，并进行本地控制。
- 选择原生 AI、手动或 HA 平稳智能；HA 调速支持真实样本去重、趋势预测、人工接管保护和缓慢降风。
- 生成适合自己设备的配置，保存 OpenWrt 服务自启与精确转发规则，支持防火墙重载恢复。

## 开始前

需要 AIRMX PRO、配套检测仪、已有 HA，以及能观察设备流量和运行本地服务的主机。当前完整部署模板针对 **OpenWrt 网关运行服务、执行电脑已在同一内网** 的情况。NAS/其他 Linux 也可承载程序，但网络转发和来源限制需另行适配。

先确认两台设备的实际身份、固定地址、旧服务器地址和主题 ID。保留现有 Wi-Fi 和设备身份；已有设备应先尝试恢复连接。详细兼容范围和验收条件见[架构与适用范围](docs/architecture.md)。

## 搭建流程

1. **识别设备并准备参数**：阅读[从零搭建](docs/getting-started.md)，交叉确认风机与检测仪，记录实际协议和地址。
2. **生成本地部署文件**：填写配置，生成路由器服务、转发规则、配网页和 HA 配置。
3. **恢复设备通信**：先启动服务和定向转发，确认设备连接、有效签名和新空气数据；需要时再 BLE 配网。
4. **接入现有 HA**：按 [Home Assistant 接入](docs/home-assistant.md)加载配置，先只读与观察，再验证一次真实控制回报。
5. **选择并优化控制模式**：按[平稳智能与参数调优](docs/smart-control.md)决定使用原生 AI 还是 HA 趋势控制。

从仓库根目录生成配置：

```sh
git clone https://github.com/aloneice1982/airmx-local.git
cd airmx-local
cp config/config.example.json config.local.json
# 按搭建文档，用自己的观测值编辑 config.local.json。
python3 scripts/render_project.py --config config.local.json --output deploy
```

生成器只写本地文件，不连接设备，也不会覆盖已有输出目录。生成后继续按[搭建文档](docs/getting-started.md)安装到对应主机。

示例中的地址和设备身份均为虚构值，必须替换；生成目录包含自己的网络和设备信息，应留在本地。仓库不包含真实家庭地址、设备 key、API token、HA 登录信息、私人历史数据或抓包。

## 文档导航

| 要解决的问题 | 文档 |
|---|---|
| 理解整体结构、确认是否适用 | [架构与适用范围](docs/architecture.md) |
| 首次恢复、BLE 配网、开机自启与转发 | [从零搭建](docs/getting-started.md) |
| HA 实体、手动控制与模式切换 | [Home Assistant 接入](docs/home-assistant.md) |
| CO₂ 斜率、目标区间与风量波动优化 | [平稳智能与参数调优](docs/smart-control.md) |
| MQTT 主题、签名、字段和认证边界 | [协议说明](docs/protocol.md) |
| 失联、AI 双响、ID 未登记及回退 | [排障与回退](docs/troubleshooting.md) |
| 理解方案如何演进、哪些已经验证 | [设计演进与验证记录](docs/design-history.md) |

## 项目结构

```text
src/                         本地恢复服务、HA 控制器、配网页网关
scripts/render_project.py    为自己的设备生成部署文件
config/                      虚构参数示例
templates/                  部署模板
  openwrt/                   自启与转发规则应用脚本
  home-assistant/            HA package
web/pairing/                 Web Bluetooth 配网页与协议测试
docs/                        搭建、接入、调优与排障文档
tests/                       Python 离线回归
deploy/                      本地生成结果，不提交到仓库
```

`src/` 中使用文档保留地址和虚构身份作为模板默认值。实际部署使用生成结果，不直接把模板中的地址发给设备。

## 开发与测试

Python 3.10+；恢复服务只使用标准库，HA 控制器读取 secrets 时需要 PyYAML。BLE 测试需要 Node.js 18+。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test web/pairing/pairing-core.test.mjs
```

离线测试使用临时文件、合成数据和回环连接，不连接家庭设备。真实部署需要分别确认设备连接、新测量、有效签名、控制回报与重载恢复；固定历史曲线回放不能证明真实房间中的闭环效果。

## 兼容与许可

本项目基于 AIRMX PRO 风机及配套检测仪的单户实测和社区协议研究，不是厂商通用协议规范。不同固件、TLS 设备、主题或签名布局需要重新验证。“平稳智能”是本地趋势控制与有界自适应，不代表恢复了原厂云模型，也不宣称已经学得全屋最优控制。

项目采用 MIT 许可，上游归属见 [THIRD_PARTY.md](THIRD_PARTY.md)。
