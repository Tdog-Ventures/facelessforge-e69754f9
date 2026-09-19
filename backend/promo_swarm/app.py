from fastapi import APIRouter

router = APIRouter()
app = router

@router.get("/")
def root():
    return {"status": "promo_swarm ok"}

@router.get("/health")
def health():
    return {"ok": True}

def mount():
    return True
