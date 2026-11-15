# 批量显著性校准的固定上限与参考口径。
# 上限是明确拒绝线：超出时整批拒绝创建，绝不自动下调模拟次数冒充完成。
MAX_ITEMS = 12
MAX_SIMULATIONS = 2000
# 评估量 = 模拟次数 × Σ(窗口实际点数 × LS网格点数)。约对应单线程数十秒的批处理量。
MAX_EVALUATIONS = 120_000_000_000
REFERENCE_ALPHA = 0.05
SIMULATION_CHUNK = 256
GRID_BLOCK = 256
# sin/cos 缓存上限（点数×网格点数）：约 4MB double，超出则按块重算但复用伪逆。
GEOMETRY_CACHE_CELLS = 500_000
