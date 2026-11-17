# binance-price-monitor

币安标的价格监控：价格满足条件时通过 PushPlus 推送微信提醒，由 GitHub Actions 定时驱动。

## 工作原理

- **数据源**：币安公开行情 API（优先 `data-api.binance.vision`，无地区限制）
- **运行**：GitHub Actions 北京时间每天 06:00–24:00，每小时触发一场，每场循环 70 分钟、每 60 秒检测一次（10 分钟拖尾吸收 cron 触发延迟）
- **推送**：命中条件后按频率规则去重，通过 PushPlus 推送到微信
- **状态**：`state.json` 记录每条规则的提醒状态，由 workflow 自动提交回仓库

## 配置（config.yaml）

```yaml
pushplus_token: "你的PushPlus token"

rules:
  - symbol: SMHBUSDT        # 币安交易对
    alert_type: price_below # price_below=低于时提醒 / price_above=高于时提醒
    threshold: 460          # 触发阈值
    frequency: daily        # once=仅提醒一次 / daily=每日最多提醒一次
```

改配置 = 修改 `config.yaml` 提交即可，支持多条规则。

## 本地调试

```bash
pip install -r requirements.txt
python monitor.py --test            # 发送一条测试推送
python monitor.py --once            # 只检测一轮
python monitor.py --loop-minutes 5  # 循环监控 5 分钟
```

## 手动触发

Actions → price-monitor → Run workflow，只检测一轮，用于验证链路。
