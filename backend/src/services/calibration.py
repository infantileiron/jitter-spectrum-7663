"""批量显著性校准的纯数值实现（无数据库依赖，worker与种子共用）。

口径：
- 冻结每个所选窗口的实际采样时刻与观测值；批次创建后不再读取观测集。
- 无周期原假设：在完全相同的采样时刻上生成平稳高斯 OU 噪声，
  相邻时刻相关系数 exp(-Δt/τ)，τ 为用户声明的相关时长；无周期成分。
- 同一 (观测集, 链路) 的窗口共享同一条模拟噪声序列，重叠时刻数值完全一致。
- 每次模拟都对每个窗口重新去趋势（mean/linear）并在声明频率范围的全部网格上搜索，
  不重复使用观测残差。
- 检验统计量：整批最大峰 = max_w(该窗模拟 LS 解释方差比例最大值)。
  各窗误报概率 FAP_w = (1 + #{模拟整批最大峰 >= 本窗观测最大峰}) / (模拟次数 + 1)。
- 种子按 (批次种子, 观测集, 链路, 模拟序号) 派生，不依赖窗口提交顺序；
  计算顺序按稳定键排序，因此调换窗口顺序结果逐字段一致。
"""
import hashlib
import json

import numpy as np

from src.constants.significance import GEOMETRY_CACHE_CELLS, GRID_BLOCK, SIMULATION_CHUNK

STATISTIC_LABEL = "整批各窗浮动均值LS全频搜索最大解释方差比例的最大值"
CORRELATION_MODEL = "平稳高斯OU过程：Cov(lag)=σ²·exp(-Δt/相关时长)，在原采样时刻生成，无周期成分"

_ASSUMPTIONS = [
    "误报概率是在声明的无周期OU噪声模型下的经验值，不是因果诊断，也不等于真实链路故障概率。",
    "整批最大峰统计量把所选窗口视为一次同时检验的族，控制的是整批误报；未建模先看峰值再选窗口的选择偏差。",
    "模拟使用原采样时刻与原观测跨度，不插值、不补零、不重采样；同一观测组的重叠窗口共享同一条模拟噪声，噪声幅度按该组窗口观测残差标准差缩放。",
    "每次模拟都重新去趋势并在声明频率范围的全部LS网格上搜索，不复用观测残差，也不只在候选峰附近搜索。",
    "观测最大峰与模拟统计量由同一数值管线计算；两窗单侧FFT的PSD单位不同，不参与本批次校准。",
    "经验误报概率的最小非零分辨率为 1/(模拟次数+1)；模拟次数越多分辨率越细，不可外推到更小概率。模拟次数不足 1/α−1 时参考水平阈值不可达，阈值返回 null，不降口径。",
    "相关时长不足时噪声接近独立白噪声，偏长时去趋势会吸收更多慢起伏；均改变零分布，需在报告中声明。",
    "同一批次、同一种子、同一冻结观测重跑得到逐字段相同结果；调换窗口提交顺序也一致（种子与计算顺序均与顺序无关）。",
    "批次从待计算到全部窗口校准完成后一次性转为可用；任一窗口失败时整批 failed，不提供任何单窗误报概率。",
]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def input_checksum(items):
    """冻结输入的稳定校验码：按研究ID排序，与提交顺序无关。"""
    ordered = sorted(items, key=lambda item: item["study_id"])
    return hashlib.sha256(_canonical(ordered).encode()).hexdigest()


def _seed_key(seed, dataset_id, link_id):
    digest = hashlib.sha256(("batch-significance|%d|%s|%s" % (seed, dataset_id, link_id)).encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2 ** 63 - 1), int.from_bytes(digest[8:16], "big") % (2 ** 63 - 1)


def _design(t, detrend):
    """与 preprocessing.prepare 同一口径的去趋势设计矩阵（相对首观测时刻）。"""
    x = t - t[0]
    if detrend == "linear":
        return np.column_stack([np.ones(len(t)), x])
    if detrend == "mean":
        return np.ones((len(t), 1))
    raise ValueError("去趋势方式只能为mean或linear")


def observed_residuals(times, values, detrend):
    t = np.asarray(times, dtype=float)
    y = np.asarray(values, dtype=float)
    if len(t) < 16:
        raise ValueError("窗口内至少16个观测点，不对不足样本造周期")
    if not np.all(np.isfinite(t)) or not np.all(np.isfinite(y)):
        raise ValueError("时间与抖动值必须有限")
    if np.any(np.diff(t) <= 0):
        raise ValueError("时间戳重复或非递增，不允许静默去重")
    design = _design(t, detrend)
    coefficients = np.linalg.lstsq(design, y, rcond=None)[0]
    residual = y - design @ coefficients
    variance = float(np.mean(residual ** 2))
    if variance <= 1e-20 * max(1.0, float(np.mean(y ** 2))):
        raise ValueError("原序列或去趋势后为常量，没有可分析的周期波动")
    return t, y, residual, variance


def validate_item(item):
    """冻结快照的静态校验；创建批次时即可发现失败窗口。"""
    t, _, _, variance = observed_residuals(item["times"], item["observed"], item["detrend"])
    span = float(t[-1] - t[0])
    if item["min_frequency"] < 1 / span * (1 - 1e-9):
        raise ValueError("最大搜索周期不能超过观测跨度")
    dt = np.diff(t)
    median = float(np.median(dt))
    regular = bool(np.allclose(dt, median, rtol=1e-6, atol=1e-9))
    if regular and item["max_frequency"] > 1 / (2 * median) * (1 + 1e-9):
        raise ValueError("规则采样搜索上限超过FFT Nyquist频率")
    if not 32 <= item["frequency_points"] <= 4000:
        raise ValueError("LS网格点数须在32—4000之间")
    if not 0 < item["min_frequency"] < item["max_frequency"] <= 10:
        raise ValueError("频率范围须满足 0<最低频率<最高频率≤10/min")
    return {"span": span, "regular": regular, "median_interval": median, "residual_variance": variance}


def _detrend_rows(y, t, detrend):
    if detrend == "mean":
        return y - y.mean(axis=1, keepdims=True)
    if detrend != "linear":
        raise ValueError("去趋势方式只能为mean或linear")
    x = t - t[0]
    xc = x - x.mean()
    total = float(xc @ xc)
    slope = (y @ xc) / total
    offset = y.mean(axis=1) - slope * x.mean()
    return y - offset[:, None] - slope[:, None] * x[None, :]


def _geometry(t, grid):
    """与数据无关的搜索几何：逐频率块的设计矩阵伪逆，小网格同时缓存sin/cos。

    伪逆按 (块, 网格点数) 缓存（每窗仅数百KB）；sin/cos 仅在 n×网格点数不超过
    GEOMETRY_CACHE_CELLS 时缓存，避免大窗口把内存撑爆；未缓存时按块重算，
    与缓存路径逐位一致。"""
    blocks = []
    n = len(t)
    relative = t - t[0]
    cache_trig = n * len(grid) <= GEOMETRY_CACHE_CELLS
    for start in range(0, len(grid), GRID_BLOCK):
        block = grid[start:start + GRID_BLOCK]
        sin = np.sin(2 * np.pi * np.outer(block, relative)) if cache_trig else None
        cos = np.cos(2 * np.pi * np.outer(block, relative)) if cache_trig else None
        if cache_trig:
            design = np.empty((len(block), 3, 3))
            design[:, 0, 0] = n
            design[:, 0, 1] = design[:, 1, 0] = sin.sum(axis=1)
            design[:, 0, 2] = design[:, 2, 0] = cos.sum(axis=1)
            design[:, 1, 1] = np.sum(sin * sin, axis=1)
            design[:, 2, 2] = np.sum(cos * cos, axis=1)
            design[:, 1, 2] = design[:, 2, 1] = np.sum(sin * cos, axis=1)
            inverse = np.linalg.pinv(design, rcond=n * np.finfo(float).eps, hermitian=True)
        else:
            block_sin = np.sin(2 * np.pi * np.outer(block, relative))
            block_cos = np.cos(2 * np.pi * np.outer(block, relative))
            design = np.empty((len(block), 3, 3))
            design[:, 0, 0] = n
            design[:, 0, 1] = design[:, 1, 0] = block_sin.sum(axis=1)
            design[:, 0, 2] = design[:, 2, 0] = block_cos.sum(axis=1)
            design[:, 1, 1] = np.sum(block_sin * block_sin, axis=1)
            design[:, 2, 2] = np.sum(block_cos * block_cos, axis=1)
            design[:, 1, 2] = design[:, 2, 1] = np.sum(block_sin * block_cos, axis=1)
            inverse = np.linalg.pinv(design, rcond=n * np.finfo(float).eps, hermitian=True)
            del block_sin, block_cos
        blocks.append((block, sin, cos, inverse))
    return blocks


def _row_maxima(y, t, detrend, grid, geometry=None):
    """逐行去趋势后在全部频率网格上搜索，返回每行最大解释方差比例与对应频率。"""
    rows = np.asarray(y, dtype=float)
    single = rows.ndim == 1
    if single:
        rows = rows[None, :]
    residual = _detrend_rows(rows, t, detrend)
    baseline = np.sum((residual - residual.mean(axis=1, keepdims=True)) ** 2, axis=1)
    if np.any(baseline <= 0):
        raise ValueError("模拟残差为常量，无法归一化LS周期谱")
    sum_residual = residual.sum(axis=1)
    count = residual.shape[0]
    n = len(t)
    relative = t - t[0]
    best = np.full(count, -np.inf)
    best_frequency = np.zeros(count)
    reference = np.arange(count)
    if geometry is None:
        geometry = _geometry(t, grid)
    for block, sin, cos, inverse in geometry:
        if sin is None:
            sin = np.sin(2 * np.pi * np.outer(block, relative))
            cos = np.cos(2 * np.pi * np.outer(block, relative))
        products = np.empty((len(block), 3, count))
        products[:, 0, :] = sum_residual[None, :]
        products[:, 1, :] = sin @ residual.T
        products[:, 2, :] = cos @ residual.T
        coefficients = np.einsum("mij,mjs->mis", inverse, products, optimize=True)
        explained = np.einsum("mis,mis->ms", coefficients, products, optimize=True)
        explained -= sum_residual[None, :] ** 2 / n
        power = np.clip(explained / baseline[None, :], 0, 1)
        index = np.argmax(power, axis=0)
        values = power[index, reference]
        better = values > best
        best[better] = values[better]
        best_frequency[better] = block[index[better]]
    if single:
        return float(best[0]), float(best_frequency[0])
    return best, best_frequency


def _ou_noise(times, tau, simulations, seed_values):
    """按原采样时刻生成平稳高斯OU噪声；每行一条序列，方差约1。"""
    dt = np.diff(times)
    decay = np.exp(-dt / tau)
    innovation = np.sqrt(np.maximum(0.0, 1.0 - decay * decay))
    noise = np.empty((simulations, len(times)))
    rng = np.random.default_rng(seed_values)
    noise[:, 0] = rng.standard_normal(simulations)
    for index in range(1, len(times)):
        fresh = rng.standard_normal(simulations)
        noise[:, index] = decay[index - 1] * noise[:, index - 1] + innovation[index - 1] * fresh
    return noise


def _group_key(item):
    return (item["dataset_id"], item["link_id"])


def calibrate(items, simulations, seed, correlation_time_minutes, reference_alpha, progress=None):
    """执行完整批次。返回结果字典；任一窗口失败会抛出带窗口名的 ValueError。"""
    if not items:
        raise ValueError("批次没有窗口")
    ordered = sorted(items, key=lambda item: item["study_id"])
    for item in ordered:
        validate_item(item)
    simulations = int(simulations)
    seed = int(seed)
    tau = float(correlation_time_minutes)
    span_limit = max(validate_item(item)["span"] for item in ordered)
    if not np.isfinite(tau) or tau <= 0:
        raise ValueError("相关时长必须为有限正数（分钟）")
    if tau > span_limit * (1 + 1e-9):
        raise ValueError("相关时长不能超过批次内最长观测跨度 %.3f 分钟，否则不属短程相关" % span_limit)

    groups = {}
    for item in ordered:
        groups.setdefault(_group_key(item), []).append(item)
    group_info = {}
    total = len(ordered)
    for key in sorted(groups):
        members = groups[key]
        union = np.unique(np.concatenate([np.asarray(m["times"], dtype=float) for m in members]))
        described = {
            "key": key,
            "dataset_id": key[0],
            "link_id": key[1],
            "members": members,
            "item_count": len(members),
            "shared_times": union,
            "shared_observation_count": int(len(union)),
            "scale": float(np.sqrt(np.mean([validate_item(m)["residual_variance"] for m in members]))),
            "seed_key": _seed_key(seed, key[0], key[1]),
        }
        group_info[key] = described
        for item in members:
            t = np.asarray(item["times"], dtype=float)
            indices = np.searchsorted(union, t)
            if np.any(indices >= len(union)) or not np.allclose(union[indices], t):
                raise ValueError("窗口「%s」校准失败：冻结时刻不在观测组共享时间轴上" % item["window_name"])
            described.setdefault("indices", {})[item["study_id"]] = indices

    # 先算每个窗口的观测统计量（全频搜索），失败即整批失败。
    # 观测统计量与模拟使用同一数值管线：按同一块几何缓存复用设计矩阵与频率网格。
    outcomes = {}
    for position, item in enumerate(ordered):
        t, values, _, _ = observed_residuals(item["times"], item["observed"], item["detrend"])
        grid = np.linspace(item["min_frequency"], item["max_frequency"], item["frequency_points"])
        try:
            observed_geometry = _geometry(t, grid)
            observed_power, observed_frequency = _row_maxima(values, t, item["detrend"], grid, observed_geometry)
        except ValueError as error:
            raise ValueError("窗口「%s」校准失败：%s" % (item["window_name"], error))
        outcomes[item["study_id"]] = {
            "item": item,
            "order": position,
            "observed_power": observed_power,
            "observed_frequency": observed_frequency,
            "maxima": np.full(simulations, -np.inf),
            "grid": grid,
            "geometry": observed_geometry,
        }
        if progress:
            progress({
                "phase": "observing",
                "items_done": position + 1,
                "items_total": total,
                "simulations_done": 0,
                "simulations_total": simulations,
                "current_window": item["window_name"],
                "percent": 0.0,
            })

    # 模拟块：同一观测组共享一条噪声序列，逐窗口重新去趋势并全频搜索。
    batch_max = np.full(simulations, -np.inf)
    for start in range(0, simulations, SIMULATION_CHUNK):
        stop = min(simulations, start + SIMULATION_CHUNK)
        for key in sorted(group_info):
            group = group_info[key]
            seeds = [(group["seed_key"][0], group["seed_key"][1], index) for index in range(start, stop)]
            noise = _ou_noise(group["shared_times"], tau, stop - start, seeds) * group["scale"]
            for item in group["members"]:
                outcome = outcomes[item["study_id"]]
                t = np.asarray(item["times"], dtype=float)
                try:
                    maxima, _ = _row_maxima(noise[:, group["indices"][item["study_id"]]], t, item["detrend"], outcome["grid"], outcome["geometry"])
                except ValueError as error:
                    raise ValueError("窗口「%s」校准失败：%s" % (item["window_name"], error))
                outcome["maxima"][start:stop] = maxima
                batch_max[start:stop] = np.maximum(batch_max[start:stop], maxima)
        if progress:
            progress({
                "phase": "simulating",
                "items_done": total,
                "items_total": total,
                "simulations_done": stop,
                "simulations_total": simulations,
                "current_window": None,
                "percent": round(100.0 * stop / simulations, 2),
            })
    outcomes = [outcomes[item["study_id"]] for item in ordered]

    if not np.all(np.isfinite(batch_max)):
        raise ValueError("批次最大峰统计量包含非有限值，整批不结论")

    ordered_batch = np.sort(batch_max)
    reachable = int(np.floor(reference_alpha * (simulations + 1) - 1))
    # 模拟次数太少时 α 水平上没有可达到的阈值：返回 None（不下调口径、不假装可达）。
    threshold = float(ordered_batch[len(ordered_batch) - 1 - reachable]) if reachable >= 0 else None
    quantiles = {
        "median": float(np.quantile(batch_max, 0.5)),
        "p90": float(np.quantile(batch_max, 0.9)),
        "p95": float(np.quantile(batch_max, 0.95)),
        "p99": float(np.quantile(batch_max, 0.99)),
        "max": float(batch_max.max()),
        "threshold_at_reference_alpha": threshold,
    }
    resolution = 1.0 / (simulations + 1)
    results = []
    for outcome in outcomes:
        item = outcome["item"]
        exceedances = int(np.sum(batch_max >= outcome["observed_power"]))
        probability = (1 + exceedances) / (simulations + 1)
        results.append({
            "study_id": item["study_id"],
            "study_name": item["study_name"],
            "window_id": item["window_id"],
            "window_name": item["window_name"],
            "link_id": item["link_id"],
            "dataset_id": item["dataset_id"],
            "detrend": item["detrend"],
            "fft_window": item["fft_window"],
            "min_frequency": item["min_frequency"],
            "max_frequency": item["max_frequency"],
            "frequency_points": item["frequency_points"],
            "observation_count": len(item["times"]),
            "span_minutes": float(item["times"][-1] - item["times"][0]),
            "observed_max_power": outcome["observed_power"],
            "peak_frequency_per_minute": outcome["observed_frequency"],
            "peak_period_minutes": 1.0 / outcome["observed_frequency"],
            "exceedances": exceedances,
            "false_alarm_probability": probability,
            "simulations": simulations,
            "resolution": resolution,
            "batch_max_threshold_at_reference_alpha": threshold,
            "reference_alpha": reference_alpha,
            "significant_at_reference_alpha": bool(probability <= reference_alpha),
        })
    results.sort(key=lambda entry: entry["study_id"])
    checksum = hashlib.sha256(_canonical({"items": results, "batch_max": quantiles, "simulations": simulations, "seed": seed, "correlation_time_minutes": tau}).encode()).hexdigest()
    return {
        "seed": seed,
        "simulations": simulations,
        "correlation_time_minutes": tau,
        "reference_alpha": reference_alpha,
        "resolution": resolution,
        "statistic": STATISTIC_LABEL,
        "correlation_model": CORRELATION_MODEL,
        "input_checksum": input_checksum(items),
        "result_checksum": checksum,
        "batch_max": quantiles,
        "groups": [{"dataset_id": g["dataset_id"], "link_id": g["link_id"], "item_count": g["item_count"], "shared_observation_count": g["shared_observation_count"]} for g in (group_info[key] for key in sorted(group_info))],
        "items": results,
        "assumptions": _ASSUMPTIONS,
    }
