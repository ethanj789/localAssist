import os
import gzip
import json
import base64
import shutil
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional

class RenamePageRequest(BaseModel):
    title: str

router = APIRouter()

# Anchor the path to localAssist/tool_apps/notes/data
WORKSPACE_DIR = Path(__file__).parent / "data"

class CreatePageRequest(BaseModel):
    title: str
    type: str  # "notes" or "art"

class PageMeta(BaseModel):
    id: str
    type: str
    title: str
    createdAt: str
    updatedAt: str
    tags: List[str]
    appVersion: str
    viewTransform: Optional[dict] = None

class SavePageRequest(BaseModel):
    meta: PageMeta
    strokes: dict
    pageDataUrl: Optional[str] = None

def get_base_dir(type_str: str) -> Path:
    if type_str not in ["notes", "art"]:
        raise HTTPException(status_code=400, detail="Type must be 'notes' or 'art'")
    return WORKSPACE_DIR / type_str

@router.get("/pages")
async def list_pages(type: str = Query(..., description="notes or art")):
    base_dir = get_base_dir(type)
    pages = []
    if base_dir.exists():
        for page_dir in base_dir.iterdir():
            if page_dir.is_dir():
                meta_path = page_dir / "meta.json"
                if meta_path.exists():
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        pages.append(meta)
                    except Exception as e:
                        print(f"Error loading meta.json for {page_dir.name}: {e}")
    # Sort by updatedAt descending
    pages.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
    return {"pages": pages}

@router.post("/pages")
async def create_page(req: CreatePageRequest):
    base_dir = get_base_dir(req.type)
    base_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate ID: YYYY-MM-DD_title-slug
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = "".join(c if c.isalnum() else "-" for c in req.title.lower())
    slug = "-".join(filter(None, slug.split("-"))) # remove duplicate dashes
    if not slug:
        slug = "untitled"
        
    base_id = f"{date_str}_{slug}"
    page_id = base_id
    
    # Uniqueness check
    counter = 1
    while (base_dir / page_id).exists():
        page_id = f"{base_id}-{counter}"
        counter += 1
        
    page_dir = base_dir / page_id
    page_dir.mkdir(parents=True, exist_ok=True)
    
    now = datetime.now(timezone.utc).isoformat() + "Z"
    now = now.replace("+00:00Z", "Z")
    
    meta = {
        "id": page_id,
        "type": req.type,
        "title": req.title,
        "createdAt": now,
        "updatedAt": now,
        "tags": [],
        "appVersion": "0.1"
    }
    
    with open(page_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        
    strokes = {"v": 3, "strokes": []}
    with gzip.open(page_dir / "strokes.json.gz", "wt", encoding="utf-8") as f:
        json.dump(strokes, f, separators=(',', ':'))
        
    return {"meta": meta, "strokes": strokes}

@router.get("/pages/{type}/{page_id}")
async def get_page(type: str, page_id: str):
    base_dir = get_base_dir(type)
    page_dir = base_dir / page_id
    
    if not page_dir.exists():
        raise HTTPException(status_code=404, detail="Page not found")
        
    try:
        with open(page_dir / "meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
        gz_path   = page_dir / "strokes.json.gz"
        json_path  = page_dir / "strokes.json"
        if gz_path.exists():
            with gzip.open(gz_path, "rt", encoding="utf-8") as f:
                strokes = json.load(f)
        elif json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                strokes = json.load(f)
        else:
            strokes = {"v": 3, "strokes": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read page data: {str(e)}")
        
    return {"meta": meta, "strokes": strokes}

@router.put("/pages/{type}/{page_id}")
async def save_page(type: str, page_id: str, req: SavePageRequest):
    base_dir = get_base_dir(type)
    page_dir = base_dir / page_id
    
    if not page_dir.exists():
        page_dir.mkdir(parents=True, exist_ok=True)
        
    now = datetime.now(timezone.utc).isoformat() + "Z"
    now = now.replace("+00:00Z", "Z")
    
    req.meta.updatedAt = now
    
    with open(page_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(req.meta.model_dump(), f, indent=2)
        
    with gzip.open(page_dir / "strokes.json.gz", "wt", encoding="utf-8") as f:
        json.dump(req.strokes, f, separators=(',', ':'))

    # Remove legacy plain-JSON file after successful gz write
    legacy_json = page_dir / "strokes.json"
    if legacy_json.exists():
        legacy_json.unlink()

    if req.pageDataUrl and req.pageDataUrl.startswith("data:image/png;base64,"):
        try:
            b64_data = req.pageDataUrl.split(",")[1]
            image_data = base64.b64decode(b64_data)
            with open(page_dir / "page.png", "wb") as f:
                f.write(image_data)
        except Exception as e:
            print(f"Failed to save page.png: {e}")
            
    return {"status": "ok", "updatedAt": now}

@router.patch("/pages/{type}/{page_id}/rename")
async def rename_page(type: str, page_id: str, req: RenamePageRequest):
    base_dir = get_base_dir(type)
    page_dir = base_dir / page_id
    
    if not page_dir.exists():
        raise HTTPException(status_code=404, detail="Page not found")
    
    meta_path = page_dir / "meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["title"] = req.title
        now = datetime.now(timezone.utc).isoformat() + "Z"
        meta["updatedAt"] = now.replace("+00:00Z", "Z")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename page: {str(e)}")
    
    return {"status": "ok", "meta": meta}

@router.delete("/pages/{type}/{page_id}")
async def delete_page(type: str, page_id: str):
    base_dir = get_base_dir(type)
    page_dir = base_dir / page_id
    
    if not page_dir.exists():
        raise HTTPException(status_code=404, detail="Page not found")
        
    shutil.rmtree(page_dir)
    return {"status": "ok"}
