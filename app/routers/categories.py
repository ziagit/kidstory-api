from fastapi import APIRouter

from ..schemas import CATEGORY_LABELS, Category

router = APIRouter(tags=["meta"])


@router.get("/categories", response_model=list[Category])
async def list_categories() -> list[Category]:
    """The fixed category list, mirroring app/data/categories.ts (minus the icons)."""
    return [Category(slug=slug, label=label) for slug, label in CATEGORY_LABELS.items()]
