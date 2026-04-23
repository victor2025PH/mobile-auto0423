# OpenClaw 手机集群自动化系统

## 项目简介

基于 ADB + uiautomator2 的分布式手机集群管理与自动化平台。支持多 Worker 节点、24/7 无人值守运行、AI 驱动的智能操控。

## 版本: v1.1.0

**最后更新**: 2026-03-28

## 核心功能

- **集群管理**: Coordinator + Worker 分布式架构，ZeroTier 跨网段通信
- **设备控制**: ADB/uiautomator2 双通道，19+ 台 Redmi 13C 手机
- **应用自动化**: Telegram、WhatsApp、TikTok、Facebook、Instagram、LinkedIn、Twitter
- **AI 驱动**: DeepSeek/Gemini LLM 意图识别、智能回复、视觉分析
- **防掉线**: Watchdog + HealthMonitor + 预测性维护，L0-L4 恢复等级
- **合规引擎**: 配额管控、人类行为模拟、VPN 地理伪装
- **实时流**: Scrcpy H.264 直播 + WebSocket 群控（<20ms 延迟）
- **数据分析**: 转化漏斗、ROI 面板、A/B 实验、增长分析

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python 3.13 + FastAPI + uvicorn |
| 设备控制 | uiautomator2 + ADB |
| 流媒体 | Scrcpy + WebCodecs H.264 |
| 数据库 | SQLite (WAL) |
| AI/LLM | DeepSeek / Gemini / Ollama |
| 集群通信 | HTTP + WebSocket + ZeroTier |
| 前端 | 原生 JS + Chart.js |

## 架构

```
                    ┌─────────────────┐
                    │   Coordinator   │
                    │  192.168.0.118  │
                    │   Port 18080    │
                    └────────┬────────┘
                             │ HTTP/WS 心跳
              ┌──────────────┼──────────────┐
              │              │              │
    ┌─────────▼──┐  ┌───────▼────┐  ┌──────▼─────┐
    │ Worker-01  │  │ Worker-02  │  │ Worker-N   │
    │ 19台手机   │  │  (扩展)    │  │  (扩展)    │
    └────────────┘  └────────────┘  └────────────┘
```

## 项目结构

```
mobile-auto-project/
├── src/
│   ├── host/                   # FastAPI 主控服务
│   │   ├── api.py             # 核心框架 (439行)
│   │   ├── dashboard.py       # Web 控制面板
│   │   ├── routers/           # 29个 API 路由模块
│   │   ├── static/            # CSS + 16个 JS 模块
│   │   ├── job_scheduler.py   # 定时任务调度
│   │   ├── analytics_store.py # 分析数据存储
│   │   ├── notification_center.py # 通知中心
│   │   └── audit_helpers.py   # 审计日志
│   ├── device_control/        # 设备管理 (ADB/u2)
│   ├── app_automation/        # 7平台自动化
│   ├── ai/                    # AI/LLM 集成
│   ├── behavior/              # 人类行为模拟
│   ├── workflow/              # 工作流引擎
│   ├── vision/                # 视觉识别
│   ├── leads/                 # CRM 系统
│   └── observability/         # 日志/监控/告警
├── config/                    # YAML/JSON 配置
├── tests/                     # 600个测试用例
├── scripts/                   # 工具脚本
├── server.py                  # 启动入口
├── requirements.txt           # Python 依赖
├── .env.example               # 环境变量模板
└── Dockerfile                 # 容器部署
```

## 快速开始

### 环境要求

- Python 3.10+
- ADB (Android Debug Bridge)
- 已开启 USB 调试的 Android 设备

### 安装

```bash
# 克隆项目
git clone <repo-url>
cd mobile-auto-project

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填写 DEEPSEEK_API_KEY 等

# 启动服务
python server.py
# 访问 http://localhost:18080/dashboard（或设置 OPENCLAW_PORT）
```

### Worker 部署

```bash
# 方式1: 一键安装器
运行 OpenClaw-Worker-Setup.exe

# 方式2: 增量更新
python update_worker.py http://coordinator-ip:18080
```

## API 文档

启动后访问 http://localhost:18080/docs 查看 Swagger 文档。

**29个路由模块**涵盖:
- 设备管理: `/devices/*`
- 集群协调: `/cluster/*`
- 任务执行: `/tasks/*`, `/batch/*`
- 应用自动化: `/tiktok/*`, `/macros/*`
- AI 集成: `/ai/*`
- 数据分析: `/analytics/*`
- 安全管理: `/security/*`, `/risk/*`
- 监控告警: `/monitoring/*`, `/notify/*`

## 测试

```bash
python -m pytest tests/ -q
# 600 passed (80s)
```

## 开发团队

AI 协同开发:
- **Claude** — 架构设计与重构
- **DeepSeek** — 推理与核心开发
- **千问** — DevOps 与高级功能

## 许可证

Private — All rights reserved.
