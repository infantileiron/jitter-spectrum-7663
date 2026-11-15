import traceback
import uuid

import redis
from fastapi import HTTPException
from sqlalchemy import func,select
from sqlalchemy.exc import IntegrityError

from src.config.settings import REDIS_URL
from src.config.database import Session
from src.constants.enums import SignificanceStatus
from src.constants.significance import MAX_EVALUATIONS, MAX_ITEMS, MAX_SIMULATIONS, REFERENCE_ALPHA
from src.models import SamplingWindow, SignalDataset, SignificanceBatch, SignificanceItem, SpectralStudy
from src.services.calibration import calibrate, input_checksum, validate_item
from src.services.windows import points_for
from src.utils.serialization import serialize
from src.utils.time import now

QUEUE = "jitter-spectrum:significance-jobs"


def freeze_study(db, study):
 """冻结所选窗口的实际采样时刻与观测，只读一次观测集。"""
 window = db.get(SamplingWindow, study.window_id)
 if not window:
  raise ValueError("采样窗口不存在")
 dataset = db.get(SignalDataset, window.dataset_id)
 if not dataset:
  raise ValueError("观测集不存在")
 points = sorted(points_for(dataset, window), key=lambda p: p["time_minutes"])
 if len(points) < 16:
  raise ValueError("窗口内至少16个观测点，不对不足样本造周期")
 times = [float(p["time_minutes"]) for p in points]
 observed = [float(p["jitter_ms"]) for p in points]
 if len(set(times)) != len(times):
  raise ValueError("冻结观测含重复时间戳，不允许静默去重")
 item = {
  "study_id": study.id,
  "study_name": study.name,
  "window_id": window.id,
  "window_name": window.name,
  "dataset_id": dataset.id,
  "link_id": window.link_id,
  "detrend": study.detrend,
  "fft_window": study.fft_window,
  "min_frequency": float(study.min_frequency),
  "max_frequency": float(study.max_frequency),
  "frequency_points": int(study.frequency_points),
  "times": times,
  "observed": observed,
 }
 validate_item(item)
 return item


def validate_capacity(study_ids, simulations):
 if not study_ids:
  raise HTTPException(422, "请至少选择一个频谱研究窗口")
 if len(study_ids) != len(set(study_ids)):
  raise HTTPException(422, "同一批次内不能重复选择同一研究")
 if len(study_ids) > MAX_ITEMS:
  raise HTTPException(422, "一批最多%d个窗口，当前%d个；不会自动裁剪" % (MAX_ITEMS, len(study_ids)))
 if simulations < 1:
  raise HTTPException(422, "模拟次数至少为1")
 if simulations > MAX_SIMULATIONS:
  raise HTTPException(422, "模拟次数上限为%d，当前%d；不会减少模拟冒充完成" % (MAX_SIMULATIONS, simulations))
 return simulations


def evaluations(snapshots, simulations):
 """容量口径：模拟次数 × Σ(窗口实际点数 × LS网格点数)。"""
 return int(sum(len(item["times"]) * int(item["frequency_points"]) for item in snapshots)) * int(simulations)


def create(db, payload, user):
 studies = [db.get(SpectralStudy, study_id) for study_id in payload.study_ids]
 missing = [study_id for study, study_id in zip(studies, payload.study_ids) if study is None]
 if missing:
  raise HTTPException(404, "频谱研究不存在：" + "、".join(missing))
 validate_capacity(payload.study_ids, payload.simulations)
 if db.scalar(select(SignificanceBatch).where(SignificanceBatch.name == payload.name)):
  raise HTTPException(409, "显著性批次名称已存在")
 try:
  snapshots = [freeze_study(db, study) for study in studies]
 except ValueError as error:
  raise HTTPException(422, "所选窗口无法冻结校准输入：" + str(error))
 total = evaluations(snapshots, payload.simulations)
 if total > MAX_EVALUATIONS:
  raise HTTPException(422, "容量超限：本批约需%d次点×频率网格评估，上限%d；请减少窗口数、网格点数或模拟次数后重新提交，不会自动减量" % (total, MAX_EVALUATIONS))
 span = max(validate_item(item)["span"] for item in snapshots)
 if payload.correlation_time_minutes > span * (1 + 1e-9):
  raise HTTPException(422, "相关时长%.3f分钟超过批次内最长观测跨度%.3f分钟，短程相关声明不成立" % (payload.correlation_time_minutes, span))
 stamp = now()
 batch_id = str(uuid.uuid4())
 batch = SignificanceBatch(
  id=batch_id,
  name=payload.name,
  status=SignificanceStatus.queued.value,
  seed=int(payload.seed),
  simulations=int(payload.simulations),
  correlation_time_minutes=float(payload.correlation_time_minutes),
  reference_alpha=REFERENCE_ALPHA,
  total_evaluations=total,
  input_checksum=input_checksum(snapshots),
  progress={"phase": "queued", "items_done": 0, "items_total": len(snapshots), "simulations_done": 0, "simulations_total": int(payload.simulations), "current_window": None, "percent": 0.0},
  result={},
  error="",
  created_by=user["email"],
  created_at=stamp,
  updated_at=stamp,
 )
 db.add(batch)
 db.flush()
 for item in snapshots:
  db.add(SignificanceItem(id=str(uuid.uuid4()), batch_id=batch_id, study_id=item["study_id"], status=SignificanceStatus.queued.value, snapshot=item, result={}, error="", created_at=stamp))
 try:
  db.commit()
 except IntegrityError:
  db.rollback()
  raise HTTPException(409, "批次名称或所选窗口与已有批次冲突，请修改名称后重试")
 try:
  redis.Redis.from_url(REDIS_URL).lpush(QUEUE, batch_id)
 except redis.RedisError:
  batch.status = SignificanceStatus.failed.value
  batch.error = "后台队列不可用，批次未开始，请重试"
  batch.updated_at = now()
  for item in db.scalars(select(SignificanceItem).where(SignificanceItem.batch_id == batch_id)):
   item.status = SignificanceStatus.failed.value
   item.error = batch.error
  db.commit()
  raise HTTPException(503, batch.error)
 return detail(db, batch_id)


def require(db, batch_id):
 batch = db.get(SignificanceBatch, batch_id)
 if not batch:
  raise HTTPException(404, "显著性批次不存在")
 return batch


def _frozen(item):
 snapshot = item.snapshot or {}
 times = snapshot.get("times") or []
 return {
  "study_id": item.study_id,
  "study_name": snapshot.get("study_name"),
  "window_id": snapshot.get("window_id"),
  "window_name": snapshot.get("window_name"),
  "link_id": snapshot.get("link_id"),
  "dataset_id": snapshot.get("dataset_id"),
  "detrend": snapshot.get("detrend"),
  "fft_window": snapshot.get("fft_window"),
  "min_frequency": snapshot.get("min_frequency"),
  "max_frequency": snapshot.get("max_frequency"),
  "frequency_points": snapshot.get("frequency_points"),
  "observation_count": len(times),
  "span_minutes": round(float(times[-1]) - float(times[0]), 6) if times else None,
 }


def detail(db, batch_id):
 batch = require(db, batch_id)
 items = list(db.scalars(select(SignificanceItem).where(SignificanceItem.batch_id == batch_id).order_by(SignificanceItem.study_id)))
 available = batch.status == SignificanceStatus.available.value
 payload = serialize(batch)
 payload["frozen_inputs"] = [_frozen(item) for item in items]
 payload["items"] = [{
  "id": item.id,
  "study_id": item.study_id,
  "status": item.status,
  "error": item.error,
  # 结果仅在整批 available 时一并给出；失败批次不暴露任何单窗概率。
  "result": item.result if available else {},
  "created_at": item.created_at,
 } for item in items]
 payload["item_count"] = len(items)
 return payload


def listing(db):
 """批次列表：只返回摘要与进度，不携带整批结果（结果由详情接口在 available 后给出）。"""
 batches = [serialize(batch) for batch in db.scalars(select(SignificanceBatch).order_by(SignificanceBatch.created_at.desc()))]
 counts = dict(db.execute(select(SignificanceItem.batch_id, func.count()).group_by(SignificanceItem.batch_id)).all())
 for batch in batches:
  batch["item_count"] = int(counts.get(batch["id"], 0))
  batch["result"] = {}
  batch["frozen_inputs"] = []
 return batches


def retry(db, batch_id):
 batch = require(db, batch_id)
 if batch.status != SignificanceStatus.failed.value:
  raise HTTPException(409, "只有失败的批次可以按原冻结输入重跑")
 batch.status = SignificanceStatus.queued.value
 batch.error = ""
 batch.progress = {"phase": "queued", "items_done": 0, "items_total": len(db.scalars(select(SignificanceItem).where(SignificanceItem.batch_id == batch_id)).all()), "simulations_done": 0, "simulations_total": batch.simulations, "current_window": None, "percent": 0.0}
 batch.updated_at = now()
 for item in db.scalars(select(SignificanceItem).where(SignificanceItem.batch_id == batch_id)):
  item.status = SignificanceStatus.queued.value
  item.result = {}
  item.error = ""
 db.commit()
 try:
  redis.Redis.from_url(REDIS_URL).lpush(QUEUE, batch_id)
 except redis.RedisError:
  batch.error = "后台队列不可用，批次未重新排队，请重试"
  batch.updated_at = now()
  db.commit()
  raise HTTPException(503, batch.error)
 return detail(db, batch_id)


def _reporter(db, batch_id):
 def report(update):
  batch = db.get(SignificanceBatch, batch_id)
  if not batch or batch.status != SignificanceStatus.running.value:
   return
  batch.progress = update
  batch.updated_at = now()
  db.commit()
 return report


def execute_batch(batch_id):
 """worker 入口：整批计算成功才切换到 available，任一窗口失败则整批 failed。"""
 batch = None
 with Session() as db:
  batch = db.get(SignificanceBatch, batch_id)
  if not batch or batch.status not in (SignificanceStatus.queued.value, SignificanceStatus.running.value):
   return
  items = list(db.scalars(select(SignificanceItem).where(SignificanceItem.batch_id == batch_id).order_by(SignificanceItem.study_id)))
  batch.status = SignificanceStatus.running.value
  batch.error = ""
  batch.progress = {"phase": "starting", "items_done": 0, "items_total": len(items), "simulations_done": 0, "simulations_total": batch.simulations, "current_window": None, "percent": 0.0}
  for item in items:
   item.status = SignificanceStatus.running.value
   item.result = {}
   item.error = ""
  batch.updated_at = now()
  db.commit()
  try:
   result = calibrate([item.snapshot for item in items], batch.simulations, batch.seed, batch.correlation_time_minutes, batch.reference_alpha, progress=_reporter(db, batch_id))
   by_study = {entry["study_id"]: entry for entry in result["items"]}
   for item in items:
    payload = by_study.get(item.study_id)
    if payload is None:
     raise ValueError("批次结果缺少窗口「%s」，整批不结论" % item.snapshot.get("window_name", item.study_id))
    item.result = payload
    item.status = SignificanceStatus.available.value
    item.error = ""
   batch.result = result
   batch.status = SignificanceStatus.available.value
   batch.progress = {"phase": "available", "items_done": len(items), "items_total": len(items), "simulations_done": batch.simulations, "simulations_total": batch.simulations, "current_window": None, "percent": 100.0}
   batch.error = ""
  except Exception as error:
   traceback.print_exc()
   message = str(error) or error.__class__.__name__
   batch.status = SignificanceStatus.failed.value
   batch.error = message
   batch.result = {}
   batch.progress = {**(batch.progress or {}), "phase": "failed", "current_window": None}
   for item in items:
    item.status = SignificanceStatus.failed.value
    item.result = {}
    item.error = message
  batch.updated_at = now()
  db.commit()
