from fastapi import APIRouter,Depends
from src.config.database import db_session
from src.constants.significance import MAX_EVALUATIONS,MAX_ITEMS,MAX_SIMULATIONS,REFERENCE_ALPHA
from src.controllers.auth import current_user,writer
from src.schemas.significance import BatchInput
from src.services import significance as service
router=APIRouter(prefix="/significance")
@router.get("/limits")
def limits(user=Depends(current_user)):return {"max_items":MAX_ITEMS,"max_simulations":MAX_SIMULATIONS,"max_evaluations":MAX_EVALUATIONS,"reference_alpha":REFERENCE_ALPHA}
@router.get("")
def listing(db=Depends(db_session),user=Depends(current_user)):return service.listing(db)
@router.post("")
def create(payload:BatchInput,db=Depends(db_session),user=Depends(writer)):return service.create(db,payload,user)
@router.get("/{id}")
def detail(id:str,db=Depends(db_session),user=Depends(current_user)):return service.detail(db,id)
@router.post("/{id}/retry")
def retry(id:str,db=Depends(db_session),user=Depends(writer)):return service.retry(db,id)
