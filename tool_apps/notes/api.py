import gzip
import json
import base64
import shutil
import re
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional


class RenamePageRequest(BaseModel):
    title: str


class MovePageRequest(BaseModel):
    folderId: Optional[str] = None


class CreateFolderRequest(BaseModel):
    name: str
    type: str


class RenameFolderRequest(BaseModel):
    name: str


class MoveFolderRequest(BaseModel):
    parentFolderId: Optional[str] = None


router = APIRouter()

# Anchor the path to localAssist/tool_apps/notes/data
WORKSPACE_DIR = Path(__file__).parent / "data"


class CreatePageRequest(BaseModel):
    title: str
    type: str  # "notes" or "art"
    folderId: Optional[str] = None


class PageMeta(BaseModel):
    id: str
    type: str
    title: str
    createdAt: str
    updatedAt: str
    tags: List[str]
    appVersion: str
    viewTransform: Optional[dict] = None
    folderId: Optional[str] = None


class SavePageRequest(BaseModel):
    meta: PageMeta
    strokes: dict
    pageDataUrl: Optional[str] = None


def get_base_dir(type_str: str) -> Path:
    if type_str not in ["notes", "art"]:
        raise HTTPException(status_code=400, detail="Type must be 'notes' or 'art'")
    return WORKSPACE_DIR / type_str


def _normalize_title(value: Optional[str], fallback: str) -> str:
    title = (value or "").strip()
    return title or fallback


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


def _sort_pages(pages: List[dict]) -> List[dict]:
    return sorted(pages, key=lambda x: ((x.get("title") or x.get("id") or "").lower(), (x.get("id") or "").lower()))


def get_folder_store_path(type_str: str) -> Path:
    return get_base_dir(type_str) / "folders.json"


def load_folders(type_str: str) -> List[dict]:
    store_path = get_folder_store_path(type_str)
    if not store_path.exists():
        return []
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            normalized = []
            for folder in data:
                if isinstance(folder, dict):
                    normalized_folder = dict(folder)
                    normalized_folder["parentFolderId"] = normalized_folder.get("parentFolderId") or None
                    normalized.append(normalized_folder)
            return normalized
    except Exception as e:
        print(f"Error loading folders.json for {type_str}: {e}")
    return []


def save_folders(type_str: str, folders: List[dict]) -> None:
    store_path = get_folder_store_path(type_str)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump(folders, f, indent=2)


def get_folder(type_str: str, folder_id: Optional[str]) -> Optional[dict]:
    if not folder_id:
        return None
    for folder in load_folders(type_str):
        if folder.get("id") == folder_id:
            return folder
    return None


@router.get("/pages")
async def list_pages(type: str = Query(..., description="notes or art")):
    base_dir = get_base_dir(type)
    pages = []
    if base_dir.exists():
        for page_dir in base_dir.iterdir():
            if page_dir.is_dir() and page_dir.name != "__pycache__":
                meta_path = page_dir / "meta.json"
                if meta_path.exists():
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        pages.append(meta)
                    except Exception as e:
                        print(f"Error loading meta.json for {page_dir.name}: {e}")
    folders = load_folders(type)
    pages = _sort_pages(pages)
    return {"pages": pages, "folders": folders}


@router.post("/pages")
async def create_page(req: CreatePageRequest):
    base_dir = get_base_dir(req.type)
    base_dir.mkdir(parents=True, exist_ok=True)

    if req.folderId is not None:
        if get_folder(req.type, req.folderId) is None:
            raise HTTPException(status_code=404, detail="Folder not found")

    # Generate ID: YYYY-MM-DD_title-slug
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = _normalize_title(req.title, "Untitled")
    slug = _slugify(title)
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
        "title": title,
        "createdAt": now,
        "updatedAt": now,
        "tags": [],
        "appVersion": "0.1",
        "folderId": req.folderId or None,
    }

    with open(page_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    strokes = {"v": 3, "strokes": []}
    with gzip.open(page_dir / "strokes.json.gz", "wt", encoding="utf-8") as f:
        json.dump(strokes, f, separators=(",", ":"))

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
        gz_path = page_dir / "strokes.json.gz"
        json_path = page_dir / "strokes.json"
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
        json.dump(req.strokes, f, separators=(",", ":"))

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
        meta["title"] = _normalize_title(req.title, "Untitled")
        now = datetime.now(timezone.utc).isoformat() + "Z"
        meta["updatedAt"] = now.replace("+00:00Z", "Z")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename page: {str(e)}")

    return {"status": "ok", "meta": meta}


@router.patch("/pages/{type}/{page_id}/move")
async def move_page(type: str, page_id: str, req: MovePageRequest):
    base_dir = get_base_dir(type)
    page_dir = base_dir / page_id
    if not page_dir.exists():
        raise HTTPException(status_code=404, detail="Page not found")

    if req.folderId is not None and get_folder(type, req.folderId) is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    meta_path = page_dir / "meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["folderId"] = req.folderId or None
        now = datetime.now(timezone.utc).isoformat() + "Z"
        meta["updatedAt"] = now.replace("+00:00Z", "Z")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to move page: {str(e)}")

    return {"status": "ok", "meta": meta}


@router.get("/folders")
async def list_folders(type: str = Query(..., description="notes or art")):
    return {"folders": load_folders(type)}


@router.post("/folders")
async def create_folder(type: str, req: CreateFolderRequest):
    base_dir = get_base_dir(type)
    base_dir.mkdir(parents=True, exist_ok=True)
    folders = load_folders(type)

    name = _normalize_title(req.name, "New folder")
    now = datetime.now(timezone.utc).isoformat() + "Z"
    now = now.replace("+00:00Z", "Z")
    slug = _slugify(name)
    folder_id = slug
    counter = 1
    while any(folder.get("id") == folder_id for folder in folders):
        folder_id = f"{slug}-{counter}"
        counter += 1

    folder = {"id": folder_id, "type": type, "name": name, "createdAt": now, "updatedAt": now, "parentFolderId": None}
    folders.append(folder)
    save_folders(type, folders)
    return folder


@router.patch("/folders/{type}/{folder_id}/rename")
async def rename_folder(type: str, folder_id: str, req: RenameFolderRequest):
    folders = load_folders(type)
    folder = next((item for item in folders if item.get("id") == folder_id), None)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder["name"] = _normalize_title(req.name, "New folder")
    folder["updatedAt"] = datetime.now(timezone.utc).isoformat() + "Z"
    folder["updatedAt"] = folder["updatedAt"].replace("+00:00Z", "Z")
    save_folders(type, folders)
    return {"status": "ok", "folder": folder}


@router.patch("/folders/{type}/{folder_id}/move")
async def move_folder(type: str, folder_id: str, req: MoveFolderRequest):
    folders = load_folders(type)
    folder = next((item for item in folders if item.get("id") == folder_id), None)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    if req.parentFolderId is not None:
        if req.parentFolderId == folder_id:
            raise HTTPException(status_code=400, detail="Folder cannot be moved into itself")
        if get_folder(type, req.parentFolderId) is None:
            raise HTTPException(status_code=404, detail="Parent folder not found")

    folder["parentFolderId"] = req.parentFolderId or None
    folder["updatedAt"] = datetime.now(timezone.utc).isoformat() + "Z"
    folder["updatedAt"] = folder["updatedAt"].replace("+00:00Z", "Z")
    save_folders(type, folders)
    return {"status": "ok", "folder": folder}


@router.delete("/folders/{type}/{folder_id}")
async def delete_folder(type: str, folder_id: str):
    base_dir = get_base_dir(type)
    if not base_dir.exists():
        raise HTTPException(status_code=404, detail="Folder not found")

    folders = load_folders(type)
    folder = next((item for item in folders if item.get("id") == folder_id), None)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    for page_dir in base_dir.iterdir():
        if page_dir.is_dir() and page_dir.name != "__pycache__":
            meta_path = page_dir / "meta.json"
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("folderId") == folder_id:
                        raise HTTPException(status_code=409, detail="Folder is not empty")
                except Exception:
                    continue

    if any(item.get("parentFolderId") == folder_id for item in folders):
        raise HTTPException(status_code=409, detail="Folder is not empty")

    folders = [item for item in folders if item.get("id") != folder_id]
    save_folders(type, folders)
    return {"status": "ok"}


@router.delete("/pages/{type}/{page_id}")
async def delete_page(type: str, page_id: str):
    base_dir = get_base_dir(type)
    page_dir = base_dir / page_id

    if not page_dir.exists():
        raise HTTPException(status_code=404, detail="Page not found")

    shutil.rmtree(page_dir)
    return {"status": "ok"}
