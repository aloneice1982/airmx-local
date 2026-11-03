# AIRMX 本地恢复 Skill

把 AIRMX PRO 从失效云服务迁回局域网：**本地 HTTP/MQTT → 配套空气检测仪 → Home Assistant → 原生 AI 或 HA 平稳智能**。

这是一次真实恢复工程提炼的可复用 Skill，包含脱敏源码、配置生成器、BLE 配网页、测试与完整操作说明。普通用户可以按文档执行；Codex 等支持 `SKILL.md` 的代理可直接加载技能。

## 包含什么

- 定向识别设备和协议、保留旧身份、验证签名后恢复登记。
- 路由器上的受限 HTTP/MQTT 服务、HA Bearer API，以及真实检测仪数据转发。
- 可选 Web Bluetooth 配网，不要求先恢复出厂。
- OpenWrt 开机自启、精确 NAT 接管及防火墙重载恢复。
- HA 空气/风量/滤芯实体、手动控制、原生 AI、观察学习与平稳智能。
- CO₂ 斜率预测、真实样本去重、人工接管、失联备用，以及“快加慢减”的最新版优化。

不包含家庭地址、真实 MAC/设备 ID、设备 key、API token、HA 登录信息、私人历史数据或抓包。示例 `192.0.2.0/24`、`198.51.100.0/24` 均为文档保留地址，必须按自己的设备修改。

## 安装 Skill

```sh
git clone https://github.com/aloneice1982/airmx-local-recovery-skill.git
cd airmx-local-recovery-skill
mkdir -p ~/.codex/skills
cp -R skills/airmx-local-recovery ~/.codex/skills/
```

调用示例：

> 使用 $airmx-local-recovery，执行机器就在内网，检查我的 AIRMX PRO 和配套检测仪，接入已有 Home Assistant，并配置平稳智能。

## 从搭建到智能优化

1. [准备、识别、生成与部署](skills/airmx-local-recovery/references/setup.md)：所需设备、JSON 参数、完整安装命令、BLE 配网、持久化和验收。
2. [协议与兼容范围](skills/airmx-local-recovery/references/protocol.md)：主题、MD5 签名、身份恢复和实际限制。
3. [HA 接入与 AI 优化](skills/airmx-local-recovery/references/ha-and-ai.md)：package、实体、控制所有权、曲线诊断及新版参数。
4. [排障与回退](skills/airmx-local-recovery/references/diagnostics.md)：失联、AI 双响、ID 未登记、滤芯与 PM 数据疑问。
5. [方案演进与验证边界](skills/airmx-local-recovery/references/experience.md)：从恢复云链路到缓慢降风的完整经验。

生成自己的部署目录：

```sh
cp skills/airmx-local-recovery/assets/config.example.json config.local.json
# 用自己的观测值编辑 config.local.json；不要复制别人的设备身份。
python3 skills/airmx-local-recovery/scripts/render_project.py \
  --config config.local.json --output deploy
```

生成器只写本地文件，拒绝覆盖已有目录。后续按搭建文档安装；未填写正确设备身份与网络参数前，不要执行远端部署。

## 测试

Python 3.10+，恢复服务只用标准库；HA 控制器读取 secrets 时需要 PyYAML（HA Core 环境通常已有，请核对）。BLE 单元测试需要 Node.js 18+。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test skills/airmx-local-recovery/assets/pairing/pairing-core.test.mjs
```

离线测试仅使用临时文件、合成数据和回环连接，不会连接任何家庭设备。首次真实调速仍应在能观察设备的条件下验收。

## 兼容与许可

已知协议来自 AIRMX PRO 风机及配套检测仪的单户实测；不是厂商通用协议规范。其他固件、TLS 设备、不同主题/签名布局需重新验证。这里的“AI”指本地趋势控制与有界自适应，不代表恢复了原厂云模型，也不宣称学得全屋最优控制。

项目为 MIT；BLE 与协议研究归属见 [THIRD_PARTY.md](THIRD_PARTY.md)。任何部署都应保留鉴权、来源限制和状态新鲜度检查。
