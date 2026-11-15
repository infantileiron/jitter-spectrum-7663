from pathlib import Path
import threading,time
import redis,traceback
from sqlalchemy import select
from src.config.settings import REDIS_URL
from src.config.database import Session
from src.constants.enums import SignificanceStatus
from src.models import SpectralRun,SpectralStudy,SamplingWindow,SignalDataset,SignificanceBatch
from src.services.windows import points_for
from src.services.analysis import analyze
from src.services.significance import QUEUE as SIGNIFICANCE_QUEUE,execute_batch
client=redis.Redis.from_url(REDIS_URL);heartbeat=Path("/artifacts/worker-heartbeat")
RUN_QUEUE="jitter-spectrum:jobs"
HEARTBEAT_SECONDS=5
def beat():
 # 长批次校准会占用主循环，心跳必须独立刷新，否则健康检查会把正在工作的worker判为失联。
 while True:
  heartbeat.touch();time.sleep(HEARTBEAT_SECONDS)
def execute_run(id):
 with Session() as db:
  run=db.get(SpectralRun,id)
  if not run or run.status not in ("queued","running"):return
  run.status="running";db.commit()
  try:
   study=db.get(SpectralStudy,run.study_id);window=db.get(SamplingWindow,study.window_id);dataset=db.get(SignalDataset,window.dataset_id)
   run.result=analyze(points_for(dataset,window),study);run.status="completed"
  except Exception as e:run.error=str(e);run.status="failed";traceback.print_exc()
  db.commit()
with Session() as db:
 for id in db.scalars(select(SpectralRun.id).where(SpectralRun.status.in_(["queued","running"]))):client.lpush(RUN_QUEUE,id)
 for id in db.scalars(select(SignificanceBatch.id).where(SignificanceBatch.status.in_([SignificanceStatus.queued.value,SignificanceStatus.running.value]))):client.lpush(SIGNIFICANCE_QUEUE,id)
threading.Thread(target=beat,daemon=True).start()
while True:
 job=client.brpop([RUN_QUEUE,SIGNIFICANCE_QUEUE],timeout=1)
 if not job:continue
 queue,name=job
 if queue.decode()==SIGNIFICANCE_QUEUE:execute_batch(name.decode())
 else:execute_run(name.decode())
