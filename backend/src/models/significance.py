from sqlalchemy import String,Double,Integer,BigInteger,JSON,ForeignKey,Text,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class SignificanceBatch(Base):
 __tablename__="significance_batches"
 id:Mapped[str]=mapped_column(String,primary_key=True)
 name:Mapped[str]=mapped_column(String,unique=True)
 status:Mapped[str]=mapped_column(String)
 seed:Mapped[int]=mapped_column(BigInteger)
 simulations:Mapped[int]=mapped_column(Integer)
 correlation_time_minutes:Mapped[float]=mapped_column(Double)
 reference_alpha:Mapped[float]=mapped_column(Double)
 total_evaluations:Mapped[int]=mapped_column(BigInteger)
 input_checksum:Mapped[str]=mapped_column(String)
 progress:Mapped[dict]=mapped_column(JSON,default=dict)
 result:Mapped[dict]=mapped_column(JSON,default=dict)
 error:Mapped[str]=mapped_column(Text,default="")
 created_by:Mapped[str]=mapped_column(String)
 created_at:Mapped[str]=mapped_column(String)
 updated_at:Mapped[str]=mapped_column(String)
class SignificanceItem(Base):
 __tablename__="significance_items"
 __table_args__=(UniqueConstraint("batch_id","study_id",name="uq_significance_items_batch_study"),)
 id:Mapped[str]=mapped_column(String,primary_key=True)
 batch_id:Mapped[str]=mapped_column(ForeignKey("significance_batches.id"))
 study_id:Mapped[str]=mapped_column(ForeignKey("spectral_studies.id"))
 status:Mapped[str]=mapped_column(String)
 snapshot:Mapped[dict]=mapped_column(JSON,default=dict)
 result:Mapped[dict]=mapped_column(JSON,default=dict)
 error:Mapped[str]=mapped_column(Text,default="")
 created_at:Mapped[str]=mapped_column(String)
