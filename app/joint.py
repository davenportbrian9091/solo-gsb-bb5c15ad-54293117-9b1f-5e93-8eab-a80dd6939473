"""多输出联合不确定度传播（GUM 一阶雅可比）。

对同一组带协方差的输入量 $\mathbf{x}$，一次提交 m 个具名测量模型
$y_k=f_k(\mathbf{x})$，设一阶雅可比

    J[k, i] = c_{ki} = ∂f_k/∂x_i（在输入估计值处取值，以输出单位/输入单位计），

输入量的协方差矩阵由标准不确定度与相关系数矩阵构成
（Σ[i,j] = u(x_i) r_ij u(x_j)），则输出协方差为

    Σ_y = J Σ Jᵀ = G R Gᵀ，  G[k, i] = c_{ki} u(x_i)

对角元 $\Sigma_y[k,k]$ 开方即各输出的合成标准不确定度 $u_c(y_k)$；
由完整的 $\Sigma_y$ 可进一步得到输出量之间的相关系数矩阵。

输入量解析、公式白名单、量纲/单位校验、相关系数矩阵校验与单输出传播
（:mod:`app.propagation`）完全复用，错误 loc 精确定位到
``outputs.<k>.formula`` 等具体位置。
"""
from __future__ import annotations

import math

import numpy as np

from .correlation import validate_correlation_matrix
from .errors import UnprocessableError
from .propagation import WHITELIST, _prepare_inputs, evaluate_model
from .units import unit_str

_VALUE_TOL = 1e-9


def _formula_loc(loc: list, k: int) -> list:
    """把单输出链路里的公式错误 loc 映射到第 k 个输出下。

    evaluate_model / parse_formula / 单位换算的公式类错误一律挂在
    ``["formula"]``（或以 formula 开头），联合请求中改写为
    ``["outputs", k, "formula", ...]``；其余 loc（理论上不会出现）原样保留。
    """
    if loc and loc[0] == "formula":
        return ["outputs", k, *loc[1:]] if len(loc) > 1 else ["outputs", k, "formula"]
    return list(loc)


def propagate_joint(req) -> dict:
    """执行多输出联合传播，返回可直接序列化的结果字典。"""
    inputs = _prepare_inputs(req)
    n = len(inputs)
    m = len(req.outputs)

    # 输出名重复在此显式校验（给出精确定位，而非依赖后续 KeyError）。
    seen: set[str] = set()
    for k, out in enumerate(req.outputs):
        if out.name in seen:
            raise UnprocessableError(
                f"输出名称 {out.name!r} 重复",
                ["outputs", k, "name"],
                code="duplicate_output_name",
            )
        seen.add(out.name)

    corr = validate_correlation_matrix(req.correlation_matrix, n)

    # 逐输出复用单输出的白名单/量纲/单位/求值校验与符号偏导。
    display_units = []
    rows: list[np.ndarray] = []
    output_payloads: list[dict] = []
    for k, out in enumerate(req.outputs):
        try:
            model = evaluate_model(inputs, out.formula)
        except UnprocessableError as exc:
            raise UnprocessableError(exc.message, _formula_loc(exc.loc, k), code=exc.code) from exc

        display_units.append(model["display_unit"])
        # G[k, i] = c_ki * u(x_i)，数值以“输出k单位”计。
        rows.append(
            np.asarray(
                [
                    model["c_mags"][i] * inputs[i]["u_magnitude"]
                    for i in range(n)
                ],
                dtype=np.float64,
            )
        )
        output_payloads.append(
            {
                "name": out.name,
                "formula": out.formula,
                "display_unit": model["display_unit"],
                "value": float(model["result_q"].magnitude),
                "sensitivity_coefficients": model["sensitivities"],
            }
        )

    g = np.vstack(rows)
    cov = g @ np.asarray(corr, dtype=np.float64) @ g.T
    cov = (cov + cov.T) / 2.0

    # 对角元即各输出方差；出现显著负值说明数值不自洽。
    for k in range(m):
        diag = float(cov[k, k])
        if diag < 0.0 and diag <= -_VALUE_TOL:
            raise UnprocessableError(
                f"输出 {output_payloads[k]['name']!r} 传播后方差为负（{diag:.6g}），"
                "相关结构数值不自洽",
                ["outputs", k, "formula"],
                code="not_positive_semidefinite",
            )
    diag = np.clip(np.diag(cov), 0.0, None)

    # 完整输出相关系数矩阵：对角恒为 1；方差为 0（相关无定义）记 0。
    out_corr = np.zeros((m, m), dtype=np.float64)
    std = np.sqrt(diag)
    for k in range(m):
        out_corr[k, k] = 1.0
        for j in range(k + 1, m):
            if std[k] > 0.0 and std[j] > 0.0:
                r = float(cov[k, j] / (std[k] * std[j]))
                # 数值误差导致的轻微越界钳制到 [-1, 1]。
                if abs(r) > 1.0 and abs(r) <= 1.0 + _VALUE_TOL:
                    r = math.copysign(1.0, r)
                out_corr[k, j] = out_corr[j, k] = r
    out_corr = np.round(out_corr, 12)

    # 协方差矩阵逐单元带单位：单元 (k,j) 的单位为“输出k单位×输出j单位”。
    covariance_cells: list[list[dict]] = [[None] * m for _ in range(m)]  # type: ignore[assignment]
    for k in range(m):
        for j in range(m):
            value = float(cov[k, j])
            if abs(value) < 1e-300:
                value = 0.0
            covariance_cells[k][j] = {
                "value": value,
                "unit": unit_str(display_units[k] * display_units[j]),
            }

    outputs = []
    for k, payload in enumerate(output_payloads):
        outputs.append(
            {
                "name": payload["name"],
                "result": {
                    "value": payload["value"],
                    "unit": unit_str(payload["display_unit"]),
                    "standard_uncertainty": float(std[k]),
                },
                "sensitivity_coefficients": payload["sensitivity_coefficients"],
                "formula": payload["formula"],
            }
        )

    return {
        "outputs": outputs,
        "number_of_inputs": n,
        "number_of_outputs": m,
        "correlation_matrix": [[float(x) for x in row] for row in corr],
        "output_correlation_matrix": [[float(x) for x in row] for row in out_corr],
        "output_covariance_matrix": covariance_cells,
        "whitelist": list(WHITELIST),
    }
