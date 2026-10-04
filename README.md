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

### 1.7 样地抽样框修订（边界重测不重写已发布抽样框）
重测后的边界、申报面积和 CRS 说明是**独立的版本行** `PlotFrameRevision`，生命周期
`draft → reviewed → published`：

* **原始边界、面积核对结果、CRS 说明、评审/发布原因全部保留**。发布行不可变；新版发布时
  旧版转为 `superseded`（历史仍可读），`Plot` 当前边界在同一事务内推进。
* 估计**显式绑定抽样框版本**（M2M + 设计快照中的 frames 块）：每公顷扩展只用所绑定版本的
  面积。历史 `TreeMeasurement` 带 `collected_frame`，永远归属采集时的边界——新边界
  **不自动迁移、删除或改写**任何观测。
* 新边界若（a）排除既有树位、（b）申报面积与多边形面积超过 1% 容差、（c）与**同层**样地
  多边形重叠、（d）CRS 声明与本站投影 CRS 不符，会生成持久化的 `FrameRevisionIssue`
  待处理项并**阻止发布**；系统不会悄悄重算或绕过。每个问题只能由**带书面说明的人工决定**
  解决。
* 发布是**原子事务**：推进当前边界的同时，把该样地所有 draft 估计在同一事务内按新版
  重新扩展；任何一步失败整体回滚，**不会留下半发布边界或错误估计**。confirmed 估计
  冻结在原绑定版本上，永不刷新。
* 同一几何+面积+CRS 重复上传**幂等返回同一修订**；同环但更正面积/CRS 是新版本。
  数据库部分唯一索引（每样地至多一个 published）保证两个发布请求竞争时**只产生一个
  published 版本**，败者保持 reviewed（409）。
* 提供发布、**新旧比较**和**影响查询** API；地图、样地详情和估计来源可切换查看当前 /
  新 / 旧边界，并高亮被排除、新纳入及仍在旧版本上的个体。

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

界面三页：
1. **Plots & individuals**：SVG 地图显示全部样地边界与 t2 个体状态；可在**当前 / 新 / 旧
   抽样框边界**间切换，被新边界排除的树位以紫色圆环标记；点入样地看 t1→t2 复测、
   改号、零生长/缺测/死亡着色，并在**样地抽样框修订工作台**上传重测边界、处理待处理项、
   评审与发布、查看影响个体；
2. **Identity conflicts**：编号矛盾核实工作台（renumber / distinct）；
3. **Estimates**：选择方程→跑 draft（自动绑定当前 published 框架版本）→查看分量、来源、
   不确定性及**框架版本绑定**→确认冻结。

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
| POST | `/api/estimates/` | 运行 draft 估计（自动绑定当前 published 框架版本） |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 + 框架绑定 |
| GET | `/api/frame-revisions/?plot=P01` | 某样地全部框架版本（draft/reviewed/published/superseded） |
| POST | `/api/frame-revisions/` | 上传重测边界→校验→draft（同内容幂等） |
| POST | `/api/frame-revisions/{id}/revalidate/` | 重新执行几何/面积/重叠/树位校验 |
| POST | `/api/frame-revisions/{id}/submit_review/` | draft → reviewed（QA 备注必填） |
| POST | `/api/frame-revisions/{id}/publish/` | reviewed → published（发布原因必填，带待处理项则 422） |
| GET | `/api/frame-revisions/{id}/compare/` | 新旧边界、面积、CRS 差异 |
| GET | `/api/frame-revisions/{id}/impact/` | 受影响个体（被排除/新纳入）、待处理项、可否发布 |
| POST | `/api/frame-issues/{id}/resolve/` | 书面人工决定关闭一个阻止发布的待处理项 |

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
23 个测试覆盖：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与直接篡改免疫，
以及**样地抽样框修订**：

* 合格的面积修订只改变新 draft 的每公顷扩展（0.10→0.20 ha 新 draft 总量减半），旧
  confirmed 数字逐字节不变；
* 排除历史树位 / 面积超容差 / 同层重叠的修订生成 open 待处理项并**阻止发布**，人工书面
  解决后才能发布，历史观测不迁移、不删除；
* 同一几何重复上传幂等返回同一修订，同环改面积产生新版本；
* 两个发布请求竞争（文件级 sqlite + 线程，子进程隔离）只产生一个 published 版本，败者
  保持 reviewed；
* 校验失败或上传坏几何后**没有半发布边界、没有错误估计、没有残留 draft 行**；
* 发布在同一事务内刷新既有 draft 估计；新观测记入新版框架、历史观测保持 v1 归属；
* compare / impact / 发布 / 评审 / 幂等全部通过 API 验收。

## 6. 虚构演示数据场景索引
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 4 条坏行（mm 当 cm、树高 cm 当 m、缺单位、坐标越界）→ **入库拒收**；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等。
