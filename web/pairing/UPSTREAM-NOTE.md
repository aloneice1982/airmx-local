# 上游参考与归属

本地配网页面以 openairmx/setup 的 AIRMX Pro Web Bluetooth 实现为协议参考。

- 上游项目：https://github.com/openairmx/setup
- 上游提交：b9c8aee50f9aff81f53be31e286d7ce2fbd52cfa
- 作者：Zhineng Li
- 许可：MIT，许可文本随本目录保存在 LICENSE.md

本地页面保留服务 UUID、特征 UUID、帧头格式和三个命令 ID，并对上游实现的通知重组、超时、状态提示和密钥获取流程作了安全边界调整。上游实现不是厂家协议规范；其握手/Wi‑Fi 响应状态语义和失败注册响应格式没有文档定义。
