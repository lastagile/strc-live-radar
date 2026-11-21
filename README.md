# STRC Live Arbitrage & Buyback Firepower Radar

实时在线地址：**https://lastagile.github.io/strc-live-radar/**

由 GitHub Actions **每小时第 12 分钟** 全自动运行 `scripts/sync_data.py`：
1. 自动抓取 `STRC`、`MSTR` 与 `BTC-USD` 最新实时价格，重算 `mNAV` 溢价倍数；
2. 自动扫描 SEC EDGAR (`CIK 0001050446`) 的最新 `Form 8-K`，提取当周 `STRC` 回购股数与金额、剩余优先股回购授权（自动识别董事会扩容）、`MSTR` 卖股筹资净额、`USD Reserve` 与 `USD Cash` 余额，无需任何手动录入；
3. 自动更新 `docs/data.json` 并发布至 GitHub Pages。
