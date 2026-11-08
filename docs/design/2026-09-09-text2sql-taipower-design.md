# 台電機組出力 Text2SQL 系統 — 專案規格

> 把「哪台機組什麼時候出力多少」這件事，做成一個**不會錯得很有自信**的中文問答系統。
>
> 重點不在讓 LLM 寫出 SQL —— 那部分現成方案很多。重點在這份資料集有一堆
> 「查得出來但答案是錯的」問法，系統必須認得出來並且說清楚。

- **狀態**：規格已定案，尚未實作
- **本階段目標**：Text2SQL 品質（管線 + 語料 + 評測）
- **不在本階段**：Web API 與前端（見 §13，契約已預留）

---

## 0. 這份文件怎麼用

照 §11 的階段順序做，每個階段有明確驗收條件。動任何程式之前先讀 §2 —— 那四個事實
決定了後面所有架構選擇，不知道它們的話會覺得很多設計是多餘的。

三份參考資料的角色：

| 參考 | 位置 | 拿它的什麼 |
|---|---|---|
| 台電名稱對齊專案 | `taipower_align/` | **資料層已完成的成果**：crosswalk、陷阱清單、對齊方法 |
| Text2SQL 實戰課程 | `架構參考/Text2SQL實戰/` | **管線骨架**：路由→RAG→生成→守門→修復→評測 的模組切分 |
| 智能電力分析系統 | `架構參考/智能電力分析系統/` | **成品外觀與 API 契約**，Phase 7 才用 |

課程那份是合成資料上的教學實作，這份規格是把同一套結構搬到真實台電資料上 ——
**難的地方全部在真實資料的語意，不在管線程式**。

---

## 1. 專案定位與範圍

### 要做的事

使用者用中文問台電機組出力，系統回答，且答案要嘛正確、要嘛誠實說不知道。

答案除了文字與表格，也要能依結果資料形狀產生經驗證的 `chart_spec`，由前端顯示折線圖、
長條圖或散點圖。成功查詢及人工修正則進入 staging corpus，通過去識別、去重、守門、
結果一致性與回歸評測後，自動晉升正式語料並重建檢索索引。

```
「台中1號機今年最高出力多少？」            → 可答，直接查
「哪些機組現在在歲修？」                   → 可答，查歲修表
「台中電廠去年總發電量多少？」              → 不可答，拒答並解釋為什麼
「興達3號機的裝置容量？」                   → 歧義，反問是燃煤還是複循環
「大潭電廠裝置容量多少？」                  → 可答但要揭露 A 檔缺 #8#9
```

### 進庫的資料

| 資料集 | 端點 | 內容 | 粒度 |
|---|---|---|---|
| 水火力發電廠位置及機組設備 | `d004011/001.csv` | 175 機組 / 22 電廠主檔 | 機組（靜態）|
| 過去電力供需資訊 | `d006005/001.csv` | 577 天 × 71 欄 | 日 |
| 機組歲修排程 | `d006008` | 138 列停機事件 | 事件 |

不進庫：`d006010`（10 分鐘粒度 190MB，需第五套命名對齊）、`d006004`（僅備轉容量 3 欄，
會造成期間不一致陷阱）、風光統計表。理由見 §12。

### LLM

| 用途 | 實作 | 何時用 |
|---|---|---|
| 實際查詢 | `OpenAILLM`（GPT API） | 人在用的時候 |
| 測試 / CI | `FakeLLM`（離線規則替身） | pytest、GitHub Actions |

兩者實作同一個 `LLMProtocol`（`generate_sql(prompt) -> str`），管線完全不知道背後是誰。
**CI 不需要 API key 也要能跑完整條管線** —— 這是硬需求，決定了介面必須這樣切。

---

## 2. 為什麼這個資料集難：決定架構的四個事實

這四件事是 `taipower_align/README.md` 用整個專案驗證出來的結論。它們不是背景知識，
是**架構約束**。

### 事實一：數值不是發電量，是尖峰那一瞬間的出力

`daily.csv` 的機組欄位記錄「系統尖峰負載發生的那一刻，這台機組的瞬時出力（萬瓩）」。
不是日發電量、不是日均、不是裝置容量。

驗證過的證據：各機組欄位加總 ÷ 當日尖峰負載，中位數 0.969（400 天樣本）。加總幾乎剛好
等於尖峰負載 —— 所以它是同一時刻的切片。

**推論**：
- 同一天跨機組加總 → **合理**（就是尖峰負載的組成）
- 跨日期加總 → **無物理意義**（把 577 個瞬時值相加不代表任何東西）

這個區別很精確，也因此可以機器檢查。守門規則 `PEAK_SUM_ACROSS_DAYS` 就是它。

### 事實二：有些欄位的定義會無聲漂移

`其他小水力`、`氣渦輪` 這類欄位的成分**由消去法決定**：它是「所有沒有專屬欄位的東西」。
哪天台電給曾文一個專屬欄位，`其他小水力` 的數字就變小 —— 欄名一樣、沒有公告、
歷史資料不回填。

**推論**：這種欄位拿來做跨期比較會製造假趨勢。守門規則 `RESIDUAL_TREND`。

注意 `離島` 不是殘差欄：它是地理定義（澎湖＋金門＋馬祖），規則穩定。所以要分
`is_residual`（會漂移）與 `is_bucket`（是彙總但規則穩定）兩個旗標，不能混為一談。

### 事實三：6 座電廠被拆到兩邊，電廠級總計拆不回來

| 電廠 | 有專屬欄位的部分 | 另有機組落在殘差桶 |
|---|---|---|
| **台中** | 台中#1…#10（燃煤） | GT#1-#4 → `氣渦輪` |
| 大甲溪 | 德基／青山／谷關／天輪／馬鞍 | 后里、社寮、后里示範 |
| 明潭 | 明潭／水里／鉅工 | 濁水、北山、湖山、南岸二 |
| 東部 | 碧海／立霧／龍澗 | 龍溪、清水、銅門… 共 12 台 |
| 萬大 | 萬大 | 松林#1#2 |
| 卓蘭 | 卓蘭 | 景山 |

台中最容易踩：10 個燃煤欄位看起來很完整，但 4 台氣渦輪跟全系統其他 GT 混在
`氣渦輪` 桶裡，拆不出來。算「台中發電廠總出力」會少算且無法補。

**推論**：`GROUP BY 電廠` 的查詢碰到這 6 座時必須揭露不完整。守門規則
`PLANT_TOTAL_INCOMPLETE`。

### 事實四：同一台機組在不同資料集有四套名字

| 資料集 | 台中燃煤1號 | 明潭5號 | 南部複循環1號 |
|---|---|---|---|
| `d004011` 機組主檔 | 中一機 | 明潭#5機 | 南部複一機 |
| `d006005` 每日供需 | 台中#1 | 併入 `明潭` | 併入 `南部(#1-#4)` |
| `d006008` 歲修排程 | — | 明潭#5 | — |
| `d006010` 十分鐘 | 台中#1 / 中火 | — | 南部CC#1 |

加上使用者的口語（中火、興達複循環、大潭CC），實體抽取要處理的其實是**五套**命名。

最陰險的一組：`興三機`（燃煤 50 萬瓩）vs `興達複一機`（燃氣複循環）。兩者都可以被
叫成「興達3號」。必須靠機組名稱裡的「複」字區分，否則兩種機組會混在一起。

**推論**：命名對齊是資料層的問題（crosswalk 已解決 A×B），但**歧義反問是查詢層的問題**，
兩者都要做。守門規則 `AMBIGUOUS_UNIT_NAME`。

### 補充：資料期間是滾動視窗，不能寫死

官方定義是「上一年度及今年度至上月份為止」—— **舊資料會被覆蓋不保留**。目前是
2025-01-01 ~ 2026-07-31（577 天），下個月就不一樣了。

**推論**：資料期間不可寫進 configs 或 prompt 範本。`src/ingest/validate.py` 每次建庫時
把實際期間寫進 manifest，`prompt.py` 在組 prompt 時動態注入。守門規則
`DATA_RANGE_OUT_OF_BOUNDS` 也讀同一個 manifest。

另外要定期跑 fetch 封存，否則歷史資料消失後要向台電付費申請（申請時務必明講要
「每日粒度」，跟 10 分鐘粒度差 7000 倍資料量）。

---

## 3. 資料層：星狀模型與語意檢視

### 兩層設計

```
物理層（英文 snake_case）        語意層（中文，LLM 唯一入口）
─────────────────────────       ──────────────────────────────
dim_plant                       v_unit    機組與電廠主檔
dim_unit                        v_peak    每日機組尖峰出力
bridge_b_column                 v_system  每日系統指標
fact_daily_peak                 v_outage  歲修事件
fact_daily_system
dim_outage
meta_pitfall
```

**為什麼分兩層**：台電真實欄名長成 `大潭 (#1-#9)(萬瓩)`、`通霄 (#1-#6、GT#9)(萬瓩)`
—— 全形括號、頓號、空格都有。直接餵給 LLM 會不斷產生引號錯誤與幻覺欄名。物理層用
乾淨英文，語意層用乾淨中文，**中文命名對齊從 schema 問題降級成 dim 表裡的資料** ——
而資料層的命名對齊 crosswalk 已經解決了。

順帶消掉兩條陷阱：中文欄名＋全形括號（陷阱 9）、`日期` 是 `YYYYMMDD` 字串（陷阱 10，
建庫時轉真正的 DATE 型別）。

### 表定義

| 表 | 列數 | 內容 | 來源 |
|---|---|---|---|
| `dim_plant` | 22 | 電廠名、縣市、地址、主要燃料類 | units.csv |
| `dim_unit` | 175 | 機組名、所屬電廠、裝置容量(瓩)、燃料、商轉日 | units.csv |
| `bridge_b_column` | 43 | B 欄位 ↔ A 機組群對照、`is_residual`、`is_bucket`、`ratio`、`confidence`、`grain` | align/ |
| `fact_daily_peak` | 36,928 | `date`、`b_column_id`、`peak_mw` | daily_long |
| `fact_daily_system` | 577 | 淨尖峰供電能力、尖峰負載、備轉容量、備轉容量率、工業用電、民生用電 | daily.csv |
| `dim_outage` | 138 | 機組、燃料類、起訖日、原因 | d006008 |
| `meta_pitfall` | ~30 | ★ 陷阱規則：作用對象 → 陷阱碼 → 說明 → 建議問法 | align/ |
| `meta_manifest` | 1 | 資料期間、各表列數、建庫時間、來源檔 hash | validate.py |

### `meta_pitfall` 是關鍵新增

現在 `taipower_align/align.py` 裡的 `RESIDUAL`、`BUCKET`、「6 座電廠拆兩邊」清單是 Python 常數，
守門層讀不到。把它們變成表，守門層才能在執行 SQL 前查「這個查詢碰到的欄位有沒有陷阱」。

```sql
CREATE TABLE meta_pitfall (
    pitfall_code  TEXT NOT NULL,   -- 對應 §6 附錄 A 的錯誤碼
    target_kind   TEXT NOT NULL,   -- 'column' | 'plant' | 'unit' | 'global'
    target_name   TEXT,            -- 如 '氣渦輪'、'台中發電廠'
    severity      TEXT NOT NULL,   -- 'refuse' | 'disclose' | 'clarify'
    reason        TEXT NOT NULL,   -- 給使用者看的解釋
    suggestion    TEXT,            -- 可答的替代問法
    evidence      TEXT             -- JSON：缺哪些機組、證據數字
);
```

**這張表是對齊步驟的產出，不是手寫的。** `align/` 算完 crosswalk 就知道哪些欄位是
殘差、哪些電廠被拆兩邊、哪些 ratio 異常 —— 一起寫進來。手寫會跟資料脫節。

### 語意檢視

`v_peak` 是最主要的一張，把 fact 跟 dim 攤平成 LLM 好寫的形狀：

```sql
CREATE VIEW v_peak AS
SELECT
    f.date              AS 日期,          -- DATE 型別
    b.b_column          AS 機組欄位,      -- '台中#1'、'大潭(#1-#9)'（已去掉「(萬瓩)」後綴）
    f.peak_mw           AS 尖峰出力_萬瓩,  -- ★ 尖峰時刻瞬時出力，非發電量
    b.a_plant           AS 電廠,
    b.grain             AS 粒度,          -- '單機' | '多機彙總' | '分廠彙總' | '殘差桶'
    b.category          AS 類別,          -- '燃煤' | '燃氣' | '水力' | '核能' | 'IPP' | '再生'
    b.n_units           AS 涵蓋機組數,
    b.cap_a_wankw       AS 對應裝置容量_萬瓩,
    b.is_residual       AS 是殘差欄,
    b.is_bucket         AS 是彙總欄
FROM fact_daily_peak f
JOIN bridge_b_column b ON f.b_column_id = b.id;
```

守門層規則因此變得極簡：**只允許 `SELECT`，且 `FROM`/`JOIN` 只能出現 `v_` 前綴的名稱**。
比逐一列舉表名的白名單好維護 —— 加新檢視不用改守門程式。

---

## 4. 對齊層：`src/align/` 純函式

這一層**不碰資料庫、不碰網路**，吃 dict list 吐 dict list。所以可以測到很細，
也可以在 notebook 裡單獨玩。

| 模組 | 職責 | 現有可重用 |
|---|---|---|
| `naming.py` | 命名轉換規則：國字→阿拉伯、廠名簡寫還原、「複」字區分燃煤/燃氣 | `taipower_align/align.py` 的 `cn()`、`pfx()` |
| `crosswalk.py` | A×B 對齊 + 裝置容量對帳 | `taipower_align/align.py` 的 `RULES` 宣告表 |
| `grain.py` | 粒度分類（單機／多機彙總／分廠彙總／殘差桶）、`is_residual` vs `is_bucket` | `taipower_align/align.py` 的 `RESIDUAL`、`BUCKET` |
| `outage_align.py` | 第四套命名對齊（`觀一#2`、`明潭#5`、`天輪#2` → `dim_unit`） | 無，新做 |
| `pitfalls.py` | 把上面的結果轉成 `meta_pitfall` 列 | 無，新做 |

### 對齊方法：用裝置容量客觀驗證，不用字串相似度

拿 A 的裝置容量 vs B 的全期實測最大值對帳（單位差 10,000 倍，A 是瓩、B 是萬瓩）：

```
B欄位             A機組                台   A容量   B實測max   比值
林口#1            林一機                1   80.00    80.30    1.004  ✓
通霄(#1-#6、GT#9)  通霄複一~複六+#GT9    7  395.19   385.70    0.976  ✓
德基              德基#1~#3機           3   23.40    21.60    0.923  ✓
```

比值判讀（門檻放 `configs/align.yaml`，不寫死）：

| 區間 | 判讀 |
|---|---|
| `ratio > 1.06` | **真問題**：A 檔缺機組，或對應錯誤 |
| `0.80 ~ 1.06` | 合理 |
| `ratio < 0.80` | 通常正常：抽蓄／水力／離島本來就不會滿載 |

`ratio < 0.80` 為什麼正常，由事實一解釋：`明潭` 是抽蓄，577 天裡有 143 天為 0，
只在尖峰放電。`立霧` 572/577 天為 0 —— 不是對應錯誤，是停機，而 `d006008` 給了證據：
`2026 | 水力 | 立霧 | 20250701 | 20260731 | 取水發電修復工程`，停機 13 個月。

**這正是把歲修表納入的理由**：沒有它，語意守門層看到一堆 0 只能拒答；有了它，
可以回答「為什麼是 0」。

### `outage_align.py` 的規模

138 列、涉及約 40 台機組，可以人工核對完。做法：先用 `naming.py` 的規則自動對，
對不上的列進 `reports/outage_unmatched.txt` 人工判斷，判斷結果寫回
`configs/outage_overrides.yaml`。**人工結果進版控，不要每次重跑重判。**

---

## 5. 查詢管線

沿用課程 `minisql/` 的模組切分，因為那個切分是對的：每個模組職責單一、可獨立測試、
管線本身不依賴資料庫（`run_sql` 用參數傳進來，測試才能全用替身）。

```
中文問句
   ↓
entities.py    抽實體：機組、電廠、日期、燃料、數量詞
   ↓
aliases.py     五套命名 → 正式機組名。歧義 → 反問（不猜）
   ↓
semantic_guard 問句層可答性判定 ─── 不可答 ──→ 拒答 + 解釋 + 改寫建議
   ↓ 可答
router.py      意圖路由：命中規則 → 手寫參數化 SQL，直接執行（不花錢問 LLM）
   ↓ 未命中
retriever.py   RAG：字元 n-gram + TF-IDF + 餘弦相似度，取 top-k 語料
   ↓
prompt.py      組 prompt：DDL + 業務規則 + 相似範例 + 動態資料期間
   ↓
llm.py         OpenAILLM / FakeLLM → 產生 SQL
   ↓
pipeline.py    清理（挖出 SQL 本體，不做字串手術）
   ↓
sql_guard.py   語法／白名單守門 ──── 不過 ──→ 把原因餵回 LLM 重生（最多 3 次）
   ↓ 過
semantic_guard SQL 層可答性判定 ──── 不過 ──→ 拒答 / 揭露 / 反問
   ↓ 過
db 執行         ─────────────────── 報錯 ──→ 把錯誤訊息餵回 LLM 重生
   ↓
結果 + 揭露警語 + trace
```

### 三個從課程繼承的原則

**規則優先於 LLM。** 問法固定又佔比高的前 8~10 種問題寫成規則 handler，通常涵蓋
7~8 成流量。規則的正確率 100%、延遲 5 毫秒、成本 0，而且 SQL 是自己寫的所以一定是
參數化查詢，連驗證都省了。剩下的長尾才交給 LLM。路由順序即優先權，**每次調整順序都要
重跑黃金題庫回歸測試**。

**修不好就誠實拋錯，不要偷偷換成預設查詢。** 常見壞味道是 SQL 修不好就默默改成
`SELECT * FROM v_peak LIMIT 10` 然後回給使用者 —— 使用者看到表格以為問題被回答了。
系統可以不會，但不可以騙人。

**重試上限 3 次，且不做字串手術。** LLM 輸出只做「挖出 SQL 本體」，不用 `replace()`
硬改（自動補引號、自動換表名那種）—— 你以為你在修，其實你在製造新 bug。修不動的交給
重生迴圈。3 次這個數字要用自己的評測資料驗證（見 §8 的 ablation）。

### 意圖路由表（規則 handler）

由具體到一般逐條比對，順序即優先權：

| # | 意圖 | 例句 | 目標 |
|---|---|---|---|
| 1 | 機組單日出力 | 台中1號機昨天出力多少 | `v_peak` 單列 |
| 2 | 機組期間極值 | 大潭今年最高出力 | `v_peak` MAX/MIN |
| 3 | 單日出力排行 | 6月20日出力前五大機組 | `v_peak` ORDER BY LIMIT |
| 4 | 系統指標查詢 | 上個月備轉容量率最低哪天 | `v_system` |
| 5 | 電廠機組清單 | 興達電廠有哪些機組 | `v_unit` |
| 6 | 燃料別統計 | 燃煤機組總裝置容量 | `v_unit` GROUP BY |
| 7 | 歲修查詢 | 現在哪些機組在歲修 | `v_outage` |
| 8 | 零出力天數 | 立霧今年有幾天沒發電 | `v_peak` COUNT |
| 9 | 兩者比較 | 比較林口1號跟2號機 | 比較意圖，走專用 handler |
| 10 | 其他 | — | 交給 LLM |

### 實體抽取要處理的東西

| 類型 | 難點 |
|---|---|
| 機組名 | 五套命名 + 口語（中火＝台中、大潭CC＝大潭複循環） |
| 電廠名 | 22 座 + 簡稱。注意「大甲溪」在 B 檔沒有欄位，只有它底下的德基／青山／… |
| 日期 | 民國年（114年5月）、西元、`YYYYMMDD`、相對日期（上個月、去年同期、今年） |
| 燃料 | 煤、氣、油、水力、核能、太陽能、風力 |
| 數量詞 | 前 N 大、最高、最低、平均 |

**中文 regex 的坑**：`\d` 抓不到全形數字，`\b` 在中文邊界不作用，機組名裡的 `#` 與
全形括號要處理。課程 lab02 專門踩過這些，照那份寫。

---

## 6. 雙守門層 ★ 本專案核心

| 層 | 檔案 | 管什麼 | 攔下的失敗形態 |
|---|---|---|---|
| SQL 守門 | `sql_guard.py` | SQL 本身安不安全 | 資料被改、被撈走 |
| **語意守門** | `semantic_guard.py` | 答案有不有效 | **錯得很有自信** |

### SQL 守門

沿用課程 lab03：優先用 `sqlglot` 做語法樹驗證，沒裝套件時退回保守實作（更嚴格，
寧可誤攔）。規則：

- 只准 `SELECT`（禁 `INSERT`/`UPDATE`/`DELETE`/`DROP`/`ATTACH`/`PRAGMA`）
- 只准單一語句（禁 `;` 後接第二句）
- 禁註解（`--`、`/* */`）—— 常用來繞過字串比對
- `FROM`/`JOIN` 只准 `v_` 前綴
- 所有字面值走參數化查詢
- 強制 `LIMIT`（上限放 configs）

驗收：`tests/test_sql_guard.py` 的 15 種攻擊情境全數攔下。

### 語意守門：三個嚴重度

原本設想是「一律拒答」，但重新對過 10 條陷阱後發現**不能全部拒答** —— 有些問題是
可以答的，只是必須揭露；有些是該反問而不是拒絕。所以分三級：

| 嚴重度 | 行為 | 適用 |
|---|---|---|
| `refuse` | 不執行，說明理由 + 給可答的替代問法 | 答案本身無意義 |
| `disclose` | 執行，但結果附上揭露警語 | 答案正確但不完整 |
| `clarify` | 不執行，反問使用者 | 問題本身有歧義 |

**這些規則全部是可機器檢查的 SQL 形狀判斷，不是丟給 LLM 自己想。** 完整規則見附錄 A。

### 拒答回應的形狀

用結構化錯誤碼，不用中文字串比對：

```json
{
  "ok": false,
  "severity": "refuse",
  "code": "PEAK_SUM_ACROSS_DAYS",
  "reason": "「尖峰出力」是系統尖峰那一刻的瞬時值（萬瓩），跨日期加總沒有物理意義 —— 把 577 個瞬間的功率相加不代表任何電量。",
  "evidence": { "column": "尖峰出力_萬瓩", "detected": "SUM() 跨 212 個日期" },
  "suggestion": [
    "改問「台中#1 今年的最高尖峰出力」",
    "改問「台中#1 今年尖峰出力的日平均」",
    "真要算發電量需要另接 d006010（10 分鐘粒度）或台電月報，本系統未涵蓋"
  ]
}
```

`智能電力分析系統/04-API契約.md` 第 5 節自己承認參考專案「靠比對中文錯誤字串決定顯示
哪種錯誤卡，改後端訊息會直接讓錯誤卡降級」，並建議改用結構化錯誤碼。這份設計從第一天
就這樣做 —— Phase 7 接前端時直接受益。

---

## 7. 語料

沿用課程三段結構：`ddl` + `documentation` + `examples`。

### `documentation` 段直接從 README 陷阱清單來

`taipower_align/README.md` 已經是一份寫好的業務規則文件。工作是把散文轉成結構化語料，
**不是重新想業務規則**。對應關係：

| README 段落 | → 語料規則 |
|---|---|
| 「B 的數值不是發電量」 | 查詢一律用 `v_peak`，`尖峰出力_萬瓩` 是瞬時值，禁跨日加總 |
| 「單位差 10,000 倍」 | `對應裝置容量_萬瓩` 與 `尖峰出力_萬瓩` 同單位可比；`dim_unit` 的瓩不可直接比 |
| 「四套命名並存」 | 機組名一律用 `v_unit.機組名`，使用者說的別名先過 `aliases` |
| 「殘差欄位定義會漂移」 | `是殘差欄 = 1` 的欄位不可跨期比較 |
| 「6 座電廠拆兩邊」 | 電廠級彙總要先查 `meta_pitfall` |
| 「退役機組欄位保留為 0」 | `尖峰出力_萬瓩 > 0` 必須配時間範圍 |

### 語料與題庫分成兩個目錄

`corpus/` 只放 RAG 語料，`benchmarks/` 放四份題庫。**目錄邊界就是訓練／測試邊界** ——
要把評測題洩進語料就得跨目錄複製檔案，那是 code review 看得見的動作，而不是同一個
資料夾裡兩個檔案不小心長得一樣。

兩者都**必須進版控**，也都不能放進 `data/` —— `.gitignore` 會排除 `data/`，而這些是
這個專案最有價值的人工產物，弄丟就要重做。

| 目錄 | 檔案 | 量測 | 內容 | 目標量 |
|---|---|---|---|---|
| `corpus/` | `training_corpus.json` | — | `ddl` + `documentation` + `examples` | 40~60 個 examples |
| `benchmarks/` | `golden_questions.json` | 意圖準確率 | 問句 → 預期意圖 | 每個意圖 ≥ 8 題 |
| `benchmarks/` | `eval_questions.json` | 執行準確率 | 問句 → 標準答案 SQL，含 `in_corpus` 旗標 | ≥ 60 題 |
| `benchmarks/` | `trap_questions.json` | ★ 陷阱拒答率 | 問句 → 預期 `code` 與 `severity` | 每條規則 ≥ 5 題 |
| `benchmarks/` | `attack_questions.json` | SQL 攔阻率 | 攻擊字串 → 預期攔下 | 15 題（照課程） |

`in_corpus` 旗標很重要：**刻意包含語料裡「有」與「沒有」的題型，才量得出真實水準**。
只測語料裡有的題型，量到的是背誦能力不是泛化能力。

### `trap_questions.json` 怎麼出題

每條守門規則各出 5 題以上，且要包含**邊界的另一側**：

```json
{ "question": "台中1號機今年尖峰出力總和",
  "expect": { "severity": "refuse", "code": "PEAK_SUM_ACROSS_DAYS" } },

{ "question": "6月20日全部機組尖峰出力加起來多少",
  "expect": { "severity": "ok" },
  "note": "同一天跨機組加總是合理的（≈當日尖峰負載），不可誤攔" }
```

第二題是關鍵 —— 只測「該攔的攔下來」會做出一個過度嚴格的守門層。**誤攔率跟漏攔率
一起量。**

---

## 8. 評測

### 三組指標

| 指標 | 怎麼算 |
|---|---|
| 意圖準確率 | 路由結果 vs `golden_questions.json` 的預期意圖 |
| 執行準確率 | 產生的 SQL 執行結果 **vs 標準答案 SQL 的執行結果**，比結果集等價 |
| 陷阱處理準確率 | 拒答率／揭露率／反問率 vs 預期，**同時報誤攔率** |

執行準確率**比結果集不比字串**：`SELECT a, b` 與 `SELECT b, a` 是同一個答案，
字串比對會說錯。實作放 `src/eval/metrics.py`：欄名集合相同 + 列集合相同（無序）即等價。

### 對照實驗（ablation）

| 實驗 | 對照 | 想知道 |
|---|---|---|
| RAG | 有 / 無 retriever | 語料檢索到底幫多少 |
| 語意守門 | 開 / 關 | 關掉會產生多少「錯得很有自信」的答案 |
| 重試次數 | `max_attempts` = 1 / 2 / 3 | 3 次是不是真的必要（成本 vs 準確率） |
| 路由 | 規則優先 / 全丟 LLM | 規則 handler 省下多少錢與延遲 |

每組實驗輸出進 `reports/`，圖表進 `reports/figures/`。

### 自動增量語料與回饋飛輪

```
成功查詢／人工修正
    ↓
候選 question–SQL pair（staging）
    ↓
去識別 → 去重 → SQL 守門 → 語意守門 → 固定資料集結果一致性
    ↓
no-leakage + benchmark 回歸
    ↓ 通過                         ↓ 不通過
正式 corpus + 重建檢索索引         隔離並留下原因
```

這裡的「自動訓練」定義為**增量擴充 corpus 並重建 TF-IDF／向量檢索索引**，不是自動把
每次模型回答拿去微調基礎模型。只有成功執行或經人工修正的 pair 能進 staging；只有所有
品質閘門通過的批次才能自動晉升。不得讓線上輸入直接進正式語料，否則一個錯誤答案會被
後續查詢反覆檢索，形成資料污染。

每筆語料至少記錄 `id`、`question`、`sql`、`source`、`created_at`、`approved_by`、
`schema_version`、`data_manifest_version` 與驗證結果。每次正式 corpus 及索引都要有版本與
checksum，發布後 smoke test 失敗時能切回前一版。

每一輪的評測數字進 `reports/eval_history.jsonl`，**只准追加不准覆蓋**。這樣才看得出
語料加了之後到底有沒有變好，還是只是換一批題目失敗。

### `tests/test_no_leakage.py`

這個專案的洩漏測試不是為了湊骨架，它防的是真實問題：

1. `benchmarks/` 任一題庫的問句不得逐字出現在 `corpus/training_corpus.json` 的 `examples`
2. RAG 檢索時不得撈到評測題本身（相似度 1.0 的完全比對要能被抓到）
3. `in_corpus: true` / `false` 兩組樣本數都要達下限

沒有第 3 條的話，「執行準確率 92%」可能只是因為 58 題裡 55 題都在語料裡。
**同一個病，換個場地。**

---

## 9. Repo 骨架

```
project_text2SQL/
├── README.md                    問題 → 成果表 → 快速開始 → 架構圖
├── SYSTEM_CARD.md               系統能答什麼、不能答什麼、已知偏誤
├── Makefile                     setup ingest align db corpus eval test
├── pyproject.toml               uv 管理依賴
├── .gitignore                   排除 data/ 與 .env
├── .env.example                 OPENAI_API_KEY=
│
├── configs/                     全部參數放這裡，程式裡不寫死
│   ├── config.yaml              主設定（組合其他檔）
│   ├── align.yaml               ratio 門檻、命名轉換規則表
│   ├── outage_overrides.yaml    歲修命名的人工判斷結果
│   ├── llm.yaml                 provider、model、temperature、max_attempts
│   ├── retriever.yaml           top-k、n-gram 範圍
│   └── guard.yaml               SQL 白名單、語意規則開關、LIMIT 上限
│
├── corpus/                      RAG 語料（必須版控）
│   └── training_corpus.json     ddl + documentation + examples
│
├── benchmarks/                  回歸測試集（必須版控，且不得進 RAG）
│   ├── golden_questions.json    意圖題庫
│   ├── eval_questions.json      執行準確率題庫（含 in_corpus）
│   ├── trap_questions.json      陷阱題庫
│   └── attack_questions.json    SQL 攻擊題庫
│
├── src/
│   ├── ingest/                  抓取、驗證、建庫
│   │   ├── fetch.py             三端點下載 + 封存（對抗滾動視窗）
│   │   ├── validate.py          schema/期間/單位驗證，不過就停；寫 meta_manifest
│   │   └── build_db.py          CSV → 星狀模型 + v_* 檢視
│   ├── align/                   對齊邏輯，純函式無 IO
│   │   ├── naming.py            五套命名轉換
│   │   ├── crosswalk.py         A×B 對齊 + 容量對帳
│   │   ├── grain.py             粒度分類、is_residual / is_bucket
│   │   ├── outage_align.py      歲修表命名對齊
│   │   └── pitfalls.py          產出 meta_pitfall 列
│   ├── text2sql/                查詢管線
│   │   ├── entities.py          實體抽取
│   │   ├── aliases.py           別名 → 正式名，歧義反問
│   │   ├── router.py            意圖路由
│   │   ├── retriever.py         RAG 檢索
│   │   ├── corpus.py            語料載入
│   │   ├── prompt.py            prompt 組裝 + 錯誤回饋 prompt
│   │   ├── llm.py               LLMProtocol / OpenAILLM / FakeLLM
│   │   ├── sql_guard.py         SQL 安全守門
│   │   ├── semantic_guard.py    ★ 語意守門
│   │   └── pipeline.py          生成 → 驗證 → 執行 → 自我修復
│   ├── eval/
│   │   ├── run_eval.py          三組指標
│   │   ├── ablation.py          對照實驗
│   │   └── metrics.py           結果集等價比對
│   ├── serving/                 Phase 7，本階段只有契約文件
│   │   └── API_CONTRACT.md
│   └── cli.py                   互動式問答入口
│
├── tests/
│   ├── test_no_leakage.py       評測題不得洩進語料
│   ├── test_naming.py           命名轉換 golden case
│   ├── test_crosswalk.py        43 列對照 + ratio 區間
│   ├── test_sql_guard.py        15 種攻擊全攔
│   ├── test_semantic_guard.py   陷阱題全中 + 誤攔率
│   └── test_pipeline.py         FakeLLM 全離線跑完管線
│
├── notebooks/                   EDA：粒度盤點、ratio 分佈、零值天數
├── reports/
│   ├── figures/                 所有圖表
│   ├── eval_history.jsonl       只追加
│   └── outage_unmatched.txt     待人工判斷的歲修列
├── data/                        gitignored
│   ├── raw/                     原始下載，按日期封存
│   ├── interim/                 crosswalk、daily_long
│   ├── processed/               power.db
│   └── archive/                 歷史快照（滾動視窗備份）
└── .github/workflows/ci.yml     全離線：ruff + pytest + FakeLLM 管線
```

### Makefile targets

| target | 做什麼 |
|---|---|
| `make setup` | `uv sync`，建 data/ 目錄 |
| `make ingest` | 抓三個端點 + 驗證 + 封存 |
| `make align` | 跑對齊，產 crosswalk + meta_pitfall 列 |
| `make db` | 建 SQLite 星狀模型 + v_* 檢視 |
| `make corpus` | 建 RAG 索引，檢查語料完整性 |
| `make eval` | 三組指標 + 對照實驗，輸出 reports/ |
| `make test` | pytest（全離線，不需 API key）|
| `make ask` | 開互動 CLI |

沒有 `make train` —— 這個專案沒有訓練模型。「訓練」在 LLM 管線裡就是建語料與建索引，
所以叫 `corpus`。硬留一個 `train` 只會誤導。

同理 `SYSTEM_CARD.md` 而非 `MODEL_CARD.md`：要交代的是**系統**的能力邊界與已知偏誤，
不是模型的。內容素材見 §12。

### 三個套件名為什麼不照 ML 骨架

| 骨架慣例 | 這裡用 | 理由 |
|---|---|---|
| `src/data/` | `src/ingest/` | 跟頂層 `data/`（gitignore 掉的產物）撞名，講「放到 data/」永遠要問是哪一個 |
| `src/features/` | `src/align/` | 這層做的是命名對齊與容量對帳，不是特徵工程。`align` 也跟既有原型 `taipower_align/` 一脈相承 |
| `src/models/` | `src/text2sql/` | **這個專案沒有任何模型。** 而且 `.gitignore` 模板幾乎都含 `models/` —— 改名之後這個誤刪程式碼的陷阱從根本上不存在，不必靠註解提醒 |

`make ingest` → `make align` → `make db` 的順序是真的依賴關係：`build_db.py` 需要
`align/` 產出的 crosswalk 才能建 `bridge_b_column`。

### CI 必須全離線

`.github/workflows/ci.yml` 用 `FakeLLM` 跑完整條管線，不需要 `OPENAI_API_KEY`。
理由：外部貢獻者的 PR 拿不到 secret；而且要能量到「管線結構有沒有壞」這件事，
它跟「LLM 今天聰不聰明」是兩回事，應該分開量。

線上評測（真 GPT）手動觸發或本機跑，數字進 `reports/eval_history.jsonl`。

---

## 10. 設定與環境

### 依賴（`pyproject.toml`，uv 管理）

| 套件 | 用途 | 必要性 |
|---|---|---|
| `openai` | GPT API | 只有實際查詢要 |
| `sqlglot` | SQL 語法樹驗證 | 建議（沒裝時 `sql_guard` 走保守退路）|
| `pyyaml` | 讀 configs | 必要 |
| `pytest` | 測試 | 開發 |
| `ruff` | lint | 開發 |
| `matplotlib` | reports 圖表 | 評測 |

刻意**不用**的東西與理由：

| 不用 | 為什麼 |
|---|---|
| Hydra | 這個專案的設定不需要 composition 與 multirun，純 YAML 就夠。少一層抽象 |
| ChromaDB / pgvector | 語料只有幾十筆，字元 n-gram + TF-IDF 夠用且零依賴。真的需要再換 |
| pandas | 資料量小（36,928 列），標準庫 `csv` + `sqlite3` 就夠。`taipower_align` 已證明可行 |
| LangChain / Vanna | 這個專案的價值在語意守門與語料，框架會把那層藏起來 |

最後一條是刻意的：**現成 Text2SQL 框架解決的是「怎麼產 SQL」，這個資料集的難處在
「產出來的 SQL 答案有不有效」**，那部分沒有框架幫得上，必須自己寫。

### 環境變數

```
OPENAI_API_KEY=      # 只有 make ask / 線上評測需要
```

`.env` 進 `.gitignore`，`.env.example` 進版控。程式在缺 key 時要**明確報錯說要哪個
變數**，不要靜默 fallback 到 FakeLLM —— 那會讓人以為在用 GPT 其實不是。

### configs 的原則

程式裡不寫死任何門檻、路徑、模型名。特別是：

- `align.yaml` 的 `ratio` 上下界（`0.80` / `1.06`）—— 這是判讀規則，會隨資料更新調整
- `llm.yaml` 的 `max_attempts` —— §8 的 ablation 要拿它做實驗
- `guard.yaml` 的每條語意規則開關 —— ablation 要能整層關掉
- **資料期間絕對不進 configs** —— 滾動視窗，只能從 `meta_manifest` 讀

---

## 11. 階段規劃與驗收條件

| Phase | 產出 | 驗收條件 | 重用 |
|---|---|---|---|
| **0** 地基 | repo 骨架、uv、ruff、CI、configs 骨架 | `make setup && make test` 通過（此時測試還很少）| — |
| **1** 資料層 | `power.db` 星狀模型 + 四個 `v_*` 檢視 + `meta_manifest` | 各表列數對得上 README 數字（175 / 43 / 36,928 / 577）；`validate.py` 期間檢查通過 | `taipower_align/` 的 `fetch.py`、`daily_long.csv` |
| **2** 對齊層 | `align/` 純函式 + `meta_pitfall` + 歲修對齊 | `test_crosswalk.py` 通過：43 列全中、ratio 全在區間內或已標記異常；歲修 138 列對齊率 ≥ 90%，剩下進 overrides | `taipower_align/align.py` 的 `RULES` / `RESIDUAL` / `BUCKET` |
| **3** 語料 | `corpus/` 語料 + staging／promotion 管線 + `benchmarks/` 四份題庫 | `test_no_leakage.py` 通過；`in_corpus` 兩組各 ≥ 20 題；失敗批次不可發布 | README 陷阱清單 |
| **4** 管線 | 路由 → RAG → 生成 → SQL 守門 → 修復 | `test_pipeline.py` 用 FakeLLM 全離線通過；`test_sql_guard.py` 15/15 攔下；意圖準確率 ≥ 90% | 課程 `minisql/` |
| **5** 語意守門 ★ | `semantic_guard.py` + 三個嚴重度 | `test_semantic_guard.py`：陷阱題命中率 ≥ 95%，**誤攔率 ≤ 5%** | 無先例 |
| **6** 評測 | 三組指標 + 四組 ablation + 自動語料晉升閘門 | `make eval` 產出完整 `reports/`；執行準確率 `in_corpus=false` 組 ≥ 60%；新索引不得使核心指標退步 | 課程 `lab06` |
| **7** 呈現層 | FastAPI + 前端 + 圖表規格產生器 | 核心 E2E 通過；圖值與 SQL rows 一致；不適合繪圖時回傳 `chart_spec: null` | `智能電力分析系統` 契約 |

### 為什麼先做資料層才做管線

因為語意守門層的規則來源是對齊層的產出（`meta_pitfall`）。跳過 Phase 2 直接寫管線，
守門規則就只能寫死在程式裡，跟資料脫節。**Phase 2 是 Phase 5 的前提。**

### 驗收數字怎麼定

`in_corpus=false` 組的執行準確率 60% 是刻意壓低的合理期望值 —— 那是語料裡完全沒有的
題型，要求 90% 不現實。**兩組數字要分開報，合起來報一個 85% 會掩蓋泛化能力不足。**

誤攔率 ≤ 5% 比命中率 ≥ 95% 更難也更重要：一個把所有加總都攔下來的守門層命中率 100%
但完全不能用。

---

## 12. 已知限制與不適用情境（`SYSTEM_CARD.md` 素材）

### 系統答不出來的問題

| 問不了 | 為什麼 |
|---|---|
| 任何「發電量」「用電度數」的問題 | 資料只有尖峰瞬時出力。要算電量需 `d006010` 或台電月報 |
| 電廠級完整總出力（6 座受影響） | 部分機組落在殘差桶，拆不回來（事實三） |
| 2025-01 之前的機組明細 | 資料集是滾動視窗，舊資料已被覆蓋 |
| IPP／核能／汽電共生的機組明細 | B 檔有 21 欄在 A 檔範圍外（機組主檔只含台電自有火力＋水力） |
| 逐時／逐十分鐘出力曲線 | 未納入 `d006010` |
| 電價、成本、碳排 | 資料集不含 |

### 已知偏誤

| 偏誤 | 影響 |
|---|---|
| A 檔缺 大潭#8#9（211.88 萬瓩）、大林#5（27.90 萬瓩）| 這兩廠的裝置容量偏低、ratio > 1，需揭露 |
| 殘差欄（`其他小水力`、`氣渦輪`）定義會漂移 | 跨期比較會出現假趨勢 |
| 退役機組欄位保留為 0 而非 NULL | `> 0` 篩選在不同期間行為不同（核三#2 於 2025-05-17 後全零）|
| 興達#1、#2 實質已除役但欄位仍在 | 全期僅 4 天／11 天有零星值（試車或殘值）|
| `離島` 欄把 37 台機組壓成 1 欄 | 澎湖／金門／馬祖無法分開 |

### 資料授權

政府資料開放授權條款第 1 版。原始資料來自 data.gov.tw 開放資料集，非機敏資料。

---

## 13. 後續階段：API 與前端

本階段不做，但介面預留好，避免將來要改管線。

### API 契約（`src/serving/API_CONTRACT.md` 收錄）

沿用 `智能電力分析系統/04-API契約.md` 的 envelope，但**錯誤層改用結構化錯誤碼** ——
那份文件自己建議的改進：

```json
{ "success": true,  "data": { ... } }
{ "success": false, "error_code": "PEAK_SUM_ACROSS_DAYS",
  "error": "…", "severity": "refuse", "suggestion": [ ... ], "evidence": { ... } }
```

`/api/query` 的 `data` 除了參考規格的 `sql` / `rows` / `record_count` / `chart_spec` /
`explanation` / `statistics`，多兩個欄位：

| 欄位 | 內容 |
|---|---|
| `disclosures` | `severity: disclose` 的揭露警語陣列（前端顯示在結果表格上方）|
| `trace` | 生成→驗證→執行的每一步（前端的階段式進度卡吃這個）|

### 前端

`智能電力分析系統` 那套 vanilla JS 三層架構（`core` / `render` / `app`）與進度卡、
雙 live region 設計可以原樣搬。屆時再討論。

一個現在就該記下的接點：參考規格的進度卡需要「階段 + 耗時」，而管線的 `trace` 已經
記了每次嘗試的 `stage` 與 `error`。**Phase 4 實作 `pipeline.py` 時把每步的耗時也記進
`trace`**，Phase 7 就不用回頭改。

### 圖表產生

圖表不能由前端猜資料含義。後端在 SQL 執行後依欄位型別、單位與問題意圖產生受限的
`chart_spec`，前端只負責驗證與渲染：

| 資料形狀 | 預設呈現 |
|---|---|
| 日期 + 數值 | 折線圖，日期升冪 |
| 類別 + 數值 | 長條圖 |
| 兩個連續數值 | 散點圖 |
| 單一 scalar | KPI，不強制產生圖表 |
| 空結果、全 NULL、純文字 | `chart_spec: null` + 表格或提示 |

白名單只允許 `line`、`bar`、`scatter` 等已測試圖型；標題、label、hover text 全部視為
不可信輸入並跳脫。圖表必須附正確座標軸名稱、單位與 disclosures，且 x／y 資料要能逐項
追溯回 SQL 回傳 rows。禁止後端傳入可執行 JavaScript 或 formatter function。

---

## 附錄 A：語意守門規則全表

| # | 錯誤碼 | 嚴重度 | 觸發條件（SQL 形狀） | 對應陷阱 |
|---|---|---|---|---|
| 1 | `PEAK_SUM_ACROSS_DAYS` | refuse | `SUM(尖峰出力_萬瓩)` 且彙總跨多個日期（`GROUP BY 日期` 則放行）| 1 |
| 2 | `UNIT_MISMATCH` | refuse | `裝置容量(瓩)` 與 `尖峰出力_萬瓩` 出現在同一算式 | 2 |
| 3 | `NO_UNIT_DETAIL` | refuse | 查詢對象是核能／IPP／汽電共生／風光彙總的機組明細 | B-only 21 欄 |
| 4 | `RESIDUAL_TREND` | refuse | `是殘差欄 = 1` 的欄位出現在跨期比較／趨勢／同比 | 4 |
| 5 | `PLANT_TOTAL_INCOMPLETE` | disclose | `GROUP BY 電廠` 且該廠在 `meta_pitfall` 的拆兩邊清單中 | 3 |
| 6 | `KNOWN_CAPACITY_GAP` | disclose | 查詢涉及大潭或大林的裝置容量／ratio | 7 |
| 7 | `ZERO_PERIOD_AMBIGUOUS` | disclose | `尖峰出力_萬瓩 > 0` 或 `= 0` 而無時間範圍 | 8 |
| 8 | `AMBIGUOUS_UNIT_NAME` | clarify | 實體抽取得到多個候選機組（興達#3 燃煤 vs 興達複一機）| 5 |
| 9 | `DATA_RANGE_OUT_OF_BOUNDS` | clarify | 問句日期落在 `meta_manifest` 的期間之外 | 滾動視窗 |

規則 1 的精確性再強調一次：**同一天跨機組加總是合理的**（已驗證加總 ÷ 尖峰負載
中位數 0.969），只有跨日期彙總才拒答。`trap_questions.json` 必須同時測這兩側。

規則 5、6、7 是 `disclose` 而非 `refuse`：答案是對的，只是不完整，直接拒答會讓系統
難用到沒人想用。

---

## 附錄 B：設計決策紀錄

| 決策 | 選擇 | 替代方案與否決理由 |
|---|---|---|
| 專案終點 | Text2SQL 品質（管線＋語料＋評測）| 完整成品：呈現層先擱著，品質沒到位做前端沒意義 |
| 誤導性問句 | 語意守門層，判可答性 | 照答附警語：使用者會直接拿數字走。白名單：接不到長尾 |
| Schema | 星狀模型（英文）+ 中文 `v_*` 檢視當唯一入口 | 全中文寬表：64 欄全形括號，LLM 幻覺率極高 |
| LLM | GPT API + FakeLLM 離線替身 | 只用 API：CI 要 key、結果不可重現 |
| 資料範圍 | units + daily + d006008 歲修 | 加 d006004：期間不一致陷阱。加 d006010：190MB＋第五套命名 |
| 錯誤回應 | 結構化錯誤碼 | 中文字串比對：參考規格自己承認脆弱 |
| 守門嚴重度 | 三級（refuse / disclose / clarify）| 一律拒答：規則 5~7 的答案是對的，全拒會讓系統不可用 |
| 設定管理 | 純 YAML | Hydra：不需要 composition 與 multirun |
| 向量檢索 | 字元 n-gram + TF-IDF | ChromaDB：語料幾十筆，不值得一個服務 |
| 目錄命名 | `ingest/` `align/` `text2sql/` | 骨架圖的 `data/` `features/` `models/` 是 ML 用語：這裡沒有特徵工程也沒有模型。`models/` 尤其危險 —— 現成 `.gitignore` 模板常含它，會把程式碼整包排除 |
| 語料與題庫 | 分 `corpus/` 與 `benchmarks/` | 同一目錄：防洩漏只能靠測試。分開之後目錄邊界＝訓練／測試邊界，洩漏變成跨目錄複製這種顯眼動作 |

---

*規格版本：2026-09-09。實作過程若發現與此文件衝突，改文件再改程式，不要讓兩者分歧。*
