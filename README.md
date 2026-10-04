# 固定样地复测森林生长 / 死亡 / 进界量评估系统

林业研究站比较固定样地两次调查（本演示为 **2019 → 2024**）的：

* **存活木生长量**（survivor growth）
* **死亡量**（mortality）
* **进界量**（ingrowth，胸径 ≥ 5 cm 阈值）

净变化恒等式：

```
Δ生物量 = 存活木生长 − 死亡 + 进界
```

技术栈：**React（Vite）+ Django REST Framework + NumPy/SciPy + PostgreSQL/PostGIS**
（开发环境用 sqlite3 也能完整运行；PostGIS 层见 `deploy/postgis.sql`）。

> 数据均为**虚构**演示数据（树种、方程、坐标、样地），仅用于说明流程。

---

## 1. 关键规则（对应验收要求）

### 1.1 胸径单位必须显式记录
* 每条测量必须带 `dbh_unit`（`cm`/`mm`/`in`），树高单位 `m`。
* 入库时转换为规范单位（胸径 cm、树高 m），**原始值与单位同时保留**，可审计。
* 合理范围检查拦截单位错误（如把 250 mm 当成 250 cm、树高 950 m）。
* 树干坐标必须落在样地边界内（PostGIS 层有 `ST_Contains` 约束兜底）。

### 1.2 异速生长方程显式记录适用树种
`AGB_kg = a · dbh_cm^b · h_m^c`，方程记录：
* 适用树种列表（多对多）、胸径适用范围、是否需要树高；
* 系数 a/b/c、残差 σ(ln AGB)、文献引用、版本号；
* 超出适用径阶的树会在结果中标记 **extrapolation**。

### 1.3 编号是标签，不是身份
**编号相同但位置矛盾时，先核实，不能直接认成同株。**

* 内部 `tree_id` 才是个体身份；同树行换标签 = 已核实改号（renumber）。
* 同编号、不同树行、位置矛盾 → 生成 `IdentityConflict`（open），
  在人工核实前**从所有分量中剔除**，不会悄悄变成死亡或进界。
* 核实结论只有人工给出：
  * `renumber`：同一株树换了牌号 → 计入存活木生长；
  * `distinct`：不同个体 → t1 计入死亡、t2 计入进界。
* 新编号出现在旧树附近 → “possible renumber” 待核实，绝不自动合并。

### 1.4 真实零生长、缺测、死亡三者严格区分
| 情况 | 字段 | 处理 |
|---|---|---|
| 真实零生长 | `alive_measured`，两次均测，|Δdbh| ≤ 0.15 cm 且有复核记录 | 计入生长量（增量≈0），结果中列出 |
| 缺测 | `alive_not_measured`（活着但胸径未测） | **不是零**；按样地比率插补，方差膨胀，列出清单 |
| 死亡 | `dead`（t2 有死亡观测） | 以 t1 生物量计入死亡量 |
| 未找到 | `missing_tree` | 不入死亡量，列入 provenance |
| 进界以下 | t2 新树 dbh < 5 cm | 记录但不计入进界 |

### 1.5 总体估计按抽样设计加权
分层简单随机抽样，**不把所有树木平均后乘面积**：

```
样地分量 y [kg/ha] = 分量(kg) / 该样地自己的面积(ha)
Y_h = A_h · mean_h(y)                 # A_h = 已知的层土地面积
SE_h = A_h · sqrt( (1−f_h) · s_h²/n )  # 有限总体校正可选
Y = Σ_h Y_h，SE 跨层合成（Welch–Satterthwaite 自由度，t 分布 95% CI）
```

演示数据刻意使用**不等面积样地**（0.20 / 0.50 / 1.00 ha）。

### 1.6 已确认调查版不可被新方程静默改变
* `EstimateVersion`：draft → `confirm` 后结果载荷、设计快照、方程校验和全部冻结。
* 模型层 + PostGIS 触发器双重禁止修改 confirmed 版本。
* 确认时同时**锁定所用方程**（系数不可改）；新系数必须以**新方程 code/version** 录入，
  并产生**新版本估计**，旧版本数字永不改变。

### 1.7 重新测绘的样地边界不能改写已发布抽样框
样地边界经重新测绘后，申报面积与历史多边形都需要修订——但**新边界不能悄悄改写已经
发布的抽样框**：

* 每次「边界 + 申报面积 + CRS 说明」都形成一条不可变的
  **`PlotFrameRevision`**：`draft → reviewed → published`；校验不过
  （排除既有树位 / 申报面积与多边形超 1% 容差 / 与**同层**样地重叠）时
  生成 `FrameIssue` 待处理项并进入 **`blocked`**，阻止 review/publish，
  绝不悄悄接受后继续计算。
* 修订永久保留**原始边界、原始面积、面积核对结果（area_check）和发布原因**；
  published 行与发布产生的 `SamplingFrameVersion` 均只增不可改
  （模型层 + PostGIS 触发器双重保护）。
* 发布一条修订原子地生成**新一版抽样框快照** `SamplingFrameVersion`（v1 为
  原始测绘基线），未涉及样地的边界原样带入。两个并发发布请求只能产生一个
  published 版本（条件状态占用 + frame version 唯一约束）。
* **估计显式绑定抽样框版本**：`EstimateVersion.frame`。新框只影响显式针对新框
  跑的 draft；旧 confirmed 估计的每公顷扩展永不改变。
* **历史 `TreeMeasurement` 永远归属其采集时的边界**：新边界不会迁移、删除或
  改写任何测量行；被排除的树位只作为待处理项列出，并在比较/影响查询中标识。
* 校验失败或刷新后不会留下半发布边界：最终校验在发布事务内复核，发现问题时
  只把修订持久化为 blocked（无 frame 产出），不会出现幻影框或错误估计。

---

## 2. 不确定性假设（结果中完整输出）
1. **设计推断**：分层 SRS，每样地等权（每公顷基准），层土地面积放大；树木从不合并平均。
2. **测量误差**：胸径 σ=0.10 cm、树高 σ=0.30 m，独立高斯，一阶误差传播；
   作为诊断分量单独报告（不与样地间抽样方差重复计入 SE 合计）。
3. **方程残差**：乘法对数正态 σ(ln AGB)；存活木两次用同一方程、残差假定完全相关故增量抵消；
   死亡（仅 t1）与进界（仅 t2）的方程误差保留。
4. **缺测存活木**：假定样地内 MAR，用“有测样木 t1 生物量生长率”做比率插补，
   抽样方差按 1/(1−缺测生物量比例) 膨胀。
5. 未核实身份、未找到、进界以下个体**排除在分量外**并在 provenance 列明。
6. 95% CI 用跨层 Welch–Satterthwaite df 的 t 分布；净变化 SE 中分量抽样协方差假定为 0。
7. 样地申报面积与多边形面积交叉核对（1% 容差）。

---

## 3. 运行

### 后端
```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate      # 可选
pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py seed_demo        # 载入虚构数据（含全部验收场景）
python3 manage.py runserver 127.0.0.1:8123
```

PostgreSQL/PostGIS：
```bash
FOREST_DB=postgis PGHOST=.. PGUSER=.. PGPASSWORD=.. \
  python3 manage.py migrate
psql -d foreststation -f ../deploy/postgis.sql
```

### 前端
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, /api 代理到 8123
```

界面四页：
1. **Plots & individuals**：SVG 地图显示样地边界与 t2 个体状态，可切换
   **原始测绘 / 最新发布抽样框**边界（被新边界排除的历史树位标 ×，记录不
   迁移）；点入样地看 t1→t2 复测、改号、零生长/缺测/死亡着色，以及该样地
   的修订历史；
2. **Frame revisions**：样地抽样框修订工作台——提交 draft、查看面积核对与
   待处理项（排除树位/面积容差/同层重叠）、revalidate→review→publish，
   并展示新旧边界叠加、受影响个体和估计版本绑定；
3. **Identity conflicts**：编号矛盾核实工作台（renumber / distinct）；
4. **Estimates**：选择**抽样框版本**与方程→跑 draft→查看分量、来源
   （含采样框绑定与修订样地）、不确定性→确认冻结。

---

## 4. API 摘要
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plots/` | 样地位置、边界、面积、CRS |
| GET | `/api/measurements/?campaign=2024` | 每株每期测量（含原始/规范单位） |
| GET | `/api/equations/` | 方程、系数、适用树种、径阶范围 |
| GET | `/api/conflicts/?status=open` | 同号位置矛盾 |
| POST | `/api/conflicts/{id}/resolve/` | `{status: renumber|distinct, note}` |
| POST | `/api/imports/` | 批量入库（拒收单位错误/越界行，207 返回明细） |
| POST | `/api/estimates/` | 运行 draft 估计（可带 `frame_version`，默认 latest） |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 |
| GET | `/api/frames/` `/api/frames/latest/` | 已发布抽样框版本（v1 基线，append-only） |
| GET | `/api/plot-revisions/?plot=P01` | 样地修订列表 |
| POST | `/api/plot-revisions/` | 提交 draft（相同几何幂等返回同一修订，`X-Idempotent-Replay`） |
| POST | `/api/plot-revisions/{id}/revalidate/` | 重跑校验 / 替换几何（问题消失→draft，新增→blocked） |
| POST | `/api/plot-revisions/{id}/review/` | 干净 draft → reviewed（blocked 返回 409） |
| POST | `/api/plot-revisions/{id}/publish/` | reviewed → published 并原子产生新 frame 版本 |
| GET | `/api/plot-revisions/{id}/compare/` | 新旧边界 / 面积 / 每株树 excluded·retained·newly_included |
| GET | `/api/plot-revisions/{id}/impact/` | 受影响个体 + 各估计版本的框绑定情况 |

### 样地修订请求示例
```json
{
  "plot": "P03",
  "boundary": [[500000,4000500],[500050,4000500],[500050,4000550],
               [500000,4000550],[500000,4000500]],
  "declared_area_ha": 0.25,
  "crs_epsg": 32650,
  "crs_note": "2026 differential-GPS re-survey (UTM 50N)"
}
```
响应中 `status` 为 `draft`（干净）或 `blocked`（携带 open issues：
`area_mismatch` / `tree_excluded` / `overlap`）。publish 必须带
发布原因 `reason`。

### 入库行示例
```json
{
  "campaign": "2024",
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 252, "dbh_unit": "mm",
    "height_raw": 16.8, "height_unit": "m"
  }]
}
```

---

## 5. 验收测试
```bash
cd backend && python3 manage.py test inventory
```
27 个测试覆盖：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与直接篡改免疫，
以及**抽样框修订**：

* 合格面积修订只改变绑定新框的 draft 的每公顷扩展，旧 confirmed 数字不变；
* 排除历史树位（以及面积超容差、同层重叠）的修订进入 `blocked`，
  无法 review/publish，历史测量行不迁移；
* 同一几何重复上传幂等返回同一修订（200 + `X-Idempotent-Replay: true`）；
* 两个并发发布请求只产生一个 published 版本与一版 frame（200 + 409）；
* 发布前最终校验失败只留下 blocked 修订，不产生 frame 版本或错误估计；
* 跨层样地重叠允许，published 修订/frame 版本不可篡改。

## 6. 虚构演示数据场景索引
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 4 条坏行（mm 当 cm、树高 cm 当 m、缺单位、坐标越界）→ **入库拒收**；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等；
* **抽样框修订**：P03 合格扩边 0.20→0.25 ha，走完整流程发布为 **frame v2**；
  P01 提议裁掉东侧条带会排除历史树位（005/006/201/202），停留在 **blocked**。
