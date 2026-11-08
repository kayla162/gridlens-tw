# 檢索評測：字元 n-gram vs 本機 embedding

> 產生時間 2026-11-07T17:00:42+00:00。問句 126 題（golden＋eval，去掉 other），語料範例 77 個；相關＝範例的 intent 與問句相同。embedding 模型：`bge-m3`。

| 檢索方式 | Hit@1 | Hit@5 | Precision@5 | MRR |
|---|---:|---:|---:|---:|
| ngram | 49.2% | 87.3% | 34.6% | 0.665 |
| embedding | 69.0% | 93.7% | 46.2% | 0.797 |
| hybrid | 65.9% | 92.9% | 41.8% | 0.771 |
