# 校准计算不确定度传播 API

基于 FastAPI + SymPy + Pint，对测量模型 $y=f(x_1,\dots,x_n)$ 做 GUM 一阶不确定度传播：
服务端解析仅含白名单运算的公式并进行量纲校验，利用符号偏导与相关系数矩阵计算结果值、
单位、合成标准不确定度，并按输入量列出含相关项的方差贡献。

另提供**不确定度逆分配**接口：把某个输入量的标准不确定度视为未知量，在完整保留相关项
的前提下反求满足目标合成标准不确定度的最大允许取值。

还提供**多输出联合传播**接口：一次提交 2～8 个具名输出公式，对同一输入协方差按一阶
雅可比计算 $J\Sigma J^\mathsf{T}$，同时返回各输出的不确定度与完整的输出相关系数矩阵。

## 启动

单条命令一键启动（自动构建镜像并以前台方式运行）：

```bash
docker compose up --build
```

依赖在**容器启动时**安装；服务随后监听 API 端口 **8000**（健康检查
`http://localhost:8000/health`，交互文档 `http://localhost:8000/docs`）。

## 配置

- 宿主机端口：通过环境变量 `PORT` 覆盖（默认 `8000`），容器内固定监听 `8000`。
  例：`PORT=9000 docker compose up --build`
- 依赖清单：`requirements.txt`（容器启动时由 `docker/entrypoint.sh` 安装）。

## API（端口 8000）

- `GET /health` — 健康检查
- `GET /api/v1/whitelist` — 公式白名单
- `POST /api/v1/propagate` — 不确定度传播；相关矩阵非对称 / 非半正定 / 单位量纲不兼容 / 公式不合法时返回 **422**，错误体 `error.loc` 精确定位（如 `correlation_matrix.0.1`、`inputs.0.unit`、`formula`）
- `POST /api/v1/propagate-joint` — 多输出联合传播（见下）
- `POST /api/v1/allocate-uncertainty` — 不确定度逆分配（见下）
- 交互文档：`http://localhost:8000/docs`

### 多输出联合传播 `POST /api/v1/propagate-joint`

请求体与 `/api/v1/propagate` 复用同一组 `inputs`（输入量、单位、标准不确定度）与同一个
`correlation_matrix`（缺省为单位阵），区别仅在于用 **2～8 个具名输出公式** `outputs`
取代单个 `formula`。输入量、公式白名单、单位量纲与相关矩阵的校验与单输出接口完全一致。

对各输出 $y_k=f_k(\mathbf{x})$，服务端求一阶雅可比 $J_{ki}=\partial f_k/\partial x_i$，
按同一输入协方差 $\Sigma$ 计算输出协方差矩阵

$$
\Sigma_y = J\Sigma J^\mathsf{T}
$$

响应中每个输出给出值、单位、合成标准不确定度（$\sqrt{\Sigma_y[k,k]}$）及对各输入量的
灵敏度系数；同时返回**完整的输出相关系数矩阵**（$m\times m$，对称、对角为 1；某输出方差
为 0 导致相关无定义时相应单元记 0），以及逐单元带单位的**输出协方差矩阵**，单元 $(i,j)$
的单位为“输出$i$单位 × 输出$j$单位”（如 `V * W`、`W ** 2`）。

请求示例（$U,I$ 相关 $r=0.5$，同时求功率 $P=U\cdot I$ 与电压示值 $U$）：

```bash
curl -s http://localhost:8000/api/v1/propagate-joint \
  -H 'Content-Type: application/json' \
  -d '{
    "inputs": [
      {"name": "U", "value": 10.0, "unit": "V", "standard_uncertainty": 0.1},
      {"name": "I", "value": 2.0, "unit": "A", "standard_uncertainty": 0.02}
    ],
    "outputs": [
      {"name": "P", "formula": "U*I"},
      {"name": "U_meas", "formula": "U"}
    ],
    "correlation_matrix": [[1.0, 0.5], [0.5, 1.0]]
  }'
```

响应（节选）：

```json
{
  "outputs": [
    {"name": "P", "result": {"value": 20.0, "unit": "W", "standard_uncertainty": 0.3464101615}, "formula": "U*I", "sensitivity_coefficients": [
      {"input": "U", "value": 2.0, "unit": "W / V"},
      {"input": "I", "value": 10.0, "unit": "W / A"}]},
    {"name": "U_meas", "result": {"value": 10.0, "unit": "V", "standard_uncertainty": 0.1}, "formula": "U", "sensitivity_coefficients": [
      {"input": "U", "value": 1.0, "unit": "dimensionless"},
      {"input": "I", "value": 0.0, "unit": "V / A"}]}
  ],
  "number_of_inputs": 2,
  "number_of_outputs": 2,
  "correlation_matrix": [[1.0, 0.5], [0.5, 1.0]],
  "output_correlation_matrix": [[1.0, 0.866025403784], [0.866025403784, 1.0]],
  "output_covariance_matrix": [
    [{"value": 0.12, "unit": "W ** 2"}, {"value": 0.03, "unit": "V * W"}],
    [{"value": 0.03, "unit": "V * W"}, {"value": 0.01, "unit": "V ** 2"}]
  ]
}
```

以下情况返回 **422**，错误体 `error.loc` 精确定位：

| 场景 | code | loc |
| --- | --- | --- |
| 输出名重复 | `duplicate_output_name` | `outputs.<i>.name` |
| 第 i 个公式语法/白名单/未声明符号/量纲/求值非法 | 沿用单输出 code | `outputs.<i>.formula` |
| 输出数量少于 2 或多于 8 | `too_short` / `too_long` | `outputs` |
| 输出名称不是合法标识符 | `string_pattern_mismatch` | `outputs.<i>.name` |
| 输入单位/不确定度单位非法 | `unknown_unit` / `incompatible_unit` 等 | `inputs.<i>...` |
| 相关矩阵非法 | `bad_matrix_shape` / `correlation_out_of_range` / `not_positive_semidefinite` 等 | `correlation_matrix...` |

### 不确定度逆分配 `POST /api/v1/allocate-uncertainty`

请求体与 `/api/v1/propagate` 相同，额外指定待分配输入量与**带单位的目标合成标准不
确定度**。待分配输入量可以省略 `standard_uncertainty`（其值即待求未知量 $u_k$，
其余输入量仍必须给出）。

设 $c_i=\partial f/\partial x_i$，$r_{ij}$ 为相关系数，总方差完整保留相关项后是关于
$u_k$（以该输入量自身单位计）的二次函数：

$$
u_c^2(u_k)=c_k^2u_k^2+2c_ku_k\sum_{j\ne k}c_ju_jr_{kj}
+\sum_{i\ne k}\sum_{j\ne k}c_iu_i c_ju_jr_{ij}=a\,u_k^2+b\,u_k+c
$$

服务端解二次不等式 $a u_k^2+b u_k+c\le U_t^2$（$u_k\ge0$），返回**最大非负根**
（满足目标的最大标准不确定度）及其单位，并把该值代回，按与传播接口完全相同的结构
给出合成值、灵敏度系数与含相关项的方差贡献；`variance_quadratic` 同时回显带单位的
$a/b/c$ 三个系数。

请求示例（$P=U\cdot I$，$u(U)=0.1\,\mathrm{V}$，$r=0.5$，目标
$u_c(P)=0.5\,\mathrm{W}$，反求 $u(I)$）：

```bash
curl -s http://localhost:8000/api/v1/allocate-uncertainty \
  -H 'Content-Type: application/json' \
  -d '{
    "inputs": [
      {"name": "U", "value": 10.0, "unit": "V", "standard_uncertainty": 0.1},
      {"name": "I", "value": 2.0, "unit": "A"}
    ],
    "formula": "U*I",
    "correlation_matrix": [[1.0, 0.5], [0.5, 1.0]],
    "allocate_input": "I",
    "target_combined_uncertainty": {"value": 0.5, "unit": "W"}
  }'
```

响应（节选）：

```json
{
  "result": {"value": 20.0, "unit": "W", "standard_uncertainty": 0.5},
  "allocate_input": "I",
  "allocated_standard_uncertainty": 0.036904157598234295,
  "allocated_standard_uncertainty_unit": "A",
  "target_combined_uncertainty": {"value": 0.5, "unit": "W", "standard_uncertainty": 0.5},
  "variance_quadratic": {
    "a": 100.0, "a_unit": "W ** 2 / A ** 2",
    "b": 2.0,   "b_unit": "W ** 2 / A",
    "c": 0.04,  "c_unit": "W ** 2",
    "unknown_uncertainty_unit": "A"
  }
}
```

目标单位须与公式输出同量纲（支持单位换算，如目标填 `{"value": 500, "unit": "mW"}`）。
以下情况返回 **422**，错误体带明确 `code` 与 `loc`：

| code | loc | 含义 |
| --- | --- | --- |
| `target_unreachable` | `target_combined_uncertainty` | 无论 $u_k$ 取何非负值，最小合成标准不确定度仍高于目标 |
| `zero_sensitivity` | `allocate_input` | $c_k=0$（该输入量不影响结果）且当前值已不满足目标 |
| `no_finite_bound` | `allocate_input` | $c_k=0$ 而目标已满足，任意大的 $u_k$ 都可行，不存在有限上界 |
| `unknown_allocate_input` | `allocate_input` | 待分配输入量未在 `inputs` 中声明 |
| `missing_uncertainty` | `inputs.<i>.standard_uncertainty` | 非待分配输入量缺少标准不确定度 |
| `incompatible_unit` | `target_combined_uncertainty.unit` | 目标单位与公式输出量纲不一致 |

相关矩阵非法、公式不合法、其余单位错误沿用传播接口的同一套校验与 code/loc。
