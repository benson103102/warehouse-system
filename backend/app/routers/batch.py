"""批次併單模擬端點 — 對應原前端 computeBatch()。"""

from fastapi import APIRouter, Depends, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an

router = APIRouter(prefix="/api/batch", tags=["batch"])


@router.get("/simulate")
def simulate(capacity: int = Query(20, ge=1, le=500),
             sess: state.SessionState = Depends(require_clean_result)):
    result = an.batch_simulate(sess.cleaning_result.clean_df, capacity)
    return jsonsafe.clean(result)
