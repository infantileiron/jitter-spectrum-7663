from pydantic import BaseModel,Field,model_validator
from typing import List
from src.constants.significance import MAX_ITEMS,MAX_SIMULATIONS
class BatchInput(BaseModel):
 name:str=Field(min_length=2,max_length=120)
 study_ids:List[str]=Field(min_length=1,max_length=MAX_ITEMS)
 simulations:int=Field(default=200,ge=1,le=MAX_SIMULATIONS)
 seed:int=Field(default=2307,ge=0,le=2**31-1)
 correlation_time_minutes:float=Field(default=5,gt=0,le=100000,allow_inf_nan=False)
 @model_validator(mode="after")
 def unique(self):
  if len(set(self.study_ids))!=len(self.study_ids):raise ValueError("同一批次内不能重复选择同一研究")
  if self.correlation_time_minutes<=0:raise ValueError("相关时长必须为正")
  return self
